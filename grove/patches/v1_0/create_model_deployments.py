# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Give every existing Model Replica a Model Deployment.

Replicas that already ran the same shape collapse into one deployment; ones that differ keep their
own. The resolved config of every replica is identical before and after — the deployment carries
exactly what the replica used to, and the replica's copy is then cleared so the deployment is the
single owner of it."""

import frappe


# Columns still on `tabModel Replica` after the fields moved up. Frappe leaves a column when a
# field is removed from the JSON, which is what lets this read the old values back.
CARRIED = (
	"engine_image",
	"pipeline_parallel_size",
	"kv_cache_dtype",
	"gpu_memory_utilization",
	"max_num_batched_tokens",
	"max_num_seqs",
	"attention_backend",
	"max_model_len",
	"allow_long_max_model_len",
	"extra_serve_args",
	"startup_command",
	"health_path",
)
# What a replica keeps as an override, and what is therefore cleared on every migrated one. Left
# populated, each replica would be an explicit override of a value identical to its deployment, and
# editing the deployment would reach nobody — which is the whole point of having one.
CLEARED = {
	# Appended, not replaced — so leaving the replica's copy would render every flag twice.
	"extra_serve_args": "",
	"kv_cache_dtype": "",
	"attention_backend": "",
	"max_model_len": "",
	"gpu_memory_utilization": 0,
	"max_num_batched_tokens": 0,
	"max_num_seqs": 0,
}


def execute():
	if not frappe.db.has_column("Model Replica", "engine_image"):
		return  # already migrated, or a fresh site that never had the old shape
	replicas = frappe.db.sql(
		"select name, model, inference_server, model_deployment, {} "
		"from `tabModel Replica`".format(", ".join(f"`{column}`" for column in CARRIED)),
		as_dict=True,
	)
	# Re-runnable: a row that already names a deployment was done by an earlier run of this patch.
	replicas = [r for r in replicas if not r.model_deployment]
	if not replicas:
		return

	env = _env_rows()
	gpu_counts = _gpu_counts()
	deployments = {}
	for replica in replicas:
		replica.max_model_len = _context_length(replica.max_model_len)
		gpus = gpu_counts.get(replica.name, 0)
		rows = env.get(replica.name, [])
		shape = _shape(replica, gpus, rows)
		if shape not in deployments:
			deployments[shape] = _create_deployment(replica, gpus, rows)
		frappe.db.set_value(
			"Model Replica",
			replica.name,
			"model_deployment",
			deployments[shape],
			update_modified=False,
		)

	_clear_carried_values()
	frappe.db.delete("Pod Env", {"parenttype": "Model Replica"})
	print(f"Model Deployment: {len(deployments)} for {len(replicas)} replicas")


def _context_length(value):
	"""A stored Max Model Len, or blank. `0` is what this field held while it was an Int column,
	before it took `32k` / `128k` — it means unset, and blank is how unset is spelled now.

	Normalised here rather than by softening `parse_context_length`, which is right to refuse a
	typed 0: an operator who types it means a mistake, and a legacy row means nothing at all.
	Normalised BEFORE the shape is taken, so a replica holding `0` and one holding blank are
	recognised as the same service rather than split into two deployments over a spelling."""
	text = str(value or "").strip()
	return "" if text == "0" else text


def _shape(replica, gpu_count, env_rows):
	"""What makes two replicas the same service: the same model off the same image, on the same
	number of cards, tuned the same way, with the same environment.

	Region is deliberately absent — a deployment is not scoped to one. Two replicas of a model in
	different regions are the same service, and `deploy:<model>` already unions them."""
	return (
		replica.model,
		gpu_count,
		tuple(replica.get(column) for column in CARRIED),
		tuple(env_rows),
	)


def _create_deployment(replica, gpu_count, env_rows):
	deployment = frappe.get_doc(
		{
			"doctype": "Model Deployment",
			"model": replica.model,
			"gpus_per_replica": gpu_count or 1,
			**{column: replica.get(column) for column in CARRIED},
			"env": [{"key": key, "value": value} for key, value in env_rows],
		}
	)
	# Defaults the old field carried that a NULL column would otherwise drop.
	deployment.kv_cache_dtype = deployment.kv_cache_dtype or "auto"
	deployment.attention_backend = deployment.attention_backend or "auto"
	deployment.pipeline_parallel_size = deployment.pipeline_parallel_size or 1
	# ignore_mandatory: a replica made before Engine Image existed names none, and its deployment
	# faithfully names none either. That deployment cannot serve until an operator picks an image —
	# which is exactly the state its replica was already in, since the serve path reads the
	# image by name. Inventing one here would turn "cannot deploy" into "deploys the wrong thing".
	deployment.insert(ignore_permissions=True, ignore_mandatory=True)
	return deployment.name


def _gpu_counts():
	"""How many cards each replica pins — the deployment's gpus_per_replica."""
	counts = {}
	for row in frappe.db.sql(
		"select parent, count(*) as cards from `tabModel Replica GPU` "
		"where parenttype = 'Model Replica' group by parent",
		as_dict=True,
	):
		counts[row.parent] = row.cards
	return counts


def _env_rows():
	"""Each replica's env rows in their stored order. Order is load-bearing — a docker
	--env-file is line-ordered, and a reorder re-renders the file and replaces the container."""
	rows = {}
	for row in frappe.db.sql(
		"select parent, `key`, `value` from `tabPod Env` "
		"where parenttype = 'Model Replica' order by parent, idx",
		as_dict=True,
	):
		rows.setdefault(row.parent, []).append((row.key, row.value))
	return rows


def _clear_carried_values():
	"""Blank the replica's own copies so the deployment owns them. The columns the replica no longer
	has a field for are left alone — Frappe keeps them, and a later migration that re-adds a field
	of the same name would rather find the old value than a blanked one."""
	frappe.db.sql(
		"update `tabModel Replica` set {}".format(
			", ".join(f"`{column}` = %({column})s" for column in CLEARED)
		),
		CLEARED,
	)
