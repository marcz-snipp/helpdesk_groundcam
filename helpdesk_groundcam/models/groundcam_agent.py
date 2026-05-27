import logging
from datetime import datetime, timezone

from odoo import api, fields, models

from ..services.groundcam_api import GroundCamAPI

_logger = logging.getLogger(__name__)


class GroundcamAgent(models.Model):
    _name = 'groundcam.agent'
    _description = 'GroundCam Agent'
    _rec_name = 'display_name_gc'

    groundcam_id = fields.Char(
        'GroundCam Agent ID', required=True, index=True)
    email = fields.Char('Email')
    display_name_gc = fields.Char('Display Name')
    role = fields.Selection([
        ('owner', 'Owner'),
        ('admin', 'Admin'),
        ('agent', 'Agent'),
        ('viewer', 'Viewer'),
    ], string='Role')
    disabled = fields.Boolean('Disabled', default=False)
    joined_at = fields.Datetime('Joined At', readonly=True)
    user_id = fields.Many2one(
        'res.users', string='Linked Odoo User',
        help='Map this GroundCam agent to an Odoo user for auto-selection')

    _sql_constraints = [
        ('groundcam_id_unique', 'unique(groundcam_id)',
         'A GroundCam agent with this ID already exists.'),
    ]

    @api.model
    def _sync_from_api(self):
        """Fetch agents from GroundCam and upsert local records.

        Agents present locally but no longer returned by the API are
        marked as disabled (not deleted) so that historical sessions keep
        their context and the wizard hides them. They will be re-enabled
        automatically if they reappear in a future sync.

        Returns the number of agents synced.
        """
        api_client = GroundCamAPI(self.env)
        result = api_client.get_agents()
        agents_data = result.get('agents', []) if isinstance(result, dict) else result
        seen_ids = set()
        synced = 0
        for agent_data in agents_data or []:
            gc_id = agent_data.get('id')
            if not gc_id:
                continue
            seen_ids.add(gc_id)
            joined_at = False
            if agent_data.get('joinedAt'):
                try:
                    dt = datetime.fromisoformat(
                        agent_data['joinedAt'].replace('Z', '+00:00'))
                    joined_at = dt.astimezone(timezone.utc).replace(tzinfo=None)
                except (ValueError, AttributeError):
                    pass
            vals = {
                'groundcam_id': gc_id,
                'email': agent_data.get('email', ''),
                'display_name_gc': agent_data.get('displayName', ''),
                'role': agent_data.get('role', 'agent'),
                'disabled': agent_data.get('disabled', False),
                'joined_at': joined_at,
            }
            existing = self.sudo().search(
                [('groundcam_id', '=', gc_id)], limit=1)
            if existing:
                existing.write(vals)
            else:
                # Auto-match to Odoo user by email on first creation
                if vals['email']:
                    user = self.env['res.users'].sudo().search(
                        [('email', '=ilike', vals['email'])], limit=1)
                    if user:
                        vals['user_id'] = user.id
                self.sudo().create(vals)
            synced += 1

        # Disable agents that vanished from the API response. Only consider
        # agents currently enabled to avoid useless writes and chatter noise.
        stale = self.sudo().search([
            ('groundcam_id', 'not in', list(seen_ids) or [''],),
            ('disabled', '=', False),
        ])
        if stale:
            stale.write({'disabled': True})
            for agent in stale:
                _logger.info(
                    'GroundCam: agent %s (%s) no longer in org, marked disabled',
                    agent.display_name_gc or agent.groundcam_id,
                    agent.groundcam_id)

        return synced
