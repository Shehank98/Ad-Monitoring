"""
Competitor Analysis module — completely independent from the core reconciliation system.
Handles: upload, detect, insights dashboard, deep competitor profiling, data management.
"""
import hashlib
import json
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import (
    Avg, Count, F, Max, Min, Q, Sum,
)
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from core.models import Account
from .models import CompetitorAd, CompetitorUploadBatch

_ALLOWED_ROLES = ['super_admin', 'admin', 'team_head', 'planner']

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_admin(user):
    return user.role in ('super_admin', 'admin')


def _account_qs(user):
    if _is_admin(user):
        return Account.objects.all().order_by('name')
    return user.accounts.all().order_by('name')


def _safe_str(v, default=''):
    if v is None:
        return default
    s = str(v).strip()
    return '' if s.lower() in ('nan', 'none', 'nat') else s


def _safe_int(v):
    try:
        f = float(v)
        return int(f) if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _safe_decimal(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _safe_date(dd, mn, yr):
    try:
        return date_cls(int(yr), int(mn), int(dd))
    except (TypeError, ValueError):
        return None


def _dedup_key(account_id, advertiser, product, advt_theme, channel, program, advt_time, d, duration):
    raw = f'{account_id}|{advertiser}|{product}|{advt_theme}|{channel}|{program}|{advt_time}|{d}|{duration}'
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _time_band(time_str):
    """Return 'morning', 'daytime', 'primetime', or 'latenight'."""
    try:
        h = int(str(time_str).split(':')[0])
        if 6 <= h < 12:
            return 'morning'
        if 12 <= h < 19:
            return 'daytime'
        if 19 <= h <= 23:
            return 'primetime'
        return 'latenight'
    except (TypeError, ValueError, IndexError):
        return 'unknown'


def _is_prime(time_str):
    return _time_band(time_str) == 'primetime'


def _find_col(df, *names):
    """Case-insensitive column finder."""
    lower_map = {c.lower().strip(): c for c in df.columns}
    for n in names:
        actual = lower_map.get(n.lower().strip())
        if actual is not None:
            return actual
    return None


def _to_json(obj):
    """Serialise obj to JSON-safe string (handles Decimal, date)."""
    def _default(o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, date_cls):
            return str(o)
        raise TypeError(repr(o))
    return json.dumps(obj, default=_default)


def _parse_competitor_df(df, account_id):
    """Parse a DataFrame into a list of CompetitorAd field dicts.
    Returns (rows, duplicated_keys_set_from_db)."""
    rows = []
    seen_keys = set()

    for _, r in df.iterrows():
        dd = _safe_int(r.get('Dd') or r.get('dd') or r.get('DD'))
        mn = _safe_int(r.get('Mn') or r.get('mn') or r.get('MN') or r.get('Month'))
        yr = _safe_int(r.get('Yr') or r.get('yr') or r.get('YR') or r.get('Year'))
        d  = _safe_date(dd, mn, yr)
        if d is None:
            continue

        advertiser = _safe_str(r.get('Advertiser') or r.get('advertiser', ''))
        product    = _safe_str(r.get('Product') or r.get('product', ''))
        theme      = _safe_str(r.get('Advt_Theme') or r.get('Theme') or r.get('advt_theme', ''))
        channel    = _safe_str(r.get('Channel') or r.get('channel', ''))
        program    = _safe_str(r.get('Program') or r.get('Programme') or r.get('program', ''))
        advt_time  = _safe_str(r.get('Advt_time') or r.get('Advt_Time') or r.get('advt_time', ''))
        duration   = _safe_int(r.get('Dur') or r.get('Duration') or r.get('dur'))

        key = _dedup_key(account_id, advertiser, product, theme, channel, program, advt_time, d, duration)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        rows.append({
            'product_group':    _safe_str(r.get('Product_Group') or r.get('ProductGroup', '')),
            'advertiser':       advertiser,
            'product':          product,
            'advt_theme':       theme,
            'channel':          channel,
            'program':          program,
            'date':             d,
            'prog_time':        _safe_str(r.get('Prog_time') or r.get('prog_time', '')),
            'advt_time':        advt_time,
            'ad_position':      _safe_int(r.get('AdPos') or r.get('ad_pos')),
            'total_ads':        _safe_int(r.get('TotAds') or r.get('total_ads')),
            'break_no':         _safe_int(r.get('BrkNo') or r.get('brk_no')),
            'position_in_break':_safe_int(r.get('PosinBrk') or r.get('pos_in_brk')),
            'ads_in_break':     _safe_int(r.get('AdsinBrk') or r.get('ads_in_brk')),
            'language':         _safe_str(r.get('Lng') or r.get('Language') or r.get('lng', '')),
            'duration':         duration,
            'cost':             _safe_decimal(r.get('Cost') or r.get('cost')),
            'dedup_key':        key,
        })
    return rows


# ── Main analysis view ────────────────────────────────────────────────────────

@login_required
@role_required(_ALLOWED_ROLES)
def competitor_analysis(request):
    user       = request.user
    account_qs = _account_qs(user)
    tab        = request.GET.get('tab', 'upload')
    account_id = request.GET.get('account_id', '')

    selected_account = None
    if account_id:
        try:
            selected_account = account_qs.get(id=int(account_id))
        except (Account.DoesNotExist, ValueError):
            account_id = ''

    # Always load upload batches for the sidebar / management tab
    batches = []
    total_rows = locked_count = 0
    all_advertisers = all_channels = all_programmes = all_products = all_durations = []
    _db_error = None

    if selected_account:
        try:
            batches = list(
                CompetitorUploadBatch.objects
                .filter(account=selected_account)
                .select_related('uploaded_by')
                .order_by('-uploaded_at')
            )
            total_rows   = sum(b.row_count for b in batches)
            locked_count = sum(1 for b in batches if b.is_locked)

            # Filter option lists (for dashboard dropdowns)
            base_qs = CompetitorAd.objects.filter(account=selected_account)
            all_advertisers = sorted({v for v in base_qs.values_list('advertiser', flat=True).distinct() if v})
            all_channels    = sorted({v for v in base_qs.values_list('channel',    flat=True).distinct() if v})
            all_programmes  = sorted({v for v in base_qs.values_list('program',    flat=True).distinct() if v})
            all_products    = sorted({v for v in base_qs.values_list('product',    flat=True).distinct() if v})
            all_durations   = sorted({d for d in base_qs.values_list('duration',   flat=True).distinct() if d})
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception('competitor_analysis DB error')
            _db_error = str(e)

    _TAB_DEFS = [
        ('upload',     'Data Upload',            'fa-cloud-arrow-up'),
        ('dashboard',  'Insights Dashboard',     'fa-chart-pie'),
        ('deep',       'Deep Competitor Analysis','fa-user-magnifying-glass'),
        ('management', 'Data Management',        'fa-database'),
    ]

    ctx = {
        'tab':              tab,
        'tab_defs':         _TAB_DEFS,
        'account_qs':       account_qs,
        'selected_account': selected_account,
        'account_id':       str(account_id),
        'batches':          batches,
        'all_advertisers':  all_advertisers,
        'all_channels':     all_channels,
        'all_programmes':   all_programmes,
        'all_products':     all_products,
        'all_durations':    all_durations,
        'total_rows':       total_rows,
        'locked_count':     locked_count,
        'db_error':         _db_error,
    }

    if tab == 'dashboard' and selected_account and not _db_error:
        try:
            ctx.update(_build_dashboard_data(request, selected_account))
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception('_build_dashboard_data error')
            ctx['db_error'] = str(e)

    if tab == 'deep' and selected_account and not _db_error:
        try:
            ctx.update(_build_deep_data(request, selected_account, all_advertisers))
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception('_build_deep_data error')
            ctx['db_error'] = str(e)

    if tab == 'management' and selected_account and not _db_error:
        try:
            ad_qs = CompetitorAd.objects.filter(account=selected_account).order_by('-date', 'advertiser')
            ctx['total_ad_count'] = ad_qs.count()
            ctx['recent_ads'] = list(ad_qs.select_related('upload_batch')[:200])
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception('management tab DB error')
            ctx['db_error'] = str(e)

    return render(request, 'competitor/analysis.html', ctx)


# ── Upload endpoints ──────────────────────────────────────────────────────────

@login_required
@role_required(_ALLOWED_ROLES)
@require_POST
def competitor_detect(request):
    """AJAX: scan uploaded file, return date range, row count, duplicate count."""
    excel_file = request.FILES.get('file')
    account_id = request.POST.get('account_id', '')

    if not excel_file:
        return JsonResponse({'ok': False, 'error': 'No file provided.'})
    if not account_id:
        return JsonResponse({'ok': False, 'error': 'Select an account first.'})

    try:
        df = pd.read_excel(excel_file)
        df.columns = df.columns.str.strip()

        # Build dates
        df['_date'] = df.apply(lambda r: _safe_date(
            _safe_int(r.get('Dd') or r.get('dd')),
            _safe_int(r.get('Mn') or r.get('mn')),
            _safe_int(r.get('Yr') or r.get('yr')),
        ), axis=1)

        valid_df = df.dropna(subset=['_date'])
        row_count = len(valid_df)
        min_date  = str(valid_df['_date'].min()) if row_count else ''
        max_date  = str(valid_df['_date'].max()) if row_count else ''

        # Duplicate detection
        parsed = _parse_competitor_df(df, int(account_id))
        keys   = [r['dedup_key'] for r in parsed]
        dup_count = CompetitorAd.objects.filter(
            account_id=int(account_id), dedup_key__in=keys
        ).count()

        # Get advertiser list preview
        adv_col = _find_col(df, 'Advertiser', 'advertiser')
        advertisers = sorted(set(
            str(v).strip() for v in (df[adv_col].dropna() if adv_col else [])
            if str(v).strip() and str(v).strip().lower() != 'nan'
        ))

        return JsonResponse({
            'ok':           True,
            'row_count':    row_count,
            'min_date':     min_date,
            'max_date':     max_date,
            'dup_count':    dup_count,
            'advertisers':  advertisers[:20],
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
@role_required(_ALLOWED_ROLES)
@require_POST
def competitor_upload(request):
    """Process a competitor Excel upload."""
    account_id     = request.POST.get('account_id', '')
    skip_dupes     = request.POST.get('skip_dupes', 'true') == 'true'

    if not account_id:
        messages.error(request, 'No account selected.')
        return redirect(_comp_url(account_id, 'upload'))

    try:
        account = _account_qs(request.user).get(id=int(account_id))
    except (Account.DoesNotExist, ValueError):
        messages.error(request, 'Invalid account.')
        return redirect('/dashboard/competitor/')

    excel_file = request.FILES.get('file')
    if not excel_file:
        messages.error(request, 'No file uploaded.')
        return redirect(_comp_url(account_id, 'upload'))

    try:
        df = pd.read_excel(excel_file)
        df.columns = df.columns.str.strip()
    except Exception as e:
        messages.error(request, f'Cannot read file: {e}')
        return redirect(_comp_url(account_id, 'upload'))

    try:
        rows = _parse_competitor_df(df, account.id)
    except Exception as e:
        messages.error(request, f'Error parsing file: {e}')
        return redirect(_comp_url(account_id, 'upload'))

    if not rows:
        messages.error(request, 'No valid rows found. Check that the file has Dd, Mn, Yr date columns.')
        return redirect(_comp_url(account_id, 'upload'))

    # Date range
    dates = [r['date'] for r in rows if r['date']]
    start_date = min(dates) if dates else None
    end_date   = max(dates) if dates else None

    try:
        # Duplicate detection
        existing_keys = set(
            CompetitorAd.objects.filter(
                account=account,
                dedup_key__in=[r['dedup_key'] for r in rows],
            ).values_list('dedup_key', flat=True)
        )

        if skip_dupes:
            rows = [r for r in rows if r['dedup_key'] not in existing_keys]

        # Create batch header
        batch = CompetitorUploadBatch.objects.create(
            account           = account,
            original_filename = excel_file.name[:500],
            start_date        = start_date,
            end_date          = end_date,
            row_count         = len(rows),
            uploaded_by       = request.user,
        )

        # Bulk insert rows
        ad_objs = [
            CompetitorAd(account=account, upload_batch=batch, **r)
            for r in rows
        ]
        CompetitorAd.objects.bulk_create(ad_objs, batch_size=500)

    except Exception as e:
        import logging
        logging.getLogger(__name__).exception('competitor_upload DB error')
        messages.error(request, f'Database error during upload: {e}')
        return redirect(_comp_url(account_id, 'upload'))

    skipped = len(existing_keys) if skip_dupes else 0
    messages.success(
        request,
        f'Uploaded {len(rows):,} competitor ads for {account} '
        f'({start_date} – {end_date})'
        + (f' · {skipped} duplicates skipped.' if skipped else '.')
    )
    return redirect(_comp_url(account_id, 'upload'))


def _comp_url(account_id, tab):
    return f'/dashboard/competitor/?account_id={account_id}&tab={tab}'


# ── Dashboard data builder ────────────────────────────────────────────────────

def _build_dashboard_data(request, account):
    date_from    = request.GET.get('date_from') or None
    date_to      = request.GET.get('date_to') or None
    sel_adv      = request.GET.getlist('advertiser')
    sel_ch       = request.GET.getlist('channel')
    sel_prod     = request.GET.getlist('product')
    sel_prog     = request.GET.getlist('programme')
    sel_dur      = request.GET.getlist('duration')
    sel_timeband = request.GET.get('timeband', '')

    qs = CompetitorAd.objects.filter(account=account)

    # Default: all available data if no date filter
    if not date_from and not date_to:
        agg = qs.aggregate(mn=Min('date'), mx=Max('date'))
        date_from = str(agg['mn']) if agg['mn'] else None
        date_to   = str(agg['mx']) if agg['mx'] else None

    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if sel_adv:
        qs = qs.filter(advertiser__in=sel_adv)
    if sel_ch:
        qs = qs.filter(channel__in=sel_ch)
    if sel_prod:
        qs = qs.filter(product__in=sel_prod)
    if sel_prog:
        qs = qs.filter(program__in=sel_prog)
    if sel_dur:
        qs = qs.filter(duration__in=[int(d) for d in sel_dur if str(d).isdigit()])

    total_count = qs.count()
    if total_count == 0:
        return {
            'date_from': date_from, 'date_to': date_to,
            'sel_adv': sel_adv, 'sel_ch': sel_ch,
            'kpis': None, 'no_data': True,
            'spend_comparison_json': '[]', 'spend_trend_json': '[]',
            'channel_strategy_json': '[]', 'duration_dist_json': '[]',
            'break_pos_json': '{}', 'time_band_json': '{}',
            'freq_data_json': '[]', 'cost_eff_json': '[]',
            'prog_dom_json': '[]', 'programme_heatmap': [],
            'top_adv_list': [], 'insights': [],
            'lang_data_json': '[]', 'top5_adv': [],
        }

    # ── KPIs ─────────────────────────────────────────────────────────────────
    agg = qs.aggregate(total_spend=Sum('cost'), total_ads=Count('id'), avg_dur=Avg('duration'))
    total_spend   = float(agg['total_spend'] or 0)
    total_ads     = agg['total_ads'] or 0
    avg_duration  = round(float(agg['avg_dur'] or 0), 1)

    top_adv_kpi = qs.values('advertiser').annotate(s=Sum('cost')).order_by('-s').first()
    top_ch_kpi  = qs.values('channel').annotate(c=Count('id')).order_by('-c').first()
    top_prog_kpi = (qs.exclude(program='').values('program')
                     .annotate(c=Count('id')).order_by('-c').first())

    prime_count = sum(1 for t in qs.values_list('advt_time', flat=True) if _is_prime(t))
    prime_pct   = round(prime_count / total_ads * 100) if total_ads else 0

    kpis = {
        'total_spend':    total_spend,
        'total_ads':      total_ads,
        'avg_duration':   avg_duration,
        'top_advertiser': top_adv_kpi['advertiser'] if top_adv_kpi else '—',
        'top_channel':    top_ch_kpi['channel'] if top_ch_kpi else '—',
        'top_programme':  top_prog_kpi['program'] if top_prog_kpi else '—',
        'prime_pct':      prime_pct,
        'market_leader':  top_adv_kpi['advertiser'] if top_adv_kpi else '—',
        'market_leader_spend': float(top_adv_kpi['s'] or 0) if top_adv_kpi else 0,
    }

    # ── Chart 1: Spend Comparison ─────────────────────────────────────────────
    spend_comparison = list(
        qs.values('advertiser')
          .annotate(spend=Sum('cost'), count=Count('id'))
          .order_by('-spend')[:15]
    )

    # ── Chart 2: Spend Trend (top 5 advertisers, daily) ──────────────────────
    top5_adv = [r['advertiser'] for r in spend_comparison[:5]]
    spend_trend = list(
        qs.filter(advertiser__in=top5_adv)
          .values('date', 'advertiser')
          .annotate(spend=Sum('cost'))
          .order_by('date')
    )

    # ── Chart 3: Channel Strategy (stacked) ──────────────────────────────────
    channel_strategy = list(
        qs.values('channel', 'advertiser')
          .annotate(spend=Sum('cost'), count=Count('id'))
          .order_by('channel', '-spend')
    )

    # ── Chart 4: Programme Heatmap (table, top 10 programmes × top 8 adv) ───
    top_programmes_raw = list(
        qs.exclude(program='')
          .values('program')
          .annotate(c=Count('id'))
          .order_by('-c')[:10]
    )
    top_adv_list = [r['advertiser'] for r in spend_comparison[:8]]
    programme_heatmap = []
    for p in top_programmes_raw:
        prog_name = p['program']
        row = {'program': prog_name, 'advertisers': {}}
        for adv in top_adv_list:
            cnt = qs.filter(program=prog_name, advertiser=adv).count()
            row['advertisers'][adv] = cnt
        programme_heatmap.append(row)

    # ── Chart 5: Duration Distribution ───────────────────────────────────────
    dur_dist = list(
        qs.values('duration')
          .annotate(count=Count('id'), spend=Sum('cost'))
          .order_by('duration')
    )

    # ── Chart 6: Break Position ───────────────────────────────────────────────
    break_pos = {'First': 0, 'Middle': 0, 'Last': 0}
    for row in qs.values('ad_position', 'total_ads'):
        pos = row['ad_position']
        tot = row['total_ads']
        if pos is None:
            continue
        if pos == 1:
            break_pos['First'] += 1
        elif tot and pos == tot:
            break_pos['Last'] += 1
        else:
            break_pos['Middle'] += 1

    # ── Chart 7: Time Band Strategy ───────────────────────────────────────────
    time_band_raw = {}
    for row in qs.filter(advertiser__in=top5_adv).values('advertiser', 'advt_time'):
        adv  = row['advertiser']
        band = _time_band(row['advt_time'])
        time_band_raw.setdefault(adv, {})
        time_band_raw[adv][band] = time_band_raw[adv].get(band, 0) + 1

    # ── Chart 8: Ad Frequency per day ────────────────────────────────────────
    freq_data = list(
        qs.filter(advertiser__in=top5_adv)
          .values('date', 'advertiser')
          .annotate(count=Count('id'))
          .order_by('date')
    )

    # ── Chart 9: Cost Efficiency Scatter ──────────────────────────────────────
    cost_eff = list(
        qs.values('advertiser')
          .annotate(
              avg_dur=Avg('duration'),
              avg_cost=Avg('cost'),
              total_spend=Sum('cost'),
              total_ads=Count('id'),
          )
          .order_by('-total_spend')[:12]
    )

    # ── Chart 10: Programme Dominance ─────────────────────────────────────────
    prog_dom = []
    for p in top_programmes_raw[:8]:
        prog_name   = p['program']
        total_in_prog = qs.filter(program=prog_name).count()
        if not total_in_prog:
            continue
        for adv in top_adv_list[:5]:
            cnt = qs.filter(program=prog_name, advertiser=adv).count()
            if cnt:
                prog_dom.append({
                    'program': prog_name, 'advertiser': adv,
                    'share': round(cnt / total_in_prog * 100, 1),
                    'count': cnt,
                })

    # ── Language strategy ─────────────────────────────────────────────────────
    lang_data = list(
        qs.exclude(language='')
          .values('language', 'advertiser')
          .annotate(count=Count('id'))
          .order_by('-count')
    )

    # ── Insights ──────────────────────────────────────────────────────────────
    insights = _compute_insights(account, qs, top5_adv)

    return {
        'date_from':              date_from,
        'date_to':                date_to,
        'sel_adv':                sel_adv,
        'sel_ch':                 sel_ch,
        'sel_prod':               sel_prod,
        'sel_prog':               sel_prog,
        'sel_dur':                sel_dur,
        'sel_timeband':           sel_timeband,
        'kpis':                   kpis,
        'no_data':                False,
        'spend_comparison_json':  _to_json(spend_comparison),
        'spend_trend_json':       _to_json(spend_trend),
        'channel_strategy_json':  _to_json(channel_strategy),
        'duration_dist_json':     _to_json(dur_dist),
        'break_pos_json':         _to_json(break_pos),
        'time_band_json':         _to_json(time_band_raw),
        'freq_data_json':         _to_json(freq_data),
        'cost_eff_json':          _to_json(cost_eff),
        'prog_dom_json':          _to_json(prog_dom),
        'programme_heatmap':      programme_heatmap,
        'top_adv_list':           top_adv_list,
        'top5_adv':               top5_adv,
        'top5_adv_json':          _to_json(top5_adv),
        'insights':               insights,
        'lang_data_json':         _to_json(lang_data),
    }


def _compute_insights(account, filtered_qs, top5_adv):
    """Generate advanced derived intelligence insights."""
    insights = []

    latest_batch = (CompetitorUploadBatch.objects
                    .filter(account=account)
                    .order_by('-uploaded_at').first())
    if not latest_batch:
        return insights

    all_ads = CompetitorAd.objects.filter(account=account)

    def _new_items(field):
        latest_vals = set(
            CompetitorAd.objects.filter(account=account, upload_batch=latest_batch)
            .values_list(field, flat=True).distinct()
        ) - {''}
        prev_vals = set(
            all_ads.exclude(upload_batch=latest_batch)
            .values_list(field, flat=True).distinct()
        ) - {''}
        return latest_vals - prev_vals

    # New product launches
    new_products = _new_items('product')
    if new_products:
        top = sorted(new_products)
        insights.append({
            'type':   'new_product',
            'icon':   'fa-box-open',
            'color':  'emerald',
            'title':  f'New Product Launch Detected ({len(new_products)})',
            'detail': ', '.join(top[:4]) + (f' +{len(top)-4} more' if len(top) > 4 else ''),
        })

    # New campaign creatives
    new_themes = _new_items('advt_theme')
    if new_themes:
        top = sorted(new_themes)
        insights.append({
            'type':   'new_campaign',
            'icon':   'fa-flag',
            'color':  'blue',
            'title':  f'New Campaign Creative{"s" if len(new_themes) > 1 else ""} ({len(new_themes)})',
            'detail': ', '.join(top[:3]) + (f' +{len(top)-3} more' if len(top) > 3 else ''),
        })

    # Channel entry
    new_channels = _new_items('channel')
    if new_channels:
        insights.append({
            'type':   'channel_entry',
            'icon':   'fa-tv',
            'color':  'violet',
            'title':  'Competitor Channel Entry Detected',
            'detail': ', '.join(sorted(new_channels)),
        })

    # Competitive pressure — programmes with 3+ advertisers
    pressure = list(
        filtered_qs.exclude(program='')
        .values('program')
        .annotate(adv_count=Count('advertiser', distinct=True))
        .filter(adv_count__gte=3)
        .order_by('-adv_count')[:3]
    )
    if pressure:
        p0 = pressure[0]
        insights.append({
            'type':   'pressure',
            'icon':   'fa-fire',
            'color':  'red',
            'title':  'High Competitive Pressure Zone',
            'detail': f'"{p0["program"]}" has {p0["adv_count"]} competitors — heavy inventory competition.',
        })

    # Weekend heavy detection
    if filtered_qs.exists():
        weekend = filtered_qs.filter(date__week_day__in=[1, 7]).count()  # Sun=1, Sat=7
        weekday = filtered_qs.count() - weekend
        if weekday > 0 and weekend / weekday > 0.6:
            insights.append({
                'type':   'flighting',
                'icon':   'fa-calendar-week',
                'color':  'amber',
                'title':  'Weekend-Heavy Flighting Pattern',
                'detail': f'{round(weekend/(weekend+weekday)*100)}% of ads air on weekends.',
            })

    # Dominant competitor (spend > 40% of total)
    total_spend = filtered_qs.aggregate(s=Sum('cost'))['s'] or 0
    if total_spend:
        for row in (filtered_qs.values('advertiser')
                    .annotate(s=Sum('cost')).order_by('-s')[:1]):
            share = float(row['s'] or 0) / float(total_spend) * 100
            if share > 40:
                insights.append({
                    'type':   'dominant',
                    'icon':   'fa-crown',
                    'color':  'yellow',
                    'title':  f'Market Dominance: {row["advertiser"]}',
                    'detail': f'{row["advertiser"]} commands {share:.0f}% of total competitor spend.',
                })

    return insights


# ── Deep competitor analysis ──────────────────────────────────────────────────

def _build_deep_data(request, account, all_advertisers):
    advertiser = request.GET.get('advertiser', '')
    if not advertiser and all_advertisers:
        advertiser = all_advertisers[0]
    if not advertiser:
        return {'deep_advertiser': '', 'deep_stats': None}

    qs = CompetitorAd.objects.filter(account=account, advertiser=advertiser)

    agg = qs.aggregate(
        total_spend=Sum('cost'),
        total_ads=Count('id'),
        avg_dur=Avg('duration'),
        first_date=Min('date'),
        last_date=Max('date'),
    )
    total_ads    = agg['total_ads'] or 0
    prime_count  = sum(1 for t in qs.values_list('advt_time', flat=True) if _is_prime(t))
    creative_cnt = qs.values('advt_theme').distinct().count()

    deep_stats = {
        'total_spend':    float(agg['total_spend'] or 0),
        'total_ads':      total_ads,
        'avg_duration':   round(float(agg['avg_dur'] or 0), 1),
        'first_date':     agg['first_date'],
        'last_date':      agg['last_date'],
        'creative_count': creative_cnt,
        'prime_pct':      round(prime_count / total_ads * 100) if total_ads else 0,
    }

    top_channels   = list(qs.values('channel').annotate(count=Count('id'), spend=Sum('cost')).order_by('-spend')[:8])
    top_programmes = list(qs.exclude(program='').values('program').annotate(count=Count('id'), spend=Sum('cost')).order_by('-spend')[:10])
    top_durations  = list(qs.values('duration').annotate(count=Count('id')).order_by('-count'))
    products       = list(qs.exclude(product='').values('product').annotate(count=Count('id'), spend=Sum('cost')).order_by('-spend')[:8])
    themes         = list(qs.exclude(advt_theme='').values('advt_theme').annotate(count=Count('id')).order_by('-count')[:10])
    spend_trend    = list(qs.values('date').annotate(spend=Sum('cost'), count=Count('id')).order_by('date'))
    peak_days      = list(qs.values('date').annotate(spend=Sum('cost'), count=Count('id')).order_by('-spend')[:7])
    campaign_cycles = list(qs.values('date').annotate(count=Count('id'), spend=Sum('cost')).order_by('date'))

    # Time band distribution
    tbands = {'Morning': 0, 'Daytime': 0, 'Prime Time': 0, 'Late Night': 0}
    band_map = {'morning': 'Morning', 'daytime': 'Daytime', 'primetime': 'Prime Time', 'latenight': 'Late Night'}
    for t in qs.values_list('advt_time', flat=True):
        bk = band_map.get(_time_band(t))
        if bk:
            tbands[bk] += 1

    # Language distribution
    lang_dist = list(qs.exclude(language='').values('language').annotate(count=Count('id')).order_by('-count'))

    # Break position for this advertiser
    bp = {'First': 0, 'Middle': 0, 'Last': 0}
    for row in qs.values('ad_position', 'total_ads'):
        pos = row['ad_position']
        tot = row['total_ads']
        if pos is None:
            continue
        if pos == 1:
            bp['First'] += 1
        elif tot and pos == tot:
            bp['Last'] += 1
        else:
            bp['Middle'] += 1

    # Radar metrics — normalised against all advertisers in this account
    all_adv_agg = list(
        CompetitorAd.objects.filter(account=account)
        .values('advertiser')
        .annotate(
            spend=Sum('cost'), count=Count('id'),
            prog_spread=Count('program', distinct=True),
            ch_spread=Count('channel', distinct=True),
        )
    )
    def _max(field):
        vals = [float(r[field] or 0) for r in all_adv_agg]
        return max(vals) if vals else 1

    max_spend = _max('spend') or 1
    max_count = _max('count') or 1
    max_prog  = _max('prog_spread') or 1
    max_ch    = _max('ch_spread') or 1

    my_agg = qs.aggregate(
        spend=Sum('cost'), count=Count('id'),
        prog_spread=Count('program', distinct=True),
        ch_spread=Count('channel', distinct=True),
    )
    radar_data = {
        'Spend':            min(100, round(float(my_agg['spend'] or 0) / max_spend * 100)),
        'Frequency':        min(100, round((my_agg['count'] or 0) / max_count * 100)),
        'Programme Spread': min(100, round((my_agg['prog_spread'] or 0) / max_prog * 100)),
        'Channel Spread':   min(100, round((my_agg['ch_spread'] or 0) / max_ch * 100)),
        'Prime Time':       deep_stats['prime_pct'],
    }

    return {
        'deep_advertiser':          advertiser,
        'deep_stats':               deep_stats,
        'deep_top_channels_json':   _to_json(top_channels),
        'deep_top_programmes_json': _to_json(top_programmes),
        'deep_top_durations_json':  _to_json(top_durations),
        'deep_spend_trend_json':    _to_json(spend_trend),
        'deep_products_json':       _to_json(products),
        'deep_themes_json':         _to_json(themes),
        'deep_time_band_json':      _to_json(tbands),
        'deep_lang_json':           _to_json(lang_dist),
        'deep_break_pos_json':      _to_json(bp),
        'deep_peak_days':           peak_days,
        'deep_campaign_cycles_json':_to_json(campaign_cycles),
        'deep_radar_json':          _to_json(radar_data),
    }


# ── Management endpoints ──────────────────────────────────────────────────────

@login_required
@role_required(_ALLOWED_ROLES)
@require_POST
def competitor_delete_batch(request, batch_id):
    """Delete an upload batch and all its ads (unless locked)."""
    batch = get_object_or_404(CompetitorUploadBatch, batch_id=batch_id)

    # Check account access
    if not _is_admin(request.user):
        if batch.account not in _account_qs(request.user):
            return HttpResponse('Access denied', status=403)

    if batch.is_locked:
        messages.error(request, 'This batch is locked and cannot be deleted.')
    else:
        account_id = batch.account_id
        row_count  = batch.ads.count()
        batch.delete()
        messages.success(request, f'Deleted batch with {row_count:,} rows.')
    return redirect(_comp_url(batch.account_id, 'management'))


@login_required
@role_required(_ALLOWED_ROLES)
@require_POST
def competitor_toggle_lock(request, batch_id):
    """Lock or unlock a historical batch."""
    batch = get_object_or_404(CompetitorUploadBatch, batch_id=batch_id)
    if not _is_admin(request.user):
        return HttpResponse('Access denied', status=403)
    batch.is_locked = not batch.is_locked
    batch.save(update_fields=['is_locked'])
    action = 'locked' if batch.is_locked else 'unlocked'
    messages.success(request, f'Batch {action} successfully.')
    return redirect(_comp_url(batch.account_id, 'management'))


@login_required
@role_required(_ALLOWED_ROLES)
def competitor_export(request):
    """Export competitor data as Excel."""
    account_id = request.GET.get('account_id', '')
    date_from  = request.GET.get('date_from') or None
    date_to    = request.GET.get('date_to') or None
    advertiser = request.GET.get('advertiser') or None

    if not account_id:
        return HttpResponse('No account selected.', status=400)

    try:
        account = _account_qs(request.user).get(id=int(account_id))
    except (Account.DoesNotExist, ValueError):
        return HttpResponse('Access denied.', status=403)

    batch_id = request.GET.get('batch_id') or None

    qs = CompetitorAd.objects.filter(account=account)
    if batch_id:
        qs = qs.filter(upload_batch__batch_id=batch_id)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if advertiser:
        qs = qs.filter(advertiser=advertiser)

    qs = qs.order_by('date', 'advertiser', 'advt_time')

    data = list(qs.values(
        'product_group', 'advertiser', 'product', 'advt_theme',
        'channel', 'program', 'date', 'prog_time', 'advt_time',
        'ad_position', 'total_ads', 'break_no', 'position_in_break',
        'ads_in_break', 'language', 'duration', 'cost',
    ))

    df = pd.DataFrame(data)
    if df.empty:
        return HttpResponse('No data to export.', status=404)

    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Competitor Analysis')
    buf.seek(0)

    fname = f'competitor_analysis_{account.name}_{date_from or "all"}_{date_to or "all"}.xlsx'
    response = HttpResponse(
        buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    return response
