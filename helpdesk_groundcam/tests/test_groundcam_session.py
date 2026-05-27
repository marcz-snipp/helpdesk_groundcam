from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import GroundCamCommon


@tagged('-at_install', 'post_install')
class TestGroundCamSession(GroundCamCommon):

    def _get_create_session_response(self):
        return {
            'data': {
                'id': 'sess_abc123',
                'status': 'pending',
                'clientUrl': 'https://groundcam.io/l/sess_abc123?token=xyz',
                'agentUrl': 'https://groundcam.io/dashboard/lobbies/sess_abc123',
                'expiresAt': '2026-03-18T16:00:00Z',
                'sms': {'sid': 'SM123', 'to': '+33612345678'},
            }
        }

    @patch('odoo.addons.helpdesk_groundcam.services.groundcam_api.requests.request')
    def test_create_session_from_wizard(self, mock_request):
        """Test creating a video session from the wizard."""
        def _side_effect(method, url, **kwargs):
            if '/credits' in url:
                return self._mock_api_response(
                    200, {'data': {'available': 100}})
            return self._mock_api_response(
                201, self._get_create_session_response())
        mock_request.side_effect = _side_effect

        wizard = self.env['groundcam.create.session'].with_user(
            self.helpdesk_user
        ).create({
            'ticket_id': self.test_ticket.id,
            'agent_id': self.gc_agent.id,
            'client_phone': '+33612345678',
            'client_name': 'Test Client',
            'language': 'fr',
            'ttl_minutes': '15',
            'send_sms': True,
        })

        result = wizard.action_start_session()

        # Should close the wizard dialog (link is in chatter instead)
        self.assertEqual(result['type'], 'ir.actions.act_window_close')

        # Verify session record was created
        session = self.env['groundcam.session'].search([
            ('groundcam_id', '=', 'sess_abc123')])
        self.assertEqual(len(session), 1)
        self.assertEqual(session.ticket_id, self.test_ticket)
        self.assertEqual(session.status, 'pending')
        self.assertEqual(session.client_phone, '+33612345678')

        # Verify chatter message was posted (lang-agnostic: search by agent URL,
        # which embeds the GroundCam session id and is not translated).
        messages = self.test_ticket.message_ids.filtered(
            lambda m: 'sess_abc123' in (m.body or ''))
        self.assertTrue(messages)

    @patch('odoo.addons.helpdesk_groundcam.services.groundcam_api.requests.request')
    def test_create_session_no_phone_raises_error(self, mock_request):
        """Test that creating a session without phone raises error."""
        wizard = self.env['groundcam.create.session'].with_user(
            self.helpdesk_user
        ).create({
            'ticket_id': self.test_ticket.id,
            'agent_id': self.gc_agent.id,
            'client_phone': '',
            'language': 'fr',
        })

        with self.assertRaises(UserError):
            wizard.action_start_session()

    def test_wizard_default_get(self):
        """Test that the wizard pre-fills fields from the ticket."""
        wizard = self.env['groundcam.create.session'].with_user(
            self.helpdesk_user
        ).with_context(default_ticket_id=self.test_ticket.id).create({})

        self.assertEqual(wizard.client_phone, '+33612345678')
        self.assertEqual(wizard.agent_id, self.gc_agent)

    def test_session_count_computed(self):
        """Test that session count is computed correctly."""
        self.assertEqual(self.test_ticket.groundcam_session_count, 0)

        self.env['groundcam.session'].create({
            'groundcam_id': 'sess_count_1',
            'ticket_id': self.test_ticket.id,
            'status': 'completed',
        })
        self.env['groundcam.session'].create({
            'groundcam_id': 'sess_count_2',
            'ticket_id': self.test_ticket.id,
            'status': 'pending',
        })

        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_session_count, 2)

    def test_active_session_computed(self):
        """Test that active session is computed correctly."""
        self.assertFalse(self.test_ticket.groundcam_active_session_id)

        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_active_1',
            'ticket_id': self.test_ticket.id,
            'status': 'active',
            'agent_url': 'https://groundcam.io/dashboard/lobbies/sess_active_1',
        })

        self.test_ticket.invalidate_recordset()
        self.assertEqual(
            self.test_ticket.groundcam_active_session_id, session)

    def test_use_groundcam_related(self):
        """Test that use_groundcam is related to team setting."""
        self.assertTrue(self.test_ticket.use_groundcam)

        self.test_team.use_groundcam = False
        self.test_ticket.invalidate_recordset()
        self.assertFalse(self.test_ticket.use_groundcam)
