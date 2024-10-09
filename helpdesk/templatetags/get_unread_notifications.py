from django import template
from helpdesk.models import Notification

register = template.Library()

@register.filter
def get_unread_notifications(user, organization):
    return Notification.objects.filter(user=user, organization=organization, is_read=False).count()
