import logging

from odoo import api, fields, models, _

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)


class HelpdeskTicket(models.Model):
    _inherit = 'helpdesk.ticket'

    use_groundcam = fields.Boolean(
        related='team_id.use_groundcam', export_string_translation=False)
    groundcam_session_ids = fields.One2many(
        'groundcam.session', 'ticket_id',
        string='Video Sessions', copy=False)
    groundcam_session_count = fields.Integer(
        compute='_compute_groundcam_session_count',
        export_string_translation=False)
    groundcam_appointment_ids = fields.One2many(
        'groundcam.appointment', 'ticket_id',
        string='Appointments', copy=False)
    groundcam_pending_appointment_ids = fields.One2many(
        'groundcam.appointment', 'ticket_id',
        string='Pending Appointments',
        compute='_compute_groundcam_pending_appointment_ids',
        export_string_translation=False)
    groundcam_appointment_count = fields.Integer(
        compute='_compute_groundcam_appointment_count',
        export_string_translation=False)
    groundcam_active_session_id = fields.Many2one(
        'groundcam.session',
        compute='_compute_groundcam_active_session',
        string='Active Session',
        export_string_translation=False)
    groundcam_media_ids = fields.One2many(
        'groundcam.session.media', 'ticket_id',
        string='Media', readonly=True, copy=False)
    groundcam_media_count = fields.Integer(
        compute='_compute_groundcam_media_count',
        export_string_translation=False)

    @api.depends('groundcam_session_ids')
    def _compute_groundcam_session_count(self):
        session_groups = self.env['groundcam.session']._read_group(
            [('ticket_id', 'in', self.ids)],
            ['ticket_id'], ['__count'],
        )
        count_mapping = {
            ticket.id: count
            for ticket, count in session_groups
        }
        for ticket in self:
            ticket.groundcam_session_count = count_mapping.get(ticket.id, 0)

    @api.depends('groundcam_appointment_ids', 'groundcam_appointment_ids.lobby_id')
    def _compute_groundcam_pending_appointment_ids(self):
        for ticket in self:
            ticket.groundcam_pending_appointment_ids = \
                ticket.groundcam_appointment_ids.filtered(lambda a: not a.lobby_id)

    @api.depends('groundcam_appointment_ids', 'groundcam_appointment_ids.lobby_id')
    def _compute_groundcam_appointment_count(self):
        appointment_groups = self.env['groundcam.appointment']._read_group(
            [('ticket_id', 'in', self.ids), ('lobby_id', '=', False)],
            ['ticket_id'], ['__count'],
        )
        count_mapping = {
            ticket.id: count
            for ticket, count in appointment_groups
        }
        for ticket in self:
            ticket.groundcam_appointment_count = count_mapping.get(ticket.id, 0)

    @api.depends('groundcam_media_ids')
    def _compute_groundcam_media_count(self):
        media_groups = self.env['groundcam.session.media']._read_group(
            [('ticket_id', 'in', self.ids)],
            ['ticket_id'], ['__count'],
        )
        count_mapping = {
            ticket.id: count
            for ticket, count in media_groups
        }
        for ticket in self:
            ticket.groundcam_media_count = count_mapping.get(ticket.id, 0)

    @api.depends('groundcam_session_ids.status')
    def _compute_groundcam_active_session(self):
        for ticket in self:
            active = ticket.groundcam_session_ids.filtered(
                lambda s: s.status in ('pending', 'active'))
            ticket.groundcam_active_session_id = active[:1]

    def action_view_groundcam_sessions(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Video Sessions'),
            'res_model': 'groundcam.session',
            'view_mode': 'list,form',
            'domain': [('ticket_id', '=', self.id)],
            'context': {'default_ticket_id': self.id},
        }
        if len(self.groundcam_session_ids) == 1:
            action.update({
                'res_id': self.groundcam_session_ids[0].id,
                'view_mode': 'form',
            })
        return action

    def action_view_groundcam_appointments(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Video Appointments'),
            'res_model': 'groundcam.appointment',
            'view_mode': 'list,form',
            'domain': [('ticket_id', '=', self.id)],
            'context': {
                'default_ticket_id': self.id,
                'search_default_pending': 1,
            },
        }
        pending = self.groundcam_appointment_ids.filtered(
            lambda a: not a.lobby_id)
        if len(pending) == 1:
            action.update({
                'res_id': pending.id,
                'view_mode': 'form',
            })
        return action

    def action_view_groundcam_media(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Video Session Media'),
            'res_model': 'groundcam.session.media',
            'view_mode': 'list',
            'views': [(
                self.env.ref(
                    'helpdesk_groundcam.view_groundcam_session_media_ticket_list'
                ).id,
                'list',
            )],
            'domain': [('ticket_id', '=', self.id)],
        }

    def action_start_groundcam_session(self):
        self.ensure_one()
        if not self.partner_id and (self.partner_name or self.partner_email):
            self.partner_id = self._partner_find_from_emails_single(
                [self.partner_email]).id
        return {
            'type': 'ir.actions.act_window',
            'name': _('Start Session'),
            'res_model': 'groundcam.create.session',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_ticket_id': self.id,
                'dialog_size': 'medium',
            },
        }

    def action_schedule_groundcam_appointment(self):
        self.ensure_one()
        if not self.partner_id and (self.partner_name or self.partner_email):
            self.partner_id = self._partner_find_from_emails_single(
                [self.partner_email]).id
        return {
            'type': 'ir.actions.act_window',
            'name': _('Schedule Video Appointment'),
            'res_model': 'groundcam.create.appointment',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_ticket_id': self.id,
                'dialog_size': 'medium',
            },
        }

    def action_open_groundcam_agent_url(self):
        self.ensure_one()
        session = self.groundcam_active_session_id
        if not session:
            return
        # Use single-use auth URL so the agent doesn't need to log in
        try:
            api = GroundCamAPI(self.env)
            result = api.generate_agent_auth_url(session.groundcam_id)
            url = result.get('authUrl', session.agent_url)
        except Exception:
            _logger.warning(
                'GroundCam: failed to generate agent auth URL, '
                'falling back to agent_url', exc_info=True)
            url = session.agent_url
        if url:
            return {
                'type': 'ir.actions.act_url',
                'url': url,
                'target': 'new',
            }
