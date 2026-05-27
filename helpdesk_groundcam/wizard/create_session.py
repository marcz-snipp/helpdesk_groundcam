import logging
import uuid
from datetime import datetime

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.addons.phone_validation.tools.phone_validation import phone_format

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)


class GroundcamCreateSession(models.TransientModel):
    _name = 'groundcam.create.session'
    _description = 'Start a GroundCam Session'

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
    language = fields.Selection([
        ('fr', 'French'),
        ('en', 'English'),
        ('es', 'Spanish'),
        ('de', 'German'),
        ('it', 'Italian'),
        ('pt', 'Portuguese'),
        ('nl', 'Dutch'),
        ('pl', 'Polish'),
        ('ru', 'Russian'),
        ('zh', 'Chinese'),
    ], string='SMS Language', default='fr')
    ttl_minutes = fields.Selection([
        ('5', '5 minutes'),
        ('10', '10 minutes'),
        ('15', '15 minutes'),
        ('20', '20 minutes'),
    ], string='Link Validity (minutes)', default='15', required=True)
    send_sms = fields.Boolean('Send SMS to client', default=True)
    send_email = fields.Boolean('Send link by email', default=True)

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

    def action_start_session(self):
        self.ensure_one()
        ticket = self.ticket_id

        # Phone validation — conditional on mode
        client_phone = None
        if self.mode == 'camera':
            if not self.client_phone:
                raise UserError(_(
                    "A client phone number is required to start a video session."
                ))
            client_phone = self._format_phone(ticket)
        elif self.client_phone:
            # Screen mode with optional phone — format if provided
            client_phone = self._format_phone(ticket)

        api = GroundCamAPI(self.env)

        # Pre-flight credit check: refresh cached value and stop early when
        # the organization is out of credits. Best-effort: if the credits
        # endpoint is unreachable, fall through and let the create call
        # surface a 402 if necessary.
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

        # Build ticketUrl from web.base.url
        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', '')
        ticket_url = (
            f"{base_url}/odoo/helpdesk/{ticket.team_id.id}/tickets/{ticket.id}"
            if base_url else ''
        )

        # Idempotency key to prevent duplicate sessions on double-click
        idempotency_key = str(uuid.uuid4())

        result = api.create_session(
            agent_id=self.agent_id.groundcam_id,
            client_phone=client_phone,
            mode=self.mode,
            clientName=self.client_name or '',
            clientEmail=self.client_email or '',
            language=self.language if self.mode == 'camera' else None,
            ttlMinutes=int(self.ttl_minutes),
            sendSms=self.send_sms if self.mode == 'camera' else False,
            crmId=ticket.ticket_ref or '',
            ticketUrl=ticket_url,
            metadata={'odoo_ticket_id': str(ticket.id)},
            idempotency_key=idempotency_key,
        )

        # Parse expiresAt (Odoo expects naive UTC datetimes)
        expires_at = False
        if result.get('expiresAt'):
            try:
                dt = datetime.fromisoformat(
                    result['expiresAt'].replace('Z', '+00:00'))
                expires_at = dt.replace(tzinfo=None)
            except (ValueError, AttributeError):
                pass

        # Create groundcam.session record
        self.env['groundcam.session'].create({
            'groundcam_id': result['id'],
            'ticket_id': ticket.id,
            'status': result.get('status', 'pending') if result.get('status') in ('pending', 'active', 'completed', 'cancelled', 'expired') else 'pending',
            'mode': self.mode,
            'source': 'api',
            'client_url': result.get('clientUrl', ''),
            'agent_url': result.get('agentUrl', ''),
            'agent_groundcam_id': self.agent_id.groundcam_id,
            'agent_name': self.agent_id.display_name_gc,
            'client_phone': client_phone or '',
            'client_name': self.client_name or '',
            'client_email': self.client_email or '',
            'user_id': self.env.uid,
            'crm_id': ticket.ticket_ref or '',
            'expires_at': expires_at,
        })

        # Send email with client link (screen mode)
        client_url = result.get('clientUrl', '')
        if self.send_email and self.client_email and client_url:
            self._send_client_email(ticket, client_url, expires_at)

        # Post chatter note with clickable agent link
        if self.mode == 'screen':
            session_label = _('Screen sharing session started.')
        else:
            session_label = _('Video session started.')

        extra_info = Markup('')
        if self.mode == 'camera' and self.send_sms and client_phone:
            extra_info = Markup(' ') + _(
                'SMS sent to %(phone)s.', phone=client_phone)
        elif self.mode == 'screen' and self.send_email and self.client_email:
            extra_info = Markup(' ') + _(
                'Email sent to %(email)s.', email=self.client_email)

        agent_url = result.get('agentUrl', '')
        link_info = Markup('')
        if agent_url:
            link_info = Markup('<br/><a href="%s" target="_blank">%s</a>') % (
                agent_url,
                _('Open GroundCam session'),
            )
        ticket.message_post(
            body=Markup('%s%s%s') % (
                session_label,
                extra_info,
                link_info,
            ),
            subtype_xmlid='mail.mt_note',
        )

        return {'type': 'ir.actions.act_window_close'}

    def _send_client_email(self, ticket, client_url, expires_at):
        """Send the session link to the client via email."""
        company = self.env.company
        client_name = self.client_name or _('Client')
        subject = _('%(company)s — Screen sharing invitation',
                     company=company.name)

        # Build expiry info
        expiry_info = ''
        if expires_at:
            from odoo.tools.misc import format_datetime
            expiry_info = _(
                'This link expires at %(time)s.',
                time=format_datetime(self.env, expires_at),
            )

        body_html = Markup(
            '<p>%(greeting)s</p>'
            '<p>%(instruction)s</p>'
            '<p style="margin: 16px 0;">'
            '<a href="%(url)s" '
            'style="background-color: #875A7B; color: white; padding: 10px 20px; '
            'text-decoration: none; border-radius: 4px;" '
            'target="_blank">%(button)s</a>'
            '</p>'
            '<p>%(expiry)s</p>'
            '<p>%(regards)s<br/>%(company)s</p>'
        ) % {
            'greeting': _('Hello %(name)s,', name=client_name),
            'instruction': _(
                'You have been invited to a screen sharing session. '
                'Click the button below to join:'
            ),
            'url': client_url,
            'button': _('Join Screen Sharing Session'),
            'expiry': expiry_info,
            'regards': _('Best regards,'),
            'company': company.name,
        }

        mail_values = {
            'subject': subject,
            'email_to': self.client_email,
            'body_html': body_html,
            'auto_delete': True,
        }
        # Use company email or current user email as sender
        email_from = company.email or self.env.user.email
        if email_from:
            mail_values['email_from'] = email_from

        try:
            self.env['mail.mail'].sudo().create(mail_values).send()
        except Exception:
            _logger.warning(
                'GroundCam: failed to send session email to %s',
                self.client_email, exc_info=True,
            )

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
