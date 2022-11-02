"""
Django Helpdesk - A Django powered ticket tracker for small enterprise.

(c) Copyright 2008 Jutda. Copyright 2018 Timothy Hobbs. All Rights Reserved.
See LICENSE for details.
"""
# import base64
import email
import imaplib
import logging
import mimetypes
import os
import poplib
import re
import socket
import ssl
import sys
from datetime import timedelta
from email.utils import getaddresses, parseaddr
from os.path import isfile, join
from time import ctime
from functools import reduce

from bs4 import BeautifulSoup
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.mail import BadHeaderError
from django.db.models import Q
from django.utils import encoding, timezone
from django.utils.translation import ugettext as _
from email_reply_parser import EmailReplyParser

from helpdesk import settings
from helpdesk.lib import safe_template_context, process_attachments
from helpdesk.models import Ticket, TicketCC, FollowUp, IgnoreEmail, FormType, CustomField
from seed.models import EmailImporter
from helpdesk.decorators import is_helpdesk_staff
from helpdesk import email_utils


# import User model, which may be a custom model
User = get_user_model()


STRIPPED_SUBJECT_STRINGS = [
    "Re: ",
    "Fw: ",
    "RE: ",
    "FW: ",
    "Automatic reply: ",
]

PATTERN_UID = re.compile(r'\d+ \(UID (?P<uid>\d+)\)')
DEBUGGING = False


def parse_uid(data):
    match = PATTERN_UID.match(data)
    return match.group('uid')


def process_email(quiet=False):
    for importer in EmailImporter.objects.filter(allow_email_imports=True):
        importer_queues = importer.queue_set.all()

        log_name = importer.username.replace('@', '_')
        log_name = log_name.replace('.', '_')
        logger = logging.getLogger('django.helpdesk.emailimporter.' + log_name)
        logging_types = {
            'info': logging.INFO,
            'warn': logging.WARN,
            'error': logging.ERROR,
            'crit': logging.CRITICAL,
            'debug': logging.DEBUG,
        }
        if importer.logging_type in logging_types:
            logger.setLevel(logging_types[importer.logging_type])
        elif not importer.logging_type or importer.logging_type == 'none':
            # disable all handlers so messages go to nowhere
            logger.handlers = []
            logger.propagate = False
        if quiet:
            logger.propagate = False  # do not propagate to root logger that would log to console

        # Log messages to specific file only if the queue has it configured
        if (importer.logging_type in logging_types) and importer.logging_dir:  # if it's enabled and the dir is set
            log_file_handler = logging.FileHandler(join(importer.logging_dir, log_name + '_get_email.log'))
            logger.addHandler(log_file_handler)
        else:
            log_file_handler = None

        try:
            if not importer.default_queue:
                logger.info("Import canceled: no default queue set")
            else:
                default_queue = importer.default_queue

                matching_queues = importer_queues.exclude(match_on__exact=[])
                address_matching_queues = importer_queues.exclude(match_on_addresses__exact=[])
                queues = {
                    'importer_queues': importer_queues,
                    'default_queue': default_queue,
                    'matching_queues': matching_queues,
                    'address_matching_queues': address_matching_queues
                }

                if not importer.email_box_last_check:
                    importer.email_box_last_check = timezone.now() - timedelta(minutes=30)

                queue_time_delta = timedelta(minutes=importer.email_box_interval or 0)
                if not DEBUGGING:
                    if (importer.email_box_last_check + queue_time_delta) < timezone.now():
                        process_importer(importer, queues, logger=logger)
                        importer.email_box_last_check = timezone.now()
                        importer.save()
                else:
                    process_importer(importer, queues, logger=logger)
                    importer.email_box_last_check = timezone.now()
                    importer.save()
            logger.info('')
        finally:
            # we must close the file handler correctly if it's created
            try:
                if log_file_handler:
                    log_file_handler.close()
            except Exception as e:
                logging.exception(e)
            try:
                if log_file_handler:
                    logger.removeHandler(log_file_handler)
            except Exception as e:
                logging.exception(e)


