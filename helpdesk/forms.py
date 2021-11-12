"""
django-helpdesk - A Django powered ticket tracker for small enterprise.

(c) Copyright 2008 Jutda. All Rights Reserved. See LICENSE for details.

forms.py - Definitions of newforms-based forms for creating and maintaining
           tickets.
"""
import logging
from datetime import datetime, date, time
from operator import itemgetter

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django import forms
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth import get_user_model
from django.utils import timezone

from helpdesk.lib import safe_template_context, process_attachments
from helpdesk.models import (Ticket, Queue, FollowUp, IgnoreEmail, TicketCC,
                             CustomField, TicketCustomFieldValue, TicketDependency, UserSettings, KBItem,
                             FormType)
from helpdesk import settings as helpdesk_settings

logger = logging.getLogger(__name__)
User = get_user_model()

CUSTOMFIELD_TO_FIELD_DICT = {
    # Store the immediate equivalences here
    'boolean': forms.BooleanField,
    'date': forms.DateField,
    'time': forms.TimeField,
    'datetime': forms.DateTimeField,
    'email': forms.EmailField,
    'url': forms.URLField,
    'ipaddress': forms.GenericIPAddressField,
    'slug': forms.SlugField,
    # TODO Add attachment type here? and foreignkey?
}

CUSTOMFIELD_DATE_FORMAT = "%Y-%m-%d"
CUSTOMFIELD_TIME_FORMAT = "%H:%M:%S"
CUSTOMFIELD_DATETIME_FORMAT = f"{CUSTOMFIELD_DATE_FORMAT} {CUSTOMFIELD_TIME_FORMAT}"


def _building_lookup(ticket_form_id, changed_data):
    """
    :param ticket_form_id: int. The ID of a ticket.
    :param changed_data: list of strings. The strings are names of all changed form fields.
    :return: list of strings. The strings are names of all changed field forms that are associated with columns

    Checks if any changed fields are associated with columns in BEAM, and returns a list of them.
    Called by save() in EditTicketForm, TicketForm and PublicTicketForm.
    """
    changed_data = map(lambda f: f.replace('e_', '', 1) if f.startswith('e_') else f, changed_data)
    custom_fields = CustomField.objects.filter(
        ticket_form=ticket_form_id,
        field_name__in=changed_data,
    ).exclude(columns=None)
    if custom_fields.exists():
        return custom_fields.values_list('field_name', flat=True)
    return []

def _field_ordering(queryset):
    # ordering fields based on form_ordering
    # if form_ordering is None, field is sorted to end of list
    ordering = sorted(
        queryset.values('field_name', 'form_ordering', 'is_extra_data'),
        key=lambda x: float('inf') if x['form_ordering'] is None else x['form_ordering']
    )
    ordering = [
        "e_%s" % field['field_name'] if field['is_extra_data']
        else field['field_name']
        for field in ordering
    ]
    return ordering

