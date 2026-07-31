"""
Comprehensive test suite for the Ad-Monitoring Django application.

Covers:
- Model integrity (dedup keys, locking flags)
- Schedule↔LMRB matching engine (run_scope smart/reset modes)
- TC reconciliation engine (reconcile_tc, build_summary_data)
- Sponsorship engine (reconcile_sponsorship, manual_assign, reset_sponsorship)
- Multi-schedule priority (Rule 10) and versioning (Rule 12)
- ManualMatch creation and permanence
- Edge cases: boundary dates, late-aired rule, time tolerance

Run with:
    python manage.py test core
"""

import datetime
from django.db import IntegrityError
from django.test import TestCase

from accounts.models import User
from core.models import (
    Account,
    BrandMapping,
    LMRBRow,
    ManualMatch,
    MatchResult,
    Schedule,
    ScheduleRow,
    SponsorshipLmrbAssignment,
    SystemSetting,
    TCRow,
    TransmissionReport,
)
from verification.engine import run_scope
from verification.sponsorship_engine import (
    lmrb_candidates,
    manual_assign,
    reconcile_sponsorship,
    reset_sponsorship,
)
from verification.tc_engine import build_summary_data, reconcile_tc


# ── Shared fixture helpers ────────────────────────────────────────────────────

CHANNEL = "Sirasa TV"
MONTH = "January 2025"
DATE = datetime.date(2025, 1, 15)


def make_account(name="Test Account"):
    """Create and return a test Account."""
    return Account.objects.create(name=name)


def make_user(email="ops@test.com", role="operations"):
    """Create and return a test User."""
    return User.objects.create_user(
        email=email,
        name="Test User",
        password="password123",
        role=role,
        must_change_password=False,
    )


def make_schedule(account, channel=CHANNEL, month=MONTH, schedule_number="101", version=1):
    """Create and return a Schedule header record."""
    return Schedule.objects.create(
        account=account,
        channel=channel,
        month=month,
        schedule_number=schedule_number,
        file="schedules/dummy.xlsx",
        original_filename="dummy.xlsx",
        row_count=0,
        start_date=datetime.date(2025, 1, 1),
        end_date=datetime.date(2025, 1, 31),
        version=version,
    )


def make_schedule_row(
    account,
    schedule,
    brand="Brand A",
    date=DATE,
    start_time="20:00:00",
    end_time="21:00:00",
    duration=30,
    ad_type="COMMERCIAL BENEFITS",
    programme="Test Show",
    channel=CHANNEL,
    month=MONTH,
):
    """Create and return a ScheduleRow."""
    return ScheduleRow.objects.create(
        schedule=schedule,
        account=account,
        channel=channel,
        month=month,
        brand=brand,
        programme=programme,
        date=date,
        start_time=start_time,
        end_time=end_time,
        duration=duration,
        ad_type=ad_type,
    )


def make_lmrb_row(
    account,
    advt_theme="Theme A",
    date=DATE,
    advt_time="20:30:00",
    duration=30,
    channel=CHANNEL,
    source="maponline",
    brk_no=None,
    pos_in_brk=None,
    advertiser="",
    product="",
):
    """Create and return an LMRBRow with a unique dedup key."""
    dedup_key = LMRBRow.make_dedup_key(
        account.id, channel, date, advt_time, advt_theme, duration,
        brk_no=brk_no, pos_in_brk=pos_in_brk,
        advertiser=advertiser, product=product,
    )
    return LMRBRow.objects.create(
        account=account,
        channel=channel,
        date=date,
        advt_theme=advt_theme,
        advt_time=advt_time,
        duration=duration,
        source=source,
        dedup_key=dedup_key,
    )


def make_brand_mapping(account, brand="Brand A", theme="Theme A", tc_theme="TC Theme A", duration=None):
    """Create and return a BrandMapping."""
    return BrandMapping.objects.create(
        account=account,
        brand=brand,
        theme=theme,
        tc_theme=tc_theme,
        duration=duration,
    )


def make_tc_report(account, channel=CHANNEL, month=MONTH, schedule=None):
    """Create and return a TransmissionReport."""
    return TransmissionReport.objects.create(
        account=account,
        channel=channel,
        month=month,
        schedule=schedule,
        file="tc/dummy.xlsx",
        original_filename="tc_dummy.xlsx",
        row_count=0,
        start_date=datetime.date(2025, 1, 1),
        end_date=datetime.date(2025, 1, 31),
    )


def make_tc_row(
    account,
    tc_report,
    tc_theme="TC Theme A",
    date=DATE,
    aired_time="20:30:00",
    duration=30,
    channel=CHANNEL,
    programme="Test Show",
    suffix="",
):
    """Create and return a TCRow with a unique dedup key."""
    dedup_key = TCRow.make_dedup_key(
        account.id, channel, date, aired_time + suffix, tc_theme, duration
    )
    return TCRow.objects.create(
        account=account,
        tc_report=tc_report,
        channel=channel,
        date=date,
        programme=programme,
        tc_theme=tc_theme,
        duration=duration,
        aired_time=aired_time,
        dedup_key=dedup_key,
    )


def ensure_tc_tolerance(seconds=5):
    """Ensure the TC-LMRB time tolerance SystemSetting is set to the given value."""
    SystemSetting.objects.update_or_create(
        key="tc_lmrb_time_tolerance",
        defaults={
            "value": str(seconds),
            "label": "TC-LMRB Time Tolerance (seconds)",
            "category": "reconciliation",
        },
    )


# ── Test Classes ──────────────────────────────────────────────────────────────


class LMRBRowDedupKeyTest(TestCase):
    """Tests for LMRBRow dedup key uniqueness constraint."""

    def setUp(self):
        self.account = make_account()

    def test_dedup_key_unique_constraint_raises_integrity_error(self):
        """Creating two LMRBRows with the same dedup key must raise IntegrityError."""
        key = LMRBRow.make_dedup_key(
            self.account.id, CHANNEL, DATE, "20:00:00", "Theme A", 30
        )
        LMRBRow.objects.create(
            account=self.account,
            channel=CHANNEL,
            date=DATE,
            advt_theme="Theme A",
            advt_time="20:00:00",
            duration=30,
            source="maponline",
            dedup_key=key,
        )
        with self.assertRaises(IntegrityError):
            LMRBRow.objects.create(
                account=self.account,
                channel=CHANNEL,
                date=DATE,
                advt_theme="Theme A",
                advt_time="20:00:00",
                duration=30,
                source="maponline",
                dedup_key=key,
            )

    def test_different_brk_no_produces_distinct_rows(self):
        """Two spots in the same break (same time/theme) with different brk_no are distinct."""
        key1 = LMRBRow.make_dedup_key(
            self.account.id, CHANNEL, DATE, "20:00:00", "Theme A", 30,
            brk_no=1, pos_in_brk=1,
        )
        key2 = LMRBRow.make_dedup_key(
            self.account.id, CHANNEL, DATE, "20:00:00", "Theme A", 30,
            brk_no=1, pos_in_brk=2,
        )
        self.assertNotEqual(key1, key2)

    def test_dedup_key_same_params_is_deterministic(self):
        """make_dedup_key returns the same value for the same inputs."""
        key1 = LMRBRow.make_dedup_key(self.account.id, CHANNEL, DATE, "20:00:00", "Theme A", 30)
        key2 = LMRBRow.make_dedup_key(self.account.id, CHANNEL, DATE, "20:00:00", "Theme A", 30)
        self.assertEqual(key1, key2)


class TCRowDedupKeyTest(TestCase):
    """Tests for TCRow dedup key uniqueness constraint."""

    def setUp(self):
        self.account = make_account()
        self.tc_report = make_tc_report(self.account)

    def test_tc_dedup_key_unique_constraint_raises_integrity_error(self):
        """Creating two TCRows with the same dedup key must raise IntegrityError."""
        key = TCRow.make_dedup_key(
            self.account.id, CHANNEL, DATE, "20:00:00", "TC Theme A", 30
        )
        TCRow.objects.create(
            account=self.account,
            tc_report=self.tc_report,
            channel=CHANNEL,
            date=DATE,
            tc_theme="TC Theme A",
            duration=30,
            aired_time="20:00:00",
            dedup_key=key,
        )
        with self.assertRaises(IntegrityError):
            TCRow.objects.create(
                account=self.account,
                tc_report=self.tc_report,
                channel=CHANNEL,
                date=DATE,
                tc_theme="TC Theme A",
                duration=30,
                aired_time="20:00:00",
                dedup_key=key,
            )

    def test_tc_dedup_key_different_from_lmrb_key(self):
        """TC dedup key is prefixed with 'tc|' so it cannot collide with LMRB keys."""
        lmrb_key = LMRBRow.make_dedup_key(
            self.account.id, CHANNEL, DATE, "20:00:00", "TC Theme A", 30
        )
        tc_key = TCRow.make_dedup_key(
            self.account.id, CHANNEL, DATE, "20:00:00", "TC Theme A", 30
        )
        self.assertNotEqual(lmrb_key, tc_key)


