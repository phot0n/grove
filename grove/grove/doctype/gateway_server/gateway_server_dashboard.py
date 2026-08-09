def get_data():
	"""Connections: every Ansible Play that ran against this proxy (agent deploy, config
	push). Dynamic link, so server_type is pinned alongside server."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Gateway Server", "server_type"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play"]}],
	}
