"""Phase 3.2 close: intake_llm_probe, AgentConfig.intake_tool_choice, forced tool_choice in the runner."""
import os
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from agent.forms import AgentConfigForm
from agent.models import AgentAction, AgentConfig, LlmCall
from agent.tests import factories as f
from intake.cron import runner
from intake.llm import probe
from intake.llm.provider import FakeProvider, ProviderError, Turn, tool_use
from intake.models import InboundAttachment, InboundEmail

from .helpers import enable, scenario
from .test_phase2_1 import decision
from .test_runner import agent_script

REFUSAL = 'tool_choice: type "tool" and "any" are not supported for this model.'


def submit_turn(i=10, o=5):
    return Turn([tool_use('submit_decision', {'attachment_id': 0, 'decision': 'needs_review', 'schedule_id': None,
                                              'reason_code': None, 'schedule_number_source': 'none',
                                              'suspicious_instruction': False, 'note': 'probe'}, 'p')],
                'tool_use', i, o, 'fake-model')


def refused(choice):
    return ProviderError('BadRequestError: 400', status_code=400, error_text=REFUSAL)


def run_fake(script):
    p = FakeProvider(script)
    return p, probe.run_probe(p)


class ProbeTest(TestCase):
    def test_all_three_supported(self):
        p, rows = run_fake([submit_turn(12, 7), submit_turn(), submit_turn()])
        self.assertEqual([r['mode'] for r in rows], ['auto', 'tool', 'any'])
        self.assertEqual([q['tool_choice'] for q in p.requests],
                         [{'type': 'auto'}, {'type': 'tool', 'name': 'submit_decision'}, {'type': 'any'}])
        self.assertEqual(p.requests[0]['tools'], ['submit_decision'])          # synthetic, one tool only
        self.assertTrue(all(r['supported'] and r['http_status'] == 200 and r['submit_called'] for r in rows))
        calls = LlmCall.objects.filter(purpose='probe').order_by('id')
        self.assertEqual([c.outcome for c in calls], ['supported'] * 3)
        self.assertEqual((calls[0].input_tokens, calls[0].output_tokens), (12, 7))
        self.assertEqual(len({c.detail['run_id'] for c in calls}), 1)
        self.assertEqual(probe.forced_supported('fake-model'), (True, 'the latest probe of fake-model showed forced tool_choice as supported'))

    def test_forced_unsupported_records_status_and_exact_error(self):
        _, rows = run_fake([submit_turn(), refused('tool'), refused('any')])
        self.assertEqual([r['outcome'] for r in rows], ['supported', 'unsupported', 'unsupported'])
        self.assertEqual((rows[1]['http_status'], rows[1]['error_text']), (400, REFUSAL))
        self.assertFalse(rows[1]['submit_called'])
        row = LlmCall.objects.get(purpose='probe', prompt_version='probe:tool')
        self.assertEqual((row.outcome, row.detail['http_status'], row.detail['error_text']), ('unsupported', 400, REFUSAL))
        ok, why = probe.forced_supported('fake-model')
        self.assertFalse(ok)
        self.assertIn('tool, any', why)

    def test_network_error_is_not_supported(self):
        _, rows = run_fake([ProviderError('APIConnectionError: timeout'), submit_turn(), submit_turn()])
        self.assertEqual((rows[0]['outcome'], rows[0]['http_status']), ('error', None))

    def test_writes_nothing_but_llmcall(self):
        before = (AgentAction.objects.count(), InboundEmail.objects.count(), InboundAttachment.objects.count())
        run_fake([submit_turn(), submit_turn(), submit_turn()])
        self.assertEqual((AgentAction.objects.count(), InboundEmail.objects.count(),
                          InboundAttachment.objects.count()), before)
        self.assertEqual(LlmCall.objects.exclude(purpose='probe').count(), 0)
        self.assertFalse(AgentConfig.objects.exists())

    def test_latest_run_wins(self):
        run_fake([submit_turn(), submit_turn(), submit_turn()])
        run_fake([submit_turn(), refused('tool'), submit_turn()])
        self.assertFalse(probe.forced_supported('fake-model')[0])


