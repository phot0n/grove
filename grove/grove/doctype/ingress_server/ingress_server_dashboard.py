def get_data():
	"""Connections: every Ansible Play that ran against this ingress. Dynamic link, so
	server_type is pinned alongside server."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Ingress Server", "server_type"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play"]}],
	}
