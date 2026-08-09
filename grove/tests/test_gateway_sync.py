# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What Grove pushes to a proxy: the routing table, and the three access records a request
resolves through — group, then user, then key. Pure — the docs are mocked, no site.

Every field here is read by something that cannot be changed in the same deploy: the Go agent
unmarshals it (`gateway_service/routing.go`, `admin.go`), Lua puts one of them on the access line,
and both run on a box that is updated separately. So the shape is asserted rather than assumed.
"""

import unittest
import unittest.mock

import frappe

from grove import gateway_sync
from grove.serve_command import DEFAULT_MAX_NUM_SEQS


def deployment(name, model="qwen3-35b", server="INF-1", status="Active", max_num_seqs=0):
	return frappe._dict(
		name=name, model=model, engine_url=f"https://10.0.0.9/e/{name.lower()}",
		status=status, inference_server=server, max_num_seqs=max_num_seqs,
	)


def pod(name, model="qwen3-35b", max_num_seqs=0):
	return frappe._dict(
		name=name, model=model, engine_url="http://1.2.3.4:8080", max_num_seqs=max_num_seqs
	)


class TestRoutesForProxy(unittest.TestCase):
	def routes(self, deployments=(), pods=(), models=("qwen3-35b",)):
		"""_routes_for_proxy against mocked docs. get_all is dispatched on doctype because the
		function reads several of them, and get_doc only ever supplies the internal key.

		No box here names an ingress, so every route is direct — the shape this whole suite was
		written against, and the shape a fleet that has cut nothing over still has. The ingress
		rows have their own file: test_gateway_routes."""

		def get_all(doctype, **kwargs):
			if doctype == "Model Deployment":
				return list(deployments)
			if doctype == "Model":
				return list(models)
			if doctype == "Pod":
				return list(pods)
			if doctype in ("Ingress Server", "Inference Server"):
				return []
			raise AssertionError(f"unexpected get_all({doctype})")

		doc = unittest.mock.Mock()
		doc.get_password.return_value = "internal-key"
		with (
			unittest.mock.patch.object(frappe, "get_all", side_effect=get_all),
			unittest.mock.patch.object(
				frappe, "db", frappe._dict(get_single_value=lambda *args: "grove.example.com")
			),
			unittest.mock.patch.object(frappe, "get_doc", return_value=doc),
		):
			return gateway_sync._routes_for_proxy("PROXY-1")

	def test_an_active_deployment_names_itself_and_its_box(self):
		[route] = self.routes([deployment("MD-00007")])["qwen3-35b"]
		self.assertEqual(route["deployment"], "MD-00007")
		self.assertEqual(route["server"], "INF-1")
		self.assertEqual(route["engine_url"], "https://10.0.0.9/e/md-00007")

	def test_two_deployments_of_one_model_on_one_box_stay_distinct(self):
		# The case the whole field exists for: `server` is identical, so it cannot name an engine,
		# and neither can the access log's $upstream_addr now that both sit behind the box's
		# engine proxy on :443.
		routes = self.routes([deployment("MD-00007"), deployment("MD-00008")])["qwen3-35b"]
		self.assertEqual([r["deployment"] for r in routes], ["MD-00007", "MD-00008"])
		self.assertEqual({r["server"] for r in routes}, {"INF-1"})
		self.assertEqual(len({r["engine_url"] for r in routes}), 2)

	def test_a_pod_is_its_own_placement(self):
		# No separate deployment doc, so both fields are the pod — kept explicit so no consumer
		# has to special-case a pod route.
		[route] = self.routes(pods=[pod("POD-1")])["qwen3-35b"]
		self.assertEqual(route["deployment"], "POD-1")
		self.assertEqual(route["server"], "POD-1")

	def test_only_active_deployments_are_routed(self):
		# A Broken engine still holds its port and its doc; routing to it would 502 every request
		# instead of the 503 that a model with nowhere to go actually means.
		self.assertEqual(self.routes([deployment("MD-00007", status="Broken")])["qwen3-35b"], [])

	def test_a_model_with_no_engine_is_sent_as_empty_not_omitted(self):
		# The agent deletes the key on an empty list. Omitted, a stale route would survive and the
		# model would keep showing in /v1/models.
		self.assertEqual(self.routes(models=["qwen3-35b"])["qwen3-35b"], [])

	def test_a_route_carries_the_engines_concurrency_cap(self):
		# --max-num-seqs is what the engine runs at once; past it vLLM queues where the gateway
		# can neither see the wait nor spend it on a replica. So it is admission control too.
		[route] = self.routes([deployment("MD-00007", max_num_seqs=64)])["qwen3-35b"]
		self.assertEqual(route["capacity"], 64)

	def test_a_pod_carries_its_own_cap(self):
		[route] = self.routes(pods=[pod("POD-1", max_num_seqs=16)])["qwen3-35b"]
		self.assertEqual(route["capacity"], 16)

	def test_an_unset_cap_resolves_to_what_the_engine_was_started_with(self):
		# Blank is not "no cap": the serve command fills the same default in, so the number the
		# gateway holds the engine to is the number the engine is running. Drift between the two
		# is the one way this whole mechanism can be quietly wrong.
		[route] = self.routes([deployment("MD-00007")])["qwen3-35b"]
		self.assertEqual(route["capacity"], DEFAULT_MAX_NUM_SEQS)


class TestEffectiveGroups(unittest.TestCase):
	"""group:<name> — the record that made this split worth doing: one push per group, however
	many keys its members hold. The gateway reads exactly these two fields."""

	def groups(self, groups=(), rows=()):
		def get_all(doctype, **kwargs):
			if doctype == "Grove User Group":
				return list(groups)
			if doctype == "Grove Model Row":
				return list(rows)
			raise AssertionError(f"unexpected get_all({doctype})")

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			return gateway_sync._effective_groups()

	def test_a_group_carries_its_models_and_flipped_priority(self):
		[group] = self.groups(
			[frappe._dict(name="acme", priority=10)],
			[frappe._dict(parent="acme", model="qwen3-35b", parentfield="models")],
		)
		self.assertEqual(group["name"], "acme")
		self.assertEqual(group["models"], "qwen3-35b")
		# Grove stores "higher = more important"; vLLM serves the lowest number first.
		self.assertEqual(group["priority"], -10)

	def test_models_are_one_sorted_comma_list(self):
		# The agent splits on commas (gateway_service/main.go modelSet), so the join is the
		# wire format, not a display choice.
		[group] = self.groups(
			[frappe._dict(name="acme", priority=0)],
			[
				frappe._dict(parent="acme", model="b", parentfield="models"),
				frappe._dict(parent="acme", model="a", parentfield="models"),
			],
		)
		self.assertEqual(group["models"], "a,b")

	def test_a_group_that_grants_nothing_is_still_pushed_as_blank(self):
		# Blank means "grants nothing", not "unset" — an emptied group has to overwrite the
		# grant already in Redis, so it cannot be omitted.
		[group] = self.groups([frappe._dict(name="acme", priority=0)])
		self.assertEqual(group["models"], "")

	def test_one_row_query_covers_every_group(self):
		groups = self.groups(
			[frappe._dict(name="a", priority=0), frappe._dict(name="b", priority=1)],
			[
				frappe._dict(parent="a", model="m1", parentfield="models"),
				frappe._dict(parent="b", model="m2", parentfield="models"),
			],
		)
		self.assertEqual([g["models"] for g in groups], ["m1", "m2"])


class TestPublicCatalog(unittest.TestCase):
	"""The anonymous /v1/models list. Served to anyone who asks, so what it leaves out matters:
	it names models, it never grants them, and only a group explicitly flagged for it takes part."""

	def catalog(self, public_groups=(), rows=()):
		seen = {}

		def get_all(doctype, filters=None, **kwargs):
			seen[doctype] = filters or {}
			if doctype == "Grove User Group":
				return list(public_groups)
			if doctype == "Grove Model Row":
				return list(rows)
			raise AssertionError(f"unexpected get_all({doctype})")

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			return gateway_sync._public_catalog(), seen

	def test_only_groups_flagged_for_it_are_asked_for(self):
		# The filter is the whole access story: a group nobody ticked must never be read here.
		_catalog, seen = self.catalog(["acme"], ["qwen3-4b"])
		self.assertEqual(seen["Grove User Group"], {"public_catalog": 1})

	def test_a_flagged_group_publishes_its_models(self):
		catalog, _seen = self.catalog(["acme"], ["qwen3-4b", "qwen3.5-27b"])
		self.assertEqual(catalog, "qwen3-4b,qwen3.5-27b")

	def test_no_flagged_group_publishes_nothing(self):
		# The default for every group, and the default for a fleet that never ticks the box.
		catalog, seen = self.catalog()
		self.assertEqual(catalog, "")
		# Nothing to pool → the row query is not even made.
		self.assertNotIn("Grove Model Row", seen)

	def test_two_groups_naming_one_model_publish_it_once(self):
		# Pooled across groups, so an overlap must not produce a duplicate entry on the wire.
		catalog, _seen = self.catalog(["acme", "beta"], ["qwen3-4b", "qwen3-4b", "qwen3.5-27b"])
		self.assertEqual(catalog, "qwen3-4b,qwen3.5-27b")

	def test_the_list_is_sorted(self):
		catalog, _seen = self.catalog(["acme"], ["b", "a"])
		self.assertEqual(catalog, "a,b")

	def test_only_the_flagged_groups_rows_are_read(self):
		_catalog, seen = self.catalog(["acme"], ["qwen3-4b"])
		self.assertEqual(
			seen["Grove Model Row"],
			{"parenttype": "Grove User Group", "parent": ("in", ["acme"])},
		)


class TestEffectiveUsers(unittest.TestCase):
	"""user:<name> — the record that holds everything belonging to the person rather than to a
	credential, so a budget flip or an access edit is one push however many keys they hold."""

	def users(self, users=(), rows=(), only=None):
		def get_all(doctype, **kwargs):
			if doctype == "Grove User":
				return list(users)
			if doctype == "Grove Model Row":
				return list(rows)
			raise AssertionError(f"unexpected get_all({doctype})")

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			return gateway_sync._effective_users(only)

	def test_a_user_carries_their_group_their_deltas_and_their_budget_flag(self):
		[user] = self.users(
			[frappe._dict(name="GU-1", user="a@x.com", user_group="acme", rate_limited=1)],
			[
				frappe._dict(parent="GU-1", model="qwen3-4b", parentfield="allow"),
				frappe._dict(parent="GU-1", model="qwen3-35b", parentfield="deny"),
			],
		)
		self.assertEqual(user["name"], "GU-1")
		self.assertEqual(user["email"], "a@x.com")
		self.assertEqual(user["group"], "acme")
		self.assertEqual(user["allow"], "qwen3-4b")
		self.assertEqual(user["deny"], "qwen3-35b")
		self.assertIs(user["limited"], True)

	def test_the_lists_are_sorted_comma_joins(self):
		# The agent splits on commas (gateway_service/main.go modelSet), so the join is the wire
		# format, not a display choice.
		[user] = self.users(
			[frappe._dict(name="GU-1", user="a@x.com", user_group="", rate_limited=0)],
			[
				frappe._dict(parent="GU-1", model="b", parentfield="allow"),
				frappe._dict(parent="GU-1", model="a", parentfield="allow"),
			],
		)
		self.assertEqual(user["allow"], "a,b")

	def test_a_user_who_grants_nothing_is_still_pushed_as_blank(self):
		# Blank overwrites what is already in Redis. Omitting the fields would leave a removed
		# allow in force.
		[user] = self.users([frappe._dict(name="GU-1", user="a@x.com", user_group=None, rate_limited=0)])
		self.assertEqual((user["group"], user["allow"], user["deny"]), ("", "", ""))
		self.assertIs(user["limited"], False)

	def test_one_row_query_covers_every_user(self):
		# The N+1 this projection exists to remove: one query for the users, one for their rows,
		# whatever the number of people.
		users = self.users(
			[
				frappe._dict(name="GU-1", user="a@x.com", user_group="", rate_limited=0),
				frappe._dict(name="GU-2", user="b@x.com", user_group="", rate_limited=0),
			],
			[
				frappe._dict(parent="GU-1", model="m1", parentfield="allow"),
				frappe._dict(parent="GU-2", model="m2", parentfield="allow"),
			],
		)
		self.assertEqual([u["allow"] for u in users], ["m1", "m2"])


class TestEffectiveKeys(unittest.TestCase):
	"""key:<hash> — the index from a presented secret to its holder, plus the one fact that is
	genuinely the credential's own."""

	def keys(self, keys=()):
		seen = {}

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Grove API Key":
				seen["filters"] = filters
				return list(keys)
			raise AssertionError(f"unexpected get_all({doctype})")

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			projected = gateway_sync._effective_keys()
		self.filters = seen.get("filters")
		return projected

	def test_a_key_points_at_its_holder_and_says_nothing_about_access(self):
		# The whole split: anything the gateway reads here would have to be rewritten on every
		# key the person holds each time their access or budget moved.
		[key] = self.keys([frappe._dict(name="KEY-1", key_hash="abc", user="GU-1", status="active")])
		self.assertEqual(key, {"key_hash": "abc", "prefix": "KEY-1", "user": "GU-1", "status": "active"})

	def test_the_pointer_is_the_doc_name_not_the_email(self):
		# The agent resolves user:<name>, so an email here would resolve against nothing.
		[key] = self.keys([frappe._dict(name="KEY-1", key_hash="abc", user="GU-1", status="active")])
		self.assertEqual(key["user"], "GU-1")

	def test_a_key_with_no_hash_is_dropped(self):
		# Nothing can present it, and the agent would key the record on an empty string.
		self.assertEqual(self.keys([frappe._dict(name="KEY-1", key_hash=None, user="GU-1", status="active")]), [])

	def test_only_live_keys_are_asked_for(self):
		# A revoked key is not restated as revoked — it is not projected at all, and the Gateway
		# Deletion its revoke wrote is what takes it off the boxes. Asserted on the filter because
		# leaving it out would silently re-push every dead credential.
		self.keys([frappe._dict(name="KEY-1", key_hash="abc", user="GU-1", status="active")])
		self.assertEqual(self.filters, {"status": "active"})


