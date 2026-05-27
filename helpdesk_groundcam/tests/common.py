from unittest.mock import patch, MagicMock

from odoo.addons.helpdesk.tests.common import HelpdeskCommon


class GroundCamCommon(HelpdeskCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Enable GroundCam on test team
        cls.test_team.use_groundcam = True

        # Add phone to partner
        cls.partner.write({'phone': '+33612345678'})

        # Set GroundCam config
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param('groundcam.api_key', 'gc_live_test_key_12345678')
        ICP.set_param('groundcam.base_url', 'https://test.groundcam.io')
        ICP.set_param('groundcam.webhook_secret', 'test_webhook_secret_abc')
        ICP.set_param('web.base.url', 'https://odoo.example.com')

        # Create test GroundCam agent mapped to helpdesk_user
        cls.gc_agent = cls.env['groundcam.agent'].create({
            'groundcam_id': 'gc_agent_test_001',
            'email': 'hu@example.com',
            'display_name_gc': 'Test Agent',
            'role': 'agent',
            'user_id': cls.helpdesk_user.id,
        })

        # Create test ticket
        cls.test_ticket = cls.env['helpdesk.ticket'].create({
            'name': 'Test Ticket for GroundCam',
            'partner_id': cls.partner.id,
            'team_id': cls.test_team.id,
            'user_id': cls.helpdesk_user.id,
        })

    @classmethod
    def _mock_api_response(cls, status_code=200, json_data=None):
        """Create a mock response object for requests.request."""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.ok = 200 <= status_code < 300
        mock_response.json.return_value = json_data or {}
        return mock_response
