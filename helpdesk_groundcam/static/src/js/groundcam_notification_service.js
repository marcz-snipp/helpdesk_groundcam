/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";

export const groundcamNotificationService = {
    dependencies: ["bus_service", "notification", "action"],

    start(env, { bus_service, notification, action }) {
        bus_service.subscribe("groundcam/updated", (payload) => {
            notification.add(
                _t("%(message)s", { message: payload.message }),
                {
                    title: _t("GroundCam"),
                    type: "info",
                    sticky: false,
                    buttons: [{
                        name: _t("Refresh"),
                        primary: true,
                        onClick: () => {
                            action.doAction("reload");
                        },
                    }],
                }
            );
        });
        bus_service.start();
    },
};

registry.category("services").add("groundcam_notification", groundcamNotificationService);