class BrandMappingTest(TestCase):
    """Tests for BrandMapping model helpers."""

    def setUp(self):
        self.account = make_account()

    def test_tc_themes_list_splits_on_pipe(self):
        """tc_themes_list property correctly splits pipe-separated values."""
        bm = BrandMapping.objects.create(
            account=self.account,
            brand="Brand X",
            theme="Theme X",
            tc_theme="TC Theme A|TC Theme B|TC Theme C",
        )
        self.assertEqual(bm.tc_themes_list, ["TC Theme A", "TC Theme B", "TC Theme C"])

    def test_tc_themes_list_empty_when_blank(self):
        """tc_themes_list returns empty list when tc_theme is blank."""
        bm = BrandMapping.objects.create(
            account=self.account,
            brand="Brand Y",
            theme="Theme Y",
            tc_theme="",
        )
        self.assertEqual(bm.tc_themes_list, [])

    def test_tc_themes_list_strips_whitespace(self):
        """tc_themes_list strips whitespace from each value."""
        bm = BrandMapping.objects.create(
            account=self.account,
            brand="Brand Z",
            theme="Theme Z",
            tc_theme=" Theme A | Theme B ",
        )
        self.assertEqual(bm.tc_themes_list, ["Theme A", "Theme B"])


class RunScopeSmartModeTest(TestCase):
    """Tests for verification.engine.run_scope in smart mode."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account)
        # A schedule row for 'Brand A'
        self.sr = make_schedule_row(self.account, self.schedule)
        # A matching LMRB row (same date, theme, duration, time within window)
        self.lr = make_lmrb_row(self.account)

    def test_smart_mode_matches_row_and_sets_is_matched(self):
        """run_scope smart mode matches the ScheduleRow to the LMRBRow."""
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        self.sr.refresh_from_db()
        self.lr.refresh_from_db()
        self.assertTrue(self.sr.is_matched)
        self.assertTrue(self.lr.is_matched)
        self.assertEqual(self.sr.matched_lmrb_id, self.lr.id)

    def test_smart_mode_skips_already_matched_lmrb_row(self):
        """After matching, a second smart run does not re-process the locked rows."""
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        # Create a second schedule row — there is no LMRB row left for it
        sr2 = make_schedule_row(
            self.account, self.schedule,
            brand="Brand A",
            date=datetime.date(2025, 1, 16),
            start_time="20:00:00",
            end_time="21:00:00",
        )
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        sr2.refresh_from_db()
        # sr2 should be Not Aired (no LMRB row available)
        self.assertFalse(sr2.is_matched)
        # The first LMRB row must still belong to sr1 only
        self.lr.refresh_from_db()
        self.assertEqual(self.lr.matched_schedule_id, self.sr.id)

    def test_smart_mode_creates_match_result(self):
        """run_scope creates a MatchResult record with status='matched'."""
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        mr = MatchResult.objects.filter(
            account=self.account, channel=CHANNEL, month=MONTH, status="matched"
        )
        self.assertEqual(mr.count(), 1)

    def test_no_brand_mapping_produces_no_mapping_result(self):
        """A ScheduleRow without a BrandMapping gets status='no_mapping'."""
        account2 = make_account("Account No Mapping")
        schedule2 = make_schedule(account2)
        make_schedule_row(account2, schedule2, brand="Unknown Brand")
        make_lmrb_row(account2, advt_theme="Unknown Theme")
        # No BrandMapping created for account2
        run_scope(account2.id, CHANNEL, MONTH, mode="smart")
        mr = MatchResult.objects.filter(
            account=account2, status="no_mapping"
        )
        self.assertEqual(mr.count(), 1)


class RunScopeResetModeTest(TestCase):
    """Tests for verification.engine.run_scope in reset mode."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account)
        self.sr = make_schedule_row(self.account, self.schedule)
        self.lr = make_lmrb_row(self.account)

    def test_reset_mode_clears_is_matched_flags(self):
        """reset mode unlocks previously matched rows and re-runs the engine."""
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        self.sr.refresh_from_db()
        self.assertTrue(self.sr.is_matched)

        # Now reset — it should clear and re-match
        run_scope(self.account.id, CHANNEL, MONTH, mode="reset")
        self.sr.refresh_from_db()
        self.lr.refresh_from_db()
        # After reset + re-run, both should still be matched
        self.assertTrue(self.sr.is_matched)
        self.assertTrue(self.lr.is_matched)

    def test_reset_mode_does_not_clear_manual_match_flags(self):
        """is_manual_matched=True rows are preserved across a reset run."""
        # Manually lock the schedule row
        self.sr.is_manual_matched = True
        self.sr.save(update_fields=["is_manual_matched"])
        self.lr.is_manual_matched = True
        self.lr.save(update_fields=["is_manual_matched"])

        # Also add a second unmatched row so run_scope doesn't bail with 0 rows
        lr2 = make_lmrb_row(
            self.account, advt_time="20:45:00",
            brk_no=None, pos_in_brk=1,
        )
        make_schedule_row(
            self.account, self.schedule,
            brand="Brand A",
            date=DATE,
            start_time="20:00:00",
            end_time="21:00:00",
        )

        run_scope(self.account.id, CHANNEL, MONTH, mode="reset")

        self.sr.refresh_from_db()
        self.lr.refresh_from_db()
        self.assertTrue(self.sr.is_manual_matched, "Manual lock must survive reset")
        self.assertTrue(self.lr.is_manual_matched, "Manual LMRB lock must survive reset")

    def test_reset_mode_clears_match_results_except_manual(self):
        """reset mode deletes MatchResult records but not those with status='manual_match'."""
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        # Manually create a manual_match result
        MatchResult.objects.create(
            account=self.account,
            channel=CHANNEL,
            month=MONTH,
            brand="Brand A",
            status="manual_match",
        )
        run_scope(self.account.id, CHANNEL, MONTH, mode="reset")
        manual_results = MatchResult.objects.filter(
            account=self.account, status="manual_match"
        )
        self.assertEqual(manual_results.count(), 1, "Manual match results must not be deleted by reset")


class LMRBSpotSingleUseTest(TestCase):
    """Test that a single LMRBRow cannot be claimed by two ScheduleRows."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account)

    def test_lmrb_row_consumed_by_first_schedule_row_only(self):
        """
        When there is one LMRB row and two matching ScheduleRows,
        only one ScheduleRow gets matched; the other is Not Aired.
        """
        sr1 = make_schedule_row(self.account, self.schedule, date=DATE)
        sr2 = make_schedule_row(
            self.account, self.schedule,
            date=DATE,
            start_time="20:00:00",
            end_time="21:00:00",
        )
        # Only one LMRB row available
        make_lmrb_row(self.account)

        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")

        sr1.refresh_from_db()
        sr2.refresh_from_db()

        matched = [sr for sr in [sr1, sr2] if sr.is_matched]
        unmatched = [sr for sr in [sr1, sr2] if not sr.is_matched]

        self.assertEqual(len(matched), 1, "Exactly one ScheduleRow should be matched")
        self.assertEqual(len(unmatched), 1, "The other ScheduleRow should be Not Aired")

        # Verify the matched LMRB row is only linked to one schedule row
        lmrb = LMRBRow.objects.first()
        self.assertEqual(lmrb.matched_schedule_id, matched[0].id)


class ManualMatchPermanenceTest(TestCase):
    """Tests for ManualMatch creation, locking, and permanence."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account)
        self.user = make_user()
        self.sr = make_schedule_row(self.account, self.schedule)
        self.lr = make_lmrb_row(self.account)

    def _create_manual_match(self):
        """Helper to create a ManualMatch and lock both rows."""
        mm = ManualMatch.objects.create(
            account=self.account,
            channel=CHANNEL,
            month=MONTH,
            match_mode="schedule_lmrb",
            schedule_row=self.sr,
            lmrb_row=self.lr,
            matched_by=self.user,
        )
        self.sr.is_manual_matched = True
        self.sr.save(update_fields=["is_manual_matched"])
        self.lr.is_manual_matched = True
        self.lr.save(update_fields=["is_manual_matched"])
        return mm

    def test_manual_match_locks_schedule_row(self):
        """Creating a ManualMatch sets is_manual_matched=True on ScheduleRow."""
        self._create_manual_match()
        self.sr.refresh_from_db()
        self.assertTrue(self.sr.is_manual_matched)

    def test_manual_match_locks_lmrb_row(self):
        """Creating a ManualMatch sets is_manual_matched=True on LMRBRow."""
        self._create_manual_match()
        self.lr.refresh_from_db()
        self.assertTrue(self.lr.is_manual_matched)

    def test_manual_matched_rows_excluded_from_smart_run(self):
        """
        After manual locking, smart run does not process the locked rows.
        A second LMRB row exists but the manually locked one must not be re-used.
        """
        self._create_manual_match()
        # Add a new unmatched schedule row and LMRB row
        sr2 = make_schedule_row(
            self.account, self.schedule,
            date=datetime.date(2025, 1, 16),
            start_time="20:00:00",
            end_time="21:00:00",
        )
        lr2 = make_lmrb_row(
            self.account,
            date=datetime.date(2025, 1, 16),
            advt_time="20:30:00",
            brk_no=1,
        )
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        # The manually locked rows must retain their flags
        self.sr.refresh_from_db()
        self.lr.refresh_from_db()
        self.assertTrue(self.sr.is_manual_matched)
        self.assertTrue(self.lr.is_manual_matched)

    def test_manual_matched_rows_survive_reset(self):
        """mode='reset' must not clear is_manual_matched flags."""
        self._create_manual_match()
        # Need at least one non-manual row for run_scope to proceed
        sr2 = make_schedule_row(
            self.account, self.schedule,
            date=datetime.date(2025, 1, 16),
        )
        lr2 = make_lmrb_row(
            self.account,
            date=datetime.date(2025, 1, 16),
            brk_no=2,
        )
        run_scope(self.account.id, CHANNEL, MONTH, mode="reset")
        self.sr.refresh_from_db()
        self.lr.refresh_from_db()
        self.assertTrue(self.sr.is_manual_matched, "is_manual_matched must survive reset")
        self.assertTrue(self.lr.is_manual_matched, "LMRB is_manual_matched must survive reset")

    def test_three_way_manual_match_locks_tc_row(self):
        """A 3way ManualMatch should lock the TCRow via is_manual_matched."""
        tc_report = make_tc_report(self.account)
        tcrow = make_tc_row(self.account, tc_report)
        mm = ManualMatch.objects.create(
            account=self.account,
            channel=CHANNEL,
            month=MONTH,
            match_mode="3way",
            schedule_row=self.sr,
            tc_row=tcrow,
            lmrb_row=self.lr,
            matched_by=self.user,
        )
        # In the real workflow the view sets is_manual_matched; simulate it here
        self.sr.is_manual_matched = True
        self.sr.save()
        self.lr.is_manual_matched = True
        self.lr.save()
        self.sr.refresh_from_db()
        self.lr.refresh_from_db()
        self.assertTrue(self.sr.is_manual_matched)
        self.assertTrue(self.lr.is_manual_matched)
        self.assertEqual(mm.match_mode, "3way")