def pop3_sync(importer, queues, logger, server):
    server.getwelcome()
    try:
        server.stls()
    except Exception:
        logger.warning("POP3 StartTLS failed or unsupported. Connection will be unencrypted.")
    server.user(importer.username or settings.QUEUE_EMAIL_BOX_USER)
    server.pass_(importer.password or settings.QUEUE_EMAIL_BOX_PASSWORD)

    messages_info = server.list()[1]
    logger.info("Received %s messages from POP3 server" % len(messages_info))

    for msg_raw in messages_info:
        if type(msg_raw) is bytes:
            try:
                msg = msg_raw.decode("utf-8")
            except UnicodeError:
                # if couldn't decode easily, just leave it raw
                msg = msg_raw
        else:
            # already a str
            msg = msg_raw
        msg_num = msg.split(" ")[0]
        logger.info("Processing message %s" % msg_num)

        raw_content = server.retr(msg_num)[1]
        if type(raw_content[0]) is bytes:
            full_message = "\n".join([elm.decode('utf-8') for elm in raw_content])
        else:
            full_message = encoding.force_text("\n".join(raw_content), errors='replace')
        ticket = object_from_message(full_message, importer, queues, logger)

        if ticket:
            if not DEBUGGING:
                server.dele(msg_num)
            logger.info("Successfully processed message %s, deleted from POP3 server\n" % msg_num)
        else:
            logger.warn("Message %s was not successfully processed, and will be left on POP3 server\n" % msg_num)

    server.quit()


def imap_sync(importer, queues, logger, server):
    try:
        # Check if importer is Gmail. If so, we must authenticate
        if importer.email_box_host == 'imap.gmail.com':
            logger.info("* Checking for tokens.")
            if not hasattr(importer, 'access_token') or not hasattr(importer, 'refresh_token'):
                logger.error("Gmail IMAP login failed. Please check that you have provided an access and refresh token.")
                sys.exit()

            logger.info("* Checking for unexpired access token.")
            if importer.token_expiration < timezone.now():
                logger.info("* Generating new access token.")
                response = email_utils.refresh_tokens(importer.refresh_token)
                if 'access_token' in response:
                    importer.access_token = response['access_token']
                    importer.token_expiration = timezone.now() + timedelta(seconds=response['expires_in'])
                    importer.save()
                else:
                    logger.error("Access token could not be generated.")
                    sys.exit()

            # Obtain authentication string from access token
            logger.info("* Obtaining authentication string.")
            auth_string = email_utils.generate_oauth2_string(importer.username, importer.access_token, base64_encode=False)

            try:
                logger.info("* Authenticating and selecting box.")
                authenticated = email_utils.imap_authentication(server, auth_string, importer.email_box_imap_folder)
            except:
                logger.error("Gmail IMAP authentication failed. Check that the server is accessible and that "
                             "the username and password are correct.")
            else:
                if not authenticated:
                    logger.error("Gmail IMAP unable to authenticate. Check that the server is accessible and that "
                                 "the username and password are correct.")
        else:
            # If not Gmail, continue without the rest.
            try:
                server.starttls()
            except Exception:
                logger.warning("IMAP4 StartTLS unsupported or failed. Connection will be unencrypted.")
            else:
                server.login(importer.username or settings.QUEUE_EMAIL_BOX_USER,
                             importer.password or settings.QUEUE_EMAIL_BOX_PASSWORD)
                server.select(importer.email_box_imap_folder)
    except imaplib.IMAP4.abort:
        logger.error(
            "IMAP login failed. Check that the server is accessible and that "
            "the username and password are correct."
        )
        server.logout()
        sys.exit()
    except ssl.SSLError:
        logger.error(
            "IMAP login failed due to SSL error. This is often due to a timeout. "
            "Please check your connection and try again."
        )
        server.logout()
        sys.exit()

    try:
        if importer.keep_mail:
            status, data = server.search(None, 'NOT', 'ANSWERED')
        else:
            status, data = server.search(None, 'NOT', 'DELETED')
        if data:
            msg_nums = data[0].split()
            logger.info("Received %s messages from IMAP server" % len(msg_nums))

            for num_raw in msg_nums:
                # Get UID and use that
                resp, uid = server.fetch(num_raw, "(UID)")
                uid = uid[0].decode('ascii')
                msg_uid = parse_uid(uid)
                logger.info("Received message UID: %s" % msg_uid)

                # Grab message first to get date to sort by
                status, data = server.uid('fetch', msg_uid, '(RFC822)')
                full_message = encoding.force_text(data[0][1], errors='replace')
                try:
                    ticket = object_from_message(full_message, importer, queues, logger)
                except TypeError:
                    ticket = None  # hotfix. Need to work out WHY.
                except BadHeaderError:
                    # Malformed email received from the server
                    ticket = None
                if ticket:
                    if DEBUGGING:
                        logger.info("Successfully processed message %s, left untouched on IMAP server\n" % msg_uid)
                    elif importer.keep_mail:
                        # server.store(num, '+FLAGS', '\\Answered')
                        ov, data = server.uid('STORE', msg_uid, '+FLAGS', '(\\Answered)')
                        logger.info("Successfully processed message %s, marked as Answered on IMAP server\n" % msg_uid)
                    else:
                        # server.store(num, '+FLAGS', '\\Deleted')
                        ov, data = server.uid('STORE', msg_uid, '+FLAGS', '(\\Deleted)')
                        logger.info("Successfully processed message %s, deleted from IMAP server\n" % msg_uid)
                else:
                    logger.warn("Message %s was not successfully processed, and will be left on IMAP server\n" % msg_uid)
    except imaplib.IMAP4.error:
        logger.error(
            "IMAP retrieve failed. Is the folder '%s' spelled correctly, and does it exist on the server?",
            importer.email_box_imap_folder
        )

    server.expunge()
    server.close()
    server.logout()


