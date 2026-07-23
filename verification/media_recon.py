"""Media Reconciliation Report — shared context builder.

One source of truth for the report so the on-screen UI, the Excel export and the
PDF export all show the exact same numbers and layout.

Financial formulas (confirmed with the client):
    net_cost         = schedule_cost - deviated_cost
    sscl             = net_cost * sscl_pct/100
    sub_total        = net_cost + sscl
    vat              = sub_total * vat_pct/100
    total_with_taxes = sub_total + vat

Payment split (each computed from NET COST, rates editable per report):
    media_house = net_cost * media_house_pct/100
    agency      = net_cost * agency_pct/100
    client      = net_cost * client_pct/100
    cag         = net_cost * cag_pct/100
    total       = media_house + agency + client + cag
"""
from decimal import Decimal, ROUND_HALF_UP


def _d(v):
    """Coerce anything to Decimal, None/'' -> 0."""
    if v is None or v == '':
        return Decimal('0')
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal('0')


def _money(v):
    return _d(v).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _pct(base, rate):
    return _money(_d(base) * _d(rate) / Decimal('100'))


def _money_str(s):
    """Parse free-text money ('LKR 5,000,000' / '25000') -> Decimal or None."""
    if s is None:
        return None
    c = ''.join(ch for ch in str(s) if ch.isdigit() or ch in '.-')
    if c in ('', '.', '-', '-.', '.-'):
        return None
    try:
        return Decimal(c)
    except Exception:
        return None


