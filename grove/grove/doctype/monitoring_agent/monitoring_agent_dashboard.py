def get_data():
	"""Connections: every Ansible Play that ran against this agent's box — its install, each
	config update and each pushed target list. Dynamic link, so server_type is pinned alongside
	server."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Monitoring Agent", "server_type"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play"]}],
	}
