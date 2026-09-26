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
  FP    a finding of an evaluated code in a labelled scope that no label explains
        (in a no_issue scope every evaluated finding is an FP)
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
from agent.ledger import ACTIONABLE
from agent.models import AgentRun, ScopeState
from agent.readiness import assess

NOT_SCORED = ('no_issue', 'other')


def _match(f, lab) -> bool:
    return (f['code'] == lab.cause_code
            and (not lab.brand or f['brand'].lower() == lab.brand.lower())
            and (lab.duration is None or f['duration'] == lab.duration))


def evaluate(labels, findings_by_scope: dict) -> dict:
    """Pure. `findings_by_scope`: {(account_id, channel, month): [{'code','brand','duration'}]}."""
    codes = ({l.cause_code for l in labels} - set(NOT_SCORED)) | set(ACTIONABLE)
    per = defaultdict(lambda: {'tp': 0, 'fp': 0, 'miss': 0})
    by_scope = defaultdict(list)
    for lab in labels:
        s = lab.schedule
        by_scope[(s.account_id, s.channel, s.month)].append(lab)
    for key, labs in by_scope.items():
        found = [f for f in findings_by_scope.get(key, []) if f['code'] in codes]
        explained = set()
        for lab in labs:
            if lab.cause_code in NOT_SCORED:
                continue
            hit = [i for i, f in enumerate(found) if _match(f, lab)]
            if hit:
                per[lab.cause_code]['tp'] += 1
                explained.update(hit)
            else:
                per[lab.cause_code]['miss'] += 1
        for i, f in enumerate(found):
            if i not in explained:
                per[f['code']]['fp'] += 1
    rows, tot = [], {'tp': 0, 'fp': 0, 'miss': 0}
    for code in sorted(per):
        d = per[code]
        rows.append({'code': code, **d,
                     'precision': round(d['tp'] / (d['tp'] + d['fp']), 3) if d['tp'] + d['fp'] else None,
                     'recall': round(d['tp'] / (d['tp'] + d['miss']), 3) if d['tp'] + d['miss'] else None})
        for k in tot:
            tot[k] += d[k]
    overall = {**tot,
               'precision': round(tot['tp'] / (tot['tp'] + tot['fp']), 3) if tot['tp'] + tot['fp'] else None,
               'recall': round(tot['tp'] / (tot['tp'] + tot['miss']), 3) if tot['tp'] + tot['miss'] else None}
    return {'codes': rows, 'overall': overall, 'scopes': len(by_scope), 'labels': len(labels),
            'evaluated_codes': sorted(codes)}


def scope_findings(account_id, channel, month) -> list[dict]:
    sc = ScopeState(account_id=account_id, channel=channel, month=month)     # never saved
    with guard(read_only=True):
        r = assess(sc)
        fs = diagnose(sc, r)
        if r.reason == 'tc_not_linked':
            fs.append(Finding('TC_NOT_LINKED', 'TC not linked'))
    return [{'code': f.code, 'brand': f.brand or '', 'duration': (f.evidence or {}).get('duration')} for f in fs]


def render(res, csv_path, today) -> str:
    lines = [f'# Labelled diagnosis eval — {today}', '',
             f'Labels: `{csv_path}` · {res["labels"]} label row(s) · {res["scopes"]} scope(s). '
             'Readiness + diagnose only (no engine, no dry run), on a restored pre-fix copy.', '',
             '| Code | TP | FP | Misses | Precision | Recall |', '|---|---|---|---|---|---|']
    fmt = lambda v: '—' if v is None else f'{v:.3f}'      # noqa: E731
    for r in res['codes']:
        lines.append(f'| {r["code"]} | {r["tp"]} | {r["fp"]} | {r["miss"]} | {fmt(r["precision"])} | {fmt(r["recall"])} |')
    o = res['overall']
    lines += [f'| **Overall** | {o["tp"]} | {o["fp"]} | {o["miss"]} | {fmt(o["precision"])} | {fmt(o["recall"])} |', '',
              f'Evaluated codes: {", ".join(res["evaluated_codes"])}.', '',
              'Exit criterion 3 (recall ≥ 0.70) is read from the Overall row.', '']
    return '\n'.join(lines)


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