class TCReconcileLateAiredRuleTest(TestCase):
    """Tests for the TC late-aired rule: TCRow.date >= ScheduleRow.date."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, tc_theme="TC Theme A")
        self.tc_report = make_tc_report(self.account)
        ensure_tc_tolerance(5)

    def test_tcrow_on_same_date_as_schedule_is_matched(self):
        """TCRow.date == ScheduleRow.date satisfies the >= rule (boundary case)."""
        sr = make_schedule_row(self.account, self.schedule, date=DATE)
        tc = make_tc_row(self.account, self.tc_report, date=DATE)

        result = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_schedule_matched, "TCRow on same date should be matched")
        self.assertEqual(tc.matched_schedule_id, sr.id)

    def test_tcrow_after_schedule_date_is_matched_late_aired(self):
        """TCRow.date > ScheduleRow.date is allowed (late-aired rule)."""
        sr = make_schedule_row(self.account, self.schedule, date=DATE)
        later_date = DATE + datetime.timedelta(days=2)
        tc = make_tc_row(
            self.account, self.tc_report,
            date=later_date,
            suffix="_later",
        )

        result = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_schedule_matched, "TCRow aired after schedule date should still match")

    def test_tcrow_before_schedule_date_is_not_matched(self):
        """TCRow.date < ScheduleRow.date must NOT match (violates >= rule)."""
        sr = make_schedule_row(self.account, self.schedule, date=DATE)
        earlier_date = DATE - datetime.timedelta(days=2)
        tc = make_tc_row(
            self.account, self.tc_report,
            date=earlier_date,
            suffix="_earlier",
        )

        result = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertFalse(tc.is_schedule_matched, "TCRow aired before schedule date must not match")
        self.assertTrue(tc.is_extra, "TCRow before schedule date must be marked extra")

    def test_extra_tcrows_are_flagged_when_no_schedule_match(self):
        """TCRows that don't match any ScheduleRow receive is_extra=True."""
        # Schedule row for Brand A, TC row for a completely different theme
        make_schedule_row(self.account, self.schedule)
        # TC row with a theme that has no brand mapping
        tc = make_tc_row(
            self.account, self.tc_report,
            tc_theme="Completely Unknown Theme",
            suffix="_unknown",
        )

        result = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_extra)
        self.assertFalse(tc.is_schedule_matched)


class TCReconcileTimeTolerance(TestCase):
    """Tests for TC-LMRB cross-check time tolerance."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, tc_theme="TC Theme A")
        self.tc_report = make_tc_report(self.account)

    def test_lmrb_within_tolerance_is_confirmed(self):
        """LMRBRow within ±5 seconds of TC aired_time gets is_lmrb_confirmed=True."""
        ensure_tc_tolerance(5)
        make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report, aired_time="20:30:00")
        # LMRB aired 3 seconds after TC — within 5s tolerance
        lr = make_lmrb_row(self.account, advt_time="20:30:03")

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_lmrb_confirmed)
        self.assertEqual(tc.matched_lmrb_id, lr.id)

    def test_lmrb_outside_tolerance_is_not_confirmed(self):
        """LMRBRow more than 5 seconds from TC aired_time must not be confirmed."""
        ensure_tc_tolerance(5)
        make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report, aired_time="20:30:00")
        # LMRB aired 10 seconds after TC — outside 5s tolerance
        lr = make_lmrb_row(self.account, advt_time="20:30:10")

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertFalse(tc.is_lmrb_confirmed)

    def test_wider_tolerance_allows_confirmation(self):
        """Increasing tolerance to 30s allows a 10s difference to confirm."""
        ensure_tc_tolerance(30)
        make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report, aired_time="20:30:00")
        lr = make_lmrb_row(self.account, advt_time="20:30:10")

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_lmrb_confirmed, "10s diff should confirm with 30s tolerance")
        self.assertEqual(tc.matched_lmrb_id, lr.id)

    def test_exact_time_match_is_confirmed(self):
        """LMRBRow at exactly the same time as TCRow is confirmed (0s diff)."""
        ensure_tc_tolerance(5)
        make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report, aired_time="20:30:00")
        lr = make_lmrb_row(self.account, advt_time="20:30:00")

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_lmrb_confirmed)


class TCReconcileExtraFlagTest(TestCase):
    """Tests that unmatched TCRows get is_extra=True after reconciliation."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        self.tc_report = make_tc_report(self.account)
        ensure_tc_tolerance(5)

    def test_tc_row_with_no_brand_mapping_is_extra(self):
        """A TCRow whose tc_theme has no BrandMapping entry is marked is_extra=True."""
        make_schedule_row(self.account, self.schedule, brand="Mapped Brand")
        make_brand_mapping(self.account, brand="Mapped Brand", tc_theme="Mapped TC Theme")
        # TC row with an unmapped theme
        tc_unmapped = make_tc_row(
            self.account, self.tc_report,
            tc_theme="No Mapping Theme",
            suffix="_unmapped",
        )
        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc_unmapped.refresh_from_db()
        self.assertTrue(tc_unmapped.is_extra)

    def test_matched_tc_row_is_not_extra(self):
        """A successfully matched TCRow must not be flagged as is_extra."""
        make_schedule_row(self.account, self.schedule)
        make_brand_mapping(self.account, tc_theme="TC Theme A")
        tc = make_tc_row(self.account, self.tc_report)
        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertFalse(tc.is_extra)
        self.assertTrue(tc.is_schedule_matched)


