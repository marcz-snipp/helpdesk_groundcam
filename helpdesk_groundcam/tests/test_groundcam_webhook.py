import hashlib
import hmac
import json
import time

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import GroundCamCommon


@tagged('-at_install', 'post_install')
class TestGroundCamWebhook(HttpCase, GroundCamCommon):

    @classmethod
    def setUpClass(cls):
        # HttpCase and GroundCamCommon both call super().setUpClass()
        super().setUpClass()

    def _make_signature(self, payload_bytes, timestamp, secret='test_webhook_secret_abc'):
        """Generate a valid HMAC signature for webhook testing."""
        signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
        sig = hmac.new(
            secret.encode('utf-8'),
            signed_payload.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={sig}"

    def _send_webhook(self, event_type, data, secret='test_webhook_secret_abc'):
        """Send a simulated webhook request."""
        payload = json.dumps({'data': data}).encode('utf-8')
        timestamp = str(int(time.time()))
        signature = self._make_signature(payload, timestamp, secret)

        return self.url_open(
            '/groundcam/webhook',
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'X-GroundCam-Signature': signature,
                'X-GroundCam-Timestamp': timestamp,
                'X-GroundCam-Event': event_type,
            },
        )

    def test_webhook_session_completed(self):
        """Test that session.completed webhook updates the session."""
        # Create a session record first
        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_webhook_1',
            'ticket_id': self.test_ticket.id,
            'status': 'active',
        })

        response = self._send_webhook('session.completed', {
            'id': 'sess_webhook_1',
            'status': 'completed',
            'duration': 300,
            'agentDuration': 300,
            'clientDuration': 280,
            'closureTypeName': 'Resolved',
            'completedAt': '2026-03-18T15:00:00Z',
            'notes': 'Replaced motherboard',
            'sessionTags': ['hardware', 'resolved'],
        })

        self.assertEqual(response.status_code, 200)

        session.invalidate_recordset()
        self.assertEqual(session.status, 'completed')
        self.assertEqual(session.duration, 300)
        self.assertEqual(session.notes, 'Replaced motherboard')
        tag_names = session.tag_ids.mapped('name')
        self.assertIn('hardware', tag_names)
        self.assertIn('resolved', tag_names)

    def test_webhook_session_rated(self):
        """Test that session.rated webhook captures rating and comment."""
        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_webhook_rated',
            'ticket_id': self.test_ticket.id,
            'status': 'completed',
        })

        response = self._send_webhook('session.rated', {
            'id': 'sess_webhook_rated',
            'status': 'completed',
            'clientRating': 5,
            'clientComment': 'Great support!',
            'ratedAt': '2026-03-18T15:05:00Z',
        })

        self.assertEqual(response.status_code, 200)
        session.invalidate_recordset()
        self.assertEqual(session.client_rating, 5)
        self.assertEqual(session.client_comment, 'Great support!')

    def test_webhook_session_active(self):
        """Test that session.active webhook updates the session."""
        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_webhook_2',
            'ticket_id': self.test_ticket.id,
            'status': 'pending',
        })

        response = self._send_webhook('session.active', {
            'id': 'sess_webhook_2',
            'status': 'active',
            'startedAt': '2026-03-18T14:00:00Z',
        })

        self.assertEqual(response.status_code, 200)
        session.invalidate_recordset()
        self.assertEqual(session.status, 'active')
        self.assertTrue(session.started_at)

    def test_webhook_invalid_signature(self):
        """Test that invalid signature returns 401."""
        response = self._send_webhook(
            'session.completed',
            {'id': 'sess_bad_sig'},
            secret='wrong_secret',
        )
        self.assertEqual(response.status_code, 401)

    def test_webhook_creates_session_from_crm_id(self):
        """Test that webhook creates a session if crmId matches a ticket."""
        ticket_ref = self.test_ticket.ticket_ref

        response = self._send_webhook('session.created', {
            'id': 'sess_new_from_webhook',
            'status': 'pending',
            'crmId': ticket_ref,
            'agentId': 'agent_1',
            'agentName': 'External Agent',
            'clientPhone': '+33699887766',
            'clientName': 'External Client',
        })

        self.assertEqual(response.status_code, 200)

        session = self.env['groundcam.session'].search([
            ('groundcam_id', '=', 'sess_new_from_webhook')])
        self.assertEqual(len(session), 1)
        self.assertEqual(session.ticket_id, self.test_ticket)

    def test_webhook_unknown_session_no_ticket(self):
        """Test that webhook with unknown session and no matching ticket returns 200."""
        response = self._send_webhook('session.completed', {
            'id': 'sess_unknown',
            'status': 'completed',
            'crmId': 'NONEXISTENT-999',
        })
        # Should still return 200 (acknowledge) even if no ticket found
        self.assertEqual(response.status_code, 200)

    def test_webhook_session_expired(self):
        """Test that session.expired webhook captures expiredAt."""
        session = self.env['groundcam.session'].create({
            'groundcam_id': 'sess_webhook_expired',
            'ticket_id': self.test_ticket.id,
            'status': 'pending',
        })

        response = self._send_webhook('session.expired', {
            'id': 'sess_webhook_expired',
            'status': 'expired',
            'expiredAt': '2026-03-18T16:00:00Z',
        })

        self.assertEqual(response.status_code, 200)
        session.invalidate_recordset()
        self.assertEqual(session.status, 'expired')
        self.assertTrue(session.ended_at)

    def test_webhook_session_deleted(self):
        """Test that session.deleted webhook posts enriched chatter message."""
        self.env['groundcam.session'].create({
            'groundcam_id': 'sess_webhook_deleted',
            'ticket_id': self.test_ticket.id,
            'status': 'completed',
        })

        response = self._send_webhook('session.deleted', {
            'id': 'sess_webhook_deleted',
            'deletedBy': 'agent@example.com',
            'deletedAt': '2026-03-22T10:30:00.000Z',
        })
        self.assertEqual(response.status_code, 200)

        # Verify chatter message includes deletedBy and deletedAt
        last_message = self.test_ticket.message_ids[0]
        self.assertIn('agent@example.com', last_message.body)
        self.assertIn('22/03/2026 10:30', last_message.body)

    def test_webhook_appointment_cancelled(self):
        """Test that appointment.cancelled captures cancellationReason."""
        appointment = self.env['groundcam.appointment'].create({
            'groundcam_id': 'apt_webhook_cancel',
            'ticket_id': self.test_ticket.id,
            'status': 'scheduled',
        })

        response = self._send_webhook('appointment.cancelled', {
            'id': 'apt_webhook_cancel',
            'status': 'cancelled',
            'cancellationReason': 'Client requested reschedule',
        })

        self.assertEqual(response.status_code, 200)
        appointment.invalidate_recordset()
        self.assertEqual(appointment.status, 'cancelled')
        self.assertEqual(
            appointment.cancellation_reason, 'Client requested reschedule')

    def test_webhook_test_event(self):
        """Test that webhook.test event is handled gracefully."""
        response = self._send_webhook('webhook.test', {})
        self.assertEqual(response.status_code, 200)
