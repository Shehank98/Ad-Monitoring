from django.db import IntegrityError, transaction
from django.test import TestCase

from agent.models import AgentConfig, NotificationLog, ScopeState

from .factories import CHANNEL, MONTH, account


class ModelsTest(TestCase):
    def test_config_singleton(self):
        a = AgentConfig.get_solo()
        b = AgentConfig.get_solo()
        self.assertEqual((a.pk, b.pk), (1, 1))
        AgentConfig(enabled=True).save()
        self.assertEqual(AgentConfig.objects.count(), 1)

    def test_scope_state_unique(self):
        acc = account()
        ScopeState.objects.create(account=acc, channel=CHANNEL, month=MONTH)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScopeState.objects.create(account=acc, channel=CHANNEL, month=MONTH)
        # different case is a different (exact) string, stored as given
        ScopeState.objects.create(account=acc, channel=CHANNEL.upper(), month=MONTH)

    def test_notification_dedupe_key_unique(self):
        NotificationLog.objects.create(dedupe_key='s|tpl|u|2025-01-01', template_key='tpl')
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationLog.objects.create(dedupe_key='s|tpl|u|2025-01-01', template_key='tpl')