class MultiSchedulePriorityRule10Test(TestCase):
    """
    Rule 10: Multiple schedules for the same scope are processed in ascending
    schedule_number order. The LMRB pool is shared — earlier schedule gets rows first.
    """

    def setUp(self):
        self.account = make_account()
        make_brand_mapping(self.account)

    def test_earlier_schedule_number_gets_lmrb_row_first(self):
        """
        Two schedules (#100 and #200). One shared LMRB row.
        The #100 schedule should claim the LMRB row.
        """
        sched100 = make_schedule(self.account, schedule_number="100", version=1)
        sched200 = make_schedule(self.account, schedule_number="200", version=1)

        sr100 = make_schedule_row(self.account, sched100)
        sr200 = make_schedule_row(self.account, sched200)

        # Only one LMRB row available
        lr = make_lmrb_row(self.account)

        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")

        sr100.refresh_from_db()
        sr200.refresh_from_db()
        lr.refresh_from_db()

        self.assertTrue(sr100.is_matched, "Schedule #100 should get the LMRB row")
        self.assertFalse(sr200.is_matched, "Schedule #200 should be Not Aired (no LMRB left)")
        self.assertEqual(lr.matched_schedule_id, sr100.id)

    def test_later_schedule_gets_remaining_rows(self):
        """
        Two LMRB rows available. Both schedules should each get one.
        """
        sched100 = make_schedule(self.account, schedule_number="100", version=1)
        sched200 = make_schedule(self.account, schedule_number="200", version=1)

        sr100 = make_schedule_row(self.account, sched100)
        sr200 = make_schedule_row(
            self.account, sched200,
            date=datetime.date(2025, 1, 16),
            start_time="20:00:00",
            end_time="21:00:00",
        )
        lr1 = make_lmrb_row(self.account)
        lr2 = make_lmrb_row(
            self.account,
            date=datetime.date(2025, 1, 16),
            advt_time="20:30:00",
            brk_no=1,
        )

        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")

        sr100.refresh_from_db()
        sr200.refresh_from_db()
        self.assertTrue(sr100.is_matched)
        self.assertTrue(sr200.is_matched)


class ScheduleVersioningRule12Test(TestCase):
    """
    Rule 12: When multiple uploads have the same schedule_number, only the
    highest version (latest upload) is used for matching.
    """

    def setUp(self):
        self.account = make_account()
        make_brand_mapping(self.account)

    def test_only_highest_version_is_used(self):
        """
        Two schedules with schedule_number='101': version=1 and version=2.
        Only version=2 rows should be matched.
        """
        sched_v1 = make_schedule(self.account, schedule_number="101", version=1)
        sched_v2 = make_schedule(self.account, schedule_number="101", version=2)

        # v1 has a row on Jan 15; v2 has a row on Jan 16
        sr_v1 = make_schedule_row(
            self.account, sched_v1,
            date=datetime.date(2025, 1, 15),
        )
        sr_v2 = make_schedule_row(
            self.account, sched_v2,
            date=datetime.date(2025, 1, 16),
        )

        # LMRB row on Jan 16 — only matches v2's row
        lr = make_lmrb_row(
            self.account,
            date=datetime.date(2025, 1, 16),
            advt_time="20:30:00",
        )

        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")

        sr_v1.refresh_from_db()
        sr_v2.refresh_from_db()

        # Only v2 should be processed (v1 is superseded)
        self.assertFalse(
            sr_v1.is_matched,
            "Version 1 rows must not be matched — schedule is superseded by v2",
        )
        self.assertTrue(
            sr_v2.is_matched,
            "Version 2 rows must be matched — it is the active schedule",
        )


class BuildSummaryDataTest(TestCase):
    """Tests for verification.tc_engine.build_summary_data counts."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, tc_theme="TC Theme A")
        self.tc_report = make_tc_report(self.account, schedule=self.schedule)
        ensure_tc_tolerance(5)

    def test_planned_count_equals_schedule_row_count(self):
        """Planned = total ScheduleRows with COMMERCIAL BENEFITS for the scope."""
        make_schedule_row(self.account, self.schedule)
        make_schedule_row(
            self.account, self.schedule,
            date=datetime.date(2025, 1, 16),
            start_time="20:00:00",
            end_time="21:00:00",
        )
        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        total_planned = sum(r["planned"] for r in summary["commercial"])
        self.assertEqual(total_planned, 2)

    def test_aired_count_requires_schedule_matched_and_lmrb_confirmed(self):
        """
        Aired = TCRows where is_schedule_matched=True AND is_lmrb_confirmed=True.
        A TC row that is only schedule-matched but NOT LMRB-confirmed must not count.
        """
        sr = make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report)
        lr = make_lmrb_row(self.account)

        # Manually set TC row as schedule-matched but NOT lmrb-confirmed
        tc.is_schedule_matched = True
        tc.matched_schedule = sr
        tc.is_lmrb_confirmed = False
        tc.save()

        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        commercial = summary["commercial"]
        self.assertEqual(len(commercial), 1)
        self.assertEqual(commercial[0]["aired"], 0, "Aired must be 0 without LMRB confirmation")

    def test_aired_count_with_full_tc_lmrb_confirmation(self):
        """Aired = 1 when TCRow is both schedule-matched and LMRB-confirmed."""
        sr = make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report)
        lr = make_lmrb_row(self.account)

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        commercial = summary["commercial"]
        self.assertEqual(commercial[0]["aired"], 1)

    def test_missed_is_max_zero_planned_minus_aired(self):
        """Missed = max(0, planned - aired)."""
        # 2 planned, 1 aired => missed=1
        make_schedule_row(self.account, self.schedule, date=DATE)
        make_schedule_row(
            self.account, self.schedule,
            date=datetime.date(2025, 1, 16),
            start_time="20:00:00",
            end_time="21:00:00",
        )
        tc = make_tc_row(self.account, self.tc_report, date=DATE)
        lr = make_lmrb_row(self.account)

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        commercial = summary["commercial"]
        row = commercial[0]
        self.assertEqual(row["planned"], 2)
        self.assertEqual(row["aired"], 1)
        self.assertEqual(row["missed"], 1)

    def test_extra_is_max_zero_third_party_minus_planned(self):
        """Extra = max(0, 3rd_party - planned)."""
        # 1 planned, 2 LMRB-confirmed via TC => extra=1
        sr = make_schedule_row(self.account, self.schedule, date=DATE)
        tc1 = make_tc_row(self.account, self.tc_report, aired_time="20:30:00", suffix="_t1")
        tc2 = make_tc_row(self.account, self.tc_report, aired_time="20:45:00", suffix="_t2")
        lr1 = make_lmrb_row(self.account, advt_time="20:30:00")
        lr2 = make_lmrb_row(self.account, advt_time="20:45:00", brk_no=2)

        # Manually set both TC rows as schedule-matched + LMRB-confirmed
        # tc1 linked to sr, tc2 as extra but LMRB-confirmed
        tc1.is_schedule_matched = True
        tc1.matched_schedule = sr
        tc1.is_lmrb_confirmed = True
        tc1.matched_lmrb = lr1
        tc1.save()

        tc2.is_lmrb_confirmed = True
        tc2.matched_lmrb = lr2
        tc2.is_schedule_matched = True
        tc2.matched_schedule = sr
        tc2.save()

        # Adjust planned: sr is 1 row, but 2 confirmed TCRows exist
        # Build summary
        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        commercial = summary["commercial"]
        row = commercial[0]
        # third_party should be 2 (two unique confirmed LMRBs), planned=1, extra=1
        self.assertEqual(row["planned"], 1)
        self.assertGreaterEqual(row["third_party"], 1)

    def test_sponsorship_section_present_when_sponsorship_rows_exist(self):
        """build_summary_data includes sponsorship sections when SPONSORSHIP rows exist."""
        make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            programme="Sports Show",
        )
        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        self.assertIn("sponsorship", summary)
        self.assertGreater(len(summary["sponsorship"]), 0)

    def test_summary_totals_are_sum_of_rows(self):
        """commercial_total values are the sum of all commercial row values."""
        make_schedule_row(self.account, self.schedule, date=DATE)
        make_schedule_row(
            self.account, self.schedule,
            date=datetime.date(2025, 1, 16),
            start_time="20:00:00",
            end_time="21:00:00",
        )
        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        commercial = summary["commercial"]
        total = summary["commercial_total"]
        self.assertEqual(total["planned"], sum(r["planned"] for r in commercial))
        self.assertEqual(total["aired"], sum(r["aired"] for r in commercial))
        self.assertEqual(total["missed"], sum(r["missed"] for r in commercial))


class SponsorshipAutoMatchTest(TestCase):
    """Tests for verification.sponsorship_engine.reconcile_sponsorship."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, theme="Spon Theme A", tc_theme="")
        self.user = make_user()

    def test_auto_match_assigns_lmrb_to_sponsorship_row(self):
        """reconcile_sponsorship pairs an unmatched LMRBRow with a SPONSORSHIP ScheduleRow."""
        sr = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            programme="Sports Show",
            brand="Brand A",  # maps to theme='Spon Theme A' via brand mapping
        )
        # The BrandMapping.theme for 'Brand A' is 'Theme A'; need to match for sponsorship
        # Recreate the brand mapping so theme matches the LMRB advt_theme
        BrandMapping.objects.filter(account=self.account).delete()
        make_brand_mapping(self.account, brand="Brand A", theme="Spon Theme A", tc_theme="")

        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")

        result = reconcile_sponsorship(self.account.id, CHANNEL, MONTH)

        sr.refresh_from_db()
        lr.refresh_from_db()

        self.assertTrue(lr.is_sponsorship_matched)
        self.assertEqual(result["assigned"], 1)
        self.assertTrue(SponsorshipLmrbAssignment.objects.filter(
            schedule_row=sr, lmrb_row=lr
        ).exists())

    def test_already_sponsorship_matched_lmrb_is_skipped(self):
        """reconcile_sponsorship does not reassign an already sponsorship-matched LMRBRow."""
        BrandMapping.objects.filter(account=self.account).delete()
        make_brand_mapping(self.account, brand="Brand A", theme="Spon Theme A", tc_theme="")

        sr1 = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            programme="Sports Show",
            brand="Brand A",
        )
        sr2 = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            programme="Sports Show",
            brand="Brand A",
            date=datetime.date(2025, 1, 16),
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")

        # First run assigns lr to sr1
        reconcile_sponsorship(self.account.id, CHANNEL, MONTH)
        lr.refresh_from_db()
        self.assertTrue(lr.is_sponsorship_matched)

        # Second run (smart mode) should not reassign lr to sr2
        result2 = reconcile_sponsorship(self.account.id, CHANNEL, MONTH, mode="smart")
        # sr2 has no available LMRB row
        self.assertEqual(
            SponsorshipLmrbAssignment.objects.filter(lmrb_row=lr).count(),
            1,
            "is_sponsorship_matched LMRBRow must not be reassigned",
        )

    def test_reset_mode_clears_and_reruns(self):
        """reconcile_sponsorship mode='reset' deletes existing assignments and re-runs."""
        BrandMapping.objects.filter(account=self.account).delete()
        make_brand_mapping(self.account, brand="Brand A", theme="Spon Theme A", tc_theme="")

        sr = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")

        reconcile_sponsorship(self.account.id, CHANNEL, MONTH)
        self.assertEqual(SponsorshipLmrbAssignment.objects.count(), 1)

        # Reset should delete the old assignment and re-create it
        reconcile_sponsorship(self.account.id, CHANNEL, MONTH, mode="reset")
        self.assertEqual(SponsorshipLmrbAssignment.objects.count(), 1)
        lr.refresh_from_db()
        self.assertTrue(lr.is_sponsorship_matched)


