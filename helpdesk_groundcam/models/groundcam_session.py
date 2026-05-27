import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)

RECONCILE_AGE_MINUTES = 15
RECONCILE_BATCH_SIZE = 50


class GroundcamSession(models.Model):
    _name = 'groundcam.session'
    _description = 'GroundCam Video Session'
    _order = 'create_date desc'
    _rec_name = 'groundcam_id'

    # GroundCam remote fields
    groundcam_id = fields.Char(
        'GroundCam Session ID', required=True, index=True, readonly=True)
    status = fields.Selection([
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
    ], string='Status', default='pending', readonly=True, tracking=True)
    client_url = fields.Char('Client URL', readonly=True)
    agent_url = fields.Char('Agent URL', readonly=True)
    agent_groundcam_id = fields.Char('GroundCam Agent ID', readonly=True)
    agent_name = fields.Char('Agent Name', readonly=True)
    client_phone = fields.Char('Client Phone', readonly=True)
    client_name = fields.Char('Client Name', readonly=True)
    client_email = fields.Char('Client Email', readonly=True)
    mode = fields.Selection([
        ('camera', 'Camera'),
        ('screen', 'Screen'),
    ], string='Mode', default='camera', readonly=True)
    duration = fields.Integer('Duration (seconds)', readonly=True)
    agent_duration = fields.Integer('Agent Duration (s)', readonly=True)
    client_duration = fields.Integer('Client Duration (s)', readonly=True)
    client_rating = fields.Integer('Client Rating (1-5)', readonly=True)
    client_comment = fields.Char('Client Comment', readonly=True)
    closure_type_name = fields.Char('Closure Type', readonly=True)
    closure_type_id = fields.Many2one(
        'groundcam.closure.type', string='Closure Type (Synced)',
        readonly=True, ondelete='set null', index=True,
        help='Resolved from closure_type_name. Empty if the type has not been synced yet.')
    notes = fields.Text('Session Notes')
    screenshots_count = fields.Integer('Screenshots Count', readonly=True)
    recordings_count = fields.Integer('Recordings Count', readonly=True)
    tag_ids = fields.Many2many(
        'groundcam.session.tag', string='Tags', readonly=True)
    admitted_at = fields.Datetime('Admitted At', readonly=True)
    expires_at = fields.Datetime('Expires At', readonly=True)
    started_at = fields.Datetime('Started At', readonly=True)
    ended_at = fields.Datetime('Ended At', readonly=True)
    updated_at = fields.Datetime('Last Updated (GroundCam)', readonly=True)
    crm_id = fields.Char('CRM ID', readonly=True)
    source = fields.Selection([
        ('dashboard', 'Dashboard'),
        ('api', 'API'),
        ('scheduled', 'Scheduled'),
    ], string='Source', readonly=True)

    # Odoo relational fields
    ticket_id = fields.Many2one(
        'helpdesk.ticket', string='Helpdesk Ticket',
        required=True, ondelete='cascade', index=True)
    user_id = fields.Many2one(
        'res.users', string='Odoo User', readonly=True,
        help='The Odoo user who initiated this session')
    appointment_id = fields.Many2one(
        'groundcam.appointment', string='Source Appointment',
        readonly=True, ondelete='set null', index=True)
    media_ids = fields.One2many(
        'groundcam.session.media', 'session_id', string='Media')

    # Computed
    duration_display = fields.Float(
        'Duration', compute='_compute_duration_display',
        help='Duration in hours:minutes format')
    is_active_session = fields.Boolean(
        compute='_compute_is_active_session')

    _sql_constraints = [
        ('groundcam_id_unique', 'unique(groundcam_id)',
         'A session with this GroundCam ID already exists.'),
    ]

    @api.depends('duration')
    def _compute_duration_display(self):
        for record in self:
            record.duration_display = record.duration / 3600.0 if record.duration else 0.0

    @api.depends('status')
    def _compute_is_active_session(self):
        for record in self:
            record.is_active_session = record.status in ('pending', 'active')

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
        """Pull every session modified since the last cursor via updatedAfter.

        Catches webhooks lost beyond GroundCam's 15 min retry window and
        mutations made directly on the GroundCam dashboard.
        """
        from ..services.groundcam_sync import run_incremental_sync_sessions
        run_incremental_sync_sessions(self.env)

    def action_resend_sms(self):
        self.ensure_one()
        if self.status != 'pending':
            raise UserError(_(
                'Can only resend SMS for pending sessions.'))
        api = GroundCamAPI(self.env)
        api.resend_sms(self.groundcam_id)
        if self.ticket_id:
            self.ticket_id.message_post(
                body=self.env._(
                    'SMS invitation resent for session %(session)s.',
                    session=self.groundcam_id,
                ),
                subtype_xmlid='mail.mt_note',
            )

    def action_cancel_session(self):
        self.ensure_one()
        if self.status != 'pending':
            raise UserError(_(
                'Can only cancel pending sessions. '
                'Use "End session" for active ones.'))
        api = GroundCamAPI(self.env)
        api.cancel_session(self.groundcam_id)
        self.status = 'cancelled'

    def action_open_end_wizard(self):
        self.ensure_one()
        if self.status not in ('pending', 'active'):
            raise UserError(_(
                'Can only end sessions that are pending or active.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('End Session'),
            'res_model': 'groundcam.end.session',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_session_id': self.id,
                'dialog_size': 'medium',
            },
        }
