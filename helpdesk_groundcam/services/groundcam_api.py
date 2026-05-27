import json
import logging

import requests

from odoo import _
from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

_logger = logging.getLogger(__name__)
_lt = LazyTranslate(__name__)

GROUNDCAM_TIMEOUT = 15  # seconds

# Map GroundCam business error codes to user-friendly, translated messages.
# Codes not listed here fall back to the raw API message.
ERROR_CODE_MESSAGES = {
    'AGENT_NOT_MEMBER': _lt("The agent is no longer a member of your GroundCam organization."),
    'AGENT_DISABLED': _lt("This GroundCam agent account is disabled."),
    'INSUFFICIENT_ROLE': _lt("This GroundCam role is not allowed to create sessions."),
    'INVALID_PHONE': _lt("Invalid phone number. Use the international format, e.g. +33612345678."),
    'INVALID_PHONE_NUMBER': _lt("Invalid phone number. Use the international format, e.g. +33612345678."),
    'INVALID_CLOSURE_TYPE': _lt("Unknown closure type on GroundCam side. Resynchronize closure types from the settings."),
    'SMS_NOT_CONFIGURED': _lt("SMS is not configured for your GroundCam organization."),
    'SMS_LIMIT_REACHED': _lt("SMS limit reached for this session."),
    'IDEMPOTENCY_CONFLICT': _lt("A similar request is already being processed."),
    'TOKEN_RATE_LIMITED': _lt("Too many authentication URL generations (limit: 5 per hour)."),
    'DUPLICATE_URL': _lt("A webhook with this URL already exists."),
    'ORG_NOT_FOUND': _lt("Your GroundCam organization could not be found. Check the API key configuration."),
}