class CustomFieldMixin(object):
    """
    Mixin that provides a method to turn CustomFields into an actual field
    """

    def customfield_to_field(self, field, instanceargs):
        # Field is an object in CustomField, with attributes like field_name, label, help_text, etc
        # instanceargs dict is for the frontend display settings like max_length, the kind of form widget, etc
        # Use TextInput widget by default
        instanceargs['widget'] = forms.TextInput(attrs={'class': 'form-control'})
        # if-elif branches start with special cases
        if field.data_type is None:
            fieldclass = forms.NullBooleanField
        elif field.data_type == 'varchar':
            fieldclass = forms.CharField
            instanceargs['max_length'] = field.max_length
        elif field.data_type == 'text':
            fieldclass = forms.CharField
            instanceargs['widget'] = forms.Textarea(attrs={'class': 'form-control'})
            instanceargs['max_length'] = field.max_length
        elif field.data_type == 'integer':
            fieldclass = forms.IntegerField
            instanceargs['widget'] = forms.NumberInput(attrs={'class': 'form-control'})
        elif field.data_type == 'decimal':
            fieldclass = forms.DecimalField
            instanceargs['decimal_places'] = field.decimal_places
            instanceargs['max_digits'] = field.max_length
            instanceargs['widget'] = forms.NumberInput(attrs={'class': 'form-control'})
        elif field.data_type == 'list':
            fieldclass = forms.ChoiceField
            choices = field.choices_as_array
            if field.empty_selection_list:
                choices.insert(0, ('', '---------'))
            instanceargs['choices'] = choices
            instanceargs['widget'] = forms.Select(attrs={'class': 'form-control'})
        else:
            # Try to use the immediate equivalences dictionary
            try:
                fieldclass = CUSTOMFIELD_TO_FIELD_DICT[field.data_type]
                # Change widgets for the following classes
                if fieldclass == forms.DateField:
                    instanceargs['widget'] = forms.DateInput(attrs={'class': 'form-control date-field', 'autocomplete': 'off'})
                elif fieldclass == forms.DateTimeField:
                    instanceargs['widget'] = forms.DateTimeInput(attrs={'class': 'form-control datetime-field', 'autocomplete': 'off'})
                elif fieldclass == forms.TimeField:
                    instanceargs['widget'] = forms.TimeInput(attrs={'class': 'form-control time-field', 'autocomplete': 'off'})
                elif fieldclass == forms.BooleanField:
                    instanceargs['widget'] = forms.CheckboxInput(attrs={'class': 'form-control'})

            except KeyError:
                # The data_type was not found anywhere
                raise NameError("Unrecognized data_type %s" % field.data_type)

        # TODO change this
        if field.is_extra_data:
            self.fields['e_%s' % field.field_name] = fieldclass(**instanceargs)
        else:
            self.fields[field.field_name] = fieldclass(**instanceargs)


class EditTicketForm(CustomFieldMixin, forms.ModelForm):

    class Meta:
        model = Ticket
        exclude = ('assigned_to', 'created', 'modified', 'status', 'on_hold', 'resolution', 'last_escalation',
                   'organization', 'ticket_form', 'beam_property', 'beam_taxlot')

    class Media:
        js = ('helpdesk/js/init_due_date.js', 'helpdesk/js/init_datetime_classes.js', 'helpdesk/js/validate.js')

    def __init__(self, *args, **kwargs):
        """
        Add any custom fields that are defined to the form
        """
        super(EditTicketForm, self).__init__(*args, **kwargs)
        form_id = self.instance.ticket_form.pk
        extra_data = self.instance.extra_data

        # CustomField already excludes builtin_fields and SEED fields
        display_objects = CustomField.objects.filter(ticket_form=form_id).exclude(field_name='queue')

        # Disable and add help_text to the merged_to field on this form
        self.fields['merged_to'].disabled = True
        self.fields['merged_to'].help_text = _('This ticket is merged into the selected ticket.')
        for display_data in display_objects:
            initial_value = None
            if display_data.editable and display_data.is_extra_data:
                try:
                    initial_value = extra_data[display_data.field_name]
                    # Attempt to convert from fixed format string to date/time data type
                    if 'datetime' == display_data.data_type:
                        initial_value = datetime.strptime(initial_value, CUSTOMFIELD_DATETIME_FORMAT)
                    elif 'date' == display_data.data_type:
                        initial_value = datetime.strptime(initial_value, CUSTOMFIELD_DATE_FORMAT)
                    elif 'time' == display_data.data_type:
                        initial_value = datetime.strptime(initial_value, CUSTOMFIELD_TIME_FORMAT)
                    # If it is boolean field, transform the value to a real boolean instead of a string
                    elif 'boolean' == display_data.data_type:
                        initial_value = 'True' == initial_value
                except (KeyError, ValueError, TypeError):  # TicketCustomFieldValue.DoesNotExist,
                    # ValueError error if parsing fails, using initial_value = current_value.value
                    # TypeError if parsing None type
                    pass
                instanceargs = {
                    'label': display_data.label,
                    'help_text': display_data.get_markdown(),
                    'required': display_data.required,
                    'initial': initial_value,
                }
                self.customfield_to_field(display_data, instanceargs)

            elif display_data.field_name in self.fields:
                if not display_data.editable:
                    self.fields[display_data.field_name].widget = forms.HiddenInput()
                else:
                    attrs = ['label', 'help_text', 'list_values', 'required', 'data_type']
                    for attr in attrs:
                        display_info = getattr(display_data, attr, None)
                        if display_info is not None and display_info != '':
                            if attr == 'help_text':
                                setattr(self.fields[display_data.field_name], attr, display_data.get_markdown())
                            elif attr == 'data_type':
                                if display_info == 'datetime' or display_info == 'time' or display_info == 'date':
                                    self.fields[display_data.field_name].widget.attrs.update({'autocomplete': 'off'})
                            else:
                                setattr(self.fields[display_data.field_name], attr, display_info)
                            # print('--%s: %s' % (attr, display_info))
        self.fields['extra_data'].widget = forms.HiddenInput()

        self.order_fields(_field_ordering(display_objects))

    def clean(self):
        cleaned_data = super(EditTicketForm, self).clean()
        for field, value in cleaned_data.items():
            if field.startswith('e_'):
                field_name = field.replace('e_', '', 1)
                # Convert date/time data type to known fixed format string.
                if datetime is type(value):
                    value = value.strftime(CUSTOMFIELD_DATETIME_FORMAT)
                elif date is type(value):
                    value = value.strftime(CUSTOMFIELD_DATE_FORMAT)
                elif time is type(value):
                    value = value.strftime(CUSTOMFIELD_TIME_FORMAT)
                cleaned_data['extra_data'][field_name] = value
        return cleaned_data

    def save(self, commit=True):
        # Overrides save() to include building lookup method.
        instance = super(EditTicketForm, self).save(commit=False)
        changed_fields = None
        if self.cleaned_data['lookup']:
            changed_fields = _building_lookup(instance.ticket_form.id, self.changed_data)
        if commit:
            instance.save(query_fields=changed_fields)
        return instance


