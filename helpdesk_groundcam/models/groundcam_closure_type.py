import logging

from odoo import api, fields, models

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)


class GroundcamClosureType(models.Model):
    _name = 'groundcam.closure.type'
    _description = 'GroundCam Closure Type'
    _order = 'name'

    groundcam_id = fields.Char(
        'GroundCam Closure Type ID', required=True, index=True)
    name = fields.Char('Name', required=True, index=True)
    icon = fields.Char('Icon')
    color = fields.Char('Color')
    require_comment = fields.Boolean('Requires Notes', default=False)

    _sql_constraints = [
        ('groundcam_id_unique', 'unique(groundcam_id)',
         'A closure type with this GroundCam ID already exists.'),
    ]

    @api.model
    def _sync_from_api(self):
        """Fetch closure types from GroundCam and upsert local records.

        Returns the number of types synced. Safe to call from anywhere (UI
        button, lazy webhook handler). Caller is responsible for error
        handling if a UserError surfaces.
        """
        api = GroundCamAPI(self.env)
        result = api.get_closure_types()
        items = result.get('closureTypes', []) if isinstance(result, dict) else result
        seen_ids = []
        for item in items or []:
            gc_id = item.get('id')
            if not gc_id:
                continue
            seen_ids.append(gc_id)
            vals = {
                'groundcam_id': gc_id,
                'name': item.get('name') or gc_id,
                'icon': item.get('icon') or '',
                'color': item.get('color') or '',
                'require_comment': bool(item.get('requireComment')),
            }
            existing = self.sudo().search(
                [('groundcam_id', '=', gc_id)], limit=1)
            if existing:
                existing.write(vals)
            else:
                self.sudo().create(vals)
        # Drop orphans (types that no longer exist on GroundCam side)
        if seen_ids:
            self.sudo().search(
                [('groundcam_id', 'not in', seen_ids)]
            ).unlink()
        return len(seen_ids)
