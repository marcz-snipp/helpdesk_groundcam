from odoo import fields, models, _
from odoo.exceptions import UserError

from ..services.groundcam_api import GroundCamAPI

WEBHOOK_EVENTS = [
    'session.created',
    'session.client_waiting',
    'session.active',
    'session.completed',
    'session.rated',
    'session.cancelled',
    'session.expired',
    'session.updated',
    'session.deleted',
    'appointment.created',
    'appointment.updated',
    'appointment.cancelled',
]


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    groundcam_api_key = fields.Char(
        string='GroundCam API Key',
        config_parameter='groundcam.api_key')
    groundcam_base_url = fields.Char(
        string='GroundCam Base URL',
        config_parameter='groundcam.base_url',
        default='https://api.groundcam.io')
    groundcam_webhook_secret = fields.Char(
        string='Webhook Secret',
        config_parameter='groundcam.webhook_secret',
        readonly=True)
    groundcam_webhook_id = fields.Char(
        string='Webhook ID',
        config_parameter='groundcam.webhook_id',
        readonly=True)
    groundcam_org_name = fields.Char(
        string='Organization Name',
        config_parameter='groundcam.org_name',
        readonly=True)
    groundcam_credits_available = fields.Integer(
        string='Credits Available',
        config_parameter='groundcam.credits_available',
        readonly=True)

    def action_groundcam_verify_key(self):
        self.ensure_one()
        # Save the key first so the API client can read it
        self.env['ir.config_parameter'].sudo().set_param(
            'groundcam.api_key', self.groundcam_api_key or '')
        self.env['ir.config_parameter'].sudo().set_param(
            'groundcam.base_url',
            self.groundcam_base_url or 'https://api.groundcam.io')

        api = GroundCamAPI(self.env)
        result = api.verify_key()

        org_name = result.get('organizationName', '')
        credits_info = result.get('credits', {})
        credits_available = credits_info.get('available', 0)

        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('groundcam.org_name', org_name)
        ICP.set_param('groundcam.credits_available', str(credits_available))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GroundCam'),
                'message': _(
                    'API key verified. Organization: %(org)s, '
                    'Credits: %(credits)s',
                    org=org_name,
                    credits=credits_available,
                ),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_groundcam_sync_agents(self):
        self.ensure_one()
        synced = self.env['groundcam.agent']._sync_from_api()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GroundCam'),
                'message': _('%(count)s agent(s) synchronized.', count=synced),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_groundcam_sync_closure_types(self):
        self.ensure_one()
        synced = self.env['groundcam.closure.type']._sync_from_api()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GroundCam'),
                'message': _('%(count)s closure type(s) synchronized.', count=synced),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_groundcam_sync_tags(self):
        self.ensure_one()
        synced = self.env['groundcam.session.tag']._sync_from_api()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GroundCam'),
                'message': _('%(count)s tag(s) synchronized.', count=synced),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_groundcam_register_webhook(self):
        self.ensure_one()
        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', '')
        if not base_url:
            raise UserError(_(
                "The system parameter 'web.base.url' is not set. "
                "Please configure it first."
            ))

        webhook_url = f"{base_url}/groundcam/webhook"
        api = GroundCamAPI(self.env)

        # Delete existing webhook if present
        existing_id = self.env['ir.config_parameter'].sudo().get_param(
            'groundcam.webhook_id', '')
        if existing_id:
            try:
                api.delete_webhook(existing_id)
            except UserError:
                pass  # Webhook may already be deleted on GroundCam side

        result = api.register_webhook(webhook_url, WEBHOOK_EVENTS)

        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('groundcam.webhook_id', result.get('id', ''))
        ICP.set_param('groundcam.webhook_secret', result.get('secret', ''))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GroundCam'),
                'message': _('Webhook registered successfully at %(url)s', url=webhook_url),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_groundcam_test_webhook(self):
        self.ensure_one()
        webhook_id = self.env['ir.config_parameter'].sudo().get_param(
            'groundcam.webhook_id', '')
        if not webhook_id:
            raise UserError(_(
                "No webhook is registered. "
                "Please register a webhook first."
            ))

        api = GroundCamAPI(self.env)
        api.test_webhook(webhook_id)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('GroundCam'),
                'message': _('Webhook test sent successfully.'),
                'type': 'success',
                'sticky': False,
            },
        }
