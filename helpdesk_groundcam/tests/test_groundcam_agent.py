from unittest.mock import patch

from odoo.tests import tagged

from .common import GroundCamCommon


@tagged('-at_install', 'post_install')
class TestGroundCamAgentSync(GroundCamCommon):

    def test_sync_disables_agents_missing_from_api(self):
        """Agents present locally but missing from API response are disabled."""
        Agent = self.env['groundcam.agent']

        kept = Agent.create({
            'groundcam_id': 'agent_kept',
            'display_name_gc': 'Kept Agent',
            'email': 'kept@example.com',
            'role': 'agent',
            'disabled': False,
        })
        vanished = Agent.create({
            'groundcam_id': 'agent_vanished',
            'display_name_gc': 'Jean Prod',
            'email': 'jean@example.com',
            'role': 'agent',
            'disabled': False,
        })

        api_response = {
            'agents': [
                {
                    'id': 'agent_kept',
                    'email': 'kept@example.com',
                    'displayName': 'Kept Agent',
                    'role': 'agent',
                    'disabled': False,
                },
                # agent_vanished is intentionally absent
            ],
        }

        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.get_agents',
            return_value=api_response,
        ):
            Agent._sync_from_api()

        kept.invalidate_recordset()
        vanished.invalidate_recordset()
        self.assertFalse(kept.disabled)
        self.assertTrue(vanished.disabled)

    def test_sync_reenables_agent_returning_to_api(self):
        """A previously-disabled agent reappearing in the API gets re-enabled."""
        Agent = self.env['groundcam.agent']

        agent = Agent.create({
            'groundcam_id': 'agent_resurrected',
            'display_name_gc': 'Resurrected',
            'email': 'res@example.com',
            'role': 'agent',
            'disabled': True,
        })

        api_response = {
            'agents': [
                {
                    'id': 'agent_resurrected',
                    'email': 'res@example.com',
                    'displayName': 'Resurrected',
                    'role': 'agent',
                    'disabled': False,
                },
            ],
        }

        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.get_agents',
            return_value=api_response,
        ):
            Agent._sync_from_api()

        agent.invalidate_recordset()
        self.assertFalse(agent.disabled)

    def test_sync_empty_api_disables_all_local_agents(self):
        """If the API returns no agents, all local enabled agents are disabled."""
        Agent = self.env['groundcam.agent']

        a1 = Agent.create({
            'groundcam_id': 'agent_a',
            'display_name_gc': 'A',
            'role': 'agent',
            'disabled': False,
        })
        a2 = Agent.create({
            'groundcam_id': 'agent_b',
            'display_name_gc': 'B',
            'role': 'agent',
            'disabled': False,
        })

        with patch(
            'odoo.addons.helpdesk_groundcam.services.groundcam_api.GroundCamAPI.get_agents',
            return_value={'agents': []},
        ):
            Agent._sync_from_api()

        a1.invalidate_recordset()
        a2.invalidate_recordset()
        self.assertTrue(a1.disabled)
        self.assertTrue(a2.disabled)
