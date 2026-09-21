def get_data():
	"""Connections: the replicas placed from this deployment."""
	return {
		"fieldname": "model_deployment",
		"transactions": [{"label": "Serving", "items": ["Model Replica"]}],
	}
