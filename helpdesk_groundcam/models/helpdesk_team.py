from odoo import fields, models


class HelpdeskTeam(models.Model):
    _inherit = 'helpdesk.team'

    use_groundcam = fields.Boolean('GroundCam Video Support')
