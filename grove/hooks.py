app_name = "grove"
app_title = "Grove"
app_publisher = "developers@frappe.io"
app_description = "An Inference Platform"
app_email = "developers@frappe.io"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "grove",
# 		"logo": "/assets/grove/logo.png",
# 		"title": "Grove",
# 		"route": "/grove",
# 		"has_permission": "grove.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/grove/css/grove.css"
# app_include_js = "/assets/grove/js/grove.js"

# include js, css files in header of web template
# web_include_css = "/assets/grove/css/grove.css"
# web_include_js = "/assets/grove/js/grove.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "grove/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "grove/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "grove.utils.jinja_methods",
# 	"filters": "grove.utils.jinja_filters"
# }

# Fixtures
# --------
# The Grove Control role ships with the app: the control client's User links to it, so it has
# to exist on every site. It carries exactly what grove.api touches — read on Model and Usage
# Record, read/write/create on Grove User, read/create on Grove API Key — so those endpoints run
# under permission checks instead of around them, and reach nothing else.
# Grove User is the role every registered login carries: no desk access and no perms, an identity
# marker to scope self-serve access to later, not a way into anything now.
# Re-imported on migrate (frappe.utils.fixtures.sync_fixtures), so Grove owns desk_access.
# `frappe` is the provider our own engines serve under, and every Model defaults to it, so it has to
# exist before the first Model is inserted. Filtered by name: a site's own third-party providers carry
# credentials and must never be exported into the app.
fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Grove Control", "Grove User"]]]},
	{"dt": "Model Provider", "filters": [["name", "in", ["frappe"]]]},
]

# Installation
# ------------

# before_install = "grove.install.before_install"
# after_install = "grove.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "grove.uninstall.before_uninstall"
# after_uninstall = "grove.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "grove.utils.before_app_install"
# after_app_install = "grove.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "grove.utils.before_app_uninstall"
# after_app_uninstall = "grove.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "grove.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "grove.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		# The only automatic projection there is — nothing in a doctype hook, a provision or a pod
		# lifecycle pushes inline. Hash-gated: each box is pushed only what it does not already
		# hold, and a box that lost its store lost its hashes with it, so the same tick is also
		# the full repair. An overrun tick skips on the advisory lock rather than stacking.
		"*/1 * * * *": ["grove.pathway_sync.sync_projection"],
		"*/2 * * * *": [
			"grove.usage_pull.pull_all",
			"grove.cloud_provider.reconcile.sync_all",
		],
		# Daily: reactivate rate_limited keys whose current-month usage is back
		# under budget (month rollover / raised budget). Over-budget keys stay
		# blocked for the rest of the month — the monthly cap is hard.
		"0 0 * * *": [
			"grove.usage_pull.reactivate_rate_limited",
			# Renews the fleet wildcard when certbot says it is due (inside 30 days of expiry)
			# and pushes it to every Active proxy only if the certificate actually changed.
			"grove.tls.renew_fleet_certificate",
		],
	},
}

# Testing
# -------

# before_tests = "grove.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "grove.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "grove.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "grove.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["grove.utils.before_request"]
# after_request = ["grove.utils.after_request"]

# Job Events
# ----------
# before_job = ["grove.utils.before_job"]
# after_job = ["grove.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"grove.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# Pathway Sync is a log: the scheduled run writes one doc every two minutes whether or not
# anything moved, each with a row per box and the payload that box was sent. Registering it here
# is what lets Log Settings clear it, and what puts it in that form for an operator to retune.
default_log_clearing_doctypes = {
	"Pathway Sync": 60,  # days to retain sync runs
}

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
