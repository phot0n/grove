# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What Grove pushes to a proxy: the routing table and the group grants. Pure — the docs are
mocked, no site.

Every field here is read by something that cannot be changed in the same deploy: the Go agent
unmarshals it (`gateway_service/routing.go`, `admin.go`), Lua puts one of them on the access line,
and both run on a box that is updated separately. So the shape is asserted rather than assumed.
"""

import unittest
import unittest.mock

import frappe

from grove import gateway_sync


def deployment(name, model="qwen3-35b", server="INF-1", status="Active"):
	return frappe._dict(
		name=name, model=model, engine_url=f"https://10.0.0.9/e/{name.lower()}",
		status=status, inference_server=server,
	)


def pod(name, model="qwen3-35b"):
	return frappe._dict(name=name, model=model, engine_url="http://1.2.3.4:8080")


class TestRoutesForProxy(unittest.TestCase):
	def routes(self, deployments=(), pods=(), models=("qwen3-35b",)):
		"""_routes_for_proxy against mocked docs. get_all is dispatched on doctype because the
		function reads three of them, and get_doc only ever supplies the internal key."""

		def get_all(doctype, **kwargs):
			if doctype == "Model Deployment":
				return list(deployments)
			if doctype == "Model":
				return list(models)
			if doctype == "Pod":
				return list(pods)
			raise AssertionError(f"unexpected get_all({doctype})")

		doc = unittest.mock.Mock()
		doc.get_password.return_value = "internal-key"
		with (
			unittest.mock.patch.object(frappe, "get_all", side_effect=get_all),
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
			[frappe._dict(parent="acme", model="qwen3-35b")],
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
			[frappe._dict(parent="acme", model="b"), frappe._dict(parent="acme", model="a")],
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
			[frappe._dict(parent="a", model="m1"), frappe._dict(parent="b", model="m2")],
		)
		self.assertEqual([g["models"] for g in groups], ["m1", "m2"])


class TestPushOrder(unittest.TestCase):
	def test_groups_land_before_the_keys_that_name_them(self):
		# A key resolves against group:<name>. Pushed first, a key naming a brand-new group
		# would 403 its user until the next tick.
		calls = []
		with (
			unittest.mock.patch.object(gateway_sync, "push_groups", lambda *a: calls.append("groups") or {}),
			unittest.mock.patch.object(gateway_sync, "push_keys", lambda *a: calls.append("keys") or {}),
			unittest.mock.patch.object(gateway_sync, "sync_routes", lambda *a: calls.append("routes") or {}),
		):
			res = gateway_sync._push_and_classify("PROXY-1", ["acme"], ["KEY-1"], gateway_sync._ALL)
		self.assertEqual(calls, ["groups", "keys", "routes"])
		self.assertEqual(res["success"], 1)

	def test_a_skipped_push_is_not_made(self):
		calls = []
		with (
			unittest.mock.patch.object(gateway_sync, "push_groups", lambda *a: calls.append("groups") or {}),
			unittest.mock.patch.object(gateway_sync, "push_keys", lambda *a: calls.append("keys") or {}),
			unittest.mock.patch.object(gateway_sync, "sync_routes", lambda *a: calls.append("routes") or {}),
		):
			gateway_sync._push_and_classify("PROXY-1", None, None, gateway_sync._ALL)
		self.assertEqual(calls, ["routes"])


if __name__ == "__main__":
	unittest.main()
