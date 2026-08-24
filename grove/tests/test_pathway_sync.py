# Copyright (c) 2026, Grove and contributors
# See license.txt
"""What Grove pushes to a proxy: the routing table, and the three access records a request
resolves through — group, then user, then key — plus the hash gate that decides whether a
box is pushed at all. Pure — the docs are mocked, no site.

Every field here is read by something that cannot be changed in the same deploy: the Go agent
unmarshals it, and it lives in its own repo on a box that is updated separately — a released
binary, not a tree this deploy compiles. So the shape is asserted rather than assumed. The
contract is plan_agent_state_sync.md at the repo root.
"""

import unittest
import unittest.mock
from pathlib import Path

import frappe

from grove import pathway_sync
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


class TestGatewayRoutes(unittest.TestCase):
	def routes(self, deployments=(), pods=(), models=("qwen3-35b",)):
		"""_gateway_routes against mocked docs. get_all is dispatched on doctype because the
		function reads several of them, and get_doc only ever supplies the internal key.

		No box here names an ingress, so every route is direct — the shape this whole suite was
		written against, and the shape a fleet that has cut nothing over still has. The ingress
		rows have their own file: test_gateway_routes."""

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Model Deployment":
				# The Active filter moved into the query, so the mock honours it.
				return [d for d in deployments if d.status == (filters or {}).get("status")]
			if doctype == "Model":
				return [frappe._dict(name=m, modality="text") for m in models]
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
			return pathway_sync._gateway_routes()

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
		self.assertEqual(self.routes([deployment("MD-00007", status="Broken")]), {})

	def test_a_model_with_no_engine_is_absent_not_sent_empty(self):
		# The push is the whole table and absence prunes, so an unpublished model's deploy key
		# is deleted because it is no longer named — no empty list needed to say so.
		self.assertNotIn("qwen3-35b", self.routes(models=["qwen3-35b"]))

	def test_the_rows_are_ordered_by_deployment(self):
		# The snapshot hash is over the serialized table, so a query returning the same rows in
		# a different order must not read as drift and re-push the fleet.
		routes = self.routes([deployment("MD-00008"), deployment("MD-00007")])["qwen3-35b"]
		self.assertEqual([r["deployment"] for r in routes], ["MD-00007", "MD-00008"])

	def test_a_route_carries_the_engines_concurrency_cap(self):
		# --max-num-seqs is what the engine runs at once; past it vLLM queues where the gateway
		# can neither see the wait nor spend it on a replica. So it is admission control too.
		[route] = self.routes([deployment("MD-00007", max_num_seqs=64)])["qwen3-35b"]
		self.assertEqual(route["capacity"], 64)

	def test_a_pod_carries_its_own_cap(self):
		[route] = self.routes(pods=[pod("POD-1", max_num_seqs=16)])["qwen3-35b"]
		self.assertEqual(route["capacity"], 16)

	def test_an_unset_cap_falls_back_to_the_assumed_one(self):
		# Blank is not "no cap": the route still carries a number, because the capacity gate has to
		# hold the engine to something. It is an ASSUMPTION though — the serve command passes no
		# --max-num-seqs when the placement names none, so vLLM sizes its own and the two can
		# differ. A placement that needs them identical sets max_num_seqs, which pins both.
		[route] = self.routes([deployment("MD-00007")])["qwen3-35b"]
		self.assertEqual(route["capacity"], DEFAULT_MAX_NUM_SEQS)


