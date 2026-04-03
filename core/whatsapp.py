"""
WhatsApp Business Cloud API integration.

Meta WhatsApp Cloud API docs:
  https://developers.facebook.com/docs/whatsapp/cloud-api/get-started

Setup (one-time, done by admin):
  1. Go to developers.facebook.com → create a Business App → add WhatsApp product
  2. From API Setup: copy Phone Number ID and temporary Access Token
  3. Add your personal number as a test recipient on the same page
  4. Paste both values into /dashboard/settings/ under WhatsApp Notifications
  5. Set whatsapp_enabled = 1 and whatsapp_test_number = your WhatsApp number
  6. Click "Test WhatsApp" button to verify

In test mode (whatsapp_test_number is set), ALL messages go to that number.
In production mode (whatsapp_test_number is blank), messages go to each officer's number.
"""

import requests
import logging

logger = logging.getLogger(__name__)

_META_API = 'https://graph.facebook.com/v19.0'


def _get_config():
    """Return WhatsApp config dict from SystemSettings. Imported lazily to avoid
    circular imports at module load time."""
    from core.models import get_setting, get_setting_int
    return {
        'enabled':        get_setting_int('whatsapp_enabled', 0),
        'access_token':   get_setting('whatsapp_access_token', ''),
        'phone_number_id': get_setting('whatsapp_phone_number_id', ''),
        'test_number':    get_setting('whatsapp_test_number', ''),
        'base_url':       get_setting('whatsapp_app_base_url', ''),
    }


def _resolve_to(cfg, real_number: str) -> str:
    """Return the actual number to send to. In test mode always returns test number."""
    if cfg['test_number']:
        return cfg['test_number']
    return real_number


