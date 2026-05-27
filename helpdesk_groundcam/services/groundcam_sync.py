import logging
from datetime import datetime, timedelta, timezone

from odoo import fields

_logger = logging.getLogger(__name__)

TERMINAL_SESSION_STATUSES = ('completed', 'cancelled', 'expired')
TERMINAL_APPOINTMENT_STATUSES = ('completed', 'cancelled')

VALID_SESSION_STATUSES = (
    'pending', 'active', 'completed', 'cancelled', 'expired')
VALID_APPOINTMENT_STATUSES = (
    'scheduled', 'notified', 'lobby_created', 'completed', 'cancelled')


def _parse_iso_datetime(value):
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return False


def sync_session_state(env, session, server_data):
    """Apply a full server payload onto a local groundcam.session.

    Used by the reconciliation cron when a webhook event was lost in
    transit. Returns True if any field was updated.
    """
    server_status = server_data.get('status')
    if server_status not in VALID_SESSION_STATUSES:
        _logger.warning(
            'GroundCam sync: unexpected server status %r for session %s',
            server_status, session.groundcam_id)
        return False

    vals = {}
    if server_status != session.status:
        vals['status'] = server_status

    for src, dst in (
        ('duration', 'duration'),
        ('agentDuration', 'agent_duration'),
        ('clientDuration', 'client_duration'),
        ('screenshotsCount', 'screenshots_count'),
        ('recordingsCount', 'recordings_count'),
    ):
        server_val = server_data.get(src)
        if server_val and getattr(session, dst) != server_val:
            vals[dst] = server_val

    for src, dst in (
        ('startedAt', 'started_at'),
        ('admittedAt', 'admitted_at'),
        ('endedAt', 'ended_at'),
        ('expiresAt', 'expires_at'),
        ('updatedAt', 'updated_at'),
    ):
        if server_data.get(src) and not getattr(session, dst):
            vals[dst] = _parse_iso_datetime(server_data[src])

    closure_name = server_data.get('closureTypeName') or ''
    if closure_name and closure_name != (session.closure_type_name or ''):
        vals['closure_type_name'] = closure_name
        ct = env['groundcam.closure.type'].sudo().search(
            [('name', '=', closure_name)], limit=1)
        if ct:
            vals['closure_type_id'] = ct.id

    rating = server_data.get('clientRating')
    if rating and not session.client_rating:
        vals['client_rating'] = rating
    comment = server_data.get('clientComment')
    if comment and not session.client_comment:
        vals['client_comment'] = comment

    notes = server_data.get('notes')
    if notes and not session.notes:
        vals['notes'] = notes

    server_tags = server_data.get('sessionTags') or []
    if server_tags and not session.tag_ids:
        tag_ids = _resolve_tags(env, server_tags)
        if tag_ids:
            vals['tag_ids'] = [(6, 0, tag_ids)]

    if not vals:
        return False

    is_terminal_transition = (
        vals.get('status') in TERMINAL_SESSION_STATUSES
        and session.status not in TERMINAL_SESSION_STATUSES
    )

    session.sudo().write(vals)

    if is_terminal_transition and session.appointment_id:
        appt = session.appointment_id
        if appt.status not in TERMINAL_APPOINTMENT_STATUSES:
            appt_status = (
                'completed' if vals['status'] == 'completed' else 'cancelled')
            appt.sudo().write({'status': appt_status})

    # Re-fetch media list whenever the server side reports the session as
    # completed: covers both the initial completion and later edits (e.g.
    # screenshot tags) on already-completed sessions detected by the
    # incremental sync cron.
    if server_status == 'completed':
        try:
            sync_session_media(env, session)
        except Exception:
            _logger.warning(
                'GroundCam: failed to sync media for session %s',
                session.groundcam_id, exc_info=True)

    _post_reconcile_chatter(session.ticket_id, server_status)
    _notify_bus(
        session.ticket_id, 'groundcam.session', f'session.{server_status}')
    return True


