"""agent_diagnose_labelled <csv>  (Phase 3 S2c). Disposable database only.

Refuses unless AGENT_DISPOSABLE_DB=1. On a restored PRE-FIX backup, for every scope in the
owner's label CSV (agent_label_scopes format) it runs readiness + diagnose only: no engine,
no dry run, reads inside a READ ONLY rolled-back guard. It compares the findings with the
labels and writes docs/agent/labelled_eval_<date>.md: per code TP / FP / misses, precision
and recall.

  evaluated codes  every code the labels use, plus the actionable codes (S4). Information-only
                   codes (MAKEUP_LINKED, SCHEDULE_NUMBER_WIDTH, SUPERSEDED_ROWS_PRESENT,
                   TIME_BELT_UNATTRIBUTED, LOCK_ORPHANED, BASELINE) are listed but not scored
                   unless a label names them.
  TP    a label whose code (and brand / duration, when given) diagnose also reports
  miss  a label diagnose does not report
  FP    a finding of an evaluated code that no label explains, in a scope whose labels are
        complete=yes or labelled no_issue (Phase 3.1 T4)
  unverified  the same, in any other labelled scope: listed in the report, left out of precision
Precision is reported twice: over complete-labelled scopes only, and overall.
BASELINE (a state) and the cycle codes V5_UNEXPLAINED / RECONCILE_PENDING are never scored.
"""
import datetime
import os
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from agent.db import guard
from agent.diagnose import Finding, diagnose
from agent.labels import parse
from agent.labels import complete_scopes
from agent.ledger import ACTIONABLE, CYCLE_CODES, STATES
from agent.models import AgentRun, ScopeState
from agent.readiness import assess

NOT_SCORED = ('no_issue', 'other')


def _match(f, lab) -> bool:
    return (f['code'] == lab.cause_code
            and (not lab.brand or f['brand'].lower() == lab.brand.lower())
            and (lab.duration is None or f['duration'] == lab.duration))


def _prf(d):
    p = round(d['tp'] / (d['tp'] + d['fp']), 3) if d['tp'] + d['fp'] else None
    r = round(d['tp'] / (d['tp'] + d['miss']), 3) if d['tp'] + d['miss'] else None
    return p, r


def evaluate(labels, findings_by_scope: dict) -> dict:
    """Pure. `findings_by_scope`: {(account_id, channel, month): [{'code','brand','duration'}]}."""
    codes = (({l.cause_code for l in labels} - set(NOT_SCORED)) | set(ACTIONABLE)) - set(CYCLE_CODES) - set(STATES)
    complete = complete_scopes(labels)
    per = defaultdict(lambda: {'tp': 0, 'fp': 0, 'miss': 0, 'unverified': 0})
    per_c = defaultdict(lambda: {'tp': 0, 'fp': 0, 'miss': 0})       # complete-labelled scopes only
    unverified = []
    by_scope = defaultdict(list)
    for lab in labels:
        s = lab.schedule
        by_scope[(s.account_id, s.channel, s.month)].append(lab)
    for key, labs in by_scope.items():
        found = [f for f in findings_by_scope.get(key, []) if f['code'] in codes]
        closed_world = key in complete or any(l.cause_code == 'no_issue' for l in labs)
        explained = set()
        for lab in labs:
            if lab.cause_code in NOT_SCORED:
                continue
            hit = [i for i, f in enumerate(found) if _match(f, lab)]
            k = 'tp' if hit else 'miss'
            per[lab.cause_code][k] += 1
            if key in complete:
                per_c[lab.cause_code][k] += 1
            explained.update(hit)
        for i, f in enumerate(found):
            if i in explained:
                continue
            if closed_world:
                per[f['code']]['fp'] += 1
                if key in complete:
                    per_c[f['code']]['fp'] += 1
            else:
                per[f['code']]['unverified'] += 1
                unverified.append({'scope': list(key), **f})
    rows, tot, tot_c = [], {'tp': 0, 'fp': 0, 'miss': 0, 'unverified': 0}, {'tp': 0, 'fp': 0, 'miss': 0}
    for code in sorted(per):
        d, dc = per[code], per_c.get(code, {'tp': 0, 'fp': 0, 'miss': 0})
        p, r = _prf(d)
        rows.append({'code': code, **d, 'precision': p, 'recall': r, 'precision_complete': _prf(dc)[0]})
        for k in tot:
            tot[k] += d[k]
        for k in tot_c:
            tot_c[k] += dc[k]
    p, r = _prf(tot)
    return {'codes': rows, 'overall': {**tot, 'precision': p, 'recall': r},
            'complete_only': {**tot_c, 'precision': _prf(tot_c)[0], 'recall': _prf(tot_c)[1],
                              'scopes': len(complete)},
            'unverified': unverified, 'scopes': len(by_scope), 'labels': len(labels),
            'evaluated_codes': sorted(codes)}


