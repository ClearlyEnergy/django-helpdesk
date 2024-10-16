# Generated by Django 3.2.20 on 2024-09-17 14:17

from django.db import migrations, models, transaction
import django.db.models.deletion

def assign_default_org_to_saved_search(apps, schema_editor):
    SavedSearch = apps.get_model('helpdesk', 'SavedSearch')
    
    with transaction.atomic():
        for saved_search in SavedSearch.objects.all():
            org_ids = saved_search.user.organizationuser_set.values_list('organization', flat=True)

            first = True
            for org_id in org_ids:
                if first: # update existing instance
                    saved_search.organization_id = org_id
                    saved_search.save()
                    first = False
                else:
                    saved_search.pk = None
                    saved_search._state.adding = True
                    saved_search.organization_id = org_id
                    saved_search.save()

def collapse_saved_searches_on_title(apps, schema_editor):
    SavedSearch = apps.get_model('helpdesk', 'SavedSearch')

    user_ids = SavedSearch.objects.all().values_list('user', flat=True)

    for user_id in user_ids:
        unique_titles = SavedSearch.objects.filter(user_id=user_id).values_list('title', flat=True).distinct()

        for title in unique_titles:
            searches = SavedSearch.objects.filter(user_id=user_id, title=title)
            searches = searches.exclude(pk=searches.first().pk)
            searches.delete()

class Migration(migrations.Migration):

    dependencies = [
        ('orgs', '0061_add_org_backgroundimage'),
        ('helpdesk', '0103_add_savedsearch_organization'),
    ]

    operations = [
        migrations.RunPython(assign_default_org_to_saved_search, collapse_saved_searches_on_title),
    ]
