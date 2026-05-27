import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_SECONDS = 60


class GroundcamSessionMedia(models.Model):
    _name = 'groundcam.session.media'
    _description = 'GroundCam Session Media'
    _order = 'timestamp desc, id desc'
    _rec_name = 'display_name'

    session_id = fields.Many2one(
        'groundcam.session', string='Session',
        required=True, ondelete='cascade', index=True)
    ticket_id = fields.Many2one(
        'helpdesk.ticket', related='session_id.ticket_id', store=True)
    groundcam_id = fields.Char(
        'GroundCam Media ID', required=True, index=True, readonly=True)
    media_type = fields.Selection([
        ('screenshot', 'Screenshot'),
        ('recording', 'Recording'),
    ], string='Type', required=True, readonly=True)
    timestamp = fields.Datetime('Captured At', readonly=True)
    duration = fields.Integer('Duration (s)', readonly=True)
    size = fields.Integer('Size (bytes)', readonly=True)
    latitude = fields.Float('Latitude', digits=(10, 7), readonly=True)
    longitude = fields.Float('Longitude', digits=(10, 7), readonly=True)
    tags = fields.Char('Tags', readonly=True)
    attachment_id = fields.Many2one(
        'ir.attachment', string='Local Copy',
        readonly=True, ondelete='set null')
    is_saved = fields.Boolean(
        'Saved in Odoo', compute='_compute_is_saved', store=True)
    display_name = fields.Char(
        compute='_compute_display_name', store=True,
        export_string_translation=False)

    _sql_constraints = [
        ('groundcam_media_unique', 'unique(session_id, groundcam_id)',
         'This media is already recorded for this session.'),
    ]

    @api.depends('attachment_id')
    def _compute_is_saved(self):
        for rec in self:
            rec.is_saved = bool(rec.attachment_id)

    @api.depends('media_type', 'timestamp', 'groundcam_id')
    def _compute_display_name(self):
        for rec in self:
            label = (
                _('Screenshot') if rec.media_type == 'screenshot'
                else _('Recording')
            )
            ts = (
                rec.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                if rec.timestamp else rec.groundcam_id
            )
            rec.display_name = f"{label} — {ts}"

    def action_refresh(self):
        """Re-fetch metadata (tags, location, etc.) from GroundCam.

        Triggers a full media sync of the related session — fetches
        screenshots and recordings via the API and upserts each record.
        """
        from ..services.groundcam_sync import sync_session_media
        for session in self.mapped('session_id'):
            sync_session_media(self.env, session)
        return True

    def action_view_online(self):
        """Re-fetch a fresh signed URL and open it in a new browser tab."""
        self.ensure_one()
        url = self._fetch_fresh_url()
        if not url:
            raise UserError(_(
                'Could not retrieve a fresh URL for this media. '
                'It may have been deleted on the GroundCam side.'))
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    def action_save_to_odoo(self):
        """Download the binary and store it as an ir.attachment."""
        Attachment = self.env['ir.attachment'].sudo()
        for rec in self:
            if rec.is_saved:
                continue
            url = rec._fetch_fresh_url()
            if not url:
                raise UserError(_(
                    'Could not retrieve a fresh URL to download this media.'))
            try:
                content = rec._download_binary(url)
            except Exception as e:
                _logger.warning(
                    'GroundCam: failed to download media %s: %s',
                    rec.groundcam_id, e)
                raise UserError(_(
                    'Failed to download the media file: %(error)s',
                    error=str(e),
                ))
            if rec.media_type == 'screenshot':
                mimetype = 'image/jpeg'
                ext = 'jpg'
            else:
                mimetype = 'video/webm'
                ext = 'webm'
            ticket = rec.session_id.ticket_id
            attachment = Attachment.create({
                'name': f"{rec.display_name}.{ext}",
                'type': 'binary',
                'raw': content,
                'mimetype': mimetype,
                'res_model': 'helpdesk.ticket' if ticket else 'groundcam.session',
                'res_id': ticket.id if ticket else rec.session_id.id,
            })
            rec.attachment_id = attachment.id
            if ticket:
                ticket.with_context(lang=ticket.user_id.lang or 'en_US').message_post(
                    body=ticket.env._(
                        'GroundCam media saved to Odoo: %(name)s',
                        name=attachment.name,
                    ),
                    attachment_ids=[attachment.id],
                    subtype_xmlid='mail.mt_note',
                )
        return True

    def _fetch_fresh_url(self):
        """Re-fetch the media URL from GroundCam.

        Screenshot URLs expire after `url_ttl` seconds (3600 by default), so we
        always refresh before opening or downloading. Recording URLs are stable
        but we keep the same flow for consistency.
        """
        self.ensure_one()
        from ..services.groundcam_api import GroundCamAPI
        api = GroundCamAPI(self.env)
        try:
            if self.media_type == 'screenshot':
                result = api.get_screenshots(self.session_id.groundcam_id)
                items = result.get('screenshots', [])
            else:
                result = api.get_recordings(self.session_id.groundcam_id)
                items = result.get('recordings', [])
        except Exception:
            _logger.warning(
                'GroundCam: failed to refresh URL for media %s',
                self.groundcam_id, exc_info=True)
            return False
        for item in items:
            if item.get('id') == self.groundcam_id:
                return item.get('url')
        return False

    @staticmethod
    def _download_binary(url):
        response = requests.get(
            url, timeout=DOWNLOAD_TIMEOUT_SECONDS, allow_redirects=True)
        response.raise_for_status()
        return response.content