def process_importer(importer, queues, logger):
    logger.info("\n***** %s: Begin processing mail for django-helpdesk" % ctime())

    if importer.socks_proxy_type and importer.socks_proxy_host and importer.socks_proxy_port:
        try:
            import socks
        except ImportError:
            no_socks_msg = "Queue has been configured with proxy settings, " \
                           "but no socks library was installed. Try to " \
                           "install PySocks via PyPI."
            logger.error(no_socks_msg)
            raise ImportError(no_socks_msg)

        proxy_type = {
            'socks4': socks.SOCKS4,
            'socks5': socks.SOCKS5,
        }.get(importer.socks_proxy_type)

        socks.set_default_proxy(proxy_type=proxy_type,
                                addr=importer.socks_proxy_host,
                                port=importer.socks_proxy_port)
        socket.socket = socks.socksocket

    email_box_type = settings.QUEUE_EMAIL_BOX_TYPE or importer.email_box_type

    mail_defaults = {
        'pop3': {
            'ssl': {
                'port': 995,
                'init': poplib.POP3_SSL,
            },
            'insecure': {
                'port': 110,
                'init': poplib.POP3,
            },
            'sync': pop3_sync,
        },
        'imap': {
            'ssl': {
                'port': 993,
                'init': imaplib.IMAP4_SSL,
            },
            'insecure': {
                'port': 143,
                'init': imaplib.IMAP4,
            },
            'sync': imap_sync
        }
    }
    if email_box_type in mail_defaults:
        encryption = 'insecure'
        if importer.email_box_ssl or settings.QUEUE_EMAIL_BOX_SSL:
            encryption = 'ssl'
        if not importer.email_box_port:
            importer.email_box_port = mail_defaults[email_box_type][encryption]['port']

        server = mail_defaults[email_box_type][encryption]['init'](
            importer.email_box_host or settings.QUEUE_EMAIL_BOX_HOST,
            int(importer.email_box_port)
        )
        logger.info("Attempting %s server login" % email_box_type.upper())
        mail_defaults[email_box_type]['sync'](importer, queues, logger, server)

    elif email_box_type == 'local':
        mail_dir = importer.email_box_local_dir or '/var/lib/mail/helpdesk/'
        mail = [join(mail_dir, f) for f in os.listdir(mail_dir) if isfile(join(mail_dir, f))]
        logger.info("Found %d messages in local mailbox directory" % len(mail))
        for i, m in enumerate(mail, 1):
            logger.info("Processing message %d" % i)
            with open(m, 'r') as f:
                full_message = encoding.force_text(f.read(), errors='replace')
                ticket = object_from_message(full_message, importer, queues, logger)
            if ticket:
                logger.info("Successfully processed message %d, ticket/comment created.", i)
                try:
                    os.unlink(m)  # delete message file if ticket was successful
                except OSError as e:
                    logger.error("Unable to delete message %d (%s).", i, str(e))
                else:
                    logger.info("Successfully deleted message %d.", i)
            else:
                logger.warn("Message %d was not successfully processed, and will be left in local directory", i)


