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
            "grove.pathway_sync.sync_projection",
        ],
        # every 2 minutes
		"*/2 * * * *": [
			"grove.usage_pull.pull_all",
			"grove.cloud_provider.reconcile.sync_all",
		],
	},
    "hourly_long": [
		# Unblocks rate_limited keys back under budget (month rollover / raised budget).
		# Over-budget keys stay blocked: the monthly cap is hard.
		"grove.usage_pull.reactivate_rate_limited",
		# Only when certbot says it is due, and pushed only if the certificate changed.
		"grove.tls.renew_fleet_certificate",
		"grove.cloud_provider.schedule.run_due_pods",
	],
}

export_python_type_annotations = True
require_type_annotated_api_methods = True

default_log_clearing_doctypes = {
	"Pathway Sync": 60,
	"Pod Activity": 90,
}
