import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

from odoo import http
from odoo.http import request
from odoo.tools.translate import LazyTranslate

from odoo.addons.helpdesk_groundcam.services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)
_lt = LazyTranslate(__name__)

# Lazy-sync anti-burst window (seconds). When a webhook references an unknown
# agent / closure type / tag, we trigger a one-shot resync — but at most once
# per this window per resource type, to avoid hammering the API on bursts.
LAZY_SYNC_COOLDOWN_SECONDS = 300

# Status mapping from GroundCam event types
EVENT_STATUS_MAP = {
    'session.created': 'pending',
    'session.client_waiting': 'pending',
    'session.active': 'active',
    'session.completed': 'completed',
    'session.cancelled': 'cancelled',
    'session.expired': 'expired',
}

EVENT_MESSAGES = {
    'session.created': _lt('Video session created.'),
    'session.client_waiting': _lt('Client is waiting to join the video session.'),
    'session.active': _lt('Video session is now active.'),
    'session.completed': _lt('Video session completed.'),
    'session.rated': _lt('Client rated the video session.'),
    'session.cancelled': _lt('Video session was cancelled.'),
    'session.expired': _lt('Video session link has expired.'),
    'session.updated': _lt('Video session was updated.'),
    'session.deleted': _lt('Video session was deleted.'),
    'appointment.created': _lt('Video appointment was created.'),
    'appointment.updated': _lt('Video appointment was updated.'),
    'appointment.cancelled': _lt('Video appointment was cancelled.'),
}


def _chatter_lang(ticket):
    """Pick a language for chatter posts done from a non-user context.

    Webhooks run as the public user (en_US) and crons run as OdooBot
    (en_US by default). We want chatter messages to match the language
    of the agent assigned to the ticket, with a sensible fallback chain.
    """
    if not ticket:
        return 'en_US'
    candidates = (
        ticket.user_id.lang if ticket.user_id else None,
        ticket.partner_id.lang if ticket.partner_id else None,
        ticket.company_id.partner_id.lang if ticket.company_id else None,
    )
    for lang in candidates:
        if lang:
            return lang
    return 'en_US'