class TestPushDeletions(unittest.TestCase):
	"""The one pruning path. Every other endpoint upserts, so a revoked key keeps serving until
	this lands."""

	def pushed(self, deletions):
		sent = []

		def post(_admin_url, _token, path, payload, method="POST"):
			sent.append((method, path, payload))
			return {"count": len(payload.get("ids", []))}

		with (
			unittest.mock.patch.object(gateway_sync, "_conn", return_value=(None, "u", "t")),
			unittest.mock.patch.object(gateway_sync, "_post", side_effect=post),
		):
			result = gateway_sync.push_deletions("PROXY-1", deletions)
		return sent, result

	def deletion(self, record_type, record_id):
		return frappe._dict(name=f"GD-{record_id}", record_type=record_type, record_id=record_id)

	def test_a_revoked_key_is_removed_by_its_hash(self):
		sent, result = self.pushed([self.deletion("Key", "abc")])
		self.assertEqual(sent, [("DELETE", "keys", {"ids": ["abc"]})])
		self.assertEqual(result["count"], 1)

	def test_keys_and_users_go_to_their_own_endpoints(self):
		# One tombstone table, two Redis prefixes — the record type is what tells them apart.
		sent, _result = self.pushed([self.deletion("Key", "abc"), self.deletion("User", "GU-1")])
		self.assertEqual(
			sent, [("DELETE", "keys", {"ids": ["abc"]}), ("DELETE", "users", {"ids": ["GU-1"]})]
		)

	def test_a_type_with_nothing_pending_is_not_called(self):
		sent, _result = self.pushed([self.deletion("Key", "abc")])
		self.assertEqual([path for _method, path, _payload in sent], ["keys"])


