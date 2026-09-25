from django.test import TestCase

from agent import gate
from agent.models import AgentAccountOverride, AgentAction, AgentConfig

from .factories import account


def cfg(enabled=True, level=1):
    c = AgentConfig.get_solo()
    c.enabled, c.autonomy_level = enabled, level
    c.save()
    return c


ALL_T3 = {k: True for k in gate.T3_CONDITIONS}


class GateTest(TestCase):
    def test_defaults_are_off(self):
        c = AgentConfig.get_solo()
        self.assertFalse(c.enabled)
        self.assertEqual(c.autonomy_level, 0)
        self.assertEqual(c.upload_debounce_minutes, 10)
        self.assertEqual(c.tc_intake_mode, 'off')

    def test_t0_always_allowed_even_when_disabled(self):
        self.assertTrue(gate.allowed(gate.T0))

    def test_kill_switch_blocks_every_write_tier(self):
        cfg(enabled=False, level=3)
        for t in (gate.T1, gate.T2, gate.T3, gate.T4):
            self.assertFalse(gate.allowed(t, conditions=ALL_T3))
        with self.assertRaises(gate.AgentDisabled):
            gate.perform(tier=gate.T1, action_type='x', before={}, apply=lambda: {})
        self.assertFalse(AgentAction.objects.exists())

    def test_levels(self):
        cfg(level=0)
        self.assertFalse(gate.allowed(gate.T1))
        cfg(level=1)
        self.assertTrue(gate.allowed(gate.T1))
        self.assertFalse(gate.allowed(gate.T2))            # A8: aliases never auto
        self.assertFalse(gate.allowed(gate.T3, conditions=ALL_T3))
        cfg(level=3)
        self.assertTrue(gate.allowed(gate.T3, conditions=ALL_T3))
        self.assertFalse(gate.allowed(gate.T4, conditions=ALL_T3))

    def test_wildcard_is_never_auto_applied(self):
        cfg(level=3)
        self.assertFalse(gate.allowed(gate.T3, conditions={**ALL_T3, 'exact_value': False}))

    def test_authorised_change_is_a_proposal(self):
        cfg(level=3)
        self.assertFalse(gate.allowed(gate.T3, conditions={**ALL_T3, 'no_authorised_change': False}))

    def test_account_override(self):
        cfg(level=3)
        acc = account()
        AgentAccountOverride.objects.create(account=acc, enabled=False)
        self.assertFalse(gate.allowed(gate.T1, acc.id))
        AgentAccountOverride.objects.filter(account=acc).update(enabled=None, autonomy_level=0)
        self.assertFalse(gate.allowed(gate.T1, acc.id))

    def test_perform_records_before_and_after(self):
        cfg(level=1)
        a = gate.perform(tier=gate.T1, action_type='test', before={'x': 1}, apply=lambda: {'x': 2},
                         reason='r', evidence={'e': 1})
        self.assertEqual((a.before, a.after, a.tier), ({'x': 1}, {'x': 2}, 1))

    def test_perform_rechecks_kill_switch_immediately_before_write(self):
        cfg(level=1)
        calls = []
        real = gate.ensure_enabled

        def flip(account_id=None):
            calls.append(1)
            if len(calls) == 2:          # disabled between the tier check and the write
                AgentConfig.objects.filter(pk=1).update(enabled=False)
            return real(account_id)
        gate.ensure_enabled = flip
        try:
            with self.assertRaises(gate.AgentDisabled):
                gate.perform(tier=gate.T1, action_type='x', before={}, apply=lambda: {})
        finally:
            gate.ensure_enabled = real
        self.assertFalse(AgentAction.objects.exists())
