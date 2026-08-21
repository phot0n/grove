# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""Registry of the torch.compile caches in the weights bucket — one row per (image digest,
GPU, TP, model), the cache key's invalidation axes. The bucket is the one owner of this
state: boxes push caches after their first healthy boot, Sync From Bucket mirrors what is
there, and absence prunes. Deleting a row deletes its S3 prefix."""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

PREFIX = "compile-cache/"


class CompileCache(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		gpu_model: DF.Data | None
		image_digest: DF.Data | None
		last_synced: DF.Datetime | None
		model_slug: DF.Data | None
		objects: DF.Int
		s3_uri: DF.Data | None
		size_mb: DF.Float
		tensor_parallel_size: DF.Int
	# end: auto-generated types

	def autoname(self):
		self.name = "-".join([
			self.model_slug, self.gpu_model, f"tp{self.tensor_parallel_size}", self.image_digest
		])

	@property
	def prefix(self):
		"""The S3 key prefix this row mirrors — same shape vllm-cache-sync.sh computes."""
		return (
			f"{PREFIX}{self.image_digest}/{self.gpu_model}"
			f"/tp{self.tensor_parallel_size}/{self.model_slug}/"
		)

	def on_trash(self):
		"""Deleting the row deletes the artifacts: the row only mirrors the bucket, so
		leaving the objects there would resurrect it on the next sync."""
		if self.flags.from_sync:
			return
		bucket, client = bucket_and_client()
		keys = list_keys(client, bucket, self.prefix)
		# delete_objects caps at 1000 per call.
		for start in range(0, len(keys), 1000):
			client.delete_objects(
				Bucket=bucket,
				Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]]},
			)


def bucket_and_client():
	"""The bucket name + an S3 client on the mirror keys — the pair that stays on the
	control plane, which is why row deletion is safe to wire to object deletion."""
	settings = frappe.get_single("Grove Settings")
	env = settings.weights_s3_write_environment
	if not env:
		frappe.throw("Set Weights Bucket and the Mirror keys in Grove Settings first.")
	import boto3  # lazy, matching cloud_provider/aws.py — most requests never touch S3

	client = boto3.client(
		"s3",
		region_name=env.get("AWS_DEFAULT_REGION") or None,
		aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
		aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
	)
	return settings.weights_bucket.removeprefix("s3://").strip("/"), client


def list_keys(client, bucket, prefix):
	keys = []
	for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
		keys += [obj["Key"] for obj in page.get("Contents", [])]
	return keys


def entries_from_keys(objects):
	"""Group raw S3 listings into one entry per cache key. A key that does not parse as
	compile-cache/<digest>/<gpu>/tp<N>/<model>/<artifact...> is skipped, not guessed at."""
	entries = {}
	for obj in objects:
		parts = obj["Key"].split("/")
		if len(parts) < 6 or parts[0] != PREFIX.rstrip("/") or not parts[3].startswith("tp"):
			continue
		try:
			key = (parts[1], parts[2], int(parts[3][2:]), parts[4])
		except ValueError:
			continue
		entry = entries.setdefault(key, {"objects": 0, "bytes": 0})
		entry["objects"] += 1
		entry["bytes"] += obj.get("Size") or 0
	return entries


@frappe.whitelist()
def sync_from_bucket():
	"""Button (list view): mirror compile-cache/ into rows — upsert what the bucket holds,
	prune what it no longer does."""
	bucket, client = bucket_and_client()
	entries = entries_from_keys(
		[{"Key": key, "Size": size} for key, size in _keys_with_sizes(client, bucket)]
	)
	seen = set()
	for (digest, gpu, tp, model), stats in entries.items():
		doc = _upsert(digest, gpu, tp, model, stats, bucket)
		seen.add(doc.name)
	for name in frappe.get_list("Compile Cache", pluck="name"):
		if name not in seen:
			stale = frappe.get_doc("Compile Cache", name)
			stale.flags.from_sync = True
			stale.delete()
	return len(seen)


def _keys_with_sizes(client, bucket):
	for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
		for obj in page.get("Contents", []):
			yield obj["Key"], obj.get("Size") or 0


def _upsert(digest, gpu, tp, model, stats, bucket):
	values = {
		"image_digest": digest,
		"gpu_model": gpu,
		"tensor_parallel_size": tp,
		"model_slug": model,
		"objects": stats["objects"],
		"size_mb": round(stats["bytes"] / 1_000_000, 1),
		"last_synced": now_datetime(),
	}
	name = f"{model}-{gpu}-tp{tp}-{digest}"
	if frappe.db.exists("Compile Cache", name):
		doc = frappe.get_doc("Compile Cache", name)
		doc.update(values)
	else:
		doc = frappe.get_doc({"doctype": "Compile Cache", **values})
	doc.s3_uri = f"s3://{bucket}/{PREFIX}{digest}/{gpu}/tp{tp}/{model}"
	doc.save()
	return doc
