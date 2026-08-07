# Copyright (c) 2026, Grove and contributors
# For license information, please see license.txt
"""The one wildcard certificate every Proxy Server serves: issued here on the control plane,
stored on Grove Settings, pushed to each box by Ansible.

DNS-01, not HTTP-01, and that is the whole point. Gateway Host resolves to a different proxy
depending on where the client is, so no single box can answer a challenge for it — and with one
wildcard for `*.<zone>`, the same certificate also covers each box's own `<name>.<zone>`, which is
what /grove-admin and /metrics are reached on. One issuance, one key, N deliveries.

certbot is the ACME client; Grove never speaks ACME. It runs unprivileged with its config, work
and log directories inside the site, so nothing here needs root and the private key never sits in
a world-readable /etc on this host.

Two prerequisites on the control plane, both outside this app: `certbot` and its route53 plugin
(`apt install certbot python3-certbot-dns-route53`, or the pip equivalent in the bench env), and a
DNS Provider whose IAM user holds route53:ChangeResourceRecordSets, route53:ListHostedZonesByName
and route53:GetChange for the zone."""

import os
import subprocess

import frappe

CERTBOT = "certbot"
# Long enough for a DNS-01 round trip: certbot writes the TXT record, then waits on Route53
# propagation before it even asks Let's Encrypt to look.
TIMEOUT = 600


def issue_fleet_certificate():
	"""Obtain the wildcard for the proxy zone, or renew it early if certbot already holds one.
	Called by the Grove Settings button; the daily job calls renew_fleet_certificate instead."""
	settings = frappe.get_single("Grove Settings")
	zone = require_zone(settings)
	arguments = ["certonly", "--dns-route53", "--cert-name", zone, "-d", f"*.{zone}"]
	# --force-renewal counts against the duplicate-certificate limit (5 a week for one name), so
	# it is passed only when keeping what is on disk would be wrong.
	arguments += ["--force-renewal"] if is_lineage_stale(settings, zone) else ["--keep-until-expiring"]
	run_certbot(settings, arguments)
	if store_certificate(zone):
		push_to_proxies()


def is_lineage_stale(settings, zone):
	"""True when certbot holds a certificate for this zone from the OTHER ACME server.

	Its renewal conf pins the server it was issued from, and `certonly --keep-until-expiring` on a
	certificate that is still valid keeps it. So unchecking Use ACME Staging and pressing Issue
	would exit 0, change nothing, and leave every proxy serving a certificate no client trusts."""
	path = os.path.join(certbot_dir("config"), "renewal", f"{zone}.conf")
	if not os.path.exists(path):
		return False
	with open(path) as handle:
		issued_from_staging = "acme-staging" in handle.read()
	return issued_from_staging != bool(settings.acme_staging)


def renew_fleet_certificate():
	"""Daily. certbot decides whether anything is due — it renews inside its own 30-day window
	and exits 0 having done nothing otherwise. Grove notices only when the files on disk stop
	matching what is stored, which is what keeps this from reloading the fleet every night."""
	settings = frappe.get_single("Grove Settings")
	if not (settings.proxy_zone and settings.fleet_tls_cert):
		return
	run_certbot(settings, ["renew", "--cert-name", settings.proxy_zone])
	if store_certificate(settings.proxy_zone):
		push_to_proxies()


def push_to_proxies():
	"""Ship the stored certificate to every Active proxy, one enqueued play each. Separate jobs
	because a box that is unreachable must not stop the rest of the fleet from being renewed."""
	for name in frappe.get_all("Proxy Server", filters={"status": "Active"}, pluck="name"):
		frappe.enqueue_doc("Proxy Server", name, "deploy_tls", queue="long", timeout=600)


def store_certificate(zone):
	"""Read what certbot has on disk onto Grove Settings. True when it changed — a renewal that
	was not due leaves the files alone, and re-pushing an identical certificate would reload
	every proxy in the fleet for nothing.

	Loads the Single itself rather than taking the caller's copy: run_certbot has committed the
	last-run fields since then, and saving a doc read before that would write them back blank."""
	settings = frappe.get_single("Grove Settings")
	certificate = read_pem(zone, "fullchain.pem")
	key = read_pem(zone, "privkey.pem")
	stored_key = settings.get_password("fleet_tls_key", raise_exception=False) or ""
	if certificate == (settings.fleet_tls_cert or "") and key == stored_key:
		return False

	settings.fleet_tls_cert = certificate
	settings.fleet_tls_key = key
	settings.fleet_tls_expires_on = certificate_expiry(certificate)
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	return True


def certificate_expiry(pem):
	"""notAfter, naive UTC — what the Datetime field on Grove Settings holds. Parsed rather than
	assumed 90 days out, so a certificate issued anywhere else still reads correctly."""
	from cryptography import x509

	certificate = x509.load_pem_x509_certificate(pem.encode())
	expiry = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after
	return expiry.replace(tzinfo=None)


def run_certbot(settings, arguments):
	"""One certbot run under this site's own directories. Credentials go in the environment, not
	argv: /proc and the Ansible Task doc both record a command line."""
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
	if settings.acme_staging:
		command += ["--staging"]

	result = subprocess.run(
		command, env=certbot_environment(settings), capture_output=True, text=True, timeout=TIMEOUT
	)
	record_run(command, result)
	if result.returncode != 0:
		# certbot's own last lines name the failure — a rate limit, a DNS permission, a zone it
		# cannot find — and every one of them needs a different fix.
		frappe.throw(
			f"certbot exited {result.returncode}:\n{(result.stderr or result.stdout)[-2000:]}",
			title="Certificate request failed",
		)
	return result


def record_run(command, result):
	"""What certbot just said, on Grove Settings, whether it worked or not.

	Committed before the caller throws: an enqueued job rolls back on an exception, which would
	take the record with it. Failures also reach the Error Log through that exception — what this
	adds is a SUCCESSFUL run being visible at all, since nothing else moves but an expiry date.

	The command is safe to store: the AWS credentials go in the environment, never in argv."""
	output = f"$ {' '.join(command)}\n\n{result.stdout}\n{result.stderr}".strip()
	frappe.db.set_single_value(
		"Grove Settings",
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
	"""The DNS Provider's keys: what answers the DNS-01 challenge here, and what Proxy Server
	writes its records with. One reader, so the certificate and the records can never end up
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


def require_zone(settings):
	if not settings.proxy_zone:
		frappe.throw("Set a Proxy Zone on Grove Settings before requesting a certificate.")
	return settings.proxy_zone


def require_dns_provider(settings):
	if not settings.dns_provider:
		frappe.throw("Set a DNS Provider on Grove Settings — its credentials own the zone.")
	return settings.dns_provider