def build_recon_context(account, channel, month, meta, summary_data, schedule=None):
    """Assemble the full Media Reconciliation Report context.

    account       : Account instance (the "Brand")
    channel/month : scope strings
    meta          : SummaryReportMeta instance or None
    summary_data  : output of verification.tc_engine.build_summary_data()
    schedule      : the Schedule record for this scope (for Schedule No / Medium)
    """
    client = getattr(account, 'client', None)
    dev_notes = (meta.deviation_notes if meta and isinstance(meta.deviation_notes, dict) else {}) or {}
    spot_labels = (meta.spot_labels if meta and isinstance(meta.spot_labels, dict) else {}) or {}

    def _default_label(brand, dur):
        if not brand:
            return ''
        return f'{brand} {dur}s' if dur else str(brand)

    def _label(key, brand, dur):
        custom = spot_labels.get(key) if isinstance(spot_labels, dict) else ''
        return custom if (custom and str(custom).strip()) else _default_label(brand, dur)

    # ── Spot table (No. of Spots) + deviation rows ──
    # Commercial rows first, then the sponsorship products so their names also
    # appear in the "Spot/VA Duration" column.  Every spot carries a stable
    # `key` and a user-editable `label`.
    spots, deviations = [], []
    tot_planned = tot_aired = tot_third = tot_dev = tot_notaired = 0
    for row in (summary_data.get('commercial') or []):
        brand = row.get('product', '')
        dur   = row.get('dur', 0)
        planned = row.get('planned', 0) or 0
        aired   = row.get('aired', 0) or 0
        third   = row.get('third_party', 0) or 0
        extra   = row.get('extra', 0) or 0
        missed  = row.get('missed', 0) or 0
        key = f'{brand}|{dur}'
        spots.append({'brand': brand, 'dur': dur, 'planned': planned,
                      'aired': aired, 'third_party': third,
                      'key': key, 'label': _label(key, brand, dur),
                      'is_sponsorship': False})
        tot_planned += planned
        tot_aired   += aired
        tot_third   += third
        if extra or missed:
            note = dev_notes.get(key, {}) if isinstance(dev_notes, dict) else {}
            deviations.append({
                'brand': brand, 'dur': dur,
                'deviated': extra, 'not_aired': missed,
                'reason': (note.get('reason', '') if isinstance(note, dict) else ''),
                'dev_value': (note.get('dev_value', '') if isinstance(note, dict) else ''),
                'key': key,
            })
            tot_dev      += extra
            tot_notaired += missed

    # Sponsorship products (grouped rows) — names shown in the same column.
    for section in (summary_data.get('sponsorship') or []):
        for row in (section.get('rows') or []):
            product = row.get('product', '')
            dur     = row.get('dur', 0)
            planned = row.get('planned', 0) or 0
            aired   = row.get('aired', 0) or 0
            third   = row.get('third_party', 0) or 0
            key = f'spon|{product}|{dur}'
            spots.append({'brand': product, 'dur': dur, 'planned': planned,
                          'aired': aired, 'third_party': third,
                          'key': key, 'label': _label(key, product, dur),
                          'is_sponsorship': True})
            tot_planned += planned
            tot_aired   += aired
            tot_third   += third

    # ── Financials ──
    # Schedule Cost = numeric value of Schedule Value (fallback: stored schedule_cost)
    sv = _money_str(getattr(meta, 'schedule_value', '') if meta else '')
    if sv is not None:
        sc = _money(sv)
    elif meta and meta.schedule_cost is not None:
        sc = _money(meta.schedule_cost)
    else:
        sc = Decimal('0')
    # Deviated Cost = sum of the per-row Deviated Values (fallback: stored deviated_cost)
    dc_sum, has_dc = Decimal('0'), False
    for dv in deviations:
        pv = _money_str(dv.get('dev_value'))
        if pv is not None:
            dc_sum += pv
            has_dc = True
    if has_dc:
        dc = _money(dc_sum)
    elif meta and meta.deviated_cost is not None:
        dc = _money(meta.deviated_cost)
    else:
        dc = Decimal('0')
    sscl_pct = _d(meta.sscl_pct) if meta else Decimal('2.5')
    vat_pct  = _d(meta.vat_pct) if meta else Decimal('18')
    mh_pct   = _d(meta.media_house_pct) if meta else Decimal('85')
    ag_pct   = _d(meta.agency_pct) if meta else Decimal('10.75')
    cl_pct   = _d(meta.client_pct) if meta else Decimal('4.25')
    cag_pct  = _d(meta.cag_pct) if meta else Decimal('0')

    net       = _money(sc - dc)
    sscl      = _pct(net, sscl_pct)
    sub_total = _money(net + sscl)
    vat       = _pct(sub_total, vat_pct)
    total_tax = _money(sub_total + vat)

    mh  = _pct(net, mh_pct)
    ag  = _pct(net, ag_pct)
    cl  = _pct(net, cl_pct)
    cag = _pct(net, cag_pct)

    fin = {
        'schedule_cost': sc, 'deviated_cost': dc, 'net': net,
        'sscl_pct': sscl_pct, 'sscl': sscl, 'sub_total': sub_total,
        'vat_pct': vat_pct, 'vat': vat, 'total_with_taxes': total_tax,
        'media_house_pct': mh_pct, 'media_house': mh,
        'agency_pct': ag_pct, 'agency': ag,
        'client_pct': cl_pct, 'client': cl,
        'cag_pct': cag_pct, 'cag': cag,
        'split_total': _money(mh + ag + cl + cag),
    }

    logo = getattr(client, 'logo', None) if client else None
    date_recon = ''
    if meta and meta.date_of_reconciliation:
        date_recon = meta.date_of_reconciliation.strftime('%d %b %Y')

    return {
        'title': 'Media Reconciliation Report',
        'client': client.name if client else '',
        'brand': account.name,
        'campaign': (meta.campaign if meta else ''),
        'media': (meta.media if meta else ''),
        'medium': (meta.medium if meta else '') or (getattr(schedule, 'media_type', '') or ''),
        'channel_publication': channel,
        'specification': (meta.specification if meta else ''),
        'schedule_value': (meta.schedule_value if meta else ''),
        'schedule_no': (schedule.schedule_number if schedule else ''),
        'client_po_no': (meta.po_no if meta else ''),
        'supplier_reference': (meta.supplier_reference if meta else ''),
        'date_recon': date_recon,
        'scheduling_month': month,
        'logo': logo,
        'spots': spots,
        'spots_total': {'planned': tot_planned, 'aired': tot_aired, 'third_party': tot_third},
        'deviations': deviations,
        'dev_total': {'deviated': tot_dev, 'not_aired': tot_notaired},
        'notes': (meta.notes if meta else ''),
        'fin': fin,
        'prepared_by': (meta.prepared_by if meta else ''),
        'checked_by': (meta.checked_by if meta else ''),
        'authorised_by': (meta.authorised_by if meta else ''),
    }
