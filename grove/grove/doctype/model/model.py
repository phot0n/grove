# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.utils import slugify


class Model(Document):
	def autoname(self):
		"""Name = slugged Display Name. The name is the client-facing model id (what
		clients send as `model` and what routes are keyed by), so it's set once at insert
		— editing Display Name later does NOT rename it and break live clients."""
		self.name = slugify(self.display_name)
		if not self.name:
			frappe.throw("Display Name must contain at least one letter or digit.")

	def validate(self):
		# `published` is the user-facing catalog gate (allowed_models blank = all
		# published). A model is only publishable while it actually has a live
		# (Active) Model Deployment — never let a manual toggle claim otherwise.
		if self.published and not has_active_deployment(self.name):
			self.published = 0


def has_active_deployment(model, exclude=None):
	"""True if `model` has a live deployment: >=1 Active Model Deployment (on-prem) OR a
	Running standalone Pod (cloud). `exclude` drops one deployment name (used from Model
	Deployment.on_trash, where the row still exists in the DB during delete)."""
	filters = {"model": model, "status": "Active"}
	if exclude:
		filters["name"] = ("!=", exclude)
	if frappe.db.get_all("Model Deployment", filters=filters, limit=1):
		return True
	return bool(frappe.db.get_all("Pod", filters={"model": model, "status": "Running"}, limit=1))


def sync_published(model, exclude=None):
	"""Recompute Model.published to reflect whether it has a live deployment.
	Called whenever a deployment's status changes (deploy / teardown / broken),
	since that's what makes the model reachable. Written via db.set_value so it
	skips validate (no recursion) and is cheap."""
	if not model or not frappe.db.exists("Model", model):
		return
	want = 1 if has_active_deployment(model, exclude=exclude) else 0
	if frappe.db.get_value("Model", model, "published") != want:
		frappe.db.set_value("Model", model, "published", want)