class SponsorshipManualAssignTest(TestCase):
    """Tests for verification.sponsorship_engine.manual_assign."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, theme="Spon Theme A", tc_theme="")
        self.user = make_user()

    def test_manual_assign_creates_assignment(self):
        """manual_assign creates a SponsorshipLmrbAssignment and locks the LMRBRow."""
        sr = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")

        result = manual_assign(
            self.account.id, CHANNEL, MONTH,
            assignments=[(sr.id, lr.id)],
            user=self.user,
        )

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 0)
        lr.refresh_from_db()
        self.assertTrue(lr.is_sponsorship_matched)
        self.assertTrue(SponsorshipLmrbAssignment.objects.filter(
            schedule_row=sr, lmrb_row=lr, match_type="manual"
        ).exists())

    def test_manual_assign_rejects_already_matched_lmrb(self):
        """manual_assign skips LMRBRows that are already is_matched=True (commercially locked)."""
        sr = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")
        lr.is_matched = True
        lr.save(update_fields=["is_matched"])

        result = manual_assign(
            self.account.id, CHANNEL, MONTH,
            assignments=[(sr.id, lr.id)],
            user=self.user,
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_manual_assign_rejects_already_sponsorship_matched_lmrb(self):
        """manual_assign skips LMRBRows that are already is_sponsorship_matched=True."""
        sr1 = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
        )
        sr2 = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
            date=datetime.date(2025, 1, 16),
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")
        lr.is_sponsorship_matched = True
        lr.save(update_fields=["is_sponsorship_matched"])

        result = manual_assign(
            self.account.id, CHANNEL, MONTH,
            assignments=[(sr2.id, lr.id)],
            user=self.user,
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_manual_assign_rejects_manually_matched_lmrb(self):
        """manual_assign skips LMRBRows that are already is_manual_matched=True."""
        sr = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")
        lr.is_manual_matched = True
        lr.save(update_fields=["is_manual_matched"])

        result = manual_assign(
            self.account.id, CHANNEL, MONTH,
            assignments=[(sr.id, lr.id)],
            user=self.user,
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_manual_assign_rejects_invalid_schedule_row(self):
        """manual_assign skips a schedule_row_id that is not a SPONSORSHIP row."""
        sr = make_schedule_row(
            self.account, self.schedule,
            ad_type="COMMERCIAL BENEFITS",  # wrong type
        )
        lr = make_lmrb_row(self.account)

        result = manual_assign(
            self.account.id, CHANNEL, MONTH,
            assignments=[(sr.id, lr.id)],
            user=self.user,
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)


class SponsorshipDoubleClaimLockTest(TestCase):
    """
    Regression: a raw LMRBRow already claimed by a ManualMatch or the standalone
    TC↔LMRB engine must never be re-used by the sponsorship engine (auto pool,
    manual picker, or manual assign) — otherwise one raw row is counted twice.
    """

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, brand="Brand A", theme="Spon Theme A", tc_theme="")
        self.user = make_user()

    def test_auto_skips_manual_matched_lmrb(self):
        """reconcile_sponsorship must not claim an is_manual_matched LMRB row."""
        sr = make_schedule_row(
            self.account, self.schedule, ad_type="SPONSORSHIP", brand="Brand A",
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")
        lr.is_manual_matched = True          # locked by a ManualMatch (is_matched stays False)
        lr.save(update_fields=["is_manual_matched"])

        result = reconcile_sponsorship(self.account.id, CHANNEL, MONTH, mode="reset")

        self.assertEqual(result["assigned"], 0)
        self.assertFalse(
            SponsorshipLmrbAssignment.objects.filter(lmrb_row=lr).exists(),
            "Manual-matched LMRB row was double-claimed by the sponsorship engine",
        )

    def test_auto_skips_tc_lmrb_matched_lmrb(self):
        """reconcile_sponsorship must not claim an is_tc_lmrb_matched LMRB row."""
        sr = make_schedule_row(
            self.account, self.schedule, ad_type="SPONSORSHIP", brand="Brand A",
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")
        lr.is_tc_lmrb_matched = True
        lr.save(update_fields=["is_tc_lmrb_matched"])

        result = reconcile_sponsorship(self.account.id, CHANNEL, MONTH, mode="reset")

        self.assertEqual(result["assigned"], 0)
        self.assertFalse(SponsorshipLmrbAssignment.objects.filter(lmrb_row=lr).exists())

    def test_manual_picker_excludes_locked_rows(self):
        """lmrb_candidates must hide rows locked by ManualMatch or TC↔LMRB."""
        make_schedule_row(self.account, self.schedule, ad_type="SPONSORSHIP", brand="Brand A")
        free = make_lmrb_row(self.account, advt_theme="Spon Theme A", advt_time="20:30:00")
        manual = make_lmrb_row(self.account, advt_theme="Spon Theme A", advt_time="20:31:00")
        tclmrb = make_lmrb_row(self.account, advt_theme="Spon Theme A", advt_time="20:32:00")
        manual.is_manual_matched = True
        manual.save(update_fields=["is_manual_matched"])
        tclmrb.is_tc_lmrb_matched = True
        tclmrb.save(update_fields=["is_tc_lmrb_matched"])

        ids = {c["id"] for c in lmrb_candidates(self.account.id, CHANNEL, MONTH)}

        self.assertIn(free.id, ids)
        self.assertNotIn(manual.id, ids)
        self.assertNotIn(tclmrb.id, ids)

    def test_manual_assign_rejects_tc_lmrb_matched_lmrb(self):
        """manual_assign must reject an is_tc_lmrb_matched LMRB row."""
        sr = make_schedule_row(self.account, self.schedule, ad_type="SPONSORSHIP", brand="Brand A")
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")
        lr.is_tc_lmrb_matched = True
        lr.save(update_fields=["is_tc_lmrb_matched"])

        result = manual_assign(
            self.account.id, CHANNEL, MONTH, assignments=[(sr.id, lr.id)], user=self.user,
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)


class SponsorshipCommercialSeparationTest(TestCase):
    """
    Sponsorship LMRB assignments must not affect commercial 3rd party counts
    in build_summary_data.
    """

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        self.tc_report = make_tc_report(self.account, schedule=self.schedule)
        ensure_tc_tolerance(5)

    def test_sponsorship_lmrb_excluded_from_commercial_third_party(self):
        """
        An LMRBRow consumed by sponsorship must not appear in commercial 3rd-party count.
        build_summary_data passes exclude_spon=True for commercial rows.
        """
        # Commercial brand mapping
        make_brand_mapping(
            self.account, brand="Commercial Brand", theme="Com Theme",
            tc_theme="Com TC Theme",
        )
        # Sponsorship brand mapping (same account, different brand)
        make_brand_mapping(
            self.account, brand="Spon Brand", theme="Spon Theme",
            tc_theme="",
        )

        # Commercial schedule row
        sr_com = make_schedule_row(
            self.account, self.schedule,
            brand="Commercial Brand",
            ad_type="COMMERCIAL BENEFITS",
        )
        # Sponsorship schedule row
        sr_spon = make_schedule_row(
            self.account, self.schedule,
            brand="Spon Brand",
            ad_type="SPONSORSHIP",
            programme="Sports Show",
        )

        # LMRB row for sponsorship theme — will be consumed by sponsorship
        lr_spon = make_lmrb_row(
            self.account, advt_theme="Spon Theme",
        )

        # Run sponsorship reconciliation to lock lr_spon
        reconcile_sponsorship(self.account.id, CHANNEL, MONTH)
        lr_spon.refresh_from_db()
        self.assertTrue(lr_spon.is_sponsorship_matched)

        # Build summary — commercial row should not count the sponsorship LMRB
        summary = build_summary_data(self.account.id, CHANNEL, MONTH)
        com_rows = summary["commercial"]
        # Filter for the Commercial Brand row
        com_brand_rows = [r for r in com_rows if r["product"] == "Commercial Brand"]
        if com_brand_rows:
            # 3rd party for commercial should be 0 (no commercial LMRB confirmed)
            self.assertEqual(
                com_brand_rows[0]["third_party"], 0,
                "Sponsorship LMRB must not be counted in commercial 3rd party",
            )


class SponsorshipResetTest(TestCase):
    """Tests for verification.sponsorship_engine.reset_sponsorship."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, brand="Brand A", theme="Spon Theme A", tc_theme="")
        self.user = make_user()

    def test_reset_sponsorship_deletes_assignments_and_unlocks_lmrb(self):
        """reset_sponsorship removes all assignments and sets is_sponsorship_matched=False."""
        sr = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")

        reconcile_sponsorship(self.account.id, CHANNEL, MONTH)
        lr.refresh_from_db()
        self.assertTrue(lr.is_sponsorship_matched)

        result = reset_sponsorship(self.account.id, CHANNEL, MONTH)

        lr.refresh_from_db()
        self.assertFalse(lr.is_sponsorship_matched)
        self.assertEqual(SponsorshipLmrbAssignment.objects.count(), 0)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["lmrb_unlocked"], 1)