class TestRouteModality(unittest.TestCase):
	"""Which OpenAI surface a model answers on rides on its route rows.

	Stamped per row because deploy:<model> is the only thing pushed per model — a separate record
	would mean a new namespace for one short string. The gateway refuses a request for a surface
	the modality does not cover, so a wrong value here is a 404 on a working model."""

	def routes(self, models, deployments=(), pods=()):
		def get_all(doctype, **kwargs):
			if doctype == "Model":
				return [frappe._dict(name=n, modality=m) for n, m in models.items()]
			if doctype == "Model Deployment":
				return list(deployments)
			if doctype == "Pod":
				return list(pods)
			return []

		with (
			unittest.mock.patch.object(pathway_sync.frappe, "get_all", get_all),
			unittest.mock.patch.object(
				pathway_sync.frappe, "get_doc",
				lambda *a: frappe._dict(get_password=lambda *_a, **_k: "k"),
			),
			unittest.mock.patch.object(pathway_sync, "_ingress_targets", lambda: {}),
		):
			return pathway_sync._gateway_routes()

	def test_a_deployment_row_carries_its_models_modality(self):
		routes = self.routes(
			{"qwen3-4b": "text"},
			deployments=[deployment("MD-1", model="qwen3-4b")],
		)
		self.assertEqual(routes["qwen3-4b"][0]["modality"], "text")

	def test_a_pod_row_carries_it_too(self):
		# The ASR container is a Pod, never a Model Deployment — the path that actually matters here.
		routes = self.routes(
			{"nemotron-asr": "audio"},
			pods=[pod("test-nemo-asr", model="nemotron-asr")],
		)
		self.assertEqual(routes["nemotron-asr"][0]["modality"], "audio")

	def test_a_model_with_no_modality_sends_blank_not_null(self):
		# The gateway reads blank as unrestricted. None would serialise as null and read as a value.
		routes = self.routes(
			{"qwen3-4b": None},
			deployments=[deployment("MD-1", model="qwen3-4b")],
		)
		self.assertEqual(routes["qwen3-4b"][0]["modality"], "")


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
			return pathway_sync._effective_groups()

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
		# Blank means "grants nothing", not "unset" — an emptied group has to overwrite the
		# grant already in Redis, so it cannot be omitted.
		[group] = self.groups(["acme"])
		self.assertEqual(group["models"], "")

	def test_the_records_are_ordered_by_name(self):
		# Same reason as the route rows: the section hash must not move on query order.
		groups = self.groups(["b", "a"])
		self.assertEqual([g["name"] for g in groups], ["a", "b"])


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
			return pathway_sync._public_catalog(), seen

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

	def users(self, users=(), rows=()):
		def get_all(doctype, **kwargs):
			if doctype == "Grove User":
				return list(users)
			if doctype == "Grove Model Row":
				return list(rows)
			raise AssertionError(f"unexpected get_all({doctype})")

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			return pathway_sync._effective_users()

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
		# The agent splits on commas (pathway, internal/domain/access.go `ModelSet`), so the
		# join is the wire format, not a display choice.
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
			projected = pathway_sync._effective_keys()
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
		# A revoked key is not restated as revoked — it is not projected at all, so its bucket's
		# hash moves and the push prunes it off every box. Asserted on the filter because leaving
		# it out would silently keep every dead credential alive.
		self.keys([frappe._dict(name="KEY-1", key_hash="abc", user="GU-1", status="active")])
		self.assertEqual(self.filters, {"status": "active"})


class TestSnapshotHashes(unittest.TestCase):
	"""The gate the whole sync stands on: same state → same hashes (or every tick re-pushes the
	fleet), and one record's change → exactly its own bucket moves (or one key minted re-ships
	the population)."""

	def key(self, key_hash, user="GU-1"):
		return {"key_hash": key_hash, "prefix": "K-" + key_hash, "user": user, "status": "active"}

	def test_bucket_of_is_a_two_hex_label(self):
		label = pathway_sync.bucket_of("anything")
		self.assertRegex(label, r"^[0-9a-f]{2}$")
		self.assertEqual(label, pathway_sync.bucket_of("anything"))

	def test_the_same_content_hashes_the_same(self):
		records = [self.key("aa"), self.key("bb")]
		one = pathway_sync._bucketed_section(records, "key_hash")
		two = pathway_sync._bucketed_section(list(records), "key_hash")
		self.assertEqual(one, two)

	def test_a_changed_record_moves_only_its_own_bucket(self):
		keys = [self.key(f"k{i}") for i in range(32)]
		before = pathway_sync._bucketed_section(keys, "key_hash")["buckets"]
		keys[0] = {**keys[0], "user": "GU-2"}
		after = pathway_sync._bucketed_section(keys, "key_hash")["buckets"]
		moved = [b for b in before if before[b]["hash"] != after[b]["hash"]]
		self.assertEqual(moved, [pathway_sync.bucket_of("k0")])

	def test_a_flat_section_carries_its_hash(self):
		section = pathway_sync._flat_section({"table": {"m": []}})
		self.assertIn("hash", section)
		self.assertEqual(section["table"], {"m": []})


