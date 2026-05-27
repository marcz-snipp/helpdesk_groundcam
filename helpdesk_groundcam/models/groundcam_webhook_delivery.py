import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

PURGE_AGE_HOURS = 24


class GroundcamWebhookDelivery(models.Model):
    _name = 'groundcam.webhook.delivery'
    _description = 'GroundCam Webhook Delivery (idempotency tracking)'
    _order = 'create_date desc'
    _rec_name = 'delivery_id'

    delivery_id = fields.Char(
        'GroundCam Delivery ID', required=True, index=True, readonly=True)
    event_type = fields.Char('Event Type', readonly=True)

    _sql_constraints = [
        ('delivery_id_unique', 'unique(delivery_id)',
         'A webhook delivery with this ID was already recorded.'),
    ]

    @api.model
    def _cron_purge_old_deliveries(self):
        """Purge delivery records older than PURGE_AGE_HOURS hours.

        GroundCam's retry deadline is 15 min after creation, so 24h is
        a wide safety margin while keeping the table small.
        """
        cutoff = fields.Datetime.now() - timedelta(hours=PURGE_AGE_HOURS)
        old = self.search([('create_date', '<', cutoff)])
        if old:
            count = len(old)
            old.unlink()
            _logger.info(
                'GroundCam webhook delivery: purged %s old records', count)