class ProbeCommandTest(TestCase):
    def test_refuses_without_environment_variables(self):
        for env in ({}, {'ANTHROPIC_MODEL': 'm'}, {'ANTHROPIC_API_KEY': 'k'}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(CommandError) as ctx:
                    call_command('intake_llm_probe', stdout=StringIO())
                self.assertIn('Refused', str(ctx.exception))
        self.assertFalse(LlmCall.objects.exists())

    def test_runs_with_the_mocked_provider(self):
        fake = FakeProvider([submit_turn(), refused('tool'), refused('any')])
        out = StringIO()
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': 'fake-model', 'ANTHROPIC_API_KEY': 'k'}), \
                mock.patch('intake.management.commands.intake_llm_probe.make_provider', return_value=fake):
            call_command('intake_llm_probe', stdout=out)
        text = out.getvalue()
        self.assertIn('"result": "unsupported"', text)
        self.assertIn('"http_status": 400', text)
        self.assertIn(REFUSAL.replace('"', '\\"'), text)
        self.assertIn('NOT supported', text)
        self.assertEqual(LlmCall.objects.filter(purpose='probe').count(), 3)


class ToolChoiceSettingTest(TestCase):
    def form(self, value):
        cfg = AgentConfig.get_solo()
        return AgentConfigForm({'intake_tool_choice': value, 'autonomy_level': 0, 'mapping_threshold': 0.92,
                                'grace_days': 3, 'upload_debounce_minutes': 10, 'tc_intake_mode': 'off',
                                'min_brand_overlap': 0.6, 'llm_daily_token_cap': 200000}, instance=cfg)

    def test_default_is_auto(self):
        self.assertEqual(AgentConfig.get_solo().intake_tool_choice, 'auto')

    def test_form_refuses_forced_without_a_passing_probe(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': 'fake-model'}):
            form = self.form('forced')
            self.assertFalse(form.is_valid())
            self.assertIn('no probe has been run', str(form.errors['intake_tool_choice']))
            run_fake([submit_turn(), refused('tool'), refused('any')])
            form = self.form('forced')
            self.assertFalse(form.is_valid())
            self.assertIn('did not show forced', str(form.errors['intake_tool_choice']))

    def test_form_accepts_forced_after_a_passing_probe(self):
        run_fake([submit_turn(), submit_turn(), submit_turn()])
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': 'fake-model'}):
            form = self.form('forced')
            self.assertTrue(form.is_valid(), form.errors)

    def test_probe_of_another_model_does_not_count(self):
        run_fake([submit_turn(), submit_turn(), submit_turn()])            # fake-model
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': 'other-model'}):
            self.assertFalse(self.form('forced').is_valid())

    def test_settings_page_refuses_forced(self):
        admin = f.user(role='admin', email='boss@x.lk')
        self.client.force_login(admin)
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': 'fake-model'}):
            r = self.client.post('/dashboard/agent/config/', {
                'what': 'config', 'autonomy_level': 0, 'mapping_threshold': 0.92, 'grace_days': 3,
                'upload_debounce_minutes': 10, 'tc_intake_mode': 'off', 'min_brand_overlap': 0.6,
                'llm_daily_token_cap': 200000, 'intake_tool_choice': 'forced'})
        self.assertEqual(r.status_code, 200)                                  # form re-rendered with the error
        self.assertEqual(AgentConfig.get_solo().intake_tool_choice, 'auto')


class RunnerToolChoiceTest(TestCase):
    def setUp(self):
        enable()
        self.acc, self.s, self.att = scenario()

    def run_with(self, script):
        p = FakeProvider(script)
        runner.process_attachment(self.att, p, runner.require_service_user())
        self.att.refresh_from_db()
        return p

    def set_forced(self):
        AgentConfig.objects.filter(pk=1).update(intake_tool_choice='forced')

    def test_auto_by_default(self):
        p = self.run_with(agent_script(lambda: self.att.id))
        self.assertTrue(all(q['tool_choice'] == {'type': 'auto'} for q in p.requests))

    def test_forced_uses_any_after_a_passing_probe(self):
        run_fake([submit_turn(), submit_turn(), submit_turn()])
        self.set_forced()
        p = self.run_with(agent_script(lambda: self.att.id))
        self.assertTrue(all(q['tool_choice'] == {'type': 'any'} for q in p.requests))
        self.assertEqual(self.att.status, 'suggested')

    def test_forced_setting_without_passing_probe_falls_back_to_auto(self):
        run_fake([submit_turn(), refused('tool'), refused('any')])
        self.set_forced()
        p = self.run_with(agent_script(lambda: self.att.id))
        self.assertTrue(all(q['tool_choice'] == {'type': 'auto'} for q in p.requests))

    def test_llm_no_decision_still_applies_when_forced(self):
        run_fake([submit_turn(), submit_turn(), submit_turn()])
        self.set_forced()
        text = Turn([{'type': 'text', 'text': 'Looks like schedule 101.'}], 'end_turn', 10, 5)
        p = self.run_with([text, text])
        self.assertEqual(p.requests[0]['tool_choice'], {'type': 'any'})
        self.assertEqual(p.requests[1]['tool_choice'], {'type': 'tool', 'name': 'submit_decision'})
        self.assertEqual((self.att.status, self.att.reason), ('needs_review', 'llm_no_decision'))

    def test_duplicate_submit_still_llm_no_decision_when_forced(self):
        run_fake([submit_turn(), submit_turn(), submit_turn()])
        self.set_forced()
        steps = agent_script(lambda: self.att.id)
        tu = lambda i: tool_use('submit_decision', decision(self.att.id, self.s.id), f's{i}')   # noqa: E731
        self.run_with(steps[:2] + [lambda m: Turn([tu(1), tu(2)], 'tool_use', 10, 5)])
        self.assertEqual(self.att.reason, 'llm_no_decision')


class AnthropicProviderErrorTest(TestCase):
    """The real SDK error class is turned into ProviderError with the HTTP status and exact text."""

    def test_bad_request_keeps_status_and_exact_error_text(self):
        import anthropic
        import httpx
        from intake.llm.provider import AnthropicProvider
        resp = httpx.Response(400, request=httpx.Request('POST', 'https://api.anthropic.com/v1/messages'))
        err = anthropic.BadRequestError(f'Error code: 400 - {REFUSAL}', response=resp,
                                        body={'type': 'error', 'error': {'type': 'invalid_request_error',
                                                                         'message': REFUSAL}})
        client = mock.Mock()
        client.messages.create.side_effect = err
        p = AnthropicProvider(model='some-model', client=client)
        with self.assertRaises(ProviderError) as ctx:
            p.create('s', [{'role': 'user', 'content': 'x'}], [], tool_choice={'type': 'any'})
        self.assertEqual((ctx.exception.status_code, ctx.exception.error_text), (400, REFUSAL))
        self.assertEqual(client.messages.create.call_args.kwargs['tool_choice'], {'type': 'any'})
        client.messages.create.side_effect = None
        client.messages.create.reset_mock()
        with self.assertRaises(Exception):
            p.create('s', [], [])
        self.assertEqual(client.messages.create.call_args.kwargs['tool_choice'], {'type': 'auto'})
