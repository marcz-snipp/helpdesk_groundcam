from odoo.tests import tagged

from .common import GroundCamCommon


@tagged('-at_install', 'post_install')
class TestGroundCamAppointmentVisibility(GroundCamCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def _make_appointment(self, **kwargs):
        defaults = {
            'groundcam_id': 'apt_vis_test',
            'ticket_id': self.test_ticket.id,
            'status': 'scheduled',
            'agent_groundcam_id': self.gc_agent.groundcam_id,
            'agent_name': self.gc_agent.display_name_gc,
        }
        defaults.update(kwargs)
        return self.env['groundcam.appointment'].create(defaults)

    def test_pending_appointment_counts(self):
        """Appointment without lobby_id is counted in the badge."""
        self._make_appointment(groundcam_id='apt_pending_1')

        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_appointment_count, 1)

    def test_spawned_appointment_does_not_count(self):
        """Appointment with lobby_id set is excluded from the badge count."""
        self._make_appointment(
            groundcam_id='apt_spawned_1',
            status='lobby_created',
            lobby_id='sess_x',
        )

        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_appointment_count, 0)

    def test_count_updates_when_lobby_id_set(self):
        """Count decreases from 1 to 0 once lobby_id is set on the appointment."""
        appointment = self._make_appointment(groundcam_id='apt_transition_1')

        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_appointment_count, 1)

        appointment.write({
            'lobby_id': 'sess_y',
            'status': 'lobby_created',
        })

        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_appointment_count, 0)

    def test_completed_appointment_archived_invisible(self):
        """Completed (archived) appointment is invisible regardless of lobby_id.

        Regression guard for the `write` override in
        groundcam_appointment.py that archives terminal-state records.
        """
        appointment = self._make_appointment(
            groundcam_id='apt_completed_1',
            status='lobby_created',
            lobby_id='sess_z',
        )
        appointment.write({'status': 'completed'})

        self.assertFalse(appointment.active)
        # Default search excludes archived records
        visible_count = self.env['groundcam.appointment'].search_count(
            [('ticket_id', '=', self.test_ticket.id)])
        self.assertEqual(visible_count, 0)

    def test_one2many_remains_complete_programmatically(self):
        """`ticket.groundcam_appointment_ids` returns all appointments.

        The UI filter (domain on the XML <field>) does not alter the
        underlying One2many — webhooks, sync services and crons must
        keep seeing every appointment.
        """
        pending = self._make_appointment(groundcam_id='apt_o2m_pending')
        spawned = self._make_appointment(
            groundcam_id='apt_o2m_spawned',
            status='lobby_created',
            lobby_id='sess_o2m',
        )

        self.test_ticket.invalidate_recordset()
        all_ids = self.test_ticket.groundcam_appointment_ids.ids
        self.assertIn(pending.id, all_ids)
        self.assertIn(spawned.id, all_ids)
