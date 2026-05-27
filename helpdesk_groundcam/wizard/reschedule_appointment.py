from datetime import timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.groundcam_api import GroundCamAPI


class GroundcamRescheduleAppointment(models.TransientModel):
    _name = 'groundcam.reschedule.appointment'
    _description = 'Reschedule GroundCam Appointment'

    appointment_id = fields.Many2one(
        'groundcam.appointment', string='Appointment',
        required=True, readonly=True)
    scheduled_at = fields.Datetime('New scheduled time', required=True)
    duration_minutes = fields.Selection([
        ('5', '5'),
        ('10', '10'),
        ('15', '15'),
        ('20', '20'),
    ], string='Duration (minutes)', required=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        appt_id = res.get('appointment_id') or self.env.context.get('default_appointment_id')
        if appt_id:
            appointment = self.env['groundcam.appointment'].browse(appt_id)
            if appointment.exists():
                if 'scheduled_at' in fields_list and appointment.scheduled_at:
                    res['scheduled_at'] = appointment.scheduled_at
                if 'duration_minutes' in fields_list and appointment.duration_minutes:
                    res['duration_minutes'] = str(appointment.duration_minutes)
        return res

    def action_reschedule(self):
        self.ensure_one()
        if self.scheduled_at.minute % 5 != 0:
            raise UserError(_(
                'Scheduled time must be aligned to a 5-minute slot.'))
        scheduled_utc = self.scheduled_at.replace(tzinfo=timezone.utc)
        api = GroundCamAPI(self.env)
        api.update_appointment(
            self.appointment_id.groundcam_id,
            scheduledAt=scheduled_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            durationMinutes=int(self.duration_minutes),
        )
        return {'type': 'ir.actions.act_window_close'}