class EditFollowUpForm(forms.ModelForm):

    class Meta:
        model = FollowUp
        exclude = ('date', 'user',)

    def __init__(self, *args, **kwargs):
        """Filter not openned tickets here."""
        super(EditFollowUpForm, self).__init__(*args, **kwargs)
        self.fields["ticket"].queryset = Ticket.objects.filter(status__in=(Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS))


class AbstractTicketForm(CustomFieldMixin, forms.Form):
    """
    Contain all the common code and fields between "TicketForm" and
    "PublicTicketForm". This Form is not intended to be used directly.
    """
    # TODO clean up form fields
    form_id = None
    form_title = None
    form_introduction = None
    form_queue = None
    hidden_fields = []

    queue = forms.ChoiceField(
        widget=forms.Select(attrs={'class': 'form-control'}),
        label=_('Queue'),
        required=True,
        choices=()
    )
    priority = forms.ChoiceField(
        widget=forms.Select(attrs={'class': 'form-control'}),
        choices=Ticket.PRIORITY_CHOICES,
        initial=getattr(settings, 'HELPDESK_PUBLIC_TICKET_PRIORITY', '3'),
    )
    attachment = forms.FileField(
        widget=forms.FileInput(attrs={'class': 'form-control-file'}),
    )
    # TODO add beam_property and beam_taxlot so they can be viewed on the staff-side ticket page

    class Media:
        js = ('helpdesk/js/init_due_date.js', 'helpdesk/js/init_datetime_classes.js', 'helpdesk/js/validate.js')

    def __init__(self, kbcategory=None, *args, **kwargs):
        self.form_id = kwargs.pop("form_id")
        super().__init__(*args, **kwargs)

        form = FormType.objects.get(pk=self.form_id)
        self.form_title = form.name
        self.form_introduction = form.get_markdown()
        self.form_queue = form.queue
        if form.queue:
            del self.fields['queue']

        if kbcategory:
            self.fields['kbitem'] = forms.ChoiceField(
                widget=forms.Select(attrs={'class': 'form-control'}),
                required=False,
                label=_('Knowledge Base Item'),
                choices=[(kbi.pk, kbi.title) for kbi in KBItem.objects.filter(category=kbcategory.pk, enabled=True)],
            )

    def clean(self):
        cleaned_data = super(AbstractTicketForm, self).clean()

        # for hidden fields required by helpdesk code, like description
        for field, type in self.hidden_fields:
            if field in self.errors:
                cleaned_data[field] = '' if type in ['varchar', 'text', 'email'] else None
                del self._errors[field]

        form = FormType.objects.get(id=self.form_id)
        if form.queue:
            cleaned_data['queue'] = form.queue.id

        return cleaned_data

    def save(self, commit=True):
        # Overrides save() SOLELY to include building lookup method.
        instance = super(AbstractTicketForm, self).save(commit=False)
        instance = _building_lookup(instance, self.changed_data)
        if commit:
            instance.save()
        return instance

    def _create_ticket(self):
        kbitem = None
        if 'kbitem' in self.cleaned_data:
            kbitem = KBItem.objects.get(id=int(self.cleaned_data['kbitem']))

        extra_data = {}
        if 'extra_data' in self.cleaned_data:
            extra_data = self.cleaned_data['extra_data']

        for field, value in self.cleaned_data.items():
            if field.startswith('e_'):
                field_name = field.replace('e_', '', 1)
                extra_data[field_name] = value

        ticket_form = FormType.objects.get(pk=self.form_id)
        queue = Queue.objects.get(id=int(self.cleaned_data['queue']))

        ticket = Ticket(
            # TODO Necessary fields
            ticket_form=ticket_form,  # self.cleaned_data['ticket_form'],
            # Default fields + kbitem
            title=self.cleaned_data['title'],
            submitter_email=self.cleaned_data['submitter_email'],
            created=timezone.now(),
            status=Ticket.OPEN_STATUS,
            queue=queue,
            description=self.cleaned_data['description'],
            priority=self.cleaned_data.get(
                'priority',
                getattr(settings, "HELPDESK_PUBLIC_TICKET_PRIORITY", "3")),
            due_date=self.cleaned_data.get(
                'due_date',
                getattr(settings, "HELPDESK_PUBLIC_TICKET_DUE_DATE", None)
            ) or None,
            kbitem=kbitem,
            # BEAM's default fields
            contact_name=self.cleaned_data['contact_name'],
            contact_email=self.cleaned_data['contact_email'],
            building_name=self.cleaned_data['building_name'],
            building_address=self.cleaned_data['building_address'],
            pm_id=self.cleaned_data['pm_id'],
            building_id=self.cleaned_data['building_id'],
            extra_data=extra_data
        )

        return ticket, queue

    def _create_follow_up(self, ticket, title, user=None):
        followup = FollowUp(ticket=ticket,
                            title=title,
                            date=timezone.now(),
                            public=True,
                            comment=self.cleaned_data['description'],)
        if user:
            followup.user = user
        return followup

    def _attach_files_to_follow_up(self, followup):
        files = self.cleaned_data['attachment']
        if files:
            files = process_attachments(followup, [files])
        return files

    @staticmethod
    def _send_messages(ticket, queue, followup, files, user=None):
        context = safe_template_context(ticket)
        context['comment'] = followup.comment

        roles = {'submitter': ('newticket_submitter', context),
                 'new_ticket_cc': ('newticket_cc', context),
                 'ticket_cc': ('newticket_cc', context),
                 'extra': ('newticket_cc', context)}
        if ticket.assigned_to and ticket.assigned_to.usersettings_helpdesk.email_on_ticket_assign:
            roles['assigned_to'] = ('assigned_owner', context)
        ticket.send(
            roles,
            fail_silently=True,
            files=files,
        )

    # TODO move this init
    def _add_form_custom_fields(self, staff_only_filter=None):
        if self.form_id is not None:
            if staff_only_filter is None:
                queryset = CustomField.objects.filter(ticket_form=self.form_id)
                hidden_queryset = []
            else:
                queryset = CustomField.objects.filter(ticket_form=self.form_id, staff_only=staff_only_filter)
                hidden_queryset = CustomField.objects.filter(ticket_form=self.form_id,
                                                             staff_only=(not staff_only_filter))
                self.hidden_fields = hidden_queryset.values_list('field_name', 'data_type')
            if self.form_queue:
                queryset = queryset.exclude(field_name='queue')

            for field in queryset:
                if field.field_name in self.fields:
                    attrs = ['label', 'help_text', 'list_values', 'required',
                             'data_type']  # TODO view-side ordering too
                    for attr in attrs:
                        display_info = getattr(field, attr, None)
                        if display_info is not None and display_info != '':
                            if attr == 'help_text':
                                setattr(self.fields[field.field_name], attr, field.get_markdown())
                            elif attr == 'data_type':
                                if display_info == 'datetime' or display_info == 'time' or display_info == 'date':
                                    self.fields[display_data.field_name].widget.attrs.update(
                                        {'autocomplete': 'off'})
                            else:
                                setattr(self.fields[field.field_name], attr, display_info)
                else:
                    instanceargs = {
                        'label': field.label,
                        'help_text': field.get_markdown(),
                        'required': field.required,
                    }
                    self.customfield_to_field(field, instanceargs)
            for field in hidden_queryset:
                if field.field_name not in self.fields:
                    self.customfield_to_field(field, {})
                self.fields[field.field_name].widget = forms.HiddenInput()

            self.order_fields(_field_ordering(queryset))


