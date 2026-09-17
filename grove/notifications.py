"""What counts as "open" per doctype. Frappe reads this for the badge on every connections panel
(a Network's Machine link shows how many are Active, a Model Deployment's Model Replica link how
many serve, a Pod's Activity link how many calls failed) and for the desk's unread counts; the total stays in the link's count."""


def get_notification_config():
	return {
		"for_doctype": {
			"Machine": {"status": "Active"},
			"Model Replica": {"status": "Active"},
			"Pod Activity": {"outcome": "Failure"},
		}
	}
