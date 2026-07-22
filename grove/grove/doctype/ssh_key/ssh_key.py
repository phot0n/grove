# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SSHKey(Document):
	def validate(self):
		self.public_key = (self.public_key or "").strip()
		# Cheap sanity check: an OpenSSH public key starts with its type token.
		if not self.public_key.startswith(("ssh-", "ecdsa-", "sk-")):
			frappe.throw("Public Key must be a full OpenSSH public key line (e.g. 'ssh-rsa AAAA...').")


def injected_public_keys():
	"""Newline-joined public keys of every active SSH Key — the PUBLIC_KEY env
	injected into a pod at spawn so root's authorized_keys gets all of them.
	Control-plane key is required here or Ansible can't SSH into the pod."""
	keys = frappe.get_all("SSH Key", filters={"active": 1}, pluck="public_key")
	return "\n".join(k.strip() for k in keys if k and k.strip())
