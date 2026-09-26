"""Heartbeat rows for the scheduled jobs (Phase 2.1 item 6). Agent table only.

    beat_ok('intake_fetch', counts)       after a successful run
    beat_error('intake_fetch', exc)       after a failed run
    health(now)                           rows for the Agent Overview health card
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import AgentConfig, Heartbeat

JOBS = (('intake_fetch', 'Mail fetch'), ('intake_runner', 'Intake runner'), ('agent_nightly', 'Nightly audit + purge'))
FETCH_STALE_MINUTES = 20


def beat_ok(name: str, counts: dict | None = None) -> None:
    now = timezone.now()
    hb, _ = Heartbeat.objects.get_or_create(name=name, defaults={'last_beat': now})
    hb.last_beat = hb.last_ok_at = now
    hb.counts = counts or {}
    hb.save(update_fields=['last_beat', 'last_ok_at', 'counts'])


def beat_error(name: str, error) -> None:
    now = timezone.now()
    hb, _ = Heartbeat.objects.get_or_create(name=name, defaults={'last_beat': now})
    hb.last_beat = hb.last_error_at = now
    hb.last_error = f'{type(error).__name__}: {error}'[:2000] if isinstance(error, BaseException) else str(error)[:2000]
    hb.save(update_fields=['last_beat', 'last_error_at', 'last_error'])


def health(now=None) -> list[dict]:
    """One row per job. Fetch is 'stale' when intake_fetch_enabled is on and there has been
    no successful fetch for FETCH_STALE_MINUTES."""
    now = now or timezone.now()
    cfg = AgentConfig.objects.filter(pk=1).first()
    rows = {h.name: h for h in Heartbeat.objects.all()}
    out = []
    for name, label in JOBS:
        hb = rows.get(name)
        state, note = 'ok', ''
        if hb is None or hb.last_ok_at is None:
            state, note = 'never', 'No successful run yet'
        if hb and hb.last_error_at and (hb.last_ok_at is None or hb.last_error_at > hb.last_ok_at):
            state, note = 'error', hb.last_error
        if name == 'intake_fetch' and cfg and cfg.intake_fetch_enabled:
            if hb is None or hb.last_ok_at is None or now - hb.last_ok_at > timedelta(minutes=FETCH_STALE_MINUTES):
                state = 'stale' if state != 'error' else state
                note = note or f'No successful fetch for {FETCH_STALE_MINUTES}+ minutes while fetch is on'
        out.append({'name': name, 'label': label, 'state': state, 'note': note,
                    'last_ok_at': hb.last_ok_at if hb else None, 'last_error_at': hb.last_error_at if hb else None,
                    'counts': hb.counts if hb else {}})
    for h in rows.values():                                      # e.g. agent_cycle alerts
        if h.alert:
            out.append({'name': h.name, 'label': h.name, 'state': 'alert', 'note': h.alert_message,
                        'last_ok_at': h.last_ok_at, 'last_error_at': h.last_error_at, 'counts': h.counts})
    return out