def sync_appointment_state(env, appointment, server_data):
    """Apply a full server payload onto a local groundcam.appointment."""
    server_status = server_data.get('status')
    if server_status not in VALID_APPOINTMENT_STATUSES:
        _logger.warning(
            'GroundCam sync: unexpected appointment status %r for %s',
            server_status, appointment.groundcam_id)
        return False

    vals = {}
    if server_status != appointment.status:
        vals['status'] = server_status

    if server_data.get('lobbyId') and not appointment.lobby_id:
        vals['lobby_id'] = server_data['lobbyId']

    cancel_reason = server_data.get('cancellationReason')
    if cancel_reason and not appointment.cancellation_reason:
        vals['cancellation_reason'] = cancel_reason

    cancelled_by = server_data.get('cancelledBy')
    if cancelled_by and not appointment.cancelled_by:
        vals['cancelled_by'] = cancelled_by

    for src, dst in (
        ('cancelledAt', 'cancelled_at'),
        ('notifiedAt', 'notified_at'),
        ('updatedAt', 'updated_at'),
    ):
        if server_data.get(src) and not getattr(appointment, dst):
            vals[dst] = _parse_iso_datetime(server_data[src])

    if not vals:
        return False

    appointment.sudo().write(vals)

    _post_reconcile_chatter(appointment.ticket_id, server_status)
    _notify_bus(
        appointment.ticket_id, 'groundcam.appointment',
        f'appointment.{server_status}')
    return True


def mark_session_deleted_server_side(session):
    """Mark a session as cancelled when the server returns 404.

    Used by the reconciliation cron to stop polling a session that no
    longer exists on GroundCam.
    """
    if session.status in TERMINAL_SESSION_STATUSES:
        return False
    session.sudo().write({'status': 'cancelled'})
    ticket = session.ticket_id
    if ticket:
        ticket_in_lang = ticket.with_context(lang=_chatter_lang(ticket))
        ticket_in_lang.message_post(
            body=ticket_in_lang.env._(
                "GroundCam session no longer exists on the server "
                "(marked as cancelled locally)."
            ),
            subtype_xmlid='mail.mt_note',
        )
    _notify_bus(ticket, 'groundcam.session', 'session.cancelled')
    return True


def mark_appointment_deleted_server_side(appointment):
    """Mark an appointment as cancelled when the server returns 404."""
    if appointment.status in TERMINAL_APPOINTMENT_STATUSES:
        return False
    appointment.sudo().write({'status': 'cancelled'})
    ticket = appointment.ticket_id
    if ticket:
        ticket_in_lang = ticket.with_context(lang=_chatter_lang(ticket))
        ticket_in_lang.message_post(
            body=ticket_in_lang.env._(
                "GroundCam appointment no longer exists on the server "
                "(marked as cancelled locally)."
            ),
            subtype_xmlid='mail.mt_note',
        )
    _notify_bus(ticket, 'groundcam.appointment', 'appointment.cancelled')
    return True


def _resolve_tags(env, tag_names):
    Tag = env['groundcam.session.tag'].sudo()
    clean_names = [n.strip() for n in tag_names if n and n.strip()]
    if not clean_names:
        return []
    existing = Tag.search([('name', 'in', clean_names)])
    existing_by_name = {tag.name: tag for tag in existing}
    tag_ids = []
    for name in clean_names:
        tag = existing_by_name.get(name)
        if not tag:
            tag = Tag.create({'name': name})
            existing_by_name[name] = tag
        tag_ids.append(tag.id)
    return tag_ids