class GroundCamAPI:
    """Stateless API client for GroundCam V1 REST API.

    Instantiated with env to read ir.config_parameter.
    Not an Odoo model -- a plain Python class used by models and controllers.
    """

    def __init__(self, env):
        ICP = env['ir.config_parameter'].sudo()
        self.api_key = ICP.get_param('groundcam.api_key', '')
        self.base_url = ICP.get_param(
            'groundcam.base_url', 'https://api.groundcam.io'
        ).rstrip('/')

    def _headers(self, extra=None):
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method, path, **kwargs):
        """Make an HTTP request to the GroundCam API.

        Returns the 'data' key from the response on success.
        Raises UserError on API errors.
        """
        if not self.api_key:
            raise UserError(_(
                "GroundCam API key is not configured. "
                "Go to Settings > GroundCam to enter your API key."
            ))

        url = f"{self.base_url}/v1{path}"
        kwargs.setdefault('timeout', GROUNDCAM_TIMEOUT)
        kwargs.setdefault('headers', self._headers())

        try:
            response = requests.request(method, url, **kwargs)
        except requests.exceptions.Timeout:
            raise UserError(_(
                "GroundCam API request timed out. Please try again."
            ))
        except requests.exceptions.ConnectionError:
            raise UserError(_(
                "Could not connect to GroundCam. "
                "Please check your internet connection."
            ))

        if response.status_code == 429:
            raise UserError(_(
                "GroundCam rate limit exceeded. "
                "Please wait a moment and try again."
            ))

        if response.status_code == 402:
            raise UserError(_(
                "Insufficient GroundCam credits. "
                "Please purchase more credits from the GroundCam dashboard."
            ))

        try:
            body = response.json()
        except json.JSONDecodeError:
            raise UserError(_(
                "Invalid response from GroundCam (HTTP %(status)s)",
                status=response.status_code,
            ))

        if not response.ok:
            error = body.get('error', {}) or {}
            code = error.get('code', 'UNKNOWN')
            raw_msg = error.get(
                'message', f'Unknown error (HTTP {response.status_code})')
            mapped = ERROR_CODE_MESSAGES.get(code)
            if mapped is not None:
                # mapped is a LazyGettext — stringifies to the translated message
                raise UserError(str(mapped))
            raise UserError(_(
                "GroundCam API error [%(code)s]: %(msg)s",
                code=code, msg=raw_msg,
            ))

        return body.get('data', body)

    # -------------------------------------------------------------------------
    # Sessions
    # -------------------------------------------------------------------------

    def create_session(self, agent_id, client_phone=None, **kwargs):
        idempotency_key = kwargs.pop('idempotency_key', None)
        payload = {'agentId': agent_id}
        if client_phone:
            payload['clientPhone'] = client_phone
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        extra_headers = {}
        if idempotency_key:
            extra_headers['Idempotency-Key'] = idempotency_key
        return self._request(
            'POST', '/sessions',
            json=payload,
            headers=self._headers(extra_headers) if extra_headers else self._headers(),
        )

    def get_session(self, session_id):
        return self._request('GET', f'/sessions/{session_id}')

    def list_sessions(self, **params):
        return self._request('GET', '/sessions', params=params)

    def update_session(self, session_id, **kwargs):
        return self._request('PATCH', f'/sessions/{session_id}', json=kwargs)

    def end_session(self, session_id, **kwargs):
        return self._request('POST', f'/sessions/{session_id}/end',
                             json=kwargs if kwargs else None)

    def cancel_session(self, session_id):
        return self._request('POST', f'/sessions/{session_id}/cancel')

    def resend_sms(self, session_id):
        return self._request('POST', f'/sessions/{session_id}/resend-sms')

    def generate_agent_auth_url(self, session_id):
        return self._request('POST', f'/sessions/{session_id}/agent-auth-url')

    def get_screenshots(self, session_id):
        return self._request('GET', f'/sessions/{session_id}/screenshots')

    def get_recordings(self, session_id):
        return self._request('GET', f'/sessions/{session_id}/recordings')

    def create_sessions_bulk(self, sessions, idempotency_key=None):
        extra_headers = {}
        if idempotency_key:
            extra_headers['Idempotency-Key'] = idempotency_key
        return self._request(
            'POST', '/sessions/bulk',
            json={'sessions': sessions},
            headers=self._headers(extra_headers) if extra_headers else self._headers(),
        )

    # -------------------------------------------------------------------------
    # Appointments
    # -------------------------------------------------------------------------

    def create_appointment(self, agent_id, scheduled_at,
                           duration_minutes, notify_by=None,
                           client_phone=None, **kwargs):
        payload = {
            'agentId': agent_id,
            'scheduledAt': scheduled_at,
            'durationMinutes': duration_minutes,
        }
        if client_phone:
            payload['clientPhone'] = client_phone
        if notify_by:
            payload['notifyBy'] = notify_by
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return self._request('POST', '/appointments', json=payload)

    def get_appointment(self, appointment_id):
        return self._request('GET', f'/appointments/{appointment_id}')

    def list_appointments(self, **params):
        return self._request('GET', '/appointments', params=params)

    def update_appointment(self, appointment_id, **kwargs):
        return self._request('PATCH', f'/appointments/{appointment_id}', json=kwargs)

    def cancel_appointment(self, appointment_id, reason=None):
        payload = {'reason': reason} if reason else {}
        return self._request(
            'POST', f'/appointments/{appointment_id}/cancel',
            json=payload if payload else None,
        )

    def get_available_slots(self, agent_id, date, duration_minutes=15):
        return self._request('GET', '/appointments/available-slots', params={
            'agentId': agent_id,
            'date': date,
            'durationMinutes': duration_minutes,
        })

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------

    def verify_key(self):
        return self._request('GET', '/auth/verify')

    def get_agents(self):
        return self._request('GET', '/agents')

    def get_credits(self):
        return self._request('GET', '/credits')

    def get_closure_types(self):
        return self._request('GET', '/closure-types')

    def get_tags(self):
        return self._request('GET', '/tags')

    def get_stats(self, period='month'):
        return self._request('GET', '/stats', params={'period': period})

    def get_organization(self):
        return self._request('GET', '/organization')

    # -------------------------------------------------------------------------
    # Webhooks
    # -------------------------------------------------------------------------

    def register_webhook(self, url, events):
        return self._request('POST', '/webhooks', json={
            'url': url,
            'events': events,
        })

    def list_webhooks(self):
        return self._request('GET', '/webhooks')

    def update_webhook(self, webhook_id, **kwargs):
        return self._request('PATCH', f'/webhooks/{webhook_id}', json=kwargs)

    def test_webhook(self, webhook_id):
        return self._request('POST', f'/webhooks/{webhook_id}/test')

    def delete_webhook(self, webhook_id):
        return self._request('DELETE', f'/webhooks/{webhook_id}')