def scope_findings(account_id, channel, month) -> list[dict]:
    sc = ScopeState(account_id=account_id, channel=channel, month=month)     # never saved
    with guard(read_only=True):
        r = assess(sc)
        fs = [x for x in diagnose(sc, r) if x.code not in STATES]
        if r.reason == 'tc_not_linked':
            fs.append(Finding('TC_NOT_LINKED', 'TC not linked'))
    return [{'code': f.code, 'brand': f.brand or '', 'duration': (f.evidence or {}).get('duration')} for f in fs]


def render(res, csv_path, today) -> str:
    fmt = lambda v: '—' if v is None else f'{v:.3f}'      # noqa: E731
    lines = [f'# Labelled diagnosis eval — {today}', '',
             f'Labels: `{csv_path}` · {res["labels"]} label row(s) · {res["scopes"]} scope(s), '
             f'{res["complete_only"]["scopes"]} of them complete=yes. Readiness + diagnose only '
             '(no engine, no dry run), on a restored pre-fix copy.', '',
             'An unexplained finding is a false positive only in a complete=yes or no_issue scope; elsewhere it '
             'is **unverified** and left out of precision.', '',
             '| Code | TP | FP | Misses | Unverified | Precision | Precision (complete scopes) | Recall |',
             '|---|---|---|---|---|---|---|---|']
    for r in res['codes']:
        lines.append(f'| {r["code"]} | {r["tp"]} | {r["fp"]} | {r["miss"]} | {r["unverified"]} | '
                     f'{fmt(r["precision"])} | {fmt(r["precision_complete"])} | {fmt(r["recall"])} |')
    o, c = res['overall'], res['complete_only']
    lines += [f'| **Overall** | {o["tp"]} | {o["fp"]} | {o["miss"]} | {o["unverified"]} | {fmt(o["precision"])} | '
              f'{fmt(c["precision"])} | {fmt(o["recall"])} |', '',
              f'Complete-labelled scopes only: TP {c["tp"]}, FP {c["fp"]}, misses {c["miss"]}, '
              f'precision {fmt(c["precision"])}, recall {fmt(c["recall"])}.', '',
              f'Evaluated codes: {", ".join(res["evaluated_codes"])}.', '',
              'Exit criterion 3 (recall ≥ 0.70) is read from the Overall row.', '', '## Unverified findings', '']
    if res['unverified']:
        lines += ['| Account id | Channel | Month | Code | Brand | Duration |', '|---|---|---|---|---|---|']
        for u in res['unverified']:
            a, ch, mo = u['scope']
            lines.append(f'| {a} | {ch} | {mo} | {u["code"]} | {u["brand"]} | {u["duration"] if u["duration"] is not None else ""} |')
    else:
        lines.append('None.')
    return '\n'.join(lines) + '\n'


class Command(BaseCommand):
    help = 'Run readiness + diagnose on a disposable pre-fix copy and score it against owner labels.'

    def add_arguments(self, parser):
        parser.add_argument('csv')
        parser.add_argument('--output', help='default docs/agent/labelled_eval_<date>.md')

    def handle(self, *args, **opts):
        if os.environ.get('AGENT_DISPOSABLE_DB') != '1':
            raise CommandError('agent_diagnose_labelled needs AGENT_DISPOSABLE_DB=1 (disposable database only).')
        labels, errors = parse(opts['csv'])
        if errors:
            raise CommandError('label CSV refused:\n' + '\n'.join(errors))
        keys = sorted({(l.schedule.account_id, l.schedule.channel, l.schedule.month) for l in labels})
        found = {k: scope_findings(*k) for k in keys}
        res = evaluate(labels, found)
        today = datetime.date.today().isoformat()
        out = Path(opts.get('output') or Path(settings.BASE_DIR) / 'docs' / 'agent' / f'labelled_eval_{today}.md')
        out.write_text(render(res, opts['csv'], today), encoding='utf-8')
        AgentRun.objects.create(kind='labelled_eval', status='ok', detail={**res, 'output': str(out)})
        self.stdout.write(f'Wrote {out}: precision {res["overall"]["precision"]}, recall {res["overall"]["recall"]}')
