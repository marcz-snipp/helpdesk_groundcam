# Helpdesk GroundCam

GroundCam video support integration for Odoo 19 Helpdesk tickets.

Start live video sessions, schedule appointments, capture screenshots and
recordings — all from inside a helpdesk ticket. The customer joins from their
browser (no app to install) via an SMS or email link.

## Features

- Start a video session (Camera or Screen Sharing) from any helpdesk ticket
- Schedule appointments with SMS/email reminders
- Capture and archive screenshots and recordings as ticket attachments
- Real-time updates via signed GroundCam webhooks
- Multi-language customer notifications (EN, FR, DE, ES, IT, PT)
- Per-team activation — only teams with GroundCam enabled expose the buttons
- Incremental sync every 5 minutes as a safety net for missed webhooks

## Requirements

- Odoo 19 Enterprise
- Odoo modules: `helpdesk`, `phone_validation`, `bus`
- Python packages: `requests`, `phonenumbers`
- A GroundCam account with API access — sign up at <https://groundcam.io>

## Installation

1. Copy `helpdesk_groundcam` into your Odoo addons path.
2. Update the apps list and install **Helpdesk GroundCam**.
3. Go to *Settings > Helpdesk > GroundCam Video Support* and paste your API key.
4. Click **Verify API Key**, sync agents, register the webhook.
5. Enable GroundCam on each helpdesk team that should use video support.

Full setup instructions are bundled in the module description on the
[Odoo App Store](https://apps.odoo.com/).

## Support

- Email: <support@groundcam.io>
- Website: <https://groundcam.io>

## License

LGPL-3. See [LICENSE](LICENSE) for the full text.
