def get_data():
	"""Connections: this box's cards, and every Ansible Play that ran against the box itself —
	bootstrap, scan_gpus, grow_root. A role server's plays hang off that server, not here.

	Ansible Play is a dynamic link, so server_type is pinned alongside server; GPU is a plain one,
	which is what non_standard_fieldnames is for."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Machine", "server_type"]},
		"non_standard_fieldnames": {"GPU": "machine"},
		"transactions": [
			{"label": "Hardware", "items": ["GPU"]},
			{"label": "Automation", "items": ["Ansible Play"]},
		],
	}
