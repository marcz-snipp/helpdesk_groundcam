import logging

from odoo import api, fields, models

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)


class GroundcamSessionTag(models.Model):
    _name = 'groundcam.session.tag'
    _description = 'GroundCam Session Tag'
    _order = 'name'

    name = fields.Char('Tag', required=True, index=True)
    color = fields.Integer('Color')

    _sql_constraints = [
        ('name_unique', 'unique(name)',
         'A tag with this name already exists.'),
    ]

    @api.model
    def _sync_from_api(self):
        """Fetch tags from GroundCam and create local records.

        Tags are identified by name only (no remote id). Existing tags are
        kept untouched; only missing ones are created. We never delete tags
        locally — they may be linked to historical sessions.
        """
        api_client = GroundCamAPI(self.env)
        result = api_client.get_tags()
        names = result.get('tags', []) if isinstance(result, dict) else result
        synced = 0
        for raw_name in names or []:
            name = (raw_name or '').strip()
            if not name:
                continue
            existing = self.sudo().search([('name', '=', name)], limit=1)
            if not existing:
                self.sudo().create({'name': name})
                synced += 1
        return synced
