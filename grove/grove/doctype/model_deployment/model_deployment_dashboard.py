def get_data():
	"""Connections: every Ansible Play this deployment triggered (deploy / reconfigure /
	teardown). The link is dynamic — reference_docname alone would also match a play of
	another doctype with the same name, so reference_doctype is pinned alongside it."""
	return {
		"fieldname": "reference_docname",
		"dynamic_links": {"reference_docname": ["Model Deployment", "reference_doctype"]},
		"transactions": [{"label": "Automation", "items": ["Ansible Play"]}],
	}
