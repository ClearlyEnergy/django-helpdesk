#!/usr/bin/python


from django.core.management.base import BaseCommand, CommandError

import datetime

from helpdesk.models import Notification

class Command(BaseCommand):
    help = "Deletes read notifications over a day old and any expired announcements"

    def handle(self, *args, **options):
        notifications = Notification.objects.filter(announcement=False, is_read=True, delete_by__lt=datetime.datetime.now())
        announcements = Notification.objects.filter(announcement=True, is_read=True, delete_by__lt=datetime.datetime.now())

        if notifications.exists():
            count_notifs = notifications.delete()[0]
            self.stdout.write(self.style.SUCCESS(f'Successfully deleted {count_notifs} old read notifications'))
        else:
            self.stdout.write(self.style.WARNING('No old read notifications to delete'))

        if announcements.exists():
            count_announcements = announcements.delete()[0]
            self.stdout.write(self.style.SUCCESS(f'Successfully deleted {count_announcements} expired announcements'))
        else:
            self.stdout.write(self.style.WARNING('No old read announcements to delete'))