class TestDelta(unittest.TestCase):
	"""What a non-forced run sends: the sections the box does not already hold, and nothing
	when it holds everything — the no-op tick that keeps the log quiet."""

	def snapshot(self, keys=()):
		return {
			"groups": pathway_sync._flat_section({"records": [], "catalog": ""}),
			"keys": pathway_sync._bucketed_section(list(keys), "key_hash"),
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
		snapshot = self.snapshot([self.key("aa")])
		self.assertEqual(pathway_sync._delta(snapshot, self.hashes(snapshot)), {})

	def test_a_wiped_box_gets_everything(self):
		# An empty hash map is what a fresh or wiped Redis reports — the resync backstop this
		# design replaced, as one ordinary tick.
		snapshot = self.snapshot([self.key("aa")])
		self.assertEqual(pathway_sync._delta(snapshot, {}), snapshot)

	def test_only_the_changed_bucket_is_sent(self):
		old = self.snapshot([self.key("aa"), self.key("bb")])
		new = self.snapshot([self.key("aa", user="GU-2"), self.key("bb")])
		delta = pathway_sync._delta(new, self.hashes(old))
		self.assertEqual(list(delta), ["keys"])
		self.assertEqual(list(delta["keys"]["buckets"]), [pathway_sync.bucket_of("aa")])

	def test_a_bucket_the_box_still_holds_but_no_longer_exists_is_sent_empty(self):
		# The last key in a bucket was deleted. The box's hash map still names the bucket, so it
		# is pushed explicitly empty — the agent prunes its members — rather than left forever.
		old = self.snapshot([self.key("aa")])
		delta = pathway_sync._delta(self.snapshot(), self.hashes(old))
		self.assertEqual(
			delta["keys"]["buckets"][pathway_sync.bucket_of("aa")], {"records": []}
		)

	def test_a_changed_flat_section_is_sent_whole(self):
		snapshot = self.snapshot()
		remote = {**self.hashes(snapshot), "groups": "stale"}
		self.assertEqual(pathway_sync._delta(snapshot, remote), {"groups": snapshot["groups"]})


class TestSyncTarget(unittest.TestCase):
	"""One box brought to the snapshot: skipped silently when it already holds it, pushed and
	logged when it does not, and classified — down vs rejected — when the push fails."""

	SNAPSHOT = {"groups": {"records": [], "catalog": "", "hash": "h1"}}

	def sync(self, remote=None, force=False, post=None, get=None):
		calls = {"posted": None}

		def _post(_url, _token, path, payload, method="POST"):
			calls["posted"] = (path, payload)
			if post:
				raise post
			return {"counts": {"groups": 0}}

		def _remote(_url, _token):
			if get:
				raise get
			return remote or {}

		with (
			unittest.mock.patch.object(pathway_sync, "_conn", return_value=(None, "http://x", "t")),
			unittest.mock.patch.object(pathway_sync, "_post", side_effect=_post),
			unittest.mock.patch.object(pathway_sync, "remote_hashes", side_effect=_remote),
			unittest.mock.patch.object(frappe, "local", frappe._dict()),
		):
			row = pathway_sync._sync_target("Gateway Server", "gw1", self.SNAPSHOT, force)
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
		# No fallback to the per-section endpoints: a box still on the old binary logs a failed
		# row every tick until someone updates it, which is the point.
		import requests

		response = unittest.mock.Mock(status_code=404)
		row, _calls = self.sync(remote={}, post=requests.HTTPError(response=response))
		self.assertEqual((row["reachable"], row["success"], row["http_status"]), (1, 0, 404))


class TestCheckState(unittest.TestCase):
	"""The Check State button: what a tick would push, said out loud, nothing sent."""

	SNAPSHOT = {
		"groups": {"records": [], "catalog": "", "hash": "g1"},
		"keys": {"buckets": {"3f": {"records": [{"key_hash": "aa"}], "hash": "k1"}}},
	}

	def check(self, remote):
		with (
			unittest.mock.patch.object(pathway_sync, "gateway_snapshot", return_value=self.SNAPSHOT),
			unittest.mock.patch.object(pathway_sync, "_conn", return_value=(None, "http://x", "t")),
			unittest.mock.patch.object(pathway_sync, "remote_hashes", return_value=remote),
			unittest.mock.patch.object(
				pathway_sync, "_post",
				side_effect=AssertionError("check_state must never push"),
			),
		):
			return pathway_sync.check_state("Gateway Server", "gw1")

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


class TestSyncProjection(unittest.TestCase):
	"""The run: one snapshot built for all gateways, one per ingress, and a log doc only when
	something was actually pushed — a fleet in sync leaves nothing behind."""

	def run_projection(self, results, proxies=("gw1", "gw2"), entry=None, **kwargs):
		doc = FakeRun()
		targets = []
		self.stamps = []

		def sync_target(server_type, name, _snapshot, force):
			targets.append((server_type, name, force))
			return results.get(name)

		def set_value(doctype, name, field, value, update_modified=True):
			self.stamps.append((name, field))

		with (
			unittest.mock.patch.object(pathway_sync, "_new_run", return_value=doc),
			unittest.mock.patch.object(pathway_sync, "_active_proxies", return_value=list(proxies)),
			unittest.mock.patch.object(pathway_sync, "_active_ingresses", return_value=[]),
			unittest.mock.patch.object(pathway_sync, "gateway_snapshot", return_value={"s": 1}),
			unittest.mock.patch.object(pathway_sync, "_sync_target", side_effect=sync_target),
			unittest.mock.patch.object(
				frappe, "db", frappe._dict(commit=lambda: None, set_value=set_value)
			),
			unittest.mock.patch.object(
				frappe.utils, "now_datetime", lambda: "2026-08-16 00:00:00"
			),
		):
			name = (entry or pathway_sync.sync_projection)(**kwargs)
		return name, doc, targets

	def row(self, success=1):
		return {"reachable": 1, "success": success, "http_status": 0, "error": None,
		        "duration_ms": 1, "detail": "", "payload": ""}

	def test_a_fleet_in_sync_logs_no_doc(self):
		name, doc, targets = self.run_projection({})
		self.assertIsNone(name)
		self.assertFalse(doc.inserted)
		self.assertEqual(len(targets), 2)  # every box was still checked

	def test_an_in_sync_box_still_gets_its_timestamp(self):
		# The skip writes no row, so last_synced_at is the only trace the check happened —
		# it going stale is how a box that stopped answering shows up.
		self.run_projection({})
		self.assertEqual(self.stamps, [("gw1", "last_synced_at"), ("gw2", "last_synced_at")])

	def test_a_pushed_box_is_stamped_and_a_failed_one_is_not(self):
		self.run_projection({"gw1": self.row(), "gw2": self.row(success=0)})
		self.assertEqual(self.stamps, [("gw1", "last_synced_at")])

	def test_a_pushed_box_lands_on_the_run_doc(self):
		name, doc, _targets = self.run_projection({"gw1": self.row()})
		self.assertEqual(name, "AGS-TEST")
		self.assertEqual(doc.status, "Success")
		self.assertEqual((doc.targets_total, doc.targets_ok), (1, 1))
		self.assertEqual(doc.results[0]["server"], "gw1")

	def test_a_failed_push_marks_the_run(self):
		_name, doc, _targets = self.run_projection({"gw1": self.row(success=0), "gw2": self.row()})
		self.assertEqual(doc.status, "Partial")

	def test_full_sync_forces_every_box(self):
		# The wrapper the operator buttons call — it must arrive with force on.
		_name, _doc, targets = self.run_projection({"gw1": self.row()}, proxies=("gw1",), entry=pathway_sync.full_sync)
		self.assertEqual(targets, [("Gateway Server", "gw1", True)])

	def test_an_empty_proxies_list_means_no_gateway_work(self):
		# `is None` and not truthiness: an ingress-only run names proxies=[] on purpose, and
		# reading that as "unspecified" would push the whole fleet.
		doc = FakeRun()
		seen = []
		with (
			unittest.mock.patch.object(pathway_sync, "_new_run", return_value=doc),
			unittest.mock.patch.object(pathway_sync, "ingress_snapshot", return_value={}),
			unittest.mock.patch.object(
				pathway_sync, "_sync_target",
				side_effect=lambda *a: seen.append(a) or None,
			),
			unittest.mock.patch.object(
				frappe, "db",
				frappe._dict(commit=lambda: None, set_value=lambda *a, **k: None),
			),
			unittest.mock.patch.object(
				frappe.utils, "now_datetime", lambda: "2026-08-16 00:00:00"
			),
		):
			pathway_sync.sync_projection(proxies=[], ingresses=["ing1"])
		self.assertEqual([(a[0], a[1]) for a in seen], [("Ingress Server", "ing1")])



class TestTheTickIsTheOnlyAutomaticPush(unittest.TestCase):
	"""Projection is the cron tick's job. Every other caller is an operator pressing a button —
	an inline push in a lifecycle hook is the drift this replaced."""

	BUTTONS = {
		"pathway_sync.py",
		"grove/doctype/gateway_server/gateway_server.py",
		"grove/doctype/ingress_server/ingress_server.py",
		"grove/doctype/pathway_sync/pathway_sync.py",
	}

	def test_nothing_but_the_buttons_names_full_sync(self):
		root = Path(pathway_sync.__file__).parent
		found = {
			str(path.relative_to(root))
			for path in root.rglob("*.py")
			if "tests" not in path.parts and "full_sync" in path.read_text()
		}
		self.assertEqual(found, self.BUTTONS)


if __name__ == "__main__":
	unittest.main()