def send_template(to_number: str, template_name: str, params: list) -> bool:
    """Send a Meta-approved WhatsApp template message.

    Unlike send_text(), templates work for business-initiated conversations —
    no 24-hour window required. The recipient does NOT need to message first.

    params: list of strings matching the template's {{1}}, {{2}}, ... variables.
    """
    cfg = _get_config()
    if not cfg['enabled']:
        logger.info('[WhatsApp] disabled — skipping template "%s" to %s', template_name, to_number)
        return False
    if not cfg['access_token'] or not cfg['phone_number_id']:
        logger.warning('[WhatsApp] access_token or phone_number_id not configured')
        return False
    if not to_number:
        logger.warning('[WhatsApp] no recipient number — skipping template "%s"', template_name)
        return False
    if not template_name:
        logger.warning('[WhatsApp] template name is blank — skipping')
        return False

    actual_to = _resolve_to(cfg, to_number).strip().lstrip('+')
    url = f"{_META_API}/{cfg['phone_number_id']}/messages"
    components = []
    if params:
        components.append({
            'type': 'body',
            'parameters': [{'type': 'text', 'text': str(p)} for p in params],
        })
    payload = {
        'messaging_product': 'whatsapp',
        'to': actual_to,
        'type': 'template',
        'template': {
            'name': template_name,
            'language': {'code': 'en'},
            'components': components,
        },
    }
    headers = {
        'Authorization': f"Bearer {cfg['access_token']}",
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            logger.info('[WhatsApp] template "%s" sent to %s (resolved: %s)',
                        template_name, to_number, actual_to)
            return True
        else:
            logger.error('[WhatsApp] template "%s" error %s: %s',
                         template_name, resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.error('[WhatsApp] template request failed: %s', exc)
        return False


def send_text(to_number: str, body: str) -> bool:
    """Send a plain text WhatsApp message.

    Returns True if the API accepted the message, False otherwise.
    Works for test numbers without requiring template approval.
    """
    cfg = _get_config()
    if not cfg['enabled']:
        logger.info('[WhatsApp] disabled — skipping message to %s', to_number)
        return False
    if not cfg['access_token'] or not cfg['phone_number_id']:
        logger.warning('[WhatsApp] access_token or phone_number_id not configured')
        return False
    if not to_number:
        logger.warning('[WhatsApp] no recipient number — skipping')
        return False

    actual_to = _resolve_to(cfg, to_number).strip().lstrip('+')
    url = f"{_META_API}/{cfg['phone_number_id']}/messages"
    payload = {
        'messaging_product': 'whatsapp',
        'to': actual_to,
        'type': 'text',
        'text': {'body': body, 'preview_url': False},
    }
    headers = {
        'Authorization': f"Bearer {cfg['access_token']}",
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            logger.info('[WhatsApp] sent to %s (resolved: %s)', to_number, actual_to)
            return True
        else:
            logger.error('[WhatsApp] API error %s: %s', resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.error('[WhatsApp] request failed: %s', exc)
        return False


def _upload_media(token: str, phone_number_id: str, file_bytes: bytes,
                   filename: str, mime_type: str = 'application/pdf') -> str | None:
    """Upload bytes to Meta media API. Returns media_id or None on failure."""
    url = f'{_META_API}/{phone_number_id}/media'
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {token}'},
            files={'file': (filename, file_bytes, mime_type)},
            data={'messaging_product': 'whatsapp', 'type': mime_type},
            timeout=30,
        )
        media_id = resp.json().get('id')
        if media_id:
            logger.info('[WhatsApp] media uploaded, id=%s', media_id)
        else:
            logger.error('[WhatsApp] media upload failed: %s', resp.text)
        return media_id
    except Exception as exc:
        logger.error('[WhatsApp] media upload error: %s', exc)
        return None


def send_document(to_number: str, file_bytes: bytes, filename: str,
                  caption: str = '') -> bool:
    """Upload a PDF to Meta media API and send it as a WhatsApp document."""
    cfg = _get_config()
    if not cfg['enabled']:
        return False
    if not cfg['access_token'] or not cfg['phone_number_id'] or not to_number:
        return False

    actual_to = _resolve_to(cfg, to_number).strip().lstrip('+')
    media_id = _upload_media(cfg['access_token'], cfg['phone_number_id'],
                             file_bytes, filename)
    if not media_id:
        return False

    url = f"{_META_API}/{cfg['phone_number_id']}/messages"
    payload = {
        'messaging_product': 'whatsapp',
        'to': actual_to,
        'type': 'document',
        'document': {
            'id': media_id,
            'filename': filename,
            'caption': caption,
        },
    }
    headers = {
        'Authorization': f"Bearer {cfg['access_token']}",
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info('[WhatsApp] document sent to %s', actual_to)
            return True
        else:
            logger.error('[WhatsApp] document send error %s: %s', resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.error('[WhatsApp] document send failed: %s', exc)
        return False


def send_welcome_registration(officer_whatsapp: str, officer_name: str,
                               company_name: str) -> bool:
    """Send a welcome + registration prompt to a channel officer's WhatsApp."""
    body = (
        f'👋 Welcome to *{company_name}*, *{officer_name}*!\n\n'
        f'You have been registered to receive Ad-Monitoring alerts for your channel.\n\n'
        f'Please reply *CONFIRM* to activate your notifications.\n\n'
        f'_(Automated message from {company_name})_'
    )
    return send_text(officer_whatsapp, body)


# ── High-level notification functions ─────────────────────────────────────────

def notify_missed_spots(officer_whatsapp: str, account_name: str, channel: str,
                        month: str, missed_count: int, schedule_pk: int,
                        account_id: int, officer_name: str = '',
                        schedule_number: str = '', start_date=None,
                        end_date=None, pdf_bytes: bytes = b'',
                        company: str = '') -> bool:
    """Notify a channel marketing officer that spots were missed.

    Sends an approved template message + attaches the missed-spots PDF.
    Template: missed_spots_alert
    Variables: {{1}}=officer_name, {{2}}=account, {{3}}=channel,
               {{4}}=month, {{5}}=missed_count, {{6}}=schedule_number
    """
    from core.models import get_setting
    tmpl = get_setting('whatsapp_tmpl_missed_spots', 'missed_spots_alert')
    ok = send_template(officer_whatsapp, tmpl, [
        officer_name or account_name,
        account_name,
        channel,
        month,
        str(missed_count),
        f'#{schedule_number}' if schedule_number else '-',
    ])

    # After template opens the conversation, attach the PDF
    if pdf_bytes:
        fname = f'missed_ads_{channel}_{month}.pdf'.replace(' ', '_')
        caption = f'Missed Ad Report — {channel} | {month}'
        if schedule_number:
            caption += f' | #{schedule_number}'
        send_document(officer_whatsapp, pdf_bytes, fname, caption)

    return ok


def notify_tc_upload_reminder(officer_whatsapp: str, account_name: str,
                               channel: str, month: str, schedule_pk: int,
                               account_id: int, end_date=None) -> bool:
    """Remind a channel marketing officer to upload the TC after schedule ends.

    Template: tc_upload_reminder
    Variables: {{1}}=account, {{2}}=channel, {{3}}=month,
               {{4}}=end_date, {{5}}=upload_url
    """
    from core.models import get_setting
    cfg = _get_config()
    base_url = cfg['base_url'].rstrip('/')

    from urllib.parse import urlencode
    qs = urlencode({'account_id': account_id, 'channel': channel,
                    'month': month, 'schedule_pk': schedule_pk})
    upload_url = f'{base_url}/dashboard/tc/upload/?{qs}' if base_url else '-'
    end_str = end_date.strftime('%-d %B %Y') if end_date else month

    tmpl = get_setting('whatsapp_tmpl_tc_reminder', 'tc_upload_reminder')
    return send_template(officer_whatsapp, tmpl, [
        account_name,
        channel,
        month,
        end_str,
        upload_url,
    ])


def notify_reconciliation_done(ops_whatsapp: str, account_name: str, channel: str,
                                month: str, matched: int, extra: int,
                                lmrb_confirmed: int, account_id: int) -> bool:
    """Notify an operations person that TC reconciliation completed.

    Template: reconciliation_done
    Variables: {{1}}=account, {{2}}=channel, {{3}}=month,
               {{4}}=matched, {{5}}=lmrb_confirmed, {{6}}=summary_url
    """
    from core.models import get_setting
    cfg = _get_config()
    base_url = cfg['base_url'].rstrip('/')
    from urllib.parse import urlencode
    qs = urlencode({'account_id': account_id, 'channel': channel, 'month': month})
    summary_url = f'{base_url}/dashboard/summary/?{qs}' if base_url else '-'

    tmpl = get_setting('whatsapp_tmpl_reconcile_done', 'reconciliation_done')
    return send_template(ops_whatsapp, tmpl, [
        account_name,
        channel,
        month,
        str(matched),
        str(lmrb_confirmed),
        summary_url,
    ])


def notify_new_user_created(whatsapp: str, name: str, email: str,
                             password: str, role_label: str,
                             login_url: str) -> bool:
    """Send login credentials to a newly created staff user via WhatsApp.

    Template: user_welcome
    Variables: {{1}}=name, {{2}}=role, {{3}}=email, {{4}}=password, {{5}}=login_url
    """
    from core.models import get_setting
    tmpl = get_setting('whatsapp_tmpl_user_welcome', 'user_welcome')
    return send_template(whatsapp, tmpl, [
        name,
        role_label,
        email,
        password,
        login_url,
    ])


def notify_new_officer_created(whatsapp: str, name: str, account_name: str,
                                channel: str, email: str, password: str,
                                login_url: str) -> bool:
    """Send welcome + login instructions to a newly created channel officer via WhatsApp.

    Template: officer_welcome
    Variables: {{1}}=name, {{2}}=account, {{3}}=channel,
               {{4}}=email, {{5}}=password, {{6}}=login_url
    """
    from core.models import get_setting
    tmpl = get_setting('whatsapp_tmpl_officer_welcome', 'officer_welcome')
    return send_template(whatsapp, tmpl, [
        name,
        account_name,
        channel,
        email or '-',
        password or '-',
        login_url,
    ])