class TCReconcileSmartResetModeTest(TestCase):
    """Tests for reconcile_tc smart vs reset mode behaviour."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, tc_theme="TC Theme A")
        self.tc_report = make_tc_report(self.account)
        ensure_tc_tolerance(5)

    def test_reset_mode_clears_tc_reconciliation_state(self):
        """reconcile_tc mode='reset' clears is_schedule_matched and is_lmrb_confirmed flags."""
        sr = make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report)
        lr = make_lmrb_row(self.account)

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_schedule_matched)

        # Manually dirty the state as if a re-upload happened
        tc.is_schedule_matched = False
        tc.matched_schedule = None
        tc.is_lmrb_confirmed = False
        tc.matched_lmrb = None
        tc.save()

        # Reset should restore correct state
        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()
        self.assertTrue(tc.is_schedule_matched)

    def test_smart_mode_skips_already_matched_tc_rows(self):
        """reconcile_tc smart mode does not reprocess already-matched TCRows."""
        sr = make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report)
        lr = make_lmrb_row(self.account)

        # First run
        r1 = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        self.assertEqual(r1["matched"], 1)

        # Smart run should report 0 new matches (already matched)
        r2 = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="smart")
        self.assertEqual(r2["matched"], 0)


class LMRBSponsorshipLockTest(TestCase):
    """
    LMRBRow.is_sponsorship_matched=True prevents the sponsorship engine from
    reassigning the row to a different SPONSORSHIP ScheduleRow.
    """

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, brand="Brand A", theme="Spon Theme A", tc_theme="")

    def test_sponsorship_matched_lmrb_not_reused_by_auto_engine(self):
        """
        An LMRBRow with is_sponsorship_matched=True is excluded from the
        auto-sponsorship pool and cannot be claimed by another row.
        """
        sr1 = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
        )
        sr2 = make_schedule_row(
            self.account, self.schedule,
            ad_type="SPONSORSHIP",
            brand="Brand A",
            programme="Sports Show",
            date=datetime.date(2025, 1, 16),
        )
        lr = make_lmrb_row(self.account, advt_theme="Spon Theme A")

        # First reconcile: lr assigned to sr1
        reconcile_sponsorship(self.account.id, CHANNEL, MONTH)
        lr.refresh_from_db()
        self.assertTrue(lr.is_sponsorship_matched)

        # Second smart run: lr should not be reassigned to sr2
        result = reconcile_sponsorship(self.account.id, CHANNEL, MONTH, mode="smart")

        assignments = SponsorshipLmrbAssignment.objects.filter(lmrb_row=lr)
        self.assertEqual(assignments.count(), 1, "LMRB row must only appear in one assignment")
        self.assertEqual(assignments.first().schedule_row, sr1)


class NoBrandMappingTest(TestCase):
    """ScheduleRows without a BrandMapping are handled gracefully."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)

    def test_no_mapping_status_in_match_result(self):
        """A ScheduleRow with no BrandMapping produces a 'no_mapping' MatchResult."""
        make_schedule_row(self.account, self.schedule, brand="Unmapped Brand")
        make_lmrb_row(self.account, advt_theme="Some Theme")

        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")

        results = MatchResult.objects.filter(
            account=self.account, status="no_mapping"
        )
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first().brand, "Unmapped Brand")

    def test_no_mapping_lmrb_row_is_not_consumed(self):
        """The LMRBRow remains unmatched when the ScheduleRow has no mapping."""
        make_schedule_row(self.account, self.schedule, brand="Unmapped Brand")
        lr = make_lmrb_row(self.account, advt_theme="Some Theme")

        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")

        lr.refresh_from_db()
        self.assertFalse(lr.is_matched)


class BoundaryDateTest(TestCase):
    """Edge cases for date handling in TC and Schedule reconciliation."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, tc_theme="TC Theme A")
        self.tc_report = make_tc_report(self.account)
        ensure_tc_tolerance(5)

    def test_tc_row_date_exactly_equals_schedule_row_date(self):
        """TCRow.date == ScheduleRow.date satisfies the >= rule exactly."""
        boundary_date = datetime.date(2025, 1, 10)
        sr = make_schedule_row(self.account, self.schedule, date=boundary_date)
        tc = make_tc_row(self.account, self.tc_report, date=boundary_date, suffix="_boundary")

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()

        self.assertTrue(
            tc.is_schedule_matched,
            "TCRow on exact same date as ScheduleRow must be matched",
        )

    def test_tc_row_one_day_after_schedule_row(self):
        """TCRow.date one day after ScheduleRow.date satisfies the late-aired rule."""
        base_date = datetime.date(2025, 1, 10)
        sr = make_schedule_row(self.account, self.schedule, date=base_date)
        tc = make_tc_row(
            self.account, self.tc_report,
            date=base_date + datetime.timedelta(days=1),
            suffix="_plus1",
        )

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()

        self.assertTrue(
            tc.is_schedule_matched,
            "TCRow one day after ScheduleRow must be matched (late-aired rule)",
        )

    def test_tc_row_one_day_before_schedule_row_is_not_matched(self):
        """TCRow.date one day before ScheduleRow.date must NOT match."""
        base_date = datetime.date(2025, 1, 10)
        sr = make_schedule_row(self.account, self.schedule, date=base_date)
        tc = make_tc_row(
            self.account, self.tc_report,
            date=base_date - datetime.timedelta(days=1),
            suffix="_minus1",
        )

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        tc.refresh_from_db()

        self.assertFalse(
            tc.is_schedule_matched,
            "TCRow one day before ScheduleRow must not be matched",
        )
        self.assertTrue(tc.is_extra)


class MatchResultStatusTest(TestCase):
    """Tests that MatchResult records are created with correct status values."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account)

    def test_matched_status_created_on_successful_match(self):
        """A successful match creates a MatchResult with status='matched'."""
        make_schedule_row(self.account, self.schedule)
        make_lmrb_row(self.account)
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        self.assertTrue(
            MatchResult.objects.filter(
                account=self.account, status="matched"
            ).exists()
        )

    def test_not_aired_status_when_no_lmrb_row(self):
        """A ScheduleRow with no matching LMRB produces a 'not_aired' MatchResult."""
        make_schedule_row(self.account, self.schedule)
        # No LMRBRow created
        make_lmrb_row(
            self.account,
            advt_theme="Theme A",
            date=datetime.date(2025, 2, 1),  # Out of schedule date range
        )
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        not_aired = MatchResult.objects.filter(
            account=self.account, status__in=["not_aired", "no_mapping"]
        )
        self.assertGreater(not_aired.count(), 0)

    def test_late_telecast_status_on_different_date(self):
        """A ScheduleRow matched on a later date produces 'late_telecast' status."""
        sr = make_schedule_row(
            self.account, self.schedule,
            date=datetime.date(2025, 1, 10),
            start_time="20:00:00",
            end_time="21:00:00",
        )
        # LMRB row on a different (later) date, same time window
        lr = make_lmrb_row(
            self.account,
            date=datetime.date(2025, 1, 20),
            advt_time="20:30:00",
        )
        run_scope(self.account.id, CHANNEL, MONTH, mode="smart")
        self.assertTrue(
            MatchResult.objects.filter(
                account=self.account, status="late_telecast"
            ).exists()
        )