def decode_unknown(charset, string):
    if type(string) is not str:
        if not charset:
            try:
                return str(string, encoding='utf-8', errors='replace')
            except UnicodeError:
                return str(string, encoding='iso8859-1', errors='replace')
        return str(string, encoding=charset, errors='replace')
    return string


def decode_mail_headers(string):
    decoded = email.header.decode_header(string)
    return u' '.join([
        str(msg, encoding=charset, errors='replace') if charset else str(msg)
        for msg, charset
        in decoded
    ])


def is_autoreply(message):
    """
    Accepting message as something with .get(header_name) method
    Returns True if it's likely to be auto-reply or False otherwise
    So we don't start mail loops
    """
    any_if_this = [
        False if not message.get("Auto-Submitted") else message.get("Auto-Submitted").lower() != "no",
        True if message.get("X-Auto-Response-Suppress") in ("DR", "AutoReply", "All") else False,
        message.get("List-Id"),
        message.get("List-Unsubscribe"),
    ]
    return any(any_if_this)


def create_ticket_cc(ticket, cc_list):

    if not cc_list:
        return []

    # Local import to deal with non-defined / circular reference problem
    from helpdesk.views.staff import User, subscribe_to_ticket_updates

    new_ticket_ccs = []
    for cced_name, cced_email in cc_list:
        cced_email = cced_email.strip()
        if cced_email == ticket.queue.email_address:
            continue

        user = None
        try:
            user = User.objects.get(email=cced_email)
        except User.DoesNotExist:
            pass

        try:
            ticket_cc = subscribe_to_ticket_updates(ticket=ticket, user=user, email=cced_email)
            new_ticket_ccs.append(ticket_cc)
        except ValidationError:
            pass

    return new_ticket_ccs