class TicketForm(AbstractTicketForm):
    """
    Ticket Form creation for registered users.
    """
    submitter_email = forms.EmailField(
        required=False,
        label=_('Submitter E-Mail Address'),
        widget=forms.TextInput(attrs={'class': 'form-control', 'type': 'email'}),
        help_text=_('This e-mail address will receive copies of all public '
                    'updates to this ticket.'),
    )
    assigned_to = forms.ChoiceField(
        widget=(
            forms.Select(attrs={'class': 'form-control'})
            if not helpdesk_settings.HELPDESK_CREATE_TICKET_HIDE_ASSIGNED_TO
            else forms.HiddenInput()
        ),
        required=False,
        label=_('Case owner'),
        help_text=_('If you select an owner other than yourself, they\'ll be '
                    'e-mailed details of this ticket immediately.'),
        choices=()
    )

    def __init__(self, *args, **kwargs):
        """
        Add any custom fields that are defined to the form.
        """
        queue_choices = kwargs.pop("queue_choices")

        super().__init__(*args, **kwargs)
        self._add_form_custom_fields()

        if self.form_queue is None:
            self.fields['queue'].choices = queue_choices

        if helpdesk_settings.HELPDESK_STAFF_ONLY_TICKET_OWNERS:
            assignable_users = User.objects.filter(is_active=True, is_staff=True).order_by(User.USERNAME_FIELD)
        else:
            assignable_users = User.objects.filter(is_active=True).order_by(User.USERNAME_FIELD)
        self.fields['assigned_to'].choices = [('', '--------')] + [(u.id, u.get_username()) for u in assignable_users]

    def save(self, user, form_id=None):
        """
        Writes and returns a Ticket() object
        """
        self.form_id = form_id
        ticket, queue = self._create_ticket()

        if self.cleaned_data['assigned_to']:
            try:
                u = User.objects.get(id=self.cleaned_data['assigned_to'])
                ticket.assigned_to = u
            except User.DoesNotExist:
                ticket.assigned_to = None
        elif queue.default_owner and not ticket.assigned_to:
            ticket.assigned_to = queue.default_owner

        changed_fields = _building_lookup(ticket.ticket_form.id, self.changed_data)
        ticket.save(query_fields=changed_fields)

        if self.cleaned_data['assigned_to']:
            title = _('Ticket Opened & Assigned to %(name)s') % {
                'name': ticket.get_assigned_to or _("<invalid user>")
            }
        else:
            title = _('Ticket Opened')
        followup = self._create_follow_up(ticket, title=title, user=user)
        followup.save()

        files = self._attach_files_to_follow_up(followup)
        self._send_messages(ticket=ticket,
                            queue=queue,
                            followup=followup,
                            files=files,
                            user=user)
        return ticket


