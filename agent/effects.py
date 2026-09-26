"""Pending effect = shadow (what an agent run would give) minus observed (what core shows now).
Pure functions; no database access."""
from __future__ import annotations

METRICS = ('planned', 'aired', 'third_party', 'extra', 'missed')


def _rows(summary: dict) -> dict:
    """{(section, programme, product, dur): {metric: value}} for commercial and sponsorship."""
    out = {}
    for r in summary.get('commercial') or []:
        out[('commercial', '', str(r.get('product', '')), r.get('dur'))] = {m: int(r.get(m) or 0) for m in METRICS}
    for r in summary.get('sponsorship') or []:
        rows = r.get('rows') if isinstance(r, dict) and 'rows' in r else [r]
        prog = str(r.get('programme', '')) if isinstance(r, dict) else ''
        for x in rows:
            out[('sponsorship', prog, str(x.get('product', '')), x.get('dur'))] = {m: int(x.get(m) or 0) for m in METRICS}
    return out


def diff_summaries(observed: dict, shadow: dict) -> dict:
    a, b = _rows(observed or {}), _rows(shadow or {})
    by_brand, max_abs = [], 0
    for key in sorted(set(a) | set(b), key=lambda k: (k[0], k[1], k[2], k[3] if k[3] is not None else -1)):
        old, new = a.get(key, dict.fromkeys(METRICS, 0)), b.get(key, dict.fromkeys(METRICS, 0))
        deltas = {m: new[m] - old[m] for m in METRICS if new[m] != old[m]}
        if deltas:
            by_brand.append({'section': key[0], 'programme': key[1], 'product': key[2], 'dur': key[3],
                             'deltas': deltas, 'observed': old, 'shadow': new})
            max_abs = max(max_abs, abs(deltas.get('aired', 0)), abs(deltas.get('missed', 0)))
    totals = {}
    for sec in ('commercial_total', 'sponsorship_total'):
        o, s = (observed or {}).get(sec) or {}, (shadow or {}).get(sec) or {}
        d = {m: int(s.get(m) or 0) - int(o.get(m) or 0) for m in METRICS if int(s.get(m) or 0) != int(o.get(m) or 0)}
        if d:
            totals[sec] = d
    return {'by_brand': by_brand, 'totals': totals, 'max_abs': max_abs}
