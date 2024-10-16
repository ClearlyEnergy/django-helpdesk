# Generated by Django 3.2.18 on 2023-07-20 19:15

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('orgs', '0045_merge_beam03'),
        ('helpdesk', '0092_move_presetreply_organization'),
    ]

    operations = [
        migrations.AlterField(
            model_name='presetreply',
            name='body',
            field=models.TextField(help_text="<a href='/static/seed/pdf/Markdown_Cheat_Sheet.pdf' target='_blank' rel='noopener noreferrer'             title='ClearlyEnergy Markdown Cheat Sheet'>Markdown syntax</a> allowed, but no raw HTML.<br/>Context available:<br/>{{ ticket }}: the ticket object (eg {{ ticket.title }})<br/>{{ queue }}: the queue<br/>{{ user }}: the current user.<br/>", verbose_name='Body'),
        ),
        migrations.AlterField(
            model_name='presetreply',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='orgs.organization'),
        ),
    ]