class PublicTicketForm(AbstractTicketForm):
    """
    Ticket Form creation for all users (public-facing).
    """
    # TODO remove this, replace w/ contact email
    submitter_email = forms.EmailField(
        widget=forms.TextInput(attrs={'class': 'form-control', 'type': 'email'}),
        required=True,
        label=_('Your E-Mail Address'),
        help_text=_('We will e-mail you when your ticket is updated.'),
    )

    def __init__(self, hidden_fields=(), readonly_fields=(), *args, **kwargs):
        """
        Add any (non-staff) custom fields that are defined to the form
        """
        super(PublicTicketForm, self).__init__(*args, **kwargs)
        self._add_form_custom_fields(False)

        # Hiding fields based on CustomField attributes has already been done; this is hiding based on kwargs
        for field in self.fields.keys():
            if field in hidden_fields:
                self.fields[field].widget = forms.HiddenInput()
            if field in readonly_fields:
                self.fields[field].disabled = True
   
        public_queues = Queue.objects.filter(allow_public_submission=True)  # TODO base off org_id

        if len(public_queues) == 0:
            logger.warning("There are no public queues defined - public ticket creation is impossible")

        if self.form_queue is None:
            self.fields['queue'].choices = [('', '--------')] + [
                (q.id, q.title) for q in public_queues]

    def save(self, user, form_id=None):
        """
        Writes and returns a Ticket() object
        """
        self.form_id = form_id
        ticket, queue = self._create_ticket()

        if queue.default_owner and not ticket.assigned_to:
            ticket.assigned_to = queue.default_owner

        changed_fields = _building_lookup(ticket.ticket_form.id, self.changed_data)
        ticket.save(query_fields=changed_fields)

        followup = self._create_follow_up(
            ticket, title=_('Ticket Opened Via Web'), user=user)
        followup.save()

        files = self._attach_files_to_follow_up(followup)
        self._send_messages(ticket=ticket,
                            queue=queue,
                            followup=followup,
                            files=files)
        return ticket


