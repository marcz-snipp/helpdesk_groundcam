import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)

RECONCILE_AGE_MINUTES = 15
RECONCILE_BATCH_SIZE = 50


class GroundcamAppointment(models.Model):
    _name = 'groundcam.appointment'
    _description = 'GroundCam Video Appointment'
    _order = 'scheduled_at desc'
    _rec_name = 'groundcam_id'

    active = fields.Boolean(default=True)
    groundcam_id = fields.Char(
        'GroundCam Appointment ID', required=True, index=True, readonly=True)
    status = fields.Selection([
        ('scheduled', 'Scheduled'),
        ('notified', 'Notified'),
        ('lobby_created', 'Session Created'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='scheduled', readonly=True, tracking=True)
    agent_groundcam_id = fields.Char('GroundCam Agent ID', readonly=True)
    agent_name = fields.Char('Agent Name', readonly=True)
    mode = fields.Selection([
        ('camera', 'Camera'),
        ('screen', 'Screen'),
    ], string='Mode', default='camera', readonly=True)
    client_phone = fields.Char('Client Phone', readonly=True)
    client_name = fields.Char('Client Name', readonly=True)
    client_email = fields.Char('Client Email', readonly=True)
    scheduled_at = fields.Datetime('Scheduled At', readonly=True)
    duration_minutes = fields.Integer('Duration (minutes)', readonly=True)
    notify_by = fields.Selection([
        ('sms', 'SMS'),
        ('email', 'Email'),
        ('both', 'Both'),
    ], string='Notify By', readonly=True)
    notes = fields.Text('Notes')
    lobby_id = fields.Char('Linked Session ID', readonly=True)
    cancellation_reason = fields.Char('Cancellation Reason', readonly=True)
    crm_id = fields.Char('CRM ID', readonly=True)
    organization_id = fields.Char('GroundCam Organization ID', readonly=True)
    created_by = fields.Char('Created By', readonly=True)
    cancelled_by = fields.Char('Cancelled By', readonly=True)
    notified_at = fields.Datetime('Notified At', readonly=True)
    cancelled_at = fields.Datetime('Cancelled At', readonly=True)
    updated_at = fields.Datetime('Last Updated (GroundCam)', readonly=True)

    # Odoo relational fields
    ticket_id = fields.Many2one(
        'helpdesk.ticket', string='Helpdesk Ticket',
        required=True, ondelete='cascade', index=True)
    user_id = fields.Many2one(
        'res.users', string='Odoo User', readonly=True)

    _sql_constraints = [
        ('groundcam_id_unique', 'unique(groundcam_id)',
         'An appointment with this GroundCam ID already exists.'),
    ]

    def write(self, vals):
        res = super().write(vals)
        if vals.get('status') in ('completed', 'cancelled'):
            self.filtered('active').action_archive()
        return res

    @api.model
    def _cron_reconcile_pending(self):
        """Deprecated — replaced by _cron_incremental_sync.

        Kept as a no-op for backward compatibility with any legacy ir.cron
        record that has not yet been deactivated. Safe to remove once the
        migration is fully rolled out.
        """
        return

    @api.model
    def _cron_incremental_sync(self):
        """Pull every appointment modified since the last cursor via updatedAfter."""
        from ..services.groundcam_sync import run_incremental_sync_appointments
        run_incremental_sync_appointments(self.env)

    def action_cancel_appointment(self):
        self.ensure_one()
        if self.status in ('completed', 'cancelled'):
            raise UserError(_(
                'Appointment is already %(status)s.', status=self.status))
        api = GroundCamAPI(self.env)
        api.cancel_appointment(self.groundcam_id)

    def action_open_reschedule_wizard(self):
        self.ensure_one()
        if self.status in ('completed', 'cancelled'):
            raise UserError(_(
                'Cannot reschedule a %(status)s appointment.',
                status=self.status))
        if self.lobby_id:
            raise UserError(_(
                'Cannot reschedule an appointment that already has a session.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reschedule Appointment'),
            'res_model': 'groundcam.reschedule.appointment',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_appointment_id': self.id,
                'dialog_size': 'medium',
            },
        }
