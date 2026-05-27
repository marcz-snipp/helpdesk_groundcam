from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import GroundCamCommon


@tagged('-at_install', 'post_install')
class TestGroundCamReconcile(GroundCamCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    # ------------------------------------------------------------------
    # Incremental sync — sessions
    # ------------------------------------------------------------------

    def _run_session_sync(self, server_payload, *, get_session_side_effect=None):
        """Mock list_sessions + get_session and invoke the incremental cron."""
        list_payload = {
            'sessions': [{
                'id': server_payload['id'],
                'updatedAt': server_payload.get(
                    'updatedAt', '2026-05-15T15:00:00Z'),
            }],
            'pagination': {'hasMore': False},
        }
        list_patch = patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.'
            'GroundCamAPI.list_sessions',
            return_value=list_payload,
        )
        get_kwargs = (
            {'side_effect': get_session_side_effect}
            if get_session_side_effect is not None
            else {'return_value': server_payload}
        )
        get_patch = patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.'
            'GroundCamAPI.get_session',
            **get_kwargs,
        )
        with list_patch, get_patch:
            self.env['groundcam.session']._cron_incremental_sync()

    def test_incremental_sync_session_pending_to_expired(self):
        """A pending session whose server state is expired gets synced."""
        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_to_expire',
            'ticket_id': self.test_ticket.id,
            'status': 'pending',
        })

        self._run_session_sync({
            'id': 'sess_to_expire',
            'status': 'expired',
            'expiresAt': '2026-05-15T14:56:28Z',
            'updatedAt': '2026-05-15T15:00:37Z',
            'startedAt': None,
        })

        session.invalidate_recordset()
        self.assertEqual(session.status, 'expired')
        self.assertTrue(session.expires_at)

    def test_incremental_sync_session_pending_to_completed(self):
        """A pending session completed server-side gets synced with duration."""
        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_to_complete',
            'ticket_id': self.test_ticket.id,
            'status': 'pending',
        })

        # sync_session_state re-fetches media on completion → also mock those.
        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.'
            'GroundCamAPI.get_screenshots', return_value={'screenshots': []},
        ), patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.'
            'GroundCamAPI.get_recordings', return_value={'recordings': []},
        ):
            self._run_session_sync({
                'id': 'sess_to_complete',
                'status': 'completed',
                'duration': 300,
                'agentDuration': 280,
                'clientDuration': 290,
                'endedAt': '2026-05-15T15:00:00Z',
                'closureTypeName': 'Resolved',
                'updatedAt': '2026-05-15T15:00:30Z',
            })

        session.invalidate_recordset()
        self.assertEqual(session.status, 'completed')
        self.assertEqual(session.duration, 300)
        self.assertEqual(session.agent_duration, 280)
        self.assertEqual(session.closure_type_name, 'Resolved')
        self.assertTrue(session.ended_at)

    def test_incremental_sync_session_api_error_keeps_status(self):
        """If get_session fails for one entry, the session is not mutated."""
        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_api_500',
            'ticket_id': self.test_ticket.id,
            'status': 'pending',
        })

        self._run_session_sync(
            {'id': 'sess_api_500'},
            get_session_side_effect=UserError(
                'GroundCam API error [INTERNAL]: Boom'),
        )

        session.invalidate_recordset()
        self.assertEqual(session.status, 'pending')

    # ------------------------------------------------------------------
    # Incremental sync — appointments
    # ------------------------------------------------------------------

    def test_incremental_sync_appointment_scheduled_to_cancelled(self):
        """A scheduled appointment cancelled server-side gets synced."""
        appointment = self.env['groundcam.appointment'].create({
            'groundcam_id': 'apt_to_cancel',
            'ticket_id': self.test_ticket.id,
            'status': 'scheduled',
        })

        server_payload = {
            'id': 'apt_to_cancel',
            'status': 'cancelled',
            'cancellationReason': 'Client no-show',
            'cancelledBy': 'agent@example.com',
            'cancelledAt': '2026-05-15T15:00:00Z',
            'updatedAt': '2026-05-15T15:00:00Z',
        }
        list_payload = {
            'appointments': [server_payload],
            'pagination': {'hasMore': False},
        }
        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.'
            'GroundCamAPI.list_appointments',
            return_value=list_payload,
        ):
            self.env['groundcam.appointment']._cron_incremental_sync()

        appointment.invalidate_recordset()
        self.assertEqual(appointment.status, 'cancelled')
        self.assertEqual(appointment.cancellation_reason, 'Client no-show')

    # ------------------------------------------------------------------
    # Pre-flight credits (independent of reconcile)
    # ------------------------------------------------------------------

    def test_preflight_credits_blocks_session_creation_when_empty(self):
        """The session wizard refuses to create a session when credits == 0."""
        wizard = self.env['groundcam.create.session'].create({
            'ticket_id': self.test_ticket.id,
            'mode': 'camera',
            'agent_id': self.gc_agent.id,
            'client_phone': '+33612345678',
        })

        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.get_credits',
            return_value={'available': 0},
        ), patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.create_session',
        ) as mock_create:
            with self.assertRaises(UserError):
                wizard.action_start_session()
            mock_create.assert_not_called()

    def test_preflight_credits_refreshes_cached_value(self):
        """The pre-flight check updates the cached credits ICP value."""
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('groundcam.credits_available', '999')

        wizard = self.env['groundcam.create.session'].create({
            'ticket_id': self.test_ticket.id,
            'mode': 'camera',
            'agent_id': self.gc_agent.id,
            'client_phone': '+33612345678',
        })

        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.get_credits',
            return_value={'available': 42},
        ), patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.create_session',
            return_value={'id': 'sess_credit_ok', 'status': 'pending'},
        ):
            wizard.action_start_session()

        self.assertEqual(
            ICP.get_param('groundcam.credits_available'), '42')

    def test_preflight_credits_falls_through_on_error(self):
        """A failing credits check does not block the session creation."""
        wizard = self.env['groundcam.create.session'].create({
            'ticket_id': self.test_ticket.id,
            'mode': 'camera',
            'agent_id': self.gc_agent.id,
            'client_phone': '+33612345678',
        })

        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.get_credits',
            side_effect=UserError('Network down'),
        ), patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.create_session',
            return_value={'id': 'sess_credit_fallthrough', 'status': 'pending'},
        ) as mock_create:
            wizard.action_start_session()
            mock_create.assert_called_once()