def create_object_from_email_message(message, ticket_id, payload, files, logger):

    ticket, previous_followup, new = None, None, False
    now = timezone.now()

    queue = payload['queue']
    sender_name = payload['sender'][0]
    sender_email = payload['sender'][1]
    org = queue.organization

    message_id = parseaddr(message.get('Message-Id'))[1]
    in_reply_to = parseaddr(message.get('In-Reply-To'))[1]

    if in_reply_to:
        try:
            queryset = FollowUp.objects.filter(message_id=in_reply_to).order_by('-date')
            if queryset.count() > 0:
                previous_followup = queryset.first()
                ticket = previous_followup.ticket
                logger.info('Found ticket based on in_reply_to: [%s-%s]' % (ticket.queue.slug, ticket.id))
        except FollowUp.DoesNotExist:
            logger.info('FollowUp DoesNotExist error.')
            pass  # play along. The header may be wrong

    if previous_followup is None and ticket_id is not None:
        try:
            ticket = Ticket.objects.get(id=ticket_id)  # TODO also add in organization id? or, just ticket form (which will be diff for each org)?
            logger.info('Ticket found from a ticket_id %s: [%s-%s]' % (ticket_id, ticket.queue.slug, ticket.id))
        except Ticket.DoesNotExist:
            ticket = None
        else:
            new = False
            logger.info('Ticket is not new')
            # Check if the ticket has been merged to another ticket
            if ticket.merged_to:
                logger.info("Ticket has been merged to %s" % ticket.merged_to.ticket)
                # Use the ticket in which it was merged to for next operations
                ticket = ticket.merged_to

    # New issue, create a new <Ticket> instance
    if ticket is None:
        if not settings.QUEUE_EMAIL_BOX_UPDATE_ONLY:
            ticket_form = FormType.objects.get_or_create(name=settings.HELPDESK_EMAIL_FORM_NAME, organization=org)[0]
            fields = CustomField.objects.filter(ticket_form=ticket_form.id).values_list('field_name', flat=True)

            ticket = Ticket.objects.create(
                title=payload['subject'][0:200],
                queue=queue,
                contact_name=sender_name[0:200] if 'contact_name' in fields else None,
                contact_email=sender_email[0:200] if 'contact_email' in fields else None,
                submitter_email=sender_email,
                created=now,
                description=payload['body'],
                priority=payload['priority'],
                ticket_form=ticket_form,
                assigned_to=queue.default_owner if queue.default_owner else None,
            )
            ticket.save()
            logger.debug("Created new ticket %s-%s" % (ticket.queue.slug, ticket.id))
            new = True

    f = FollowUp.objects.create(
        ticket=ticket,
        title=_('E-Mail Received from %(sender_email)s' % {'sender_email': sender_email})[0:200],
        date=now,
        public=True,
        comment=payload.get('full_body', payload['body']) or "",
        message_id=message_id
    )
    # Update ticket and follow-up status
    if not new:
        updater = User.objects.filter(email=sender_email).first()
        submitter = User.objects.filter(email=ticket.submitter_email).first()
        updater_is_staff = is_helpdesk_staff(updater, ticket.ticket_form.organization.id)
        submitter_is_staff = is_helpdesk_staff(submitter, ticket.ticket_form.organization.id)
        if (submitter_is_staff and updater is not ticket.assigned_to) or not updater_is_staff:
            # update is from a public user OR ticket's submitter is a staff member (ticket is internal)
            #   is ticket closed? -> Reopened
            #   else -> Open
            if ticket.status == Ticket.CLOSED_STATUS or ticket.status == Ticket.RESOLVED_STATUS or ticket.status == Ticket.DUPLICATE_STATUS:
                ticket.status = Ticket.REOPENED_STATUS
                f.new_status = Ticket.REOPENED_STATUS
                if updater_is_staff:
                    f.title = _('Ticket Re-Opened by E-Mail Received from %(user)s' % {'user': updater.get_full_name() or updater.get_username()})
                else:
                    f.title = _('Ticket Re-Opened by E-Mail Received from %(sender_email)s' % {'sender_email': sender_email})
            elif ticket.status == Ticket.REPLIED_STATUS:
                ticket.status = Ticket.OPEN_STATUS
                f.new_status = Ticket.OPEN_STATUS
        else:
            # reply is from staff and submitter is not staff -> Replied
            if ticket.status != Ticket.CLOSED_STATUS and ticket.status != Ticket.RESOLVED_STATUS and ticket.status != Ticket.DUPLICATE_STATUS:
                ticket.status = Ticket.REPLIED_STATUS
                f.new_status = Ticket.REPLIED_STATUS
        ticket.save()
        f.save()

    logger.debug("Created new FollowUp for Ticket")
    logger.info("[%s-%s] %s" % (ticket.queue.slug, ticket.id, ticket.title,))

    attached = process_attachments(f, files)
    for att_file in attached:
        logger.info(
            "Attachment '%s' successfully added to ticket from email.",
            att_file[0]
        )

    context = safe_template_context(ticket)
    context['private'] = False

    create_ticket_cc(ticket, payload['to_list'] + payload['cc_list'])

    autoreply = is_autoreply(message)
    if autoreply:
        logger.info("Message seems to be auto-reply, not sending any emails back to the sender")
    else:
        # send mail to appropriate people now depending on what objects
        # were created and who was CC'd
        # Add auto-reply headers because it's an auto-reply and we must
        extra_headers = {
            'In-Reply-To': message_id,
            "Auto-Submitted": "auto-replied",
            "X-Auto-Response-Suppress": "All",
            "Precedence": "auto_reply",
        }
        if new:
            roles = {'submitter': ('newticket_submitter', context),
                     'queue_new': ('newticket_cc_user', context),
                     'queue_updated': ('newticket_cc_user', context),
                     'cc_users': ('newticket_cc_user', context),
                     'cc_public': ('newticket_cc_public', context),
                     'extra': ('newticket_cc_public', context)}
            if ticket.assigned_to:
                roles['assigned_to'] = ('assigned_owner', context)
            ticket.send_ticket_mail(roles, organization=org, fail_silently=True, extra_headers=extra_headers, email_logger=logger,
                                    source="import (new ticket)")
        else:
            context.update(comment=f.comment)
            ticket.send_ticket_mail(
                {'submitter': ('updated_submitter', context),
                 'assigned_to': ('updated_owner', context),
                 'cc_users': ('updated_cc_user', context),
                 'queue_updated': ('updated_cc_user', context)},
                organization=org,
                fail_silently=True,
                extra_headers=extra_headers,
                email_logger=logger,
                source="import (submitter, staff)"
            )
            if queue.enable_notifications_on_email_events:
                ticket.send_ticket_mail(
                    {'cc_public': ('updated_cc_public', context),
                     'extra': ('updated_cc_public', context)},
                    organization=org,
                    fail_silently=True,
                    extra_headers=extra_headers,
                    email_logger=logger,
                    source="import (public)"
                )

    return ticket


