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
        f'_(This is an automated message from the Ad-Monitoring system.)_'
    )
    return send_text(officer_whatsapp, body)


# ── High-level notification functions ─────────────────────────────────────────

def notify_missed_spots(officer_whatsapp: str, account_name: str, channel: str,
                        month: str, missed_count: int, schedule_pk: int,
                        account_id: int, officer_name: str = '',
                        schedule_number: str = '', start_date=None,
                        end_date=None, pdf_bytes: bytes = b'') -> bool:
    """Notify a channel marketing officer that spots were missed.

    Sends a WhatsApp text message with details + attaches the missed-spots PDF.
    """
    cfg = _get_config()
    base_url = cfg['base_url'].rstrip('/')

    from urllib.parse import urlencode
    params = urlencode({
        'account_id':  account_id,
        'channel':     channel,
        'month':       month,
        'schedule_pk': schedule_pk,
    })
    link = f'{base_url}/dashboard/tc/upload/?{params}' if base_url else ''

    start_str = start_date.strftime('%-d %B %Y') if start_date else ''
    end_str   = end_date.strftime('%-d %B %Y')   if end_date   else ''

    lines = [
        f'*Ad-Monitoring Alert — Missed Spots*',
        f'',
    ]
    if officer_name:
        lines.append(f'Dear *{officer_name}*,')
        lines.append(f'')
    lines += [
        f'Account:  {account_name}',
        f'Channel:  {channel}',
        f'Month:    {month}',
    ]
    if schedule_number:
        lines.append(f'Schedule: #{schedule_number}')
    if start_str:
        lines.append(f'Period:   {start_str} — {end_str}')
    lines += [
        f'',
        f'*{missed_count} spot(s) were not confirmed by 3rd-party monitoring.*',
        f'',
        f'Please verify and arrange rescheduling with the channel.',
    ]
    if link:
        lines += [f'', f'Upload TC to review:', f'{link}']
    lines += [f'', f'_(Login required — use your Ad-Monitor credentials)_']
    body = '\n'.join(lines)

    ok = send_text(officer_whatsapp, body)

    # Attach the missed-spots PDF if bytes provided
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
    """Remind a channel marketing officer to upload the TC after schedule ends."""
    cfg = _get_config()
    base_url = cfg['base_url'].rstrip('/')

    from urllib.parse import urlencode
    params = urlencode({
        'account_id':  account_id,
        'channel':     channel,
        'month':       month,
        'schedule_pk': schedule_pk,
    })
    link = f'{base_url}/dashboard/tc/upload/?{params}' if base_url else ''

    end_str = end_date.strftime('%-d %B %Y') if end_date else month

    lines = [
        f'*TC Upload Reminder*',
        f'',
        f'Account: {account_name}',
        f'Channel: {channel}',
        f'Period: {month}',
        f'Schedule ended: {end_str}',
        f'',
        f'Please upload the Transmission Certificate (TC) so we can confirm aired spots.',
    ]
    if link:
        lines += [
            f'',
            f'Upload TC here:',
            f'{link}',
        ]
    lines += [
        f'',
        f'_(Login required — use your Ad-Monitor credentials)_',
    ]
    body = '\n'.join(lines)
    return send_text(officer_whatsapp, body)


def notify_reconciliation_done(ops_whatsapp: str, account_name: str, channel: str,
                                month: str, matched: int, extra: int,
                                lmrb_confirmed: int, account_id: int) -> bool:
    """Notify an operations person that TC reconciliation completed."""
    cfg = _get_config()
    base_url = cfg['base_url'].rstrip('/')
    from urllib.parse import urlencode
    params = urlencode({'account_id': account_id, 'channel': channel, 'month': month})
    link = f'{base_url}/dashboard/summary/?{params}' if base_url else ''

    lines = [
        f'*TC Reconciliation Complete*',
        f'',
        f'Account: {account_name}',
        f'Channel: {channel}',
        f'Period: {month}',
        f'',
        f'TC matched to schedule: {matched}',
        f'LMRB confirmed: {lmrb_confirmed}',
        f'Extra (unplanned): {extra}',
    ]
    if link:
        lines += [f'', f'View summary: {link}']
    body = '\n'.join(lines)
    return send_text(ops_whatsapp, body)