def _chatter_lang(ticket):
    """Pick a language for chatter posts done from a non-user context.

    Crons run as OdooBot (en_US by default). We post messages in the
    language of the agent assigned to the ticket, falling back to the
    customer or the company.
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


def _post_reconcile_chatter(ticket, server_status):
    if not ticket:
        return
    ticket_in_lang = ticket.with_context(lang=_chatter_lang(ticket))
    ticket_in_lang.message_post(
        body=ticket_in_lang.env._(
            "GroundCam status synchronized from API "
            "(webhook not received): %(status)s.",
            status=server_status,
        ),
        subtype_xmlid='mail.mt_note',
    )


def _notify_bus(ticket, model_name, event_type):
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
        user._bus_send('groundcam/updated', {
            'ticket_id': ticket.id,
            'model': model_name,
            'event_type': event_type,
            'message': '',
        })


def find_ticket_from_data(env, data):
    """Find helpdesk.ticket from crmId, metadata, linked appointment or lobbyId.

    Mirrors the logic of the webhook controller's _find_ticket, but usable from
    any service context (cron, manual sync, etc.).
    """
    Ticket = env['helpdesk.ticket'].sudo()

    crm_id = data.get('crmId')
    if crm_id:
        ticket = Ticket.search([('ticket_ref', '=', crm_id)], limit=1)
        if ticket:
            return ticket

    metadata = data.get('metadata') or {}
    odoo_ticket_id = metadata.get('odoo_ticket_id')
    if odoo_ticket_id:
        try:
            ticket = Ticket.browse(int(odoo_ticket_id)).exists()
            if ticket:
                return ticket
        except (ValueError, TypeError):
            pass

    appointment_gc_id = data.get('appointmentId')
    if appointment_gc_id:
        appointment = env['groundcam.appointment'].sudo().search(
            [('groundcam_id', '=', appointment_gc_id)], limit=1)
        if appointment and appointment.ticket_id:
            return appointment.ticket_id

    session_gc_id = data.get('id')
    if session_gc_id:
        appointment = env['groundcam.appointment'].sudo().search(
            [('lobby_id', '=', session_gc_id)], limit=1)
        if appointment and appointment.ticket_id:
            return appointment.ticket_id

    return False


def upsert_session_from_server(env, server_data):
    """Find or create a groundcam.session from a full server payload.

    Returns (session, created: bool). On creation, also links the source
    appointment via lobby_id if the appointment exists locally.
    """
    Session = env['groundcam.session'].sudo()
    session_gc_id = server_data.get('id')
    if not session_gc_id:
        return Session.browse(), False

    session = Session.search([('groundcam_id', '=', session_gc_id)], limit=1)
    if session:
        return session, False

    ticket = find_ticket_from_data(env, server_data)
    if not ticket:
        _logger.info(
            'GroundCam: cannot upsert session %s — no ticket found',
            session_gc_id)
        return Session.browse(), False

    appointment = False
    appointment_gc_id = server_data.get('appointmentId')
    if appointment_gc_id:
        appointment = env['groundcam.appointment'].sudo().search(
            [('groundcam_id', '=', appointment_gc_id)], limit=1)

    session = Session.create({
        'groundcam_id': session_gc_id,
        'ticket_id': ticket.id,
        'status': server_data.get('status', 'pending'),
        'mode': server_data.get('mode', 'camera'),
        'source': server_data.get('source', ''),
        'client_url': server_data.get('clientUrl', ''),
        'agent_url': server_data.get('agentUrl', ''),
        'agent_groundcam_id': server_data.get('agentId', ''),
        'agent_name': server_data.get('agentName', ''),
        'client_phone': server_data.get('clientPhone', ''),
        'client_name': server_data.get('clientName', ''),
        'client_email': server_data.get('clientEmail', ''),
        'crm_id': server_data.get('crmId', ''),
        'appointment_id': appointment.id if appointment else False,
        'updated_at': _parse_iso_datetime(
            server_data.get('updatedAt') or server_data.get('createdAt')),
    })

    if appointment and not appointment.lobby_id:
        appointment.sudo().write({
            'lobby_id': session_gc_id,
            'status': 'lobby_created',
        })

    return session, True


def sync_session_media(env, session):
    """Upsert groundcam.session.media records from screenshots and recordings.

    Creates new records and refreshes metadata (tags, GPS, duration, size)
    on existing ones so that edits made on GroundCam after the session
    (e.g. adding tags to a screenshot) are reflected in Odoo.

    Does not download binaries — that is user-triggered via 'Save to Odoo'.
    """
    from .groundcam_api import GroundCamAPI

    if not session or not session.groundcam_id:
        return

    api = GroundCamAPI(env)
    Media = env['groundcam.session.media'].sudo()
    existing_by_id = {
        m.groundcam_id: m for m in Media.search(
            [('session_id', '=', session.id)])
    }

    try:
        shots = api.get_screenshots(session.groundcam_id).get('screenshots', [])
    except Exception:
        _logger.warning(
            'GroundCam: failed to fetch screenshots for session %s',
            session.groundcam_id, exc_info=True)
        shots = []
    for shot in shots:
        gc_id = shot.get('id')
        if not gc_id:
            continue
        loc = shot.get('location') or {}
        tags = shot.get('tags') or []
        mutable_vals = {
            'timestamp': _parse_iso_datetime(shot.get('timestamp')),
            'latitude': loc.get('latitude') or 0.0,
            'longitude': loc.get('longitude') or 0.0,
            'tags': ', '.join(tags) if tags else False,
        }
        existing = existing_by_id.get(gc_id)
        if existing:
            existing.write(mutable_vals)
        else:
            Media.create({
                'session_id': session.id,
                'groundcam_id': gc_id,
                'media_type': 'screenshot',
                **mutable_vals,
            })

    try:
        recordings = api.get_recordings(session.groundcam_id).get('recordings', [])
    except Exception:
        _logger.warning(
            'GroundCam: failed to fetch recordings for session %s',
            session.groundcam_id, exc_info=True)
        recordings = []
    for rec in recordings:
        gc_id = rec.get('id')
        if not gc_id:
            continue
        mutable_vals = {
            'timestamp': _parse_iso_datetime(rec.get('createdAt')),
            'duration': int(rec.get('duration') or 0),
            'size': int(rec.get('size') or 0),
        }
        existing = existing_by_id.get(gc_id)
        if existing:
            existing.write(mutable_vals)
        else:
            Media.create({
                'session_id': session.id,
                'groundcam_id': gc_id,
                'media_type': 'recording',
                **mutable_vals,
            })


def upsert_appointment_from_server(env, server_data):
    """Find or create a groundcam.appointment from a server payload.

    Returns (appointment, created: bool).
    """
    Appointment = env['groundcam.appointment'].sudo()
    appointment_gc_id = server_data.get('id')
    if not appointment_gc_id:
        return Appointment.browse(), False

    appointment = Appointment.search(
        [('groundcam_id', '=', appointment_gc_id)], limit=1)
    if appointment:
        return appointment, False

    ticket = find_ticket_from_data(env, server_data)
    if not ticket:
        _logger.info(
            'GroundCam: cannot upsert appointment %s — no ticket found',
            appointment_gc_id)
        return Appointment.browse(), False

    appointment = Appointment.create({
        'groundcam_id': appointment_gc_id,
        'ticket_id': ticket.id,
        'status': server_data.get('status', 'scheduled'),
        'mode': server_data.get('mode', 'camera'),
        'agent_groundcam_id': server_data.get('agentId', ''),
        'agent_name': server_data.get('agentName', ''),
        'client_phone': server_data.get('clientPhone', ''),
        'client_name': server_data.get('clientName', ''),
        'client_email': server_data.get('clientEmail', ''),
        'scheduled_at': _parse_iso_datetime(server_data.get('scheduledAt')),
        'duration_minutes': server_data.get('durationMinutes', 0),
        'notify_by': server_data.get('notifyBy', 'sms'),
        'crm_id': server_data.get('crmId', ''),
        'organization_id': server_data.get('organizationId', ''),
        'created_by': server_data.get('createdBy', ''),
        'notified_at': _parse_iso_datetime(server_data.get('notifiedAt')),
        'lobby_id': server_data.get('lobbyId') or '',
        'updated_at': _parse_iso_datetime(
            server_data.get('updatedAt') or server_data.get('createdAt')),
    })

    return appointment, True


# -----------------------------------------------------------------------------
# Incremental sync (updatedAfter)
# -----------------------------------------------------------------------------

SYNC_BOOTSTRAP_HOURS = 24
SYNC_PAGE_LIMIT = 100
SYNC_MAX_PAGES = 20


def _bootstrap_cursor():
    """Return an ISO timestamp used when no cursor is stored yet."""
    return (
        fields.Datetime.now() - timedelta(hours=SYNC_BOOTSTRAP_HOURS)
    ).strftime('%Y-%m-%dT%H:%M:%SZ')


def run_incremental_sync_sessions(env):
    """Pull every session modified since the last cursor via GET /sessions?updatedAfter."""
    from .groundcam_api import GroundCamAPI

    ICP = env['ir.config_parameter'].sudo()
    key = 'groundcam.last_sync_sessions_at'
    cursor_iso = ICP.get_param(key) or _bootstrap_cursor()

    api = GroundCamAPI(env)
    max_updated_at = cursor_iso
    next_cursor = None

    for _page in range(SYNC_MAX_PAGES):
        params = {'updatedAfter': cursor_iso, 'limit': SYNC_PAGE_LIMIT}
        if next_cursor:
            params['startAfter'] = next_cursor
        try:
            result = api.list_sessions(**params)
        except Exception:
            _logger.exception('GroundCam: incremental session sync failed')
            return

        sessions = result.get('sessions', [])
        pagination = result.get('pagination', {})

        for item in sessions:
            session_gc_id = item.get('id')
            if not session_gc_id:
                continue
            try:
                full = api.get_session(session_gc_id)
            except Exception:
                _logger.warning(
                    'GroundCam: failed to fetch full session %s during sync',
                    session_gc_id, exc_info=True)
                continue

            session, created = upsert_session_from_server(env, full)
            if session and not created:
                sync_session_state(env, session, full)

            updated = full.get('updatedAt') or item.get('updatedAt')
            if updated and updated > max_updated_at:
                max_updated_at = updated

        if not pagination.get('hasMore'):
            break
        next_cursor = pagination.get('nextCursor')
        if not next_cursor:
            break

    if max_updated_at != cursor_iso:
        ICP.set_param(key, max_updated_at)


def run_incremental_sync_appointments(env):
    """Pull every appointment modified since the last cursor."""
    from .groundcam_api import GroundCamAPI

    ICP = env['ir.config_parameter'].sudo()
    key = 'groundcam.last_sync_appointments_at'
    cursor_iso = ICP.get_param(key) or _bootstrap_cursor()

    api = GroundCamAPI(env)
    max_updated_at = cursor_iso
    next_cursor = None

    for _page in range(SYNC_MAX_PAGES):
        params = {'updatedAfter': cursor_iso, 'limit': SYNC_PAGE_LIMIT}
        if next_cursor:
            params['startAfter'] = next_cursor
        try:
            result = api.list_appointments(**params)
        except Exception:
            _logger.exception('GroundCam: incremental appointment sync failed')
            return

        appointments = result.get('appointments', [])
        pagination = result.get('pagination', {})

        for item in appointments:
            appointment, created = upsert_appointment_from_server(env, item)
            if appointment and not created:
                sync_appointment_state(env, appointment, item)

            updated = item.get('updatedAt')
            if updated and updated > max_updated_at:
                max_updated_at = updated

        if not pagination.get('hasMore'):
            break
        next_cursor = pagination.get('nextCursor')
        if not next_cursor:
            break

    if max_updated_at != cursor_iso:
        ICP.set_param(key, max_updated_at)