class GroundCamWebhookController(http.Controller):

    @http.route(
        '/groundcam/webhook', type='http', auth='public',
        methods=['POST'], csrf=False,
    )
    def handle_webhook(self, **kwargs):
        """Receive and process GroundCam webhook events.

        Uses auth='public' + csrf=False following the Odoo payment webhook
        pattern. Security is handled via HMAC-SHA256 signature verification.
        """
        # 1. Read raw body for HMAC verification
        payload_body = request.httprequest.get_data()
        signature = request.httprequest.headers.get('X-GroundCam-Signature', '')
        timestamp = request.httprequest.headers.get('X-GroundCam-Timestamp', '')
        event_type = request.httprequest.headers.get('X-GroundCam-Event', '')
        delivery_id = request.httprequest.headers.get('X-GroundCam-Delivery', '')

        # 2. Verify HMAC signature
        secret = request.env['ir.config_parameter'].sudo().get_param(
            'groundcam.webhook_secret', '')
        if not self._verify_signature(payload_body, signature, timestamp, secret):
            _logger.warning('GroundCam webhook: invalid signature')
            return request.make_json_response(
                {'error': 'Invalid signature'}, status=401)

        # 3. Check timestamp freshness. Aligned with GroundCam's retry
        # deadline (15 min) so legitimate retries are not rejected.
        # Replay protection is provided by HMAC + delivery-id dedup.
        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > 900:
                _logger.warning('GroundCam webhook: stale timestamp')
                return request.make_json_response(
                    {'error': 'Stale timestamp'}, status=401)
        except (ValueError, TypeError):
            return request.make_json_response(
                {'error': 'Invalid timestamp'}, status=400)

        # 4. Parse payload
        try:
            payload = json.loads(payload_body)
        except json.JSONDecodeError:
            return request.make_json_response(
                {'error': 'Invalid JSON'}, status=400)

        # 5. Idempotency: skip if this delivery was already processed.
        Delivery = request.env['groundcam.webhook.delivery'].sudo()
        if delivery_id and Delivery.search_count(
                [('delivery_id', '=', delivery_id)], limit=1):
            _logger.info(
                'GroundCam webhook: duplicate delivery %s (%s) skipped',
                delivery_id, event_type)
            return request.make_json_response(
                {'status': 'ok', 'duplicate': True})

        # 6. Dispatch to handler
        data = payload.get('data', {})
        _logger.info(
            'GroundCam webhook received: event=%s delivery=%s updatedFields=%s',
            event_type, delivery_id, data.get('updatedFields'))
        try:
            if event_type.startswith('session.'):
                self._handle_session_event(event_type, data)
            elif event_type.startswith('appointment.'):
                self._handle_appointment_event(event_type, data)
            elif event_type == 'webhook.test':
                _logger.info('GroundCam webhook test received')
            else:
                _logger.info('GroundCam webhook: unknown event %s', event_type)
        except Exception:
            _logger.exception(
                'GroundCam webhook: error processing %s', event_type)
            return request.make_json_response(
                {'error': 'Processing error'}, status=500)

        # 7. Record successful delivery for future idempotency checks.
        if delivery_id:
            try:
                Delivery.create({
                    'delivery_id': delivery_id,
                    'event_type': event_type,
                })
            except Exception:
                _logger.debug(
                    'GroundCam webhook: delivery %s already recorded',
                    delivery_id)

        return request.make_json_response({'status': 'ok'})

    def _verify_signature(self, payload_body, signature, timestamp, secret):
        if not secret or not signature or not timestamp:
            return False
        signed_payload = f"{timestamp}.{payload_body.decode('utf-8')}"
        expected = "sha256=" + hmac.new(
            secret.encode('utf-8'),
            signed_payload.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _handle_session_event(self, event_type, data):
        """Find or create the groundcam.session record and update it."""
        session_id = data.get('id')
        if not session_id:
            return

        # Lazy-sync the agents list if this webhook references an unknown agent
        self._ensure_agent_known(data.get('agentId'))

        Session = request.env['groundcam.session'].sudo()
        session = Session.search([('groundcam_id', '=', session_id)], limit=1)

        if not session:
            # Session created outside Odoo — try to find ticket
            ticket = self._find_ticket(data)
            if not ticket:
                _logger.info(
                    'GroundCam webhook: no ticket found for session %s',
                    session_id)
                return

            # Link to source appointment if appointmentId is provided
            appointment = False
            appointment_gc_id = data.get('appointmentId')
            if appointment_gc_id:
                appointment = request.env['groundcam.appointment'].sudo().search(
                    [('groundcam_id', '=', appointment_gc_id)], limit=1)

            session = Session.create({
                'groundcam_id': session_id,
                'ticket_id': ticket.id,
                'status': data.get('status', 'pending'),
                'mode': data.get('mode', 'camera'),
                'source': data.get('source', ''),
                'client_url': data.get('clientUrl', ''),
                'agent_url': data.get('agentUrl', ''),
                'agent_groundcam_id': data.get('agentId', ''),
                'agent_name': data.get('agentName', ''),
                'client_phone': data.get('clientPhone', ''),
                'client_name': data.get('clientName', ''),
                'client_email': data.get('clientEmail', ''),
                'crm_id': data.get('crmId', ''),
                'appointment_id': appointment.id if appointment else False,
                'updated_at': self._parse_iso_datetime(
                    data.get('updatedAt') or data.get('createdAt')),
            })

            # Update appointment with lobby_id and status
            if appointment and not appointment.lobby_id:
                appointment.write({
                    'lobby_id': session_id,
                    'status': 'lobby_created',
                })

        # Update session fields based on event
        new_status = EVENT_STATUS_MAP.get(event_type)
        vals = {}
        if new_status:
            vals['status'] = new_status

        # Propagate updatedAt on every event that carries it
        if data.get('updatedAt'):
            vals['updated_at'] = self._parse_iso_datetime(data['updatedAt'])

        if event_type == 'session.completed':
            # Fields available in the webhook payload (per openapi.yaml)
            closure_name = data.get('closureTypeName', '')
            vals.update({
                'duration': data.get('duration', 0),
                'agent_duration': data.get('agentDuration', 0),
                'client_duration': data.get('clientDuration', 0),
                'closure_type_name': closure_name,
                'closure_type_id': self._resolve_closure_type(closure_name),
                'ended_at': self._parse_iso_datetime(
                    data.get('completedAt') or data.get('endedAt')),
            })
            if 'notes' in data:
                vals['notes'] = data['notes'] or ''
            if data.get('sessionTags'):
                vals['tag_ids'] = self._resolve_tags(data['sessionTags'])
            # Fields not in webhook payload — fetch full session via API
            try:
                api = GroundCamAPI(request.env)
                full_session = api.get_session(session_id)
                vals.update({
                    'screenshots_count': full_session.get('screenshotsCount', 0),
                    'recordings_count': full_session.get('recordingsCount', 0),
                })
                # startedAt may be missing if session.active was not received
                if full_session.get('startedAt') and not session.started_at:
                    vals['started_at'] = self._parse_iso_datetime(
                        full_session['startedAt'])
                if full_session.get('admittedAt') and not session.admitted_at:
                    vals['admitted_at'] = self._parse_iso_datetime(
                        full_session['admittedAt'])
                if full_session.get('source') and not session.source:
                    vals['source'] = full_session['source']
            except Exception:
                _logger.warning(
                    'GroundCam webhook: failed to fetch full session %s',
                    session_id, exc_info=True)
        elif event_type == 'session.rated':
            # clientRating and clientComment come via dedicated session.rated event
            vals.update({
                'client_rating': data.get('clientRating', 0),
                'client_comment': data.get('clientComment', ''),
            })
        elif event_type == 'session.active':
            vals['started_at'] = self._parse_iso_datetime(
                data.get('startedAt'))
        elif event_type == 'session.expired':
            vals['ended_at'] = self._parse_iso_datetime(
                data.get('expiredAt'))
        elif event_type == 'session.updated':
            if 'notes' in data:
                vals['notes'] = data['notes'] or ''
            if 'sessionTags' in data:
                vals['tag_ids'] = self._resolve_tags(data['sessionTags'] or [])
            if 'closureTypeName' in data:
                closure_name = data['closureTypeName'] or ''
                vals['closure_type_name'] = closure_name
                vals['closure_type_id'] = self._resolve_closure_type(closure_name)
            updated_fields = data.get('updatedFields') or []
            if 'screenshots' in updated_fields:
                self._handle_screenshot_update(session, data)
        elif event_type == 'session.deleted':
            _logger.info(
                'GroundCam webhook: session %s deleted', data.get('id'))
            self._post_chatter(session.ticket_id, event_type, data)
            self._notify_bus(session.ticket_id, event_type, 'groundcam.session')
            return

        if vals:
            session.write(vals)

        # Update linked appointment status on terminal session events
        if event_type in ('session.completed', 'session.cancelled', 'session.expired'):
            appointment = session.appointment_id
            if not appointment:
                appointment_gc_id = data.get('appointmentId')
                if appointment_gc_id:
                    appointment = request.env['groundcam.appointment'].sudo().search(
                        [('groundcam_id', '=', appointment_gc_id)], limit=1)
            if appointment and appointment.status not in ('completed', 'cancelled'):
                appt_status = 'completed' if event_type == 'session.completed' else 'cancelled'
                appointment.write({'status': appt_status})

        # On session.completed, import the list of screenshots/recordings as
        # groundcam.session.media records (binaries are downloaded on demand).
        if event_type == 'session.completed':
            from odoo.addons.helpdesk_groundcam.services.groundcam_sync import (
                sync_session_media,
            )
            try:
                sync_session_media(request.env, session)
            except Exception:
                _logger.warning(
                    'GroundCam webhook: failed to sync media for session %s',
                    session.groundcam_id, exc_info=True)

        # Post chatter message on ticket
        self._post_chatter(session.ticket_id, event_type, data)
        self._notify_bus(session.ticket_id, event_type, 'groundcam.session')

    def _handle_screenshot_update(self, session, data):
        """Apply a screenshot edit / deletion payload from session.updated.

        The dashboard sends:
        - `screenshot: {id, tags, tagDetails?}` when an agent edits tags
        - `screenshotDeleted: {id}` when an agent deletes a screenshot
        Either way, `updatedFields` contains "screenshots".
        """
        Media = request.env['groundcam.session.media'].sudo()

        deleted = data.get('screenshotDeleted') or {}
        if deleted.get('id'):
            media = Media.search([
                ('session_id', '=', session.id),
                ('groundcam_id', '=', deleted['id']),
            ], limit=1)
            if media:
                media.unlink()
            return

        shot = data.get('screenshot') or {}
        if not shot.get('id'):
            return

        tags = shot.get('tags') or []
        media = Media.search([
            ('session_id', '=', session.id),
            ('groundcam_id', '=', shot['id']),
        ], limit=1)
        if media:
            media.write({'tags': ', '.join(tags) if tags else False})
        else:
            # Screenshot not yet known locally — create a minimal record.
            # The next sync_session_media (cron or button) will fill in
            # timestamp, location, etc.
            Media.create({
                'session_id': session.id,
                'groundcam_id': shot['id'],
                'media_type': 'screenshot',
                'tags': ', '.join(tags) if tags else False,
            })

    def _handle_appointment_event(self, event_type, data):
        """Find or create the groundcam.appointment record and update it."""
        appointment_id = data.get('id')
        if not appointment_id:
            return

        # Lazy-sync the agents list if this webhook references an unknown agent
        self._ensure_agent_known(data.get('agentId'))

        Appointment = request.env['groundcam.appointment'].sudo()
        appointment = Appointment.search(
            [('groundcam_id', '=', appointment_id)], limit=1)

        if not appointment:
            ticket = self._find_ticket(data)
            if not ticket:
                _logger.info(
                    'GroundCam webhook: no ticket found for appointment %s',
                    appointment_id)
                return
            appointment = Appointment.create({
                'groundcam_id': appointment_id,
                'ticket_id': ticket.id,
                'status': data.get('status', 'scheduled'),
                'mode': data.get('mode', 'camera'),
                'agent_groundcam_id': data.get('agentId', ''),
                'agent_name': data.get('agentName', ''),
                'client_phone': data.get('clientPhone', ''),
                'client_name': data.get('clientName', ''),
                'client_email': data.get('clientEmail', ''),
                'scheduled_at': self._parse_iso_datetime(
                    data.get('scheduledAt')),
                'duration_minutes': data.get('durationMinutes', 0),
                'notify_by': data.get('notifyBy', 'sms'),
                'crm_id': data.get('crmId', ''),
                'organization_id': data.get('organizationId', ''),
                'created_by': data.get('createdBy', ''),
                'notified_at': self._parse_iso_datetime(data.get('notifiedAt')),
                'updated_at': self._parse_iso_datetime(
                    data.get('updatedAt') or data.get('createdAt')),
            })

        vals = {}
        if data.get('updatedAt'):
            vals['updated_at'] = self._parse_iso_datetime(data['updatedAt'])
        if data.get('notifiedAt'):
            vals['notified_at'] = self._parse_iso_datetime(data['notifiedAt'])

        if event_type == 'appointment.updated':
            if data.get('status'):
                vals['status'] = data['status']
            if data.get('scheduledAt'):
                vals['scheduled_at'] = self._parse_iso_datetime(
                    data['scheduledAt'])
            if data.get('notes'):
                vals['notes'] = data['notes']
            if data.get('lobbyId'):
                vals['lobby_id'] = data['lobbyId']
                vals['status'] = 'lobby_created'
        elif event_type == 'appointment.cancelled':
            vals['status'] = 'cancelled'
            vals['cancellation_reason'] = data.get('cancellationReason', '')
            if data.get('cancelledBy'):
                vals['cancelled_by'] = data['cancelledBy']
            if data.get('cancelledAt'):
                vals['cancelled_at'] = self._parse_iso_datetime(
                    data['cancelledAt'])
            if data.get('lobbyId') and not appointment.lobby_id:
                vals['lobby_id'] = data['lobbyId']

        if vals:
            appointment.write(vals)

        self._post_chatter(appointment.ticket_id, event_type, data)
        self._notify_bus(
            appointment.ticket_id, event_type, 'groundcam.appointment')

    def _resolve_tags(self, tag_names):
        """Convert a list of tag name strings to a Many2many write command.

        If at least one tag is unknown, attempt a lazy sync from the
        GroundCam API (rate-limited via _should_lazy_sync). Any tag still
        missing afterwards is created locally so the session is never
        blocked.
        """
        Tag = request.env['groundcam.session.tag'].sudo()
        clean_names = [n.strip() for n in tag_names if n and n.strip()]
        if not clean_names:
            return [(6, 0, [])]

        existing = Tag.search([('name', 'in', clean_names)])
        if len(existing) < len(set(clean_names)) and self._should_lazy_sync('tags'):
            self._lazy_sync('groundcam.session.tag')
            existing = Tag.search([('name', 'in', clean_names)])

        existing_by_name = {tag.name: tag for tag in existing}
        tag_ids = []
        for name in clean_names:
            tag = existing_by_name.get(name)
            if not tag:
                tag = Tag.create({'name': name})
                existing_by_name[name] = tag
            tag_ids.append(tag.id)
        return [(6, 0, tag_ids)]

    def _resolve_closure_type(self, name):
        """Return the id of a groundcam.closure.type matching `name`, or False.

        If no match, attempt a lazy sync (rate-limited). Returns False if
        still no match — the Char `closure_type_name` is enough to keep the
        data; the Many2one is just an enrichment.
        """
        if not name:
            return False
        ClosureType = request.env['groundcam.closure.type'].sudo()
        ct = ClosureType.search([('name', '=', name)], limit=1)
        if ct:
            return ct.id
        if self._should_lazy_sync('closure_types'):
            self._lazy_sync('groundcam.closure.type')
            ct = ClosureType.search([('name', '=', name)], limit=1)
            if ct:
                return ct.id
        return False

    def _ensure_agent_known(self, agent_groundcam_id):
        """Trigger a lazy sync if the given GroundCam agent id is unknown."""
        if not agent_groundcam_id:
            return
        Agent = request.env['groundcam.agent'].sudo()
        if Agent.search_count([('groundcam_id', '=', agent_groundcam_id)]):
            return
        if self._should_lazy_sync('agents'):
            self._lazy_sync('groundcam.agent')

    def _should_lazy_sync(self, kind):
        """Anti-burst guard: at most one resync per kind every cooldown window."""
        ICP = request.env['ir.config_parameter'].sudo()
        key = f'groundcam.last_lazy_sync.{kind}'
        last = ICP.get_param(key, '0')
        try:
            last_ts = float(last)
        except (ValueError, TypeError):
            last_ts = 0.0
        now = time.time()
        if now - last_ts < LAZY_SYNC_COOLDOWN_SECONDS:
            return False
        ICP.set_param(key, str(now))
        return True

    def _lazy_sync(self, model_name):
        """Run `_sync_from_api()` on the given model, swallowing errors.

        Lazy sync must never break webhook processing.
        """
        try:
            request.env[model_name].sudo()._sync_from_api()
        except Exception:
            _logger.warning(
                'GroundCam webhook: lazy sync failed for %s', model_name,
                exc_info=True)

    def _find_ticket(self, data):
        """Find helpdesk.ticket from crmId, metadata, or linked appointment."""
        Ticket = request.env['helpdesk.ticket'].sudo()

        # crmId maps to ticket_ref
        crm_id = data.get('crmId')
        if crm_id:
            ticket = Ticket.search([('ticket_ref', '=', crm_id)], limit=1)
            if ticket:
                return ticket

        # Fallback to metadata
        metadata = data.get('metadata') or {}
        odoo_ticket_id = metadata.get('odoo_ticket_id')
        if odoo_ticket_id:
            try:
                ticket = Ticket.browse(int(odoo_ticket_id)).exists()
                if ticket:
                    return ticket
            except (ValueError, TypeError):
                pass

        # Fallback: session created from an appointment — use appointmentId
        appointment_gc_id = data.get('appointmentId')
        if appointment_gc_id:
            appointment = request.env['groundcam.appointment'].sudo().search(
                [('groundcam_id', '=', appointment_gc_id)], limit=1)
            if appointment and appointment.ticket_id:
                return appointment.ticket_id

        # Fallback: session auto-created by an appointment — find appointment
        # by lobbyId (the session ID) and get its ticket
        session_id = data.get('id')
        if session_id:
            appointment = request.env['groundcam.appointment'].sudo().search(
                [('lobby_id', '=', session_id)], limit=1)
            if appointment and appointment.ticket_id:
                return appointment.ticket_id

        return False

    def _notify_bus(self, ticket, event_type, model_name):
        """Send a bus notification to users viewing this ticket.

        Each user receives the message translated in their own language.
        """
        if not ticket:
            return
        users = ticket.user_id
        if not users:
            users = ticket.message_follower_ids.partner_id.user_ids.filtered(
                lambda u: not u.share
            )
        if not users:
            return
        for user in users:
            user_env = ticket.with_context(lang=user.lang or 'en_US').env
            raw = EVENT_MESSAGES.get(event_type)
            if raw is not None:
                message = user_env._(raw)
            else:
                message = user_env._(
                    'GroundCam event: %(event)s', event=event_type)
            user._bus_send('groundcam/updated', {
                'ticket_id': ticket.id,
                'model': model_name,
                'event_type': event_type,
                'message': message,
            })

    def _post_chatter(self, ticket, event_type, data):
        """Post a note in the ticket chatter about the event.

        Uses the ticket assignee's language so that the message reads
        naturally for the helpdesk agent watching the ticket.
        """
        if not ticket:
            return
        lang = _chatter_lang(ticket)
        ticket = ticket.with_context(lang=lang)
        env = ticket.env

        raw = EVENT_MESSAGES.get(event_type)
        if raw is not None:
            message = env._(raw)
        else:
            message = env._(
                'GroundCam event: %(event)s', event=event_type)

        if event_type == 'session.completed':
            duration = data.get('duration', 0)
            if duration:
                minutes = duration // 60
                seconds = duration % 60
                message += ' ' + env._(
                    'Duration: %(minutes)sm %(seconds)ss.',
                    minutes=minutes, seconds=seconds)
        elif event_type == 'session.deleted':
            deleted_by = data.get('deletedBy')
            if deleted_by:
                message += ' ' + env._(
                    'Deleted by: %(actor)s.', actor=deleted_by)
            deleted_at = data.get('deletedAt')
            if deleted_at:
                dt = self._parse_iso_datetime(deleted_at)
                if dt:
                    message += ' ' + env._(
                        'Date: %(date)s UTC.',
                        date=dt.strftime('%d/%m/%Y %H:%M'))
        elif event_type == 'session.rated':
            rating = data.get('clientRating')
            if rating:
                message += ' ' + env._(
                    'Rating: %(rating)s/5.', rating=rating)
            comment = data.get('clientComment')
            if comment:
                message += ' ' + env._(
                    'Comment: %(comment)s', comment=comment)

        ticket.message_post(body=message, subtype_xmlid='mail.mt_note')

    @staticmethod
    def _parse_iso_datetime(value):
        """Parse ISO 8601 datetime string to naive datetime (UTC)."""
        if not value:
            return False
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, AttributeError):
            return False
