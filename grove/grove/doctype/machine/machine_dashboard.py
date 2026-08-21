def get_data():
	"""Connections: every Ansible Play that ran against the box itself — bootstrap, scan_gpus,
	grow_root. A role server's plays hang off that server, not here. Dynamic link, so
	server_type is pinned alongside server."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Machine", "server_type"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play"]}],
	}
