"""Amendment A6 / P5: every private or core function the agent imports is pinned here.
If core renames a function or changes its parameters, this test fails loudly."""
import inspect

from django.test import SimpleTestCase

CONTRACT = {
    # A6: TC intake helpers (option (a))
    ('core.views', '_find_col'): ['df', 'names'],
    ('core.views', '_detect_tc_meta'): ['df'],
    ('core.views', '_parse_tc_rows'): ['df', 'account', 'tc_report'],
    ('verification.tc_converters.dispatch', 'get_converter'): ['channel'],
    ('verification.tc_converters.gemini_ai', 'is_configured'): [],
    ('verification.tc_converters.gemini_ai', 'parse_pdf'): ['pdf_path', 'channel', 'extra_instructions'],
    # P5: access helpers and settings
    ('core.views', '_is_admin'): ['user'],
    ('core.views', '_account_qs'): ['user'],
    ('core.views', '_account_access'): ['user', 'account_id'],
    # Phase 2 intake (read-only helpers)
    ('core.views', '_tc_channel_prompt'): ['channel'],
    ('core.views', '_safe_str'): ['val'],
    ('core.views', '_safe_int'): ['val'],
    ('core.views', '_safe_date'): ['val'],
    ('core.models', 'get_setting'): ['key', 'default'],
    ('core.models', 'get_setting_int'): ['key', 'default'],
    ('core.models', 'get_setting_list'): ['key'],
    # Engines and engine helpers
    ('verification.engine', 'run_scope'): ['account_id', 'channel', 'month', 'mode'],
    ('verification.engine', 'active_schedule_ids'): ['account_id', 'channel', 'month'],
    ('verification.engine', '_makeup_schedules_for_scope'): ['active_schedules'],
    ('verification.engine', '_lmrb_channel_q'): ['channel'],
    ('verification.tc_engine', 'reconcile_tc'): ['account_id', 'channel', 'month', 'mode', 'schedule_id'],
    ('verification.tc_engine', 'build_summary_data'): ['account_id', 'channel', 'month', 'schedule_id'],
    ('verification.tc_engine', '_build_tc_theme_map'): ['account_id'],
    ('verification.tc_engine', '_tc_themes_for_brand'): ['brand', 'duration', 'tc_theme_map'],
    ('verification.tc_engine', '_build_reverse_tc_theme_map'): ['account_id'],
    ('verification.tc_engine', '_brands_for_tc_theme'): ['tc_theme', 'duration', 'reverse_tc_map'],
    ('verification.tc_engine', '_build_lmrb_theme_map'): ['account_id'],
    ('verification.tc_engine', '_lmrb_themes_for_brand'): ['brand', 'duration', 'lmrb_theme_map'],
    ('verification.sponsorship_engine', 'reconcile_sponsorship'): ['account_id', 'channel', 'month', 'mode', 'schedule_id'],
    ('verification.sponsorship_engine', 'remove_assignments'): ['account_id', 'channel', 'month', 'assignment_ids'],
    ('verification.period_sponsorship_engine', 'reconcile_period_sponsorship'): ['ps', 'user'],
    ('verification.period_sponsorship_engine', 'reset_period_sponsorship'): ['ps'],
    ('verification.tc_lmrb_engine', 'reconcile_tc_lmrb'): ['account_id', 'channel', 'month', 'mode', 'user'],
}
CLASSES = {('verification.tc_converters.gemini_ai', 'GeminiError')}


class CoreContractTest(SimpleTestCase):
    def test_signatures(self):
        import importlib
        for (mod, name), params in CONTRACT.items():
            with self.subTest(f'{mod}.{name}'):
                fn = getattr(importlib.import_module(mod), name, None)
                self.assertIsNotNone(fn, f'{mod}.{name} no longer exists')
                got = [p.name for p in inspect.signature(fn).parameters.values()]
                self.assertEqual(got, params, f'{mod}.{name} signature changed: {got}')

    def test_classes(self):
        import importlib
        for mod, name in CLASSES:
            cls = getattr(importlib.import_module(mod), name, None)
            self.assertTrue(inspect.isclass(cls) and issubclass(cls, Exception), f'{mod}.{name}')

    def test_every_agent_import_of_core_is_in_the_contract(self):
        """Scan agent/ and intake/ source for `from core…`/`from verification…` imports of
        private names and engine functions; each must be pinned above."""
        import ast
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        pinned = {n for (_m, n) in CONTRACT} | {n for (_m, n) in CLASSES}
        allowed_models = {'Account', 'BrandMapping', 'LMRBRow', 'ManualMatch', 'MatchResult',
                          'MonitoringData', 'PeriodSponsorship', 'PeriodSponsorshipMatch', 'Schedule',
                          'ScheduleRow', 'SponsorshipLmrbAssignment', 'SummaryReportMeta', 'SystemSetting',
                          'TCRow', 'TcLmrbMatch', 'TcLmrbThemeMap', 'TransmissionReport', 'AuditLog'}
        missing = []
        for path in list(root.glob('agent/**/*.py')) + list(root.glob('intake/**/*.py')):
            if '/tests/' in str(path) or '/migrations/' in str(path):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.split('.')[0] in ('core', 'verification'):
                    for a in node.names:
                        if a.name not in pinned and a.name not in allowed_models:
                            missing.append(f'{path.relative_to(root)}: {node.module}.{a.name}')
        self.assertEqual(missing, [], 'core/verification imports not covered by the contract')
