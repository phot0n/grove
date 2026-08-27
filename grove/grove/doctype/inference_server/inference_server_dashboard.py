def get_data():
	"""Connections: every Ansible Play that ran against this box — its own Setup plus the
	serve / reconfigure / teardown of every Model Replica on it, since they all target
	this server. Dynamic link, so server_type is pinned alongside server."""
	return {
		"fieldname": "server",
		"dynamic_links": {"server": ["Inference Server", "server_type"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play"]}],
	}
