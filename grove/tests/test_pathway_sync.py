# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""What Grove pushes to a proxy: the routing table, and the three access records a request
resolves through — group, then user, then key — plus the hash gate that decides whether a
box is pushed at all. Pure — the docs are mocked, no site.

Every field here is read by something that cannot be changed in the same deploy: the Go agent
unmarshals it, and it lives in its own repo on a box that is updated separately — a released
binary, not a tree this deploy compiles. So the shape is asserted rather than assumed. The
contract is plan_agent_state_sync.md at the repo root.
"""

import threading
import unittest
import unittest.mock
from pathlib import Path

import frappe

import grove
from grove.pathway import projection, routes, run, snapshot
from grove.pathway.run import Target, Unit
from grove.serving.vllm import VllmEngine


def replica(name, model="qwen3-35b", server="INF-1", status="Active", max_num_seqs=0,
	model_deployment=None):
	return frappe._dict(
		name=name, model=model, engine_url=f"https://10.0.0.9/e/{name.lower()}",
		status=status, inference_server=server, max_num_seqs=max_num_seqs,
		model_deployment=model_deployment,
	)


def deployment(name="qwen3-35b-ap-south-1", max_num_seqs=0, engine_image=None):
	return frappe._dict(name=name, max_num_seqs=max_num_seqs, engine_image=engine_image)


def pod(name, model="qwen3-35b", max_num_seqs=0):
	return frappe._dict(
		name=name, model=model, engine_url="http://1.2.3.4:8080", max_num_seqs=max_num_seqs
	)


class TestGatewayRoutes(unittest.TestCase):
	def routes(self, replicas=(), pods=(), models=("qwen3-35b",), deployments=()):
		"""gateway_routes against mocked docs. get_all is dispatched on doctype because the
		function reads several of them, and get_doc only ever supplies the internal key.

		No box here names an ingress, so every route is direct — the shape this whole suite was
		written against, and the shape a fleet that has cut nothing over still has. The ingress
		rows have their own file: test_gateway_routes."""

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Model Replica":
				# The Active filter moved into the query, so the mock honours it.
				return [r for r in replicas if r.status == (filters or {}).get("status")]
			if doctype == "Model":
				return [frappe._dict(name=m, modality="text") for m in models]
			if doctype == "Pod":
				return list(pods)
			if doctype == "Model Deployment":
				return list(deployments)
			if doctype in ("Ingress Server", "Inference Server", "Model Provider"):
				# No third party in this suite: every model here is one we run ourselves.
				return []
			if doctype == "Engine Image":
				# No rows, so every placement's kind resolves to vllm.
				return []
			if doctype in ("Model Pricing", "Model Price Row"):
				return []  # nothing priced, so no row carries rates
			raise AssertionError(f"unexpected get_all({doctype})")

		doc = unittest.mock.Mock()
		doc.get_password.return_value = "internal-key"
		with (
			unittest.mock.patch.object(frappe, "get_all", side_effect=get_all),
			unittest.mock.patch.object(
				frappe, "db", frappe._dict(get_value=lambda *args: "grove.example.com", get_single_value=lambda *args: "in")
			),
			unittest.mock.patch.object(frappe, "get_doc", return_value=doc),
		):
			return routes.gateway_routes("in")

	def test_an_active_deployment_names_itself_and_its_box(self):
		[route] = self.routes([replica("MD-00007")])["qwen3-35b"]
		self.assertEqual(route["deployment"], "MD-00007")
		self.assertEqual(route["server"], "INF-1")
		self.assertEqual(route["engine_url"], "https://10.0.0.9/e/md-00007")

	def test_two_deployments_of_one_model_on_one_box_stay_distinct(self):
		# The case the field exists for: `server` is identical, and so is $upstream_addr now that
		# both sit behind the box's engine proxy on :443.
		routes = self.routes([replica("MD-00007"), replica("MD-00008")])["qwen3-35b"]
		self.assertEqual([r["deployment"] for r in routes], ["MD-00007", "MD-00008"])
		self.assertEqual({r["server"] for r in routes}, {"INF-1"})
		self.assertEqual(len({r["engine_url"] for r in routes}), 2)

	def test_a_pod_is_its_own_placement(self):
		# No separate deployment doc, so both fields are the pod. Explicit, so no consumer
		# special-cases a pod route.
		[route] = self.routes(pods=[pod("POD-1")])["qwen3-35b"]
		self.assertEqual(route["deployment"], "POD-1")
		self.assertEqual(route["server"], "POD-1")

	def test_only_active_deployments_are_routed(self):
		# A Broken engine still holds its port and doc, so routing to it 502s every request
		# instead of the 503 a model with nowhere to go means.
		self.assertEqual(self.routes([replica("MD-00007", status="Broken")]), {})

	def test_a_model_with_no_engine_is_absent_not_sent_empty(self):
		# The push is the whole table and absence prunes, so no empty list is needed to say so.
		self.assertNotIn("qwen3-35b", self.routes(models=["qwen3-35b"]))

	def test_the_rows_are_ordered_by_deployment(self):
		# The hash is over the serialized table, so the same rows in a different order must not
		# read as drift and re-push the fleet.
		routes = self.routes([replica("MD-00008"), replica("MD-00007")])["qwen3-35b"]
		self.assertEqual([r["deployment"] for r in routes], ["MD-00007", "MD-00008"])

	def test_a_route_carries_the_engines_concurrency_cap(self):
		# Past it vLLM queues, where the gateway can neither see the wait nor spend it — so this
		# is admission control, not a hint.
		[route] = self.routes([replica("MD-00007", max_num_seqs=64)])["qwen3-35b"]
		self.assertEqual(route["capacity"], 64)

	def test_a_pod_carries_its_own_cap(self):
		[route] = self.routes(pods=[pod("POD-1", max_num_seqs=16)])["qwen3-35b"]
		self.assertEqual(route["capacity"], 16)

	def test_an_unset_cap_falls_back_to_the_assumed_one(self):
		# Blank is not "no cap": the gate has to hold the engine to something. It is an ASSUMPTION
		# though — the serve command passes no --max-num-seqs, so vLLM sizes its own and the two
		# can differ. Setting max_num_seqs pins both.
		[route] = self.routes([replica("MD-00007")])["qwen3-35b"]
		self.assertEqual(route["capacity"], VllmEngine.default_concurrency)


class TestRouteModality(unittest.TestCase):
	"""Which OpenAI surface a model answers on rides on its route rows.

	Stamped per row because deploy:<model> is the only thing pushed per model — a separate record
	would mean a new namespace for one short string. The gateway refuses a request for a surface
	the modality does not cover, so a wrong value here is a 404 on a working model."""

	def routes(self, models, replicas=(), pods=()):
		def get_all(doctype, **kwargs):
			if doctype == "Model":
				return [frappe._dict(name=n, modality=m) for n, m in models.items()]
			if doctype == "Model Replica":
				return list(replicas)
			if doctype == "Pod":
				return list(pods)
			return []

		with (
			unittest.mock.patch.object(frappe, "get_all", get_all),
			unittest.mock.patch.object(
				frappe, "get_doc",
				lambda *a: frappe._dict(get_password=lambda *_a, **_k: "k"),
			),
			unittest.mock.patch.object(routes, "ingress_targets", lambda geography: {}),
			unittest.mock.patch.object(frappe, "db", frappe._dict(get_single_value=lambda *args: "in")),
		):
			return routes.gateway_routes("in")

	def test_a_deployment_row_carries_its_models_modality(self):
		routes = self.routes(
			{"qwen3-4b": "text"},
			replicas=[replica("MD-1", model="qwen3-4b")],
		)
		self.assertEqual(routes["qwen3-4b"][0]["modality"], "text")

	def test_a_pod_row_carries_it_too(self):
		# The ASR container is a Pod, never a Model Replica.
		routes = self.routes(
			{"nemotron-asr": "audio"},
			pods=[pod("test-nemo-asr", model="nemotron-asr")],
		)
		self.assertEqual(routes["nemotron-asr"][0]["modality"], "audio")

	def test_a_model_with_no_modality_sends_blank_not_null(self):
		# Blank reads as unrestricted; None would serialise as null and read as a value.
		routes = self.routes(
			{"qwen3-4b": None},
			replicas=[replica("MD-1", model="qwen3-4b")],
		)
		self.assertEqual(routes["qwen3-4b"][0]["modality"], "")


class TestEffectiveGroups(unittest.TestCase):
	"""model_group:<name> — the record that made this split worth doing: one push per group, however
	many keys its members hold. The gateway reads exactly these two fields."""

	def groups(self, groups=(), rows=()):
		def get_all(doctype, **kwargs):
			if doctype == "Model Group":
				return list(groups)
			if doctype == "Grove Model Row":
				return list(rows)
			raise AssertionError(f"unexpected get_all({doctype})")

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			return snapshot.effective_groups()

	def test_a_group_carries_its_name_and_models(self):
		[group] = self.groups(
			["acme"],
			[frappe._dict(parent="acme", model="qwen3-35b", parentfield="models")],
		)
		self.assertEqual(group, {"name": "acme", "models": "qwen3-35b"})

	def test_models_are_one_sorted_comma_list(self):
		# The agent splits on commas (pathway, internal/domain/access.go `ModelSet`), so the
		# join is the wire format, not a display choice.
		[group] = self.groups(
			["acme"],
			[
				frappe._dict(parent="acme", model="b", parentfield="models"),
				frappe._dict(parent="acme", model="a", parentfield="models"),
			],
		)
		self.assertEqual(group["models"], "a,b")

	def test_a_group_that_grants_nothing_is_still_pushed_as_blank(self):
		# Blank means "grants nothing", not "unset": an emptied group has to overwrite what is
		# already in Redis.
		[group] = self.groups(["acme"])
		self.assertEqual(group["models"], "")

	def test_the_records_are_ordered_by_name(self):
		# Same as the route rows: the section hash must not move on query order.
		groups = self.groups(["b", "a"])
		self.assertEqual([g["name"] for g in groups], ["a", "b"])


class TestEffectiveUsers(unittest.TestCase):
	"""user:<name> — the record that holds everything belonging to the person rather than to a
	credential, so a budget flip or an access edit is one push however many keys they hold."""

	def users(self, users=(), rows=(), groups=()):
		self.calls = {}

		def get_all(doctype, **kwargs):
			self.calls[doctype] = self.calls.get(doctype, 0) + 1
			if doctype == "Grove User":
				return list(users)
			if doctype == "Grove Model Row":
				return list(rows)
			if doctype == "Model Group Row":
				return list(groups)
			raise AssertionError(f"unexpected get_all({doctype})")

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			return snapshot.effective_users()

	def test_a_user_carries_their_groups_their_deltas_and_their_budget_flag(self):
		[user] = self.users(
			[frappe._dict(name="GU-1", user="a@x.com", rate_limited=1)],
			[
				frappe._dict(parent="GU-1", model="qwen3-4b", parentfield="allow"),
				frappe._dict(parent="GU-1", model="qwen3-35b", parentfield="deny"),
			],
			[frappe._dict(parent="GU-1", model_group="acme")],
		)
		self.assertEqual(user["name"], "GU-1")
		self.assertEqual(user["email"], "a@x.com")
		self.assertEqual(user["group"], "acme")
		self.assertEqual(user["allow"], "qwen3-4b")
		self.assertEqual(user["deny"], "qwen3-35b")
		self.assertIs(user["limited"], True)

	def test_the_lists_are_sorted_comma_joins(self):
		# The agent splits on commas (pathway, internal/domain/access.go `ModelSet`), so the
		# join is the wire format, not a display choice.
		[user] = self.users(
			[frappe._dict(name="GU-1", user="a@x.com", rate_limited=0)],
			[
				frappe._dict(parent="GU-1", model="b", parentfield="allow"),
				frappe._dict(parent="GU-1", model="a", parentfield="allow"),
			],
			[
				frappe._dict(parent="GU-1", model_group="zeta"),
				frappe._dict(parent="GU-1", model_group="acme"),
			],
		)
		self.assertEqual(user["allow"], "a,b")
		self.assertEqual(user["group"], "acme,zeta")

	def test_the_same_group_twice_is_one_membership(self):
		# Two rows naming one group are one grant, so the hash cannot move on a duplicate.
		[user] = self.users(
			[frappe._dict(name="GU-1", user="a@x.com", rate_limited=0)],
			[],
			[
				frappe._dict(parent="GU-1", model_group="acme"),
				frappe._dict(parent="GU-1", model_group="acme"),
			],
		)
		self.assertEqual(user["group"], "acme")

	def test_a_user_who_grants_nothing_is_still_pushed_as_blank(self):
		# Blank overwrites Redis; omitting the fields would leave a removed allow in force.
		[user] = self.users([frappe._dict(name="GU-1", user="a@x.com", rate_limited=0)])
		self.assertEqual((user["group"], user["allow"], user["deny"]), ("", "", ""))
		self.assertIs(user["limited"], False)

	def test_payload_logging_is_off_unless_the_doc_opts_in(self):
		# Customer content: a doc from before the field existed (no attribute at all) stays off.
		[user] = self.users([frappe._dict(name="GU-1", user="a@x.com", rate_limited=0)])
		self.assertIs(user["log_payloads"], False)

	def test_payload_logging_opt_in_reaches_the_record(self):
		[user] = self.users(
			[frappe._dict(name="GU-1", user="a@x.com", rate_limited=0, log_payloads=1)]
		)
		self.assertIs(user["log_payloads"], True)

	def test_one_row_query_covers_every_user(self):
		# The N+1 this projection removes: one query for the users, one for their rows.
		users = self.users(
			[
				frappe._dict(name="GU-1", user="a@x.com", rate_limited=0),
				frappe._dict(name="GU-2", user="b@x.com", rate_limited=0),
			],
			[
				frappe._dict(parent="GU-1", model="m1", parentfield="allow"),
				frappe._dict(parent="GU-2", model="m2", parentfield="allow"),
			],
			[
				frappe._dict(parent="GU-1", model_group="acme"),
				frappe._dict(parent="GU-2", model_group="beta"),
			],
		)
		self.assertEqual([u["allow"] for u in users], ["m1", "m2"])
		self.assertEqual([u["group"] for u in users], ["acme", "beta"])
		# Three tables, still three queries: membership must not become the N+1 again.
		self.assertEqual(self.calls, {"Grove User": 1, "Grove Model Row": 1, "Model Group Row": 1})

	def test_a_user_carries_their_balance_in_nano_usd(self):
		# settle keeps `balance`; a store adds what it already counted, not here.
		[user] = self.users([frappe._dict(name="GU-1", user="a@x.com", rate_limited=0, free=0, balance=7.5)])
		self.assertEqual((user["prepaid"], user["budget"]), (True, 7_500_000_000))

	def test_a_free_user_carries_no_ceiling(self):
		# The wire still says `prepaid`: absent on an old push has to read as no gate.
		[user] = self.users([frappe._dict(name="GU-1", user="a@x.com", rate_limited=0, free=1, balance=2.5)])
		self.assertEqual((user["prepaid"], user["budget"]), (False, 0))


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
			projected = snapshot.effective_keys()
		self.filters = seen.get("filters")
		return projected

	def test_a_key_points_at_its_holder_and_says_nothing_about_access(self):
		# The whole split: anything read here would be rewritten on every key the person holds
		# each time their access moved.
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
		# A revoked key is not projected at all, so its bucket's hash moves and the push prunes it.
		# Asserted on the FILTER, because leaving it out keeps every dead credential alive.
		self.keys([frappe._dict(name="KEY-1", key_hash="abc", user="GU-1", status="active")])
		self.assertEqual(self.filters, {"status": "active"})


class TestSnapshotHashes(unittest.TestCase):
	"""The gate the whole sync stands on: same state → same hashes (or every tick re-pushes the
	fleet), and one record's change → exactly its own bucket moves (or one key minted re-ships
	the population)."""

	def key(self, key_hash, user="GU-1"):
		return {"key_hash": key_hash, "prefix": "K-" + key_hash, "user": user, "status": "active"}

	def test_bucket_of_is_a_two_hex_label(self):
		label = snapshot.bucket_of("anything")
		self.assertRegex(label, r"^[0-9a-f]{2}$")
		self.assertEqual(label, snapshot.bucket_of("anything"))

	def test_the_same_content_hashes_the_same(self):
		records = [self.key("aa"), self.key("bb")]
		one = snapshot.bucketed_section(records, "key_hash")
		two = snapshot.bucketed_section(list(records), "key_hash")
		self.assertEqual(one, two)

	def test_a_changed_record_moves_only_its_own_bucket(self):
		keys = [self.key(f"k{i}") for i in range(32)]
		before = snapshot.bucketed_section(keys, "key_hash")["buckets"]
		keys[0] = {**keys[0], "user": "GU-2"}
		after = snapshot.bucketed_section(keys, "key_hash")["buckets"]
		moved = [b for b in before if before[b]["hash"] != after[b]["hash"]]
		self.assertEqual(moved, [snapshot.bucket_of("k0")])

	def test_a_flat_section_carries_its_hash(self):
		section = snapshot.flat_section({"table": {"m": []}})
		self.assertIn("hash", section)
		self.assertEqual(section["table"], {"m": []})


class TestDelta(unittest.TestCase):
	"""What a non-forced run sends: the sections the box does not already hold, and nothing
	when it holds everything — the no-op tick that keeps the log quiet."""

	def snapshot(self, keys=()):
		return {
			"groups": snapshot.flat_section({"records": [], "catalog": ""}),
			"keys": snapshot.bucketed_section(list(keys), "key_hash"),
		}

	def hashes(self, snapshot):
		out = {}
		for section, content in snapshot.items():
			if "buckets" in content:
				for label, bucket in content["buckets"].items():
					out[f"{section}:{label}"] = bucket["hash"]
			else:
				out[section] = content["hash"]
		return out

	def key(self, key_hash, user="GU-1"):
		return {"key_hash": key_hash, "prefix": "K", "user": user, "status": "active"}

	def test_a_box_holding_everything_gets_nothing(self):
		desired = self.snapshot([self.key("aa")])
		self.assertEqual(snapshot.snapshot_delta(desired, self.hashes(desired)), {})

	def test_a_wiped_box_gets_everything(self):
		# What a wiped Redis reports: the resync backstop, as one ordinary tick.
		desired = self.snapshot([self.key("aa")])
		self.assertEqual(snapshot.snapshot_delta(desired, {}), desired)

	def test_only_the_changed_bucket_is_sent(self):
		old = self.snapshot([self.key("aa"), self.key("bb")])
		new = self.snapshot([self.key("aa", user="GU-2"), self.key("bb")])
		delta = snapshot.snapshot_delta(new, self.hashes(old))
		self.assertEqual(list(delta), ["keys"])
		self.assertEqual(list(delta["keys"]["buckets"]), [snapshot.bucket_of("aa")])

	def test_a_bucket_the_box_still_holds_but_no_longer_exists_is_sent_empty(self):
		# The last key in a bucket was deleted, and the box's hash map still names it — so it is
		# pushed explicitly EMPTY rather than left forever.
		old = self.snapshot([self.key("aa")])
		delta = snapshot.snapshot_delta(self.snapshot(), self.hashes(old))
		self.assertEqual(
			delta["keys"]["buckets"][snapshot.bucket_of("aa")], {"records": []}
		)

	def test_a_changed_flat_section_is_sent_whole(self):
		desired = self.snapshot()
		remote = {**self.hashes(desired), "groups": "stale"}
		self.assertEqual(snapshot.snapshot_delta(desired, remote), {"groups": desired["groups"]})


class TestSyncTarget(unittest.TestCase):
	"""One box brought to the snapshot: skipped silently when it already holds it, pushed and
	logged when it does not, and classified — down vs rejected — when the push fails."""

	SNAPSHOT = {"groups": {"records": [], "catalog": "", "hash": "h1"}}

	def sync(self, remote=None, force=False, post=None, get=None):
		calls = {"posted": None}

		def _post(path, payload):
			calls["posted"] = (path, payload)
			if post:
				raise post
			return {"counts": {"groups": 0}}

		def _remote():
			if get:
				raise get
			return remote or {}

		with (
			unittest.mock.patch.object(Target, "post", side_effect=_post),
			unittest.mock.patch.object(Target, "remote_hashes", side_effect=_remote),
		):
			target = Target("Gateway Server", "gw1", "http://x", "t")
			row = projection.push_target(target, self.SNAPSHOT, force)
		return row, calls

	def test_a_box_already_in_sync_is_not_pushed_and_leaves_no_row(self):
		row, calls = self.sync(remote={"groups": "h1"})
		self.assertIsNone(row)
		self.assertIsNone(calls["posted"])

	def test_drift_is_pushed_to_the_state_endpoint(self):
		row, calls = self.sync(remote={"groups": "stale"})
		self.assertEqual(row["success"], 1)
		self.assertEqual(calls["posted"], ("state", self.SNAPSHOT))
		self.assertIn("groups", row["detail"])

	def test_force_skips_the_hash_read_and_pushes_everything(self):
		import requests

		row, calls = self.sync(force=True, get=requests.ConnectionError("no GET should happen"))
		self.assertEqual(row["success"], 1)
		self.assertEqual(calls["posted"], ("state", self.SNAPSHOT))

	def test_a_box_that_is_down_reads_as_unreachable_not_rejected(self):
		import requests

		row, _calls = self.sync(get=requests.ConnectionError("refused"))
		self.assertEqual((row["reachable"], row["success"]), (0, 0))

	def test_an_old_agent_404_is_a_loud_failure(self):
		# No fallback: a box on the old binary logs a failed row every tick, which is the point.
		import requests

		response = unittest.mock.Mock(status_code=404)
		row, _calls = self.sync(remote={}, post=requests.HTTPError(response=response))
		self.assertEqual((row["reachable"], row["success"], row["http_status"]), (1, 0, 404))


class TestAPushNeedsNoFrappe(unittest.TestCase):
	"""push_target and in_turn run on a pool thread, where there is no frappe.local."""

	def on_a_bare_thread(self, work):
		box = {}
		thread = threading.Thread(target=lambda: box.update(result=work()))
		thread.start()
		thread.join(5)
		return box["result"]

	def push(self, target, desired):
		with (
			unittest.mock.patch.object(Target, "remote_hashes", return_value={}),
			unittest.mock.patch.object(Target, "post", return_value={}),
		):
			return self.on_a_bare_thread(
				lambda: run.in_turn(Unit(None, (target,)), lambda t: projection.push_target(t, desired, False))
			)

	def test_a_push_carries_its_own_payload(self):
		target = Target("Gateway Server", "gw1", "http://x", "t")
		desired = {"groups": {"records": [{"name": "only-gw1", "key_hash": "secret"}], "hash": "h"}}
		outcome, [row] = self.push(target, desired)
		self.assertIs(outcome, True)
		self.assertEqual((row["server_type"], row["server"], row["success"]), ("Gateway Server", "gw1", 1))
		[sent] = row["payload"]
		self.assertEqual(sent["push"], "state")
		self.assertEqual(sent["body"]["groups"]["records"], [{"name": "only-gw1", "key_hash": "***"}])

	def test_a_box_that_could_not_be_resolved_is_a_failed_row_not_a_call(self):
		target = Target("Gateway Server", "gw1", error="ValidationError: no admin_url")
		outcome, [row] = self.push(target, {"groups": {"hash": "h"}})
		self.assertIs(outcome, False)
		self.assertEqual((row["success"], row["error"], row["payload"]), (0, "ValidationError: no admin_url", []))

	def test_a_box_with_no_admin_url_resolves_to_its_reason(self):
		with unittest.mock.patch.object(frappe, "get_doc", side_effect=ValueError("no admin_url")):
			target = Target.resolve("Gateway Server", "gw1")
		self.assertEqual(target.error, "ValueError: no admin_url")


class TestCheckState(unittest.TestCase):
	"""The Check State button: what a tick would push, said out loud, nothing sent."""

	SNAPSHOT = {
		"groups": {"records": [], "catalog": "", "hash": "g1"},
		"keys": {"buckets": {"3f": {"records": [{"key_hash": "aa"}], "hash": "k1"}}},
	}

	def check(self, remote):
		with (
			unittest.mock.patch.object(snapshot, "gateway_snapshot", return_value=self.SNAPSHOT),
			unittest.mock.patch.object(snapshot, "gateway_geography", return_value="in"),
			unittest.mock.patch.object(snapshot, "gateway_redis", return_value="gw1"),
			unittest.mock.patch.object(frappe, "get_doc", return_value=None),
			unittest.mock.patch.object(Target, "of", return_value=Target("Gateway Server", "gw1", "http://x", "t")),
			unittest.mock.patch.object(Target, "remote_hashes", return_value=remote),
			unittest.mock.patch.object(
				Target, "post",
				side_effect=AssertionError("check_state must never push"),
			),
		):
			return projection.check_state("Gateway Server", "gw1")

	def test_a_matching_box_reads_in_sync(self):
		result = self.check({"groups": "g1", "keys:3f": "k1"})
		self.assertEqual(result, {"in_sync": True, "drift": []})

	def test_drift_names_the_sections_a_tick_would_push(self):
		result = self.check({"groups": "stale", "keys:3f": "k1"})
		self.assertEqual(result, {"in_sync": False, "drift": ["groups"]})

	def test_a_differing_bucket_reports_its_count(self):
		result = self.check({"groups": "g1"})
		self.assertEqual(result["drift"], ["keys[1]"])


class FakeRun:
	name = "AGS-TEST"

	def __init__(self):
		self.results = []
		self.inserted = False

	def acquire_lock(self, wait=0):
		return True

	def release_lock(self):
		pass

	def append(self, _field, row):
		self.results.append(row)

	def insert(self, ignore_permissions=False):
		self.inserted = True


def resolved(server_type, name):
	return Target(server_type, name, "http://x", "t")


class TestSyncProjection(unittest.TestCase):
	"""The run: one snapshot built for all gateways, one per ingress, and a log doc only when
	something was actually pushed — a fleet in sync leaves nothing behind."""

	def run_projection(self, results, proxies=("gw1", "gw2"), entry=None, groups=None, **kwargs):
		doc = FakeRun()
		targets = []
		self.stamps = []

		def push_target(target, _snapshot, force):
			targets.append((target.server_type, target.name, force))
			result = results.get(target.name)
			return result(target) if callable(result) else result

		def set_value(doctype, name, field, value, update_modified=True):
			self.stamps.append((name, field))

		with (
			unittest.mock.patch.object(run, "new_run", return_value=doc),
			unittest.mock.patch.object(
				run, "sync_targets",
				return_value=groups if groups is not None else [(None, [proxy]) for proxy in proxies],
			),
			unittest.mock.patch.object(projection, "active_ingresses", return_value=[]),
			unittest.mock.patch.object(snapshot, "gateway_snapshot", return_value={"s": 1}),
			unittest.mock.patch.object(snapshot, "gateway_geography", return_value="in"),
			unittest.mock.patch.object(snapshot, "gateway_redis", side_effect=lambda gateway: gateway),
			unittest.mock.patch.object(Target, "resolve", side_effect=resolved),
			unittest.mock.patch.object(projection, "push_target", side_effect=push_target),
			unittest.mock.patch.object(
				frappe, "db", frappe._dict(commit=lambda: None, set_value=set_value)
			),
			unittest.mock.patch.object(
				frappe.utils, "now_datetime", lambda: "2026-08-16 00:00:00"
			),
		):
			name = (entry or projection.sync_projection)(**kwargs)
		return name, doc, targets

	def row(self, success=1):
		return {"reachable": 1, "success": success, "http_status": 0, "error": None,
		        "duration_ms": 1, "detail": "", "payload": ""}

	def test_a_fleet_in_sync_logs_no_doc(self):
		name, doc, targets = self.run_projection({})
		self.assertIsNone(name)
		self.assertFalse(doc.inserted)
		self.assertEqual(len(targets), 2)  # every box was still checked

	def test_a_gateway_is_never_stamped(self):
		# Gateways on a store are reached through one writer, so a per-gateway timestamp would read
		# stale on every other one. The Pathway Sync rows are the record.
		self.run_projection({"gw1": self.row()})
		self.assertEqual(self.stamps, [])

	def test_a_store_is_pushed_through_its_first_writer_that_answers(self):
		unreachable = {**self.row(success=0), "reachable": 0}
		_name, doc, targets = self.run_projection(
			{"gw1": unreachable, "gw2": self.row(), "gw3": self.row()},
			groups=[("store1", ["gw1", "gw2", "gw3"])],
		)
		self.assertEqual([name for _, name, _ in targets], ["gw1", "gw2"])
		self.assertEqual([row["server"] for row in doc.results], ["gw1", "gw2"])
		# One store, and it got the state: the dead writer is a row, not a failed run.
		self.assertEqual((doc.targets_total, doc.targets_ok, doc.status), (1, 1, "Success"))

	def test_a_store_whose_writers_all_fail_fails_the_run(self):
		_name, doc, _targets = self.run_projection(
			{"gw1": self.row(success=0), "gw2": self.row(success=0)}, groups=[("store1", ["gw1", "gw2"])]
		)
		self.assertEqual((doc.targets_total, doc.targets_ok, doc.status), (1, 0, "Failed"))

	def test_a_writer_already_holding_the_state_ends_the_group(self):
		_name, _doc, targets = self.run_projection({}, groups=[("store1", ["gw1", "gw2"])])
		self.assertEqual([name for _, name, _ in targets], ["gw1"])

	def test_a_store_with_no_writer_is_reported(self):
		# Nothing would update it, and its gateways would serve revoked keys without a word.
		_name, doc, targets = self.run_projection({}, groups=[("store1", [])])
		self.assertEqual(targets, [])
		[row] = doc.results
		self.assertEqual((row["server_type"], row["server"]), ("Gateway Store", "store1"))
		self.assertEqual(doc.status, "Failed")

	def test_a_pushed_box_lands_on_the_run_doc(self):
		name, doc, _targets = self.run_projection({"gw1": self.row()})
		self.assertEqual(name, "AGS-TEST")
		self.assertEqual(doc.status, "Success")
		self.assertEqual((doc.targets_total, doc.targets_ok), (1, 1))
		self.assertEqual(doc.results[0]["server"], "gw1")

	def test_a_failed_push_marks_the_run(self):
		_name, doc, _targets = self.run_projection({"gw1": self.row(success=0), "gw2": self.row()})
		self.assertEqual(doc.status, "Partial")

	def test_rows_land_in_the_order_asked_whichever_box_answers_first(self):
		first_may_answer = threading.Event()

		def slow(_target):
			first_may_answer.wait(5)
			return self.row()

		def fast(_target):
			first_may_answer.set()
			return self.row()

		_name, doc, _targets = self.run_projection({"gw1": slow, "gw2": fast})
		self.assertEqual([row["server"] for row in doc.results], ["gw1", "gw2"])

	def test_boxes_are_dialled_side_by_side(self):
		# Each waits for the other: in sequence the first would time out alone at the barrier.
		together = threading.Barrier(2, timeout=5)

		def meet(_target):
			together.wait()
			return self.row()

		_name, doc, _targets = self.run_projection({"gw1": meet, "gw2": meet})
		self.assertEqual((doc.targets_total, doc.targets_ok), (2, 2))

	def test_one_box_needs_no_pool(self):
		with unittest.mock.patch.object(run, "ThreadPoolExecutor", side_effect=AssertionError("pool")):
			_name, doc, _targets = self.run_projection({"gw1": self.row()}, proxies=("gw1",))
		self.assertEqual(doc.status, "Success")

	def test_a_push_that_raises_fails_its_own_row_and_the_rest_land(self):
		def boom(_target):
			raise RuntimeError("bug in the push")

		_name, doc, _targets = self.run_projection({"gw1": boom, "gw2": self.row()})
		bad, good = doc.results
		self.assertEqual((bad["server"], good["server"]), ("gw1", "gw2"))
		self.assertIn("bug in the push", bad["error"])
		self.assertEqual((doc.targets_total, doc.targets_ok, doc.status), (2, 1, "Partial"))

	def test_full_sync_forces_every_box(self):
		# The wrapper the operator buttons call — it must arrive with force on.
		_name, _doc, targets = self.run_projection({"gw1": self.row()}, proxies=("gw1",), entry=projection.full_sync)
		self.assertEqual(targets, [("Gateway Server", "gw1", True)])

	def test_an_empty_proxies_list_means_no_gateway_work(self):
		# `is None` and not truthiness: an ingress-only run names proxies=[] on purpose, and
		# reading that as "unspecified" would push the whole fleet.
		doc = FakeRun()
		seen = []
		with (
			unittest.mock.patch.object(run, "new_run", return_value=doc),
			unittest.mock.patch.object(snapshot, "ingress_snapshot", return_value={}),
			unittest.mock.patch.object(Target, "resolve", side_effect=resolved),
			unittest.mock.patch.object(
				projection, "push_target",
				side_effect=lambda target, *_: seen.append((target.server_type, target.name)) or None,
			),
			unittest.mock.patch.object(
				frappe, "db",
				frappe._dict(commit=lambda: None, set_value=lambda *a, **k: None),
			),
			unittest.mock.patch.object(
				frappe.utils, "now_datetime", lambda: "2026-08-16 00:00:00"
			),
		):
			projection.sync_projection(proxies=[], ingresses=["ing1"])
		self.assertEqual(seen, [("Ingress Server", "ing1")])


class TestSyncTargets(unittest.TestCase):
	"""Which gateways a run reaches: each Redis once, a store only through the gateways marked to."""

	def targets(self, gateways):
		rows = [frappe._dict(name=name, gateway_store=store, is_store_writer=writer) for name, store, writer in gateways]
		with unittest.mock.patch.object(frappe, "get_all", return_value=rows) as get_all:
			groups = run.sync_targets()
		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": "Active"})
		return groups

	def test_a_gateway_not_yet_on_a_store_is_reached_directly(self):
		self.assertEqual(self.targets([("gw1", None, 0)]), [(None, ["gw1"])])

	def test_a_store_is_reached_through_its_writers_only(self):
		groups = self.targets([("gw1", "store1", 1), ("gw2", "store1", 0), ("gw3", "store1", 1)])
		self.assertEqual(groups, [("store1", ["gw1", "gw3"])])

	def test_a_store_with_no_writer_is_an_empty_group(self):
		self.assertEqual(self.targets([("gw1", "store1", 0)]), [("store1", [])])

	def test_gateways_not_yet_on_a_store_first_then_stores(self):
		groups = self.targets([("gw1", "store1", 1), ("gw2", None, 0)])
		self.assertEqual(groups, [(None, ["gw2"]), ("store1", ["gw1"])])


class TestTheTickIsTheOnlyAutomaticPush(unittest.TestCase):
	"""Projection is the cron tick's job. Every other caller is an operator pressing a button —
	an inline push in a lifecycle hook is the drift this replaced."""

	BUTTONS = {
		"pathway/projection.py",
		"grove/doctype/gateway_server/gateway_server.py",
		"grove/doctype/ingress_server/ingress_server.py",
		"grove/doctype/pathway_sync/pathway_sync.py",
	}

	def test_nothing_but_the_buttons_names_full_sync(self):
		root = Path(grove.__file__).parent
		found = {
			str(path.relative_to(root))
			for path in root.rglob("*.py")
			if "tests" not in path.parts and "full_sync" in path.read_text()
		}
		self.assertEqual(found, self.BUTTONS)


if __name__ == "__main__":
	unittest.main()


class TestCapacityResolvesThroughTheDeployment(unittest.TestCase):
	"""`max_num_seqs` IS the gateway's admission cap, and it moved to the deployment with a
	blank-means-inherit override left on the replica. So the route table has to resolve it.

	Unresolved, a blank replica would advertise `default_concurrency` while its engine actually
	runs the deployment's number — the gateway would admit against a cap the engine never had, and
	the ingress's authoritative per-replica gate would 429 traffic the gateway thought it had
	room for. That is a silent wrong number, not a crash, which is why it is asserted here."""

	def capacity(self, replicas, deployments):
		routes = TestGatewayRoutes().routes(replicas, deployments=deployments)
		return [route["capacity"] for route in routes["qwen3-35b"]]

	def test_a_replica_that_overrides_nothing_reports_its_deployments_cap(self):
		self.assertEqual(
			self.capacity(
				[replica("MD-00007", model_deployment="T1")],
				[deployment("T1", max_num_seqs=256)],
			),
			[256],
		)

	def test_a_replica_that_overrides_reports_its_own(self):
		self.assertEqual(
			self.capacity(
				[replica("MD-00007", max_num_seqs=64, model_deployment="T1")],
				[deployment("T1", max_num_seqs=256)],
			),
			[64],
		)

	def test_siblings_of_one_deployment_are_capped_independently(self):
		# Two replicas of one deployment, one of which was turned down for its box. The gateway
		# holds each to its own number rather than to the deployment's for both.
		self.assertEqual(
			sorted(
				self.capacity(
					[
						replica("MD-00007", model_deployment="T1"),
						replica("MD-00008", max_num_seqs=32, model_deployment="T1"),
					],
					[deployment("T1", max_num_seqs=256)],
				)
			),
			[32, 256],
		)

	def test_a_deployment_that_names_no_cap_still_leaves_a_number_on_the_route(self):
		# Blank all the way up is not "no cap": the gate has to hold the engine to something, and
		# the engine's own default is what the serve command leaves it running at.
		self.assertEqual(
			self.capacity(
				[replica("MD-00007", model_deployment="T1")], [deployment("T1")]
			),
			[VllmEngine.default_concurrency],
		)


class TestDial(unittest.TestCase):
	"""One box dialled: what `work` raised becomes the row, and the fields a run expects are on
	the row whether or not the box was ever reached."""

	def test_a_bug_in_the_work_is_a_failed_row_with_its_reason(self):
		def work(_row):
			raise RuntimeError("bug")

		row = Target("Gateway Server", "gw1", "http://x", "t").dial(work, payload=[])
		self.assertEqual((row["reachable"], row["success"], row["error"], row["payload"]), (1, 0, "RuntimeError: bug", []))

	def test_a_box_that_could_not_be_resolved_keeps_its_fields_and_is_never_dialled(self):
		target = Target("Gateway Server", "gw1", error="no admin_url")
		row = target.dial(lambda _row: self.fail("dialled"), had_data=0)
		self.assertEqual((row["success"], row["error"], row["had_data"]), (0, "no admin_url", 0))