class UserSettingsForm(forms.ModelForm):

    class Meta:
        model = UserSettings
        exclude = ['user', 'settings_pickled']


class EmailIgnoreForm(forms.ModelForm):

    class Meta:
        model = IgnoreEmail
        exclude = []


class TicketCCForm(forms.ModelForm):
    """ Adds either an email address or helpdesk user as a CC on a Ticket. Used for processing POST requests. """

    class Meta:
        model = TicketCC
        exclude = ('ticket',)

    def __init__(self, *args, **kwargs):
        super(TicketCCForm, self).__init__(*args, **kwargs)
        if helpdesk_settings.HELPDESK_STAFF_ONLY_TICKET_CC:
            users = User.objects.filter(is_active=True, is_staff=True).order_by(User.USERNAME_FIELD)
        else:
            users = User.objects.filter(is_active=True).order_by(User.USERNAME_FIELD)
        self.fields['user'].queryset = users


class TicketCCUserForm(forms.ModelForm):
    """ Adds a helpdesk user as a CC on a Ticket """

    def __init__(self, *args, **kwargs):
        super(TicketCCUserForm, self).__init__(*args, **kwargs)
        if helpdesk_settings.HELPDESK_STAFF_ONLY_TICKET_CC:
            users = User.objects.filter(is_active=True, is_staff=True).order_by(User.USERNAME_FIELD)
        else:
            users = User.objects.filter(is_active=True).order_by(User.USERNAME_FIELD)
        self.fields['user'].queryset = users

    class Meta:
        model = TicketCC
        exclude = ('ticket', 'email',)


class TicketCCEmailForm(forms.ModelForm):
    """ Adds an email address as a CC on a Ticket """

    def __init__(self, *args, **kwargs):
        super(TicketCCEmailForm, self).__init__(*args, **kwargs)

    class Meta:
        model = TicketCC
        exclude = ('ticket', 'user',)


class TicketDependencyForm(forms.ModelForm):
    """ Adds a different ticket as a dependency for this Ticket """

    class Meta:
        model = TicketDependency
        exclude = ('ticket',)


class MultipleTicketSelectForm(forms.Form):
    tickets = forms.ModelMultipleChoiceField(
        label=_('Tickets to merge'),
        queryset=Ticket.objects.filter(merged_to=None),
        widget=forms.SelectMultiple(attrs={'class': 'form-control'})
    )

    def clean_tickets(self):
        tickets = self.cleaned_data.get('tickets')
        if len(tickets) < 2:
            raise ValidationError(_('Please choose at least 2 tickets.'))
        if len(tickets) > 4:
            raise ValidationError(_('Impossible to merge more than 4 tickets...'))
        queues = tickets.order_by('queue').distinct().values_list('queue', flat=True)
        if len(queues) != 1:
            raise ValidationError(_('All selected tickets must share the same queue in order to be merged.'))
        return tickets
