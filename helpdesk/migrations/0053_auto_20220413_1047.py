# Generated by Django 3.2.7 on 2022-04-13 17:47

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('helpdesk', '0052_auto_20220307_0719'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ticketcc',
            name='can_update',
            field=models.BooleanField(blank=True, default=False, help_text='Can this person login and update the ticket?', verbose_name='Update Ticket'),
        ),
        migrations.AlterField(
            model_name='ticketcc',
            name='can_view',
            field=models.BooleanField(blank=True, default=False, help_text='Can this person login to view the ticket details?', verbose_name='View Ticket'),
        ),
        migrations.AlterField(
            model_name='ticketcc',
            name='email',
            field=models.EmailField(blank=True, help_text='This address will not receive updates from private comments.', max_length=254, null=True, verbose_name='E-Mail Address'),
        ),
        migrations.AlterField(
            model_name='ticketcc',
            name='user',
            field=models.ForeignKey(blank=True, help_text='This user will receive staff updates from both private and public comments.', null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL, verbose_name='User'),
        ),
    ]
