app_name = "grove"
app_title = "Grove"
app_publisher = "developers@frappe.io"
app_description = "An Inference Platform"
app_email = "developers@frappe.io"
app_license = "mit"

# The GPU table, drawn the same way on the Machine that owns the cards and the Inference Server
# that serves from them.
app_include_js = "/assets/grove/js/gpu_table.js"

# The "open" filter behind every connections badge
notification_config = "grove.notifications.get_notification_config"

fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Grove Control", "Grove User"]]]},
	{"dt": "Model Provider", "filters": [["is_self_hosted", "=", 1]]},  # TODO: move to after migrate/after install
]

scheduler_events = {
	"cron": {
        # every minute
		"*/1 * * * *": [
			# Ahead of the push, so the same minute's routes carry a pricing that fired today.
			"grove.grove.doctype.model_pricing.model_pricing.enable_due",
            "grove.pathway.projection.sync_projection",
			"grove.pathway.usage.pull_all",
        ],
		"*/2 * * * *": [
			"grove.cloud_provider.reconcile.sync_all",
		],
	},
    "hourly_long": [
		# Only when certbot says it is due, and pushed only if the certificate changed.
		"grove.tls.renew_fleet_certificate",
		"grove.cloud_provider.schedule.run_due_pods",
		# Usage a gateway deleted that a pull could not record, landed on the day it was drained.
		"grove.grove.doctype.lost_usage.lost_usage.replay_pending",
	],
	"daily_long": [
		# Every prepaid balance re-priced from the day rows; drift is logged, the join wins.
		"grove.pricing.verify_balances",
	],
}

export_python_type_annotations = True
require_type_annotated_api_methods = True

default_log_clearing_doctypes = {
	"Pathway Sync": 60,
	"Pod Activity": 90,
	"Lost Usage": 90,
}
