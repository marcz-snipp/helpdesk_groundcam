import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.addons.phone_validation.tools.phone_validation import phone_format

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)


class GroundcamCreateAppointment(models.TransientModel):
    _name = 'groundcam.create.appointment'
    _description = 'Schedule a GroundCam Video Appointment'

    ticket_id = fields.Many2one(
        'helpdesk.ticket', required=True)
    mode = fields.Selection([
        ('camera', 'Camera'),
        ('screen', 'Screen Sharing'),
    ], string='Mode', default='camera', required=True)
    agent_id = fields.Many2one(
        'groundcam.agent', string='GroundCam Agent',
        required=True,
        domain=[('disabled', '=', False), ('role', '!=', 'viewer')])
    client_phone = fields.Char('Client Phone')
    client_name = fields.Char('Client Name')
    client_email = fields.Char('Client Email')
    scheduled_at = fields.Datetime('Scheduled Date & Time', required=True)
    duration_minutes = fields.Selection([
        ('5', '5 minutes'),
        ('10', '10 minutes'),
        ('15', '15 minutes'),
        ('20', '20 minutes'),
    ], string='Link Validity (minutes)', default='15', required=True)
    notify_by = fields.Selection([
        ('sms', 'SMS'),
        ('email', 'Email'),
        ('both', 'Both'),
    ], string='Notify By', default='sms', required=True)
    notes = fields.Text('Notes')

    @api.onchange('mode')
    def _onchange_mode(self):
        if self.mode == 'screen':
            self.notify_by = 'email'

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        ticket_id = defaults.get('ticket_id') or self.env.context.get('default_ticket_id')
        if ticket_id:
            ticket = self.env['helpdesk.ticket'].browse(ticket_id)
            defaults['client_phone'] = (
                ticket.partner_phone
                or (ticket.partner_id.phone if ticket.partner_id else '')
            )
            defaults['client_name'] = (
                ticket.partner_id.name if ticket.partner_id
                else ticket.partner_name
            )
            defaults['client_email'] = (
                ticket.partner_email
                or (ticket.partner_id.email if ticket.partner_id else '')
            )
        # Auto-select agent mapped to current user
        agent = self.env['groundcam.agent'].search([
            ('user_id', '=', self.env.uid),
            ('disabled', '=', False),
        ], limit=1)
        if agent:
            defaults['agent_id'] = agent.id
        return defaults

    def action_schedule_appointment(self):
        self.ensure_one()
        ticket = self.ticket_id

        # Phone validation — conditional on mode
        client_phone = None
        if self.mode == 'camera':
            if not self.client_phone:
                raise UserError(_(
                    "A client phone number is required to schedule a video appointment."
                ))
            client_phone = self._format_phone(ticket)
        elif self.client_phone:
            # Screen mode with optional phone — format if provided
            client_phone = self._format_phone(ticket)

        # Email required for screen mode
        if self.mode == 'screen' and not self.client_email:
            raise UserError(_(
                "A client email is required to schedule a screen sharing appointment."
            ))

        # Validate 5-minute slot alignment
        if self.scheduled_at.minute % 5 != 0 or self.scheduled_at.second != 0:
            raise UserError(_(
                "The appointment time must be aligned to 5-minute intervals "
                "(e.g. 14:00, 14:05, 14:10)."
            ))

        # Screen mode forces email notification
        notify_by = self.notify_by
        if self.mode == 'screen':
            notify_by = 'email'

        api = GroundCamAPI(self.env)

        # Pre-flight credit check: appointments consume a credit when the
        # session is auto-created. Refresh the cached value and stop early
        # when the organization is out of credits. Best-effort: if the
        # credits endpoint is unreachable, fall through and let the create
        # call surface a 402 if necessary.
        try:
            credits = api.get_credits()
        except UserError as e:
            _logger.warning(
                'GroundCam: pre-flight credits check failed, proceeding: %s', e)
        else:
            available = credits.get('available', 0)
            self.env['ir.config_parameter'].sudo().set_param(
                'groundcam.credits_available', str(available))
            if available < 1:
                raise UserError(_(
                    "Insufficient GroundCam credits (%(available)s remaining). "
                    "Please purchase more credits from the GroundCam dashboard.",
                    available=available,
                ))

        # Build ticketUrl
        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', '')
        ticket_url = (
            f"{base_url}/odoo/helpdesk/{ticket.team_id.id}/tickets/{ticket.id}"
            if base_url else ''
        )

        result = api.create_appointment(
            agent_id=self.agent_id.groundcam_id,
            scheduled_at=self.scheduled_at.isoformat() + 'Z',
            duration_minutes=int(self.duration_minutes),
            notify_by=notify_by,
            client_phone=client_phone,
            mode=self.mode,
            clientName=self.client_name or '',
            clientEmail=self.client_email or '',
            crmId=ticket.ticket_ref or '',
            ticketUrl=ticket_url,
            metadata={'odoo_ticket_id': str(ticket.id)},
            notes=self.notes or '',
        )

        # Parse scheduledAt
        scheduled_at = self.scheduled_at

        # Create groundcam.appointment record
        self.env['groundcam.appointment'].create({
            'groundcam_id': result['id'],
            'ticket_id': ticket.id,
            'status': result.get('status', 'scheduled'),
            'mode': self.mode,
            'agent_groundcam_id': self.agent_id.groundcam_id,
            'agent_name': self.agent_id.display_name_gc,
            'client_phone': client_phone or '',
            'client_name': self.client_name or '',
            'client_email': self.client_email or '',
            'scheduled_at': scheduled_at,
            'duration_minutes': int(self.duration_minutes),
            'notify_by': notify_by,
            'notes': self.notes,
            'user_id': self.env.uid,
            'crm_id': ticket.ticket_ref or '',
        })

        # Post chatter note
        if self.mode == 'screen':
            body_msg = _(
                'Screen sharing appointment scheduled for %(date)s (%(duration)s min).',
                date=fields.Datetime.to_string(scheduled_at),
                duration=self.duration_minutes,
            )
        else:
            body_msg = _(
                'Video appointment scheduled for %(date)s (%(duration)s min).',
                date=fields.Datetime.to_string(scheduled_at),
                duration=self.duration_minutes,
            )
        ticket.message_post(body=body_msg, subtype_xmlid='mail.mt_note')

        return {'type': 'ir.actions.act_window_close'}

    def _format_phone(self, ticket):
        """Format client phone to E.164. Raises UserError if invalid."""
        country = (
            ticket.partner_id.country_id
            or ticket.company_id.country_id
            or self.env.company.country_id
        )
        country_code = country.code if country else False
        formatted_phone = phone_format(
            self.client_phone,
            country_code=country_code,
            country_phone_code=int(country.phone_code) if country and country.phone_code else False,
            force_format='E164',
            raise_exception=False,
        )
        if not formatted_phone or (formatted_phone == self.client_phone and not self.client_phone.startswith('+')):
            raise UserError(_(
                "Invalid phone number. Please use international format, e.g. +33612345678."
            ))
        return formatted_phone
