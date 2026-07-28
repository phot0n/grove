import frappe

# The group the migration parks existing access in. Plain data, not a magic name the code
# reads — nothing looks it up at runtime, so it can be renamed, pruned or deleted freely.
LEGACY_GROUP = "migrated-access"


def execute():
	"""Move policy off the API Key and onto the user.

	Two things change meaning here. Access is now granted per user (Grove User Group, or a
	Grove User's own Allow) and NOTHING is reachable without a grant — a key's projected
	model set is the exact list, where blank used to mean "everything". And the monthly
	token budget now belongs to the person, shared across their keys, instead of per-key —
	and so does the rate_limited flag it drives, which per-key let a blocked user mint a
	fresh key and walk past their own cap.

	Nobody should lose access on migrate, so whatever each user could reach before is
	written out as a real grant: a group holding the models that had a live route, with
	every previously-unrestricted user in it, and per-user Allow lists for the keys that
	carried a hard `allowed_models` scope."""
	scopes, budgets, limited = _read_old_key_policy()

	for user in set(scopes) | set(budgets):
		_write_policy(user, scopes.get(user, []), budgets.get(user, 0), user in limited)

	unrestricted = [u for u in _key_users() if u not in scopes]
	_write_legacy_group(unrestricted)

	# Every key's model set is newly derived — repush the lot on the next sync.
	frappe.db.set_value("Grove API Key", {"dirty": 0}, "dirty", 1, update_modified=False)


def _key_users():
	"""Users holding at least one API Key."""
	return {k.user for k in frappe.get_all("Grove API Key", fields=["user"]) if k.user}


def _read_old_key_policy():
	"""Per-user scope, budget and block state off the pre-migration Grove API Key columns.
	Frappe leaves dropped columns in place, so they are still readable here.

	A user with several keys had several budgets; they now share one. Take the LARGEST so
	nobody's allowance silently shrinks, and union the scopes for the same reason. One
	blocked key blocks the user, since the cap they blew is now shared."""
	scopes, budgets, limited = {}, {}, set()
	present = [
		c for c in ("allowed_models", "max_tokens", "rate_limited")
		if frappe.db.has_column("Grove API Key", c)
	]
	if not present:
		return scopes, budgets, limited

	columns = ", ".join(["user"] + present)
	for row in frappe.db.sql(f"SELECT {columns} FROM `tabGrove API Key`", as_dict=True):
		if not row.user:
			continue
		for model in (row.get("allowed_models") or "").replace("\n", ",").split(","):
			if model := model.strip():
				scopes.setdefault(row.user, []).append(model)
		budgets[row.user] = max(budgets.get(row.user, 0), int(row.get("max_tokens") or 0))
		if row.get("rate_limited"):
			limited.add(row.user)
	return scopes, budgets, limited


def _grove_user(user):
	"""The user's policy doc, created if they have none yet."""
	name = frappe.db.get_value("Grove User", {"user": user})
	if name:
		return frappe.get_doc("Grove User", name)
	doc = frappe.new_doc("Grove User")
	doc.user = user
	return doc


def _write_policy(user, models, budget, limited=False):
	"""The old `allowed_models` meant ONLY these, which is now just an Allow list with no
	group behind it — the same set, expressed the new way."""
	if not frappe.db.exists("User", user):
		return
	models = [m for m in dict.fromkeys(models) if frappe.db.exists("Model", m)]
	if not (models or budget or limited):
		return

	doc = _grove_user(user)
	if budget:
		doc.max_tokens = budget
	if limited:
		doc.rate_limited = 1
	existing = {row.model for row in doc.allow}
	for model in models:
		if model not in existing:
			doc.append("allow", {"model": model})
	doc.save(ignore_permissions=True)


def _write_legacy_group(users):
	"""An unrestricted key used to reach every model with a live route. Hand those users
	exactly that, as a normal group an admin can then split up or delete."""
	models = frappe.get_all("Model", {"published": 1}, pluck="name")
	users = [u for u in users if frappe.db.exists("User", u)]
	if not (users and models):
		return

	if frappe.db.exists("Grove User Group", LEGACY_GROUP):
		doc = frappe.get_doc("Grove User Group", LEGACY_GROUP)
	else:
		doc = frappe.new_doc("Grove User Group")
		doc.name = LEGACY_GROUP
		doc.description = (
			"Created on migrate: what an unrestricted API Key could reach before access "
			"became per-user. Safe to rename, split or delete once real groups exist."
		)

	have_models = {row.model for row in doc.models}
	for model in models:
		if model not in have_models:
			doc.append("models", {"model": model})
	doc.save(ignore_permissions=True)

	for user in users:
		member = _grove_user(user)
		member.user_group = LEGACY_GROUP
		member.save(ignore_permissions=True)
