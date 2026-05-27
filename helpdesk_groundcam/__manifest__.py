{
    'name': 'Helpdesk GroundCam',
    'version': '19.0.1.3.0',
    'category': 'Services/Helpdesk',
    'summary': 'GroundCam video support integration for Helpdesk tickets',
    'description': """
Helpdesk GroundCam
==================

Integrate GroundCam remote video assistance directly into Odoo 19 Helpdesk tickets.

Features
--------
* Start live video sessions in Camera or Screen Sharing mode from any ticket.
* Schedule video appointments with automatic SMS/email reminders.
* Capture and archive screenshots and recordings as ticket attachments.
* Receive real-time updates (status, ratings, media) through GroundCam webhooks.
* Multi-language SMS / email notifications (English, French, German, Spanish,
  Italian, Portuguese).
* Per-team activation: only teams with GroundCam enabled expose the buttons.
* Automatic incremental sync (every 5 minutes) as a safety net for any missed
  webhook delivery.

Requires a GroundCam account with API access. Sign up at https://groundcam.io.
    """,
    'depends': ['helpdesk', 'phone_validation', 'bus'],
    'data': [
        'security/groundcam_security.xml',
        'security/ir.model.access.csv',
        'data/groundcam_data.xml',
        'data/groundcam_cron.xml',
        'views/groundcam_session_views.xml',
        'views/groundcam_appointment_views.xml',
        'views/helpdesk_ticket_views.xml',
        'views/helpdesk_team_views.xml',
        'views/res_config_settings_views.xml',
        'views/groundcam_menus.xml',
        'wizard/create_session_views.xml',
        'wizard/create_appointment_views.xml',
        'wizard/end_session_views.xml',
        'wizard/reschedule_appointment_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'helpdesk_groundcam/static/src/js/**/*',
        ],
    },
    'images': ['static/description/banner.png'],
    'auto_install': False,
    'author': 'GroundCam',
    'maintainer': 'GroundCam',
    'website': 'https://groundcam.io',
    'support': 'support@groundcam.io',
    'license': 'LGPL-3',
    'external_dependencies': {
        'python': ['requests', 'phonenumbers'],
    },
}