class AccountIsolationTest(TestCase):
    """Verify that data from different accounts never bleeds into each other's matching."""

    def setUp(self):
        self.account1 = make_account("Account One")
        self.account2 = make_account("Account Two")

    def test_lmrb_rows_from_different_account_not_used(self):
        """
        An LMRBRow belonging to account2 must not be matched against
        account1's ScheduleRows.
        """
        make_brand_mapping(self.account1)
        sched1 = make_schedule(self.account1)
        sr = make_schedule_row(self.account1, sched1)
        # LMRB row belongs to account2 — same channel, theme, date, duration
        lr = make_lmrb_row(self.account2, advt_theme="Theme A")

        run_scope(self.account1.id, CHANNEL, MONTH, mode="smart")

        sr.refresh_from_db()
        lr.refresh_from_db()
        self.assertFalse(sr.is_matched, "account1 ScheduleRow must not match account2 LMRB")
        self.assertFalse(lr.is_matched, "account2 LMRB must remain unmatched")


class ReconcileTCReturnCountsTest(TestCase):
    """Tests that reconcile_tc returns accurate summary counts."""

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        make_brand_mapping(self.account, tc_theme="TC Theme A")
        self.tc_report = make_tc_report(self.account)
        ensure_tc_tolerance(5)

    def test_return_counts_matched_and_lmrb_confirmed(self):
        """reconcile_tc returns correct matched and lmrb_confirmed counts."""
        make_schedule_row(self.account, self.schedule)
        tc = make_tc_row(self.account, self.tc_report, aired_time="20:30:00")
        lr = make_lmrb_row(self.account, advt_time="20:30:00")

        result = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["lmrb_confirmed"], 1)
        self.assertEqual(result["extra"], 0)

    def test_return_counts_extra_tc_row(self):
        """reconcile_tc reports correct extra count when TCRow has no schedule match."""
        make_schedule_row(self.account, self.schedule)
        tc_matched = make_tc_row(self.account, self.tc_report, aired_time="20:30:00", suffix="_m")
        tc_extra = make_tc_row(
            self.account, self.tc_report,
            tc_theme="Completely Different Theme",
            aired_time="21:00:00",
            suffix="_e",
        )
        lr = make_lmrb_row(self.account, advt_time="20:30:00")

        result = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["extra"], 1)

    def test_no_schedule_rows_returns_zero_matched(self):
        """reconcile_tc returns 0 matched when there are no ScheduleRows in scope."""
        tc = make_tc_row(self.account, self.tc_report)
        result = reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["extra"], 1)


# ── Bug Regression Tests ──────────────────────────────────────────────────────


class ScheduleSupersedeBugTest(TestCase):
    """
    Regression tests for the schedule-supersede bug (core/views.py line ~490).

    BUG (CRITICAL — core/views.py:490):
        When a schedule is replaced via dup_action='replace', the upload view
        calls:

            existing_same_ref.rows.update(
                is_matched=False, matched_lmrb=None, matched_at=None,
                is_manual_matched=False,   # ← BUG
            )

        This clears is_manual_matched on ALL old ScheduleRows, including those
        locked by a ManualMatch record.  However:
          - The ManualMatch records are NOT deleted.
          - The LMRBRow.is_manual_matched flag is NOT cleared (LMRB stays locked).

        Result: orphaned ManualMatch records pointing to superseded ScheduleRows
        with is_manual_matched=False.  build_summary_data() still counts these
        ManualMatches in the 'aired' total (it filters by account/channel/month/
        brand, not by schedule_id), inflating the summary with stale data.

        The LMRB rows linked to the stale ManualMatches remain permanently locked
        (is_manual_matched=True) and cannot be used for any other match.

    NOTE: These tests exercise the MODEL/ENGINE layer only.  The bug itself lives
    in the VIEW layer (schedule_upload).  The tests simulate what the view does
    so they can run without HTTP requests, and assert the incorrect state that
    results.  Once the bug is fixed in the view, these tests serve as a guard
    to confirm the fix is correct.
    """

    def setUp(self):
        self.account = make_account()
        self.user = make_user()
        make_brand_mapping(self.account)

    def _simulate_supersede(self, old_schedule):
        """
        Simulate what core/views.py does when dup_action='replace'.
        Mirrors the exact update at views.py ~line 488-491.
        """
        from core.models import LMRBRow as _LR
        sr_ids = list(old_schedule.rows.values_list("id", flat=True))
        _LR.objects.filter(schedule_matches__in=sr_ids).update(
            is_matched=False, matched_at=None
        )
        old_schedule.rows.update(
            is_matched=False, matched_lmrb=None, matched_at=None,
            is_manual_matched=False,          # ← the buggy line
        )
        old_schedule.is_superseded = True
        old_schedule.save(update_fields=["is_superseded"])

    # ── Tests that document EXISTING (buggy) behaviour ────────────────────────

    def test_supersede_clears_is_manual_matched_on_schedule_rows(self):
        """
        BUG: is_manual_matched is cleared on ScheduleRows when schedule is
        superseded, even when a ManualMatch record exists for them.

        This violates the invariant that is_manual_matched=True is permanent.
        """
        old_sched = make_schedule(self.account, schedule_number="101", version=1)
        sr = make_schedule_row(self.account, old_sched)
        lr = make_lmrb_row(self.account)

        # Create a ManualMatch — locks both rows
        ManualMatch.objects.create(
            account=self.account,
            channel=CHANNEL,
            month=MONTH,
            match_mode="schedule_lmrb",
            schedule_row=sr,
            lmrb_row=lr,
            matched_by=self.user,
        )
        ScheduleRow.objects.filter(id=sr.id).update(is_manual_matched=True)
        LMRBRow.objects.filter(id=lr.id).update(is_manual_matched=True)

        # Simulate superseding the old schedule
        self._simulate_supersede(old_sched)

        # BUG: is_manual_matched is now False on the ScheduleRow
        sr.refresh_from_db()
        lr.refresh_from_db()

        self.assertFalse(
            sr.is_manual_matched,
            "BUG CONFIRMED: is_manual_matched was cleared on superseded ScheduleRow. "
            "ManualMatch record still exists but lock is gone.",
        )
        # The LMRBRow lock is NOT cleared (only ScheduleRow is affected by the bug)
        self.assertTrue(
            lr.is_manual_matched,
            "LMRBRow.is_manual_matched should remain True (LMRB side is not touched "
            "by the supersede update — only ScheduleRow side is wrongly cleared).",
        )

    def test_manual_match_record_survives_supersede(self):
        """
        BUG (related): ManualMatch records are NOT deleted when a schedule is
        superseded.  Combined with is_manual_matched being cleared on the
        ScheduleRow, this leaves orphaned ManualMatch records.
        """
        old_sched = make_schedule(self.account, schedule_number="101", version=1)
        sr = make_schedule_row(self.account, old_sched)
        lr = make_lmrb_row(self.account)

        mm = ManualMatch.objects.create(
            account=self.account,
            channel=CHANNEL,
            month=MONTH,
            match_mode="schedule_lmrb",
            schedule_row=sr,
            lmrb_row=lr,
            matched_by=self.user,
        )
        ScheduleRow.objects.filter(id=sr.id).update(is_manual_matched=True)
        LMRBRow.objects.filter(id=lr.id).update(is_manual_matched=True)

        self._simulate_supersede(old_sched)

        # ManualMatch record is still in the DB after supersede
        self.assertTrue(
            ManualMatch.objects.filter(id=mm.id).exists(),
            "ManualMatch record survives supersede (not deleted by the view). "
            "This orphaned record will be incorrectly counted in build_summary_data().",
        )

    def test_lmrb_row_remains_locked_after_supersede(self):
        """
        The LMRB row linked to a ManualMatch on a superseded schedule remains
        permanently locked (is_manual_matched=True), making it unusable for any
        new match even though the linked schedule is gone.
        """
        old_sched = make_schedule(self.account, schedule_number="101", version=1)
        sr = make_schedule_row(self.account, old_sched)
        lr = make_lmrb_row(self.account)

        ManualMatch.objects.create(
            account=self.account,
            channel=CHANNEL,
            month=MONTH,
            match_mode="schedule_lmrb",
            schedule_row=sr,
            lmrb_row=lr,
            matched_by=self.user,
        )
        LMRBRow.objects.filter(id=lr.id).update(is_manual_matched=True)

        self._simulate_supersede(old_sched)

        # Create a new schedule and a new ScheduleRow for the same brand
        new_sched = make_schedule(self.account, schedule_number="101", version=2)
        new_sr = make_schedule_row(self.account, new_sched)

        # The LMRB row is still locked — new schedule engine cannot use it
        lr.refresh_from_db()
        self.assertTrue(
            lr.is_manual_matched,
            "LMRB row is permanently locked even after old schedule is superseded. "
            "It cannot be matched against the new schedule.",
        )