def object_from_message(message, importer, queues, logger):
    # 'message' must be an RFC822 formatted message.
    message = email.message_from_string(message)

    # Replaces original helpdesk code "get_charset()", which wasn't an actual method ?
    charset = list(filter(lambda s: s is not None, message.get_charsets()))
    if charset:
        charset = charset[0]

    subject = message.get('subject', _('Comment from e-mail'))
    subject = decode_mail_headers(decode_unknown(charset, subject))
    for affix in STRIPPED_SUBJECT_STRINGS:
        subject = subject.replace(affix, "")
    subject = subject.strip()

    sender = parseaddr(message.get('from', _('Unknown Sender')))
    if sender[1] == '':
        # Delete emails if the sender email cannot be parsed correctly. This ensures that
        # mailing list emails do not become tickets as well as malformatted emails
        return True

    to_list = getaddresses(message.get_all('To', []))
    cc_list = getaddresses(message.get_all('Cc', []))

    # Ignore List applies to sender, TO emails, and CC list
    for ignored_address in IgnoreEmail.objects.filter(Q(importers=importer) | Q(importers__isnull=True)):
        for name, address in [sender] + to_list + cc_list:
            if ignored_address.test(address):
                logger.debug("Email address matched an ignored address. Ticket will not be created")
                if ignored_address.keep_in_mailbox:
                    return False  # By returning 'False' the message will be kept in the mailbox,
                return True  # and the 'True' will cause the message to be deleted.

    # Sort out which queue this email should go into #
    ticket, queue = None, None
    for q in queues['importer_queues']:
        matchobj = re.match(r".*\[" + q.slug + r"-(?P<id>\d+)\]", subject)
        if matchobj and not ticket:
            ticket = matchobj.group('id')
            queue = q
            logger.info("- Matched tracking ID %s-%s" % (q.slug, ticket))
    if not ticket:
        logger.info("- No tracking ID matched.")
        for q in queues['matching_queues']:
            if not queue:
                for m in q.match_on:
                    m_re = re.compile(r'\b%s\b' % m, re.I)
                    if m_re.search(subject):
                        queue = q
                        logger.info("- Subject matched list from '%s'" % q.slug)
    if not queue:
        sender_lower = sender[1].lower()
        for q in queues['address_matching_queues']:
            if reduce(lambda prev, e: prev or (e.lower() in sender_lower), q.match_on_addresses, False):
                queue = q
                logger.info("- Sender address matched list from '%s'" % q.slug)
    if not queue:
        logger.info("- Using default queue.")
        queue = queues['default_queue']

    # Accounting for forwarding loops
    auto_forward = message.get('X-BEAMHelpdesk-Delivered', None)

    if auto_forward is not None or sender[1].lower() == queue.email_address.lower():
        logger.info("Found a forwarding loop.")
        if ticket and Ticket.objects.filter(pk=ticket).exists():
            if sender[1].lower() == queue.email_address.lower() and auto_forward is None:
                auto_forward = [i[1] for i in to_list]
            else:
                auto_forward = auto_forward.strip().split(',')
            for address in auto_forward:
                cc = TicketCC.objects.filter(ticket_id=ticket, email__iexact=address)
                if cc:
                    cc.delete()
                    logger.info("Deleted the CC'd address from the ticket")
                    logger.debug("Address deleted was %s" % address)  # TODO remove later for privacy
        return True

    body = None
    full_body = None
    counter = 0
    files = []

    for part in message.walk():
        if part.get_content_maintype() == 'multipart':
            continue

        name = part.get_param("name")
        if name:
            name = email.utils.collapse_rfc2231_value(name)

        if part.get_content_maintype() == 'text' and name is None:
            if part.get_content_subtype() == 'plain':
                body = part.get_payload(decode=True)
                # https://github.com/django-helpdesk/django-helpdesk/issues/732
                if part['Content-Transfer-Encoding'] == '8bit' and part.get_content_charset() == 'utf-8':
                    body = body.decode('unicode_escape')
                body = decode_unknown(part.get_content_charset(), body)
                # have to use django_settings here so overwritting it works in tests
                # the default value is False anyway
                if ticket is None and getattr(django_settings, 'HELPDESK_FULL_FIRST_MESSAGE_FROM_EMAIL', False):
                    # first message in thread, we save full body to avoid losing forwards and things like that
                    body_parts = []
                    for f in EmailReplyParser.read(body).fragments:
                        body_parts.append(f.content)
                    full_body = '\n\n'.join(body_parts)
                    body = EmailReplyParser.parse_reply(body)
                else:
                    # second and other reply, save only first part of the message
                    body = EmailReplyParser.parse_reply(body)
                    full_body = body
                # workaround to get unicode text out rather than escaped text
                try:
                    body = body.encode('ascii').decode('unicode_escape')
                except UnicodeEncodeError:
                    body.encode('utf-8')
                except UnicodeDecodeError:
                    body = body.encode('utf-8')  # todo
                logger.debug("Discovered plain text MIME part")
            else:
                try:
                    email_body = encoding.smart_text(part.get_payload(decode=True))
                except UnicodeDecodeError:
                    email_body = encoding.smart_text(part.get_payload(decode=False))

                if not body and not full_body:
                    # no text has been parsed so far - try such deep parsing for some messages
                    altered_body = email_body.replace("</p>", "</p>\n").replace("<br", "\n<br")
                    mail = BeautifulSoup(str(altered_body), "html.parser")
                    full_body = mail.get_text()

                if "<body" not in email_body:
                    email_body = f"<body>{email_body}</body>"

                payload = (
                    '<html>'
                    '<head>'
                    '<meta charset="utf-8" />'
                    '</head>'
                    '%s'
                    '</html>'
                ) % email_body
                files.append(
                    SimpleUploadedFile(_("email_html_body.html"), payload.encode("utf-8"), 'text/html')
                )
                logger.debug("Discovered HTML MIME part")
        else:
            if not name:
                ext = mimetypes.guess_extension(part.get_content_type())
                name = "part-%i%s" % (counter, ext)
            else:
                name = ("part-%i_" % counter) + name

            # # FIXME: this code gets the paylods, then does something with it and then completely ignores it
            # # writing the part.get_payload(decode=True) instead; and then the payload variable is
            # # replaced by some dict later.
            # # the `payloadToWrite` has been also ignored so was commented
            # payload = part.get_payload()
            # if isinstance(payload, list):
            #     payload = payload.pop().as_string()
            # # payloadToWrite = payload
            # # check version of python to ensure use of only the correct error type
            # non_b64_err = TypeError
            # try:
            #     logger.debug("Try to base64 decode the attachment payload")
            #     # payloadToWrite = base64.decodebytes(payload)
            # except non_b64_err:
            #     logger.debug("Payload was not base64 encoded, using raw bytes")
            #     # payloadToWrite = payload
            files.append(SimpleUploadedFile(name, part.get_payload(decode=True), mimetypes.guess_type(name)[0]))
            logger.debug("Found MIME attachment %s" % name)

        counter += 1

    if not body:
        mail = BeautifulSoup(str(message), "html.parser")
        beautiful_body = mail.find('body')
        if beautiful_body:
            try:
                body = beautiful_body.text
                full_body = body
            except AttributeError:
                pass
        if not body:
            body = ""

    if getattr(django_settings, 'HELPDESK_ALWAYS_SAVE_INCOMING_EMAIL_MESSAGE', False):
        # save message as attachment in case of some complex markup renders wrong
        files.append(
            SimpleUploadedFile(
                _("original_message.eml").replace(
                    ".eml",
                    timezone.localtime().strftime("_%d-%m-%Y_%H:%M") + ".eml"
                ),
                str(message).encode("utf-8"),
                'text/plain'
            )
        )

    smtp_priority = message.get('priority', '')
    smtp_importance = message.get('importance', '')
    high_priority_types = {'high', 'important', '1', 'urgent'}
    priority = 2 if high_priority_types & {smtp_priority, smtp_importance} else 3

    payload = {
        'body': body,
        'full_body': full_body or body,
        'subject': subject,
        'queue': queue,
        'sender': sender,
        'priority': priority,
        'files': files,
        'cc_list': cc_list,
        'to_list': to_list,
    }
    return create_object_from_email_message(message, ticket, payload, files, logger=logger)
