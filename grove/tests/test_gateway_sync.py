# Copyright (c) 2026, Grove and contributors
# See license.txt
"""The routing table Grove pushes to a proxy. Pure — the docs are mocked, no site.

Every field here is read by something that cannot be changed in the same deploy: the Go agent
unmarshals it (`gateway_service/routing.go`), Lua puts one of them on the access line, and both
run on a box that is updated separately. So the shape is asserted rather than assumed.
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


if __name__ == "__main__":
	unittest.main()
