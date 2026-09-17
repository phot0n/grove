# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""One wildcard certificate per Geography, for its zone: issued here on the control plane, stored
on the Geography, pushed to that geography's boxes by Ansible.

DNS-01, not HTTP-01, and that is the whole point. A geography's endpoint resolves to several
gateways, so no single box can answer a challenge for it — and with one wildcard for `*.<zone>`,
the same certificate also covers each box's own `<name>.<zone>`, which is what /grove-admin and
/metrics are reached on. One issuance per zone, one key, N deliveries.

certbot is the ACME client; Grove never speaks ACME. It runs unprivileged with its config, work
and log directories inside the site, so nothing here needs root and the private key never sits in
a world-readable /etc on this host.

Two prerequisites on the control plane, both outside this app: `certbot` and its route53 plugin
(`apt install certbot python3-certbot-dns-route53`, or the pip equivalent in the bench env), and a
DNS Provider whose IAM user holds route53:ChangeResourceRecordSets, route53:ListHostedZonesByName
and route53:GetChange for every zone."""

import os
import subprocess

import frappe

from grove import failure

CERTBOT = "certbot"
# Long enough for a DNS-01 round trip: certbot waits on Route53 propagation before it even asks
# Let's Encrypt to look.
TIMEOUT = 600


def issue_fleet_certificate(geography):
	"""Obtain the geography's wildcard, or renew it early if certbot already holds one. The Geography
	button; the daily job calls renew_fleet_certificate instead."""
	settings = frappe.get_single("Grove Settings")
	zone = require_zone(frappe.get_doc("Geography", geography))
	arguments = ["certonly", "--dns-route53", "--cert-name", zone, "-d", f"*.{zone}"]
	# --force-renewal counts against the duplicate-certificate limit (5 a week for one name).
	arguments += ["--force-renewal"] if is_lineage_stale(zone) else ["--keep-until-expiring"]
	run_certbot(settings, arguments, geography)
	if store_certificate(geography, zone):
		push_to_proxies(geography)


def is_lineage_stale(zone):
	"""True when certbot holds a certificate for this zone from Let's Encrypt's staging CA, which no
	client trusts. Its renewal conf pins that server, so Issue would exit 0 and change nothing."""
	path = os.path.join(certbot_dir("config"), "renewal", f"{zone}.conf")
	if not os.path.exists(path):
		return False
	with open(path) as handle:
		return "acme-staging" in handle.read()


def renew_fleet_certificate():
	"""Daily, per geography that holds a certificate. certbot decides whether anything is due, and
	Grove notices only when the files on disk stop matching what is stored — which is what keeps
	this from reloading the fleet every night. One geography failing does not stop the rest."""
	settings = frappe.get_single("Grove Settings")
	issued = frappe.get_all(
		"Geography", filters={"fleet_zone": ("is", "set"), "fleet_tls_cert": ("is", "set")}, fields=["name", "fleet_zone"]
	)
	for geography in issued:
		try:
			run_certbot(settings, ["renew", "--cert-name", geography.fleet_zone], geography.name)
			if store_certificate(geography.name, geography.fleet_zone):
				push_to_proxies(geography.name)
		except Exception as error:
			frappe.db.rollback()
			frappe.log_error(f"Fleet certificate renewal failed: {geography.name}")
			failure.report("Geography", geography.name, "Fleet certificate renewal failed", str(error))


def push_to_proxies(geography):
	"""One enqueued play per box in `geography`, so an unreachable one does not stop the rest.

	Every box that serves the wildcard is here: one left off keeps its provisioning certificate
	until it expires, and then every gateway dialling it fails verification at once."""
	for doctype, filters in (
		("Gateway Server", {}),
		("Ingress Server", {}),
		("Inference Server", {"is_standalone": 1}),
	):
		filters = {"status": "Active", "geography": geography, **filters}
		for name in frappe.get_all(doctype, filters=filters, pluck="name"):
			frappe.enqueue_doc(doctype, name, "deploy_tls", queue="long", timeout=600)


def store_certificate(geography, zone):
	"""Read what certbot has on disk onto the Geography. True when it CHANGED: re-pushing an
	identical certificate would reload every box for nothing.

	Loads the doc itself rather than taking the caller's copy — run_certbot has committed the
	last-run fields since then, and an older doc would write them back blank."""
	doc = frappe.get_doc("Geography", geography)
	certificate = read_pem(zone, "fullchain.pem")
	key = read_pem(zone, "privkey.pem")
	stored_key = doc.get_password("fleet_tls_key", raise_exception=False) or ""
	if certificate == (doc.fleet_tls_cert or "") and key == stored_key:
		return False

	doc.fleet_tls_cert = certificate
	doc.fleet_tls_key = key
	doc.fleet_tls_expires_on = certificate_expiry(certificate)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return True


def certificate_expiry(pem):
	"""notAfter, naive UTC. Parsed rather than assumed 90 days out, so a certificate issued
	elsewhere still reads correctly."""
	from cryptography import x509

	certificate = x509.load_pem_x509_certificate(pem.encode())
	expiry = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after
	return expiry.replace(tzinfo=None)


def run_certbot(settings, arguments, geography):
	"""Credentials go in the environment, not argv: /proc and the Ansible Task doc both record a
	command line."""
	command = [
		CERTBOT,
		*arguments,
		"--non-interactive",
		"--agree-tos",
		"--config-dir", certbot_dir("config"),
		"--work-dir", certbot_dir("work"),
		"--logs-dir", certbot_dir("logs"),
	]
	if settings.acme_email:
		command += ["--email", settings.acme_email]
	else:
		command += ["--register-unsafely-without-email"]

	result = subprocess.run(
		command, env=certbot_environment(settings), capture_output=True, text=True, timeout=TIMEOUT
	)
	record_run(geography, command, result)
	if result.returncode != 0:
		# certbot's own last lines name the failure — a rate limit, a DNS permission, a zone it
		# cannot find — and every one of them needs a different fix.
		frappe.throw(
			f"certbot exited {result.returncode}:\n{(result.stderr or result.stdout)[-2000:]}",
			title="Certificate request failed",
		)
	return result


def record_run(geography, command, result):
	"""What certbot just said, on the Geography, whether it worked or not.

	Committed before the caller throws: an enqueued job rolls back on an exception, which would
	take the record with it. Failures also reach the Error Log through that exception — what this
	adds is a SUCCESSFUL run being visible at all, since nothing else moves but an expiry date.

	The command is safe to store: the AWS credentials go in the environment, never in argv."""
	output = f"$ {' '.join(command)}\n\n{result.stdout}\n{result.stderr}".strip()
	frappe.db.set_value(
		"Geography",
		geography,
		{"fleet_tls_last_run": frappe.utils.now_datetime(), "fleet_tls_last_output": output[-4000:]},
	)
	frappe.db.commit()


def certbot_environment(settings):
	"""The AWS credentials certbot's route53 plugin reads. Route53 is global, but boto3 still
	wants a region named, and us-east-1 is the endpoint it serves from."""
	access_key_id, secret = dns_credentials(settings)
	return {
		**os.environ,
		"AWS_ACCESS_KEY_ID": access_key_id,
		"AWS_SECRET_ACCESS_KEY": secret,
		"AWS_DEFAULT_REGION": "us-east-1",
	}


def dns_credentials(settings=None):
	"""The DNS Provider's keys: what answers every geography's DNS-01 challenge here, and what the
	boxes write their records with. One reader, so a certificate and its records can never end up
	issued against two different accounts."""
	settings = settings or frappe.get_single("Grove Settings")
	provider = frappe.get_doc("Cloud Provider", require_dns_provider(settings))
	secret = provider.get_password("api_key", raise_exception=False)
	if not (provider.access_key_id and secret):
		frappe.throw(f"Cloud Provider {provider.name} has no credentials set.")
	return provider.access_key_id, secret


def certbot_dir(kind):
	"""Under the site's private files, so certbot runs as the bench user with no sudo anywhere.
	Its default /etc/letsencrypt would need root both to write and to read the key back."""
	# Absolute: get_site_path is relative to the bench's sites directory, so the paths certbot
	# records in its renewal conf would only resolve from the cwd the first run happened to have.
	path = os.path.abspath(frappe.get_site_path("private", "letsencrypt", kind))
	os.makedirs(path, exist_ok=True)
	return path


def read_pem(zone, filename):
	"""One of certbot's output files for this zone. --cert-name pins the directory to the zone,
	so it is not the `*.`-stripped name certbot would otherwise choose for a wildcard."""
	path = os.path.join(certbot_dir("config"), "live", zone, filename)
	if not os.path.exists(path):
		frappe.throw(f"certbot wrote no {filename} for {zone} — expected it at {path}.")
	with open(path) as handle:
		return handle.read()


def require_zone(geography):
	if not geography.fleet_zone:
		frappe.throw(f"Set a Fleet Zone on Geography {geography.name} before requesting a certificate.")
	return geography.fleet_zone


def require_dns_provider(settings):
	if not settings.dns_provider:
		frappe.throw("Set a DNS Provider on Grove Settings — its credentials own the zone.")
	return settings.dns_provider
