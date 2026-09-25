"""Owner Q6: one write path (gate.perform), rules by actor kind."""
from django.core.exceptions import ValidationError
from django.test import TestCase

from agent import gate
from agent.models import AgentAction, AgentConfig
from intake.models import AllowedSender

from . import factories as f


def cfg(**kw):
    c = AgentConfig.get_solo()
    for k, v in kw.items():
        setattr(c, k, v)
    c.save()
    return c


def write(kind, actor=None, conditions=None, tier=gate.T0):
    box = []
    act = gate.perform(tier=tier, action_type='test', before={}, actor=actor, actor_kind=kind,
                       target_model='intake.InboundEmail',
                       conditions=conditions, apply=lambda: box.append(1) or {'ok': True})
    return act, box


class GateActorKindTest(TestCase):
    def setUp(self):
        self.admin = f.user(role='admin')
        self.planner = f.user(role='planner', email='p@t.com')

    def test_agent_writes_stop_on_kill_switch(self):
        cfg(enabled=False)
        with self.assertRaises(gate.AgentDisabled):
            write('agent')
        cfg(enabled=True)
        act, box = write('agent')
        self.assertEqual((act.actor_kind, act.human_confirmed, box), ('agent', False, [1]))

    def test_intake_runner_needs_kill_switch_on_and_suggest_mode(self):
        cfg(enabled=True, tc_intake_mode='off')
        with self.assertRaises(gate.IntakeModeOff):
            write('intake_runner')
        cfg(enabled=False, tc_intake_mode='suggest')
        with self.assertRaises(gate.AgentDisabled):
            write('intake_runner')
        cfg(enabled=True)
        self.assertEqual(write('intake_runner')[0].actor_kind, 'intake_runner')

    def test_fetch_ignores_kill_switch_but_needs_flag_and_sender(self):
        cfg(enabled=False, intake_fetch_enabled=False)
        with self.assertRaises(gate.FetchDisabled):
            write('intake_fetch')
        cfg(intake_fetch_enabled=True)
        with self.assertRaises(gate.FetchDisabled):                  # no active sender
            write('intake_fetch')
        AllowedSender.objects.create(email_or_domain='tv.lk', active=False)
        with self.assertRaises(gate.FetchDisabled):
            write('intake_fetch')
        AllowedSender.objects.create(email_or_domain='desk@tv.lk')
        act, box = write('intake_fetch')                              # agent disabled, still fetches
        self.assertEqual((act.actor_kind, box), ('intake_fetch', [1]))

    def test_human_writes_ignore_kill_switch_need_admin_and_conditions(self):
        cfg(enabled=False)
        act, _ = write('human', actor=self.admin)
        self.assertTrue(act.human_confirmed)
        self.assertEqual(act.actor_id, self.admin.id)
        for who in (None, self.planner):
            with self.assertRaises(gate.HumanNotAllowed):
                write('human', actor=who)
        with self.assertRaises(gate.HumanNotAllowed):
            write('human', actor=self.admin, conditions={'schedule_ok': True, 'dates_ok': False})
        self.assertEqual(AgentAction.objects.count(), 1)

    def test_nothing_written_when_refused(self):
        cfg(enabled=False)
        with self.assertRaises(gate.AgentDisabled):
            _, box = write('agent')
        self.assertFalse(AgentAction.objects.exists())

    def test_unknown_actor_kind_refused(self):
        with self.assertRaises(ValueError):
            write('robot')


class IntakeModeTest(TestCase):
    def test_auto_is_rejected(self):
        c = AgentConfig.get_solo()
        c.tc_intake_mode = 'auto'
        with self.assertRaises(ValidationError):
            c.full_clean()
        with self.assertRaises(ValueError):
            c.save()
        self.assertEqual([k for k, _ in AgentConfig.INTAKE_MODES], ['off', 'suggest'])


class AllowedSenderTest(TestCase):
    def test_exact_email_and_exact_domain(self):
        e = AllowedSender.objects.create(email_or_domain=' Desk@TV.lk ')
        d = AllowedSender.objects.create(email_or_domain='radio.lk')
        self.assertEqual(e.email_or_domain, 'desk@tv.lk')
        self.assertTrue(e.matches('DESK@tv.lk'))
        self.assertFalse(e.matches('other@tv.lk'))
        self.assertTrue(d.matches('traffic@radio.lk'))
        self.assertFalse(d.matches('traffic@sub.radio.lk'))           # exact domain only
        self.assertFalse(d.matches('radio.lk@evil.com'))
        self.assertEqual(AllowedSender.for_sender('desk@tv.lk'), [e])


class IntakeKindTargetTest(TestCase):
    def test_intake_kinds_cannot_write_core_tables(self):
        for kind in ('intake_runner', 'intake_fetch', 'intake_retention'):
            with self.assertRaises(ValueError):
                gate.perform(tier=gate.T0, action_type='x', actor_kind=kind, target_model='core.TransmissionReport',
                             before={}, apply=lambda: {})
        self.assertFalse(AgentAction.objects.exists())
