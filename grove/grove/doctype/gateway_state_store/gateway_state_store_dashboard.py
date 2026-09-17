def get_data():
	"""Connections: the gateways running on this store, and every Ansible Play against its box."""
	return {
		"fieldname": "server",
		"non_standard_fieldnames": {"Gateway Server": "state_store"},
		"dynamic_links": {"server": ["Gateway State Store", "server_type"]},
		"transactions": [
			{"label": "Gateways", "items": ["Gateway Server"]},
			{"label": "Automation", "items": ["Ansible Play"]},
		],
	}
