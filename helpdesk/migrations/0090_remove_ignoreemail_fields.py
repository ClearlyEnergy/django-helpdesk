# Generated by Django 3.2.18 on 2023-07-19 18:20

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('helpdesk', '0089_move_ignoreemail_fields'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='ignoreemail',
            name='importers',
        ),
        migrations.AlterField(
            model_name='ignoreemail',
            name='organization',
            field=models.ForeignKey(on_delete=models.deletion.CASCADE, to='orgs.organization'),
        ),
        migrations.AlterField(
            model_name='ignoreemail',
            name='modified',
            field=models.DateField(blank=True, default=None, editable=False, help_text='Date on which this e-mail address was last modified', verbose_name='Last Modified'),
        ),
    ]