class TestPushOrder(unittest.TestCase):
	def pushes(self, *args):
		calls = []
		with (
			unittest.mock.patch.object(gateway_sync, "push_groups", lambda *a: calls.append("groups") or {}),
			unittest.mock.patch.object(gateway_sync, "push_users", lambda *a: calls.append("users") or {}),
			unittest.mock.patch.object(gateway_sync, "push_keys", lambda *a: calls.append("keys") or {}),
			unittest.mock.patch.object(gateway_sync, "push_deletions", lambda *a: calls.append("deletions") or {}),
			unittest.mock.patch.object(gateway_sync, "sync_routes", lambda *a: calls.append("routes") or {}),
		):
			res = gateway_sync._push_and_classify("PROXY-1", *args)
		return calls, res

	def test_each_record_lands_before_the_one_that_names_it(self):
		# A key resolves user:<name>, which resolves group:<name>. Pushed out of order, a record
		# naming a brand-new one would 403 its holder until the next tick.
		calls, res = self.pushes(["acme"], ["GU-1"], ["KEY-1"], [], gateway_sync._ALL)
		self.assertEqual(calls, ["groups", "users", "keys", "routes"])
		self.assertEqual(res["success"], 1)

	def test_a_skipped_push_is_not_made(self):
		calls, _res = self.pushes(None, None, None, [], gateway_sync._ALL)
		self.assertEqual(calls, ["routes"])

	def test_records_are_removed_only_after_the_upserts(self):
		# A deletion pushed first would briefly take out a record this same run is about to
		# write, and the window is a 401 for whoever holds it.
		deletion = frappe._dict(name="GD-1", record_type="Key", record_id="abc")
		calls, _res = self.pushes(
			gateway_sync._ALL, gateway_sync._ALL, gateway_sync._ALL, [deletion], gateway_sync._ALL
		)
		self.assertEqual(calls, ["groups", "users", "keys", "deletions", "routes"])

	def test_nothing_to_delete_makes_no_call(self):
		calls, _res = self.pushes(None, None, None, [], None)
		self.assertEqual(calls, [])

	def test_the_dirty_doctypes_are_listed_in_push_order(self):
		# sync_dirty splats its per-doctype argument list into _push_and_classify positionally,
		# so the tuple order IS the push order.
		self.assertEqual(
			gateway_sync._DIRTY_DOCTYPES, ("Grove User Group", "Grove User", "Grove API Key")
		)


if __name__ == "__main__":
	unittest.main()
