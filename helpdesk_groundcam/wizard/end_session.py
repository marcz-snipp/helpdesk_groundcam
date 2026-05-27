from odoo import _, fields, models
from odoo.exceptions import UserError

from ..services.groundcam_api import GroundCamAPI


class GroundcamEndSession(models.TransientModel):
    _name = 'groundcam.end.session'
    _description = 'End GroundCam Session'

    session_id = fields.Many2one(
        'groundcam.session', string='Session', required=True, readonly=True)
    agent_duration_min = fields.Integer('Agent Duration (min)')
    agent_duration_sec = fields.Integer('Agent Duration (sec)')
    client_duration_min = fields.Integer('Client Duration (min)')
    client_duration_sec = fields.Integer('Client Duration (sec)')
    closure_type_id = fields.Many2one(
        'groundcam.closure.type', string='Closure Type')
    closure_type_require_comment = fields.Boolean(
        related='closure_type_id.require_comment')
    notes = fields.Text('Notes')

    def action_end(self):
        self.ensure_one()
        if self.closure_type_require_comment and not (self.notes or '').strip():
            raise UserError(_(
                'The selected closure type requires notes.'))
        payload = {}
        agent_total = (self.agent_duration_min or 0) * 60 + (self.agent_duration_sec or 0)
        client_total = (self.client_duration_min or 0) * 60 + (self.client_duration_sec or 0)
        if agent_total:
            payload['agentDuration'] = agent_total
        if client_total:
            payload['clientDuration'] = client_total
        if self.closure_type_id:
            payload['closureTypeName'] = self.closure_type_id.name
        if self.notes:
            payload['notes'] = self.notes

        api = GroundCamAPI(self.env)
        api.end_session(self.session_id.groundcam_id, **payload)
        return {'type': 'ir.actions.act_window_close'}
