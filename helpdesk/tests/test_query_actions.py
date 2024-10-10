# -*- coding: utf-8 -*-
from django.urls import reverse
from django.test import TestCase
from helpdesk.models import Queue, SavedSearch
from helpdesk.tests.helpers import get_user
from helpdesk.decorators import is_helpdesk_staff
from seed.lib.superperms.orgs.models import Organization


class TestQueryActions(TestCase):
    def setUp(self):
        # Create two users to test various query actions
        self.org = Organization.objects.create(name='org1')

        self.user1 = get_user(username='user1', is_staff=True, organization=self.org)

        self.user2 = get_user(username='user2', is_staff=True, organization=self.org)

        q = Queue(title='Q1', slug='q1', organization=self.org)
        q.save()
        self.q = q

    def test_cansavequery(self):
        """Can a query be saved"""
        self.client.login(username=self.user1.username, password='password')
        url = reverse('helpdesk:savequery')
        response = self.client.post(
            url,
            data={
                'title': 'ticket on my queue',
                'queue': self.q,
                'query_encoded':
                    'KGRwMApWZmlsdGVyaW5nCnAxCihkcDIKVnN0YXR1c19faW4KcDMKKG'
                    'xwNApJMQphSTIKYUkzCmFzc1Zzb3J0aW5nCnA1ClZjcmVhdGVkCnA2CnMu'
            })
        self.assertTrue(is_helpdesk_staff(self.user1))
        self.assertEqual(response.status_code, 302)
        self.assertTrue('tickets/?saved-query=1' in response.url)

    def test_delete_query(self):
        """Can a query be deleted"""
        self.client.login(username=self.user1.username, password='password')
        # Create sample Query
        query = SavedSearch(user=self.user1, organization=self.org)
        query.save()
        response = self.client.get(reverse('helpdesk:delete_query', kwargs={'id': query.id}))
        self.assertTemplateUsed(response, 'helpdesk/confirm_delete_saved_query.html')

        # Actually deleting it
        response = self.client.post(reverse('helpdesk:delete_query', kwargs={'id': query.id}))
        self.assertRedirects(response, reverse('helpdesk:list'))

        # Recreate the query