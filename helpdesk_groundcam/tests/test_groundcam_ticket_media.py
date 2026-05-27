from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import GroundCamCommon


@tagged('-at_install', 'post_install')
class TestGroundCamTicketMedia(GroundCamCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.session_a = cls.env['groundcam.session'].create({
            'groundcam_id': 'sess_media_a',
            'ticket_id': cls.test_ticket.id,
            'status': 'completed',
        })
        cls.session_b = cls.env['groundcam.session'].create({
            'groundcam_id': 'sess_media_b',
            'ticket_id': cls.test_ticket.id,
            'status': 'completed',
        })

    def _make_media(self, session, gc_id, media_type='screenshot'):
        return self.env['groundcam.session.media'].create({
            'session_id': session.id,
            'groundcam_id': gc_id,
            'media_type': media_type,
        })

    def test_media_aggregated_on_ticket(self):
        """All media from every session of the ticket appear in groundcam_media_ids."""
        for i in range(3):
            self._make_media(self.session_a, f'a_{i}')
            self._make_media(self.session_b, f'b_{i}', media_type='recording')

        self.test_ticket.invalidate_recordset()
        self.assertEqual(len(self.test_ticket.groundcam_media_ids), 6)
        self.assertEqual(self.test_ticket.groundcam_media_count, 6)

    def test_media_count_is_zero_without_media(self):
        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_media_count, 0)
        self.assertFalse(self.test_ticket.groundcam_media_ids)

    def test_session_delete_cascades_media(self):
        """Deleting a session cascades on its media; ticket count reflects it."""
        self._make_media(self.session_a, 'a_del_1')
        self._make_media(self.session_a, 'a_del_2')
        self._make_media(self.session_b, 'b_kept_1')

        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_media_count, 3)

        self.session_a.unlink()
        self.test_ticket.invalidate_recordset()
        self.assertEqual(self.test_ticket.groundcam_media_count, 1)

    def test_action_view_groundcam_media(self):
        """Stat button action targets the ticket-scoped list view."""
        self._make_media(self.session_a, 'a_act_1')
        action = self.test_ticket.action_view_groundcam_media()
        self.assertEqual(action['res_model'], 'groundcam.session.media')
        self.assertEqual(action['domain'], [('ticket_id', '=', self.test_ticket.id)])
        list_view_id = self.env.ref(
            'helpdesk_groundcam.view_groundcam_session_media_ticket_list').id
        self.assertIn(list_view_id, [v[0] for v in action['views']])

    def test_ir_rule_blocks_other_team_user(self):
        """A helpdesk user not following a private-team ticket cannot read its media."""
        other_team = self.env['helpdesk.team'].sudo().create({
            'name': 'Private Team',
            'use_groundcam': True,
            'privacy_visibility': 'invited_internal',
        })
        other_ticket = self.env['helpdesk.ticket'].sudo().create({
            'name': 'Private ticket',
            'team_id': other_team.id,
            'partner_id': self.partner.id,
        })
        # Remove auto-followers so helpdesk_user is definitively not invited
        other_ticket.sudo().message_unsubscribe(
            partner_ids=other_ticket.message_partner_ids.ids)
        other_session = self.env['groundcam.session'].sudo().create({
            'groundcam_id': 'sess_private',
            'ticket_id': other_ticket.id,
            'status': 'completed',
        })
        private_media = self._make_media(other_session, 'private_media_1')

        # helpdesk_user must not be able to read the media of a private ticket
        media_as_user = self.env['groundcam.session.media'].with_user(
            self.helpdesk_user)
        self.assertFalse(media_as_user.search([('id', '=', private_media.id)]))
        with self.assertRaises(AccessError):
            media_as_user.browse(private_media.id).read(['media_type'])