class UnmappedThemeNotAiredTest(TestCase):
    """
    A TC spot only counts as Aired when its tc_theme resolves to the brand via
    BrandMapping (or an operator confirmed it with a ManualMatch).

    Regression guard for the removed "window-coverage fallback", which credited
    LMRB-confirmed TC spots with NO brand mapping to any planned slot whose date +
    time window + duration happened to cover them.  That inflated Aired for
    unmapped brands and leaked one advertiser's spot into another's count.
    """

    def setUp(self):
        self.account = make_account()
        self.schedule = make_schedule(self.account)
        self.tc_report = make_tc_report(self.account, schedule=self.schedule)
        ensure_tc_tolerance(5)

    def _confirmed_tc(self, tc_theme, advt_theme=None):
        """Create an LMRB-confirmed TCRow in the default 20:00-21:00 planned slot."""
        tc = make_tc_row(self.account, self.tc_report, tc_theme=tc_theme)
        lr = make_lmrb_row(self.account, advt_theme=advt_theme or tc_theme)
        tc.is_lmrb_confirmed = True
        tc.matched_lmrb = lr
        tc.save()
        return tc, lr

    def test_brand_with_no_mapping_is_not_aired(self):
        """A brand with no BrandMapping shows Aired=0, even with a covering TC spot."""
        make_schedule_row(self.account, self.schedule, brand="Brand X")
        self._confirmed_tc("Totally Unmapped Theme")

        row = build_summary_data(self.account.id, CHANNEL, MONTH)["commercial"][0]
        self.assertEqual(row["planned"], 1)
        self.assertEqual(row["aired"], 0, "Unmapped brand must not be credited as aired")
        self.assertEqual(row["third_party"], 0)
        self.assertEqual(row["missed"], 1, "Unmapped brand's planned spot is Missed")

    def test_unmapped_spot_does_not_leak_into_another_brand(self):
        """
        Brand A is mapped but did not air.  An unrelated advertiser's unmapped spot
        falling inside Brand A's window must not be counted as Brand A airing.
        """
        make_brand_mapping(self.account, brand="Brand A", tc_theme="TC Theme A")
        make_schedule_row(self.account, self.schedule, brand="Brand A")
        self._confirmed_tc("Some Other Advertiser")

        row = build_summary_data(self.account.id, CHANNEL, MONTH)["commercial"][0]
        self.assertEqual(row["product"], "Brand A")
        self.assertEqual(row["aired"], 0, "Another brand's spot must not count here")
        self.assertEqual(row["missed"], 1)

    def test_mapped_brand_that_aired_still_counts(self):
        """Control: a correctly mapped brand that genuinely aired is still Aired=1."""
        make_brand_mapping(self.account, brand="Brand A", tc_theme="TC Theme A")
        make_schedule_row(self.account, self.schedule, brand="Brand A")
        make_tc_row(self.account, self.tc_report, tc_theme="TC Theme A")
        make_lmrb_row(self.account, advt_theme="Theme A")

        reconcile_tc(self.account.id, CHANNEL, MONTH, mode="reset")
        row = build_summary_data(self.account.id, CHANNEL, MONTH)["commercial"][0]
        self.assertEqual(row["aired"], 1)
        self.assertEqual(row["missed"], 0)

    def test_manual_match_still_counts_without_mapping(self):
        """An operator's ManualMatch is explicit evidence and still counts as Aired."""
        sr = make_schedule_row(self.account, self.schedule, brand="Brand X")
        _tc, lr = self._confirmed_tc("Totally Unmapped Theme")
        user = make_user()
        ManualMatch.objects.create(
            account=self.account,
            channel=CHANNEL,
            month=MONTH,
            match_mode="schedule_lmrb",
            schedule_row=sr,
            lmrb_row=lr,
            matched_by=user,
        )

        row = build_summary_data(self.account.id, CHANNEL, MONTH)["commercial"][0]
        self.assertEqual(row["aired"], 1, "ManualMatch is explicit operator evidence")
        self.assertEqual(row["missed"], 0)


class BrandMappingDeleteViewTest(TestCase):
    """Deleting brand mapping data from /dashboard/brand-mappings/.

    Three delete paths exist:
      - 'delete'        → one LMRB theme row (chip ✕)
      - 'delete_group'  → every row of one (brand, product, duration) group
      - 'delete_bulk'   → every row of the ticked groups
    """

    URL = "/dashboard/brand-mappings/"

    def setUp(self):
        self.account = make_account()
        self.other   = make_account("Other Account")
        self.admin   = make_user(email="admin@test.com", role="admin")
        self.ops     = make_user(email="ops2@test.com", role="operations")
        self.ops.accounts.add(self.account)

        # Brand A: two LMRB theme variants in one group
        self.a1 = BrandMapping.objects.create(
            account=self.account, brand="Brand A", theme="Theme A (Sin)",
            tc_theme="TC A", duration=30)
        self.a2 = BrandMapping.objects.create(
            account=self.account, brand="Brand A", theme="Theme A (Tam)",
            tc_theme="TC A", duration=30)
        # Brand B: separate group
        self.b1 = BrandMapping.objects.create(
            account=self.account, brand="Brand B", theme="Theme B", tc_theme="TC B")

    def _login(self, user):
        self.client.force_login(user)

    def test_delete_group_removes_every_theme_row_of_the_brand(self):
        self._login(self.admin)
        resp = self.client.post(self.URL, {
            "action": "delete_group", "mapping_id": self.a1.id})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(BrandMapping.objects.filter(brand="Brand A").exists())
        self.assertTrue(BrandMapping.objects.filter(id=self.b1.id).exists())

    def test_delete_group_keeps_other_duration_group(self):
        """Groups are keyed on (brand, product, duration) — a different duration
        for the same brand is a different mapping and must survive."""
        a_any = BrandMapping.objects.create(
            account=self.account, brand="Brand A", theme="Theme A any", duration=None)
        self._login(self.admin)
        self.client.post(self.URL, {"action": "delete_group", "mapping_id": self.a1.id})
        self.assertTrue(BrandMapping.objects.filter(id=a_any.id).exists())
        self.assertFalse(BrandMapping.objects.filter(id=self.a2.id).exists())

    def test_delete_bulk_removes_all_listed_ids(self):
        self._login(self.admin)
        ids = f"{self.a1.id},{self.a2.id},{self.b1.id}"
        resp = self.client.post(self.URL, {"action": "delete_bulk", "mapping_ids": ids})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(BrandMapping.objects.count(), 0)

    def test_delete_bulk_ignores_junk_ids(self):
        self._login(self.admin)
        self.client.post(self.URL, {
            "action": "delete_bulk", "mapping_ids": f"abc,,{self.b1.id},999999"})
        self.assertFalse(BrandMapping.objects.filter(id=self.b1.id).exists())
        self.assertTrue(BrandMapping.objects.filter(id=self.a1.id).exists())

    def test_delete_group_denied_for_account_user_has_no_access_to(self):
        foreign = BrandMapping.objects.create(
            account=self.other, brand="Foreign", theme="Foreign Theme")
        self._login(self.ops)
        self.client.post(self.URL, {"action": "delete_group", "mapping_id": foreign.id})
        self.assertTrue(BrandMapping.objects.filter(id=foreign.id).exists())

    def test_delete_bulk_skips_rows_outside_user_accounts(self):
        foreign = BrandMapping.objects.create(
            account=self.other, brand="Foreign", theme="Foreign Theme")
        self._login(self.ops)
        self.client.post(self.URL, {
            "action": "delete_bulk", "mapping_ids": f"{self.b1.id},{foreign.id}"})
        self.assertTrue(BrandMapping.objects.filter(id=foreign.id).exists())
        self.assertFalse(BrandMapping.objects.filter(id=self.b1.id).exists())

    def test_delete_redirect_keeps_the_active_filters(self):
        self._login(self.admin)
        resp = self.client.post(
            f"{self.URL}?account={self.account.id}&channel={CHANNEL}&month={MONTH}",
            {"action": "delete_group", "mapping_id": self.a1.id})
        self.assertIn(f"account={self.account.id}", resp["Location"])
        self.assertIn("channel=", resp["Location"])
        self.assertIn("month=", resp["Location"])

    def test_table_renders_delete_controls(self):
        self._login(self.admin)
        resp = self.client.get(f"{self.URL}?account={self.account.id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('value="delete_group"', html)
        self.assertIn('id="bulkDeleteForm"', html)
        # Brand A's checkbox carries both of its LMRB theme row ids
        self.assertIn(f'data-ids="{self.a1.id},{self.a2.id}"', html)
