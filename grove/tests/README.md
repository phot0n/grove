# `tests/` — two kinds, and the trap between them

## Pure tests

The default, and where nearly everything here lives. No site, no network, no SSH: boto3 is replaced by
a fake, the doc is a `SimpleNamespace` carrying the real methods off the class, and `frappe.db` is
swapped whole (it is a `Local` proxy and raises `object is not bound` without a site).

```python
class FakeProxy:
    hostname = GatewayServer.hostname          # the real property, exercised on a fake doc

doc = SimpleNamespace(name="gw1-ap-south-1", public_ip="203.0.113.7")
GatewayServer.set_admin_url(doc)
```

Run them without a site at all:

```bash
cd apps/grove
../../env/bin/python -m unittest discover -s grove -t . -p "test_*.py"
```

Nine or so failures are expected outside a bench — the site-backed classes below, whose `setUpClass`
cannot connect.

## Site-backed tests

For behaviour that only exists against a database: `autoname`, link updates, real inserts.

```bash
bench --site grove.localhost run-tests --app grove --module grove.tests.test_model_ids
```

**Subclass `IntegrationTestCase`, never `unittest.TestCase`.** `IntegrationTestCase` wraps each test in
a transaction and rolls it back; plain `unittest.TestCase` does **not**. A file that got this wrong
committed four probe Models, two invented Model Providers and one malformed record into the dev site,
and they had to be deleted by hand. If a test inserts anything, check its base class first.

## What a test here is for

Not coverage. Each one pins a rule that something outside this repo enforces and that reading the code
does not reveal:

- **Route53 rejects it** — a wrong `SetIdentifier` silently replaces another box's row; a DELETE that
  does not repeat a record exactly leaves it in place; a health check cannot be deleted while anything
  names it, so the *order* of calls is the assertion (`FakeRoute53.calls` is a call log for exactly
  that).
- **Frappe drops it** — an `append()` key that is not a field vanishes silently, so a row ships with no
  server named on it.
- **A template renders it blank** — `agent.env` is written whole from extra-vars, so
  `test_gateway_agent.py` asserts that every variable a template needs is one the button passes. That
  test is written against the play files themselves, so it catches the *next* variable someone adds.
- **The number is deliberate** — `HealthThreshold=1`, `EjectAfter=3`, `total − cached`. A test naming
  the constant is what stops a well-meaning edit.

Two conventions that follow from that: a test's name says the rule (`test_a_gateway_without_a_check_of_its_own_is_not_a_child`),
and where a rule exists because something broke, the comment says what broke.

## Fakes worth reusing

- `test_gateway_dns.FakeRoute53` — records every change batch, every health-check call, and the order.
  `test_ingress_dns` and `test_region_dns` import it rather than writing their own.
- `test_gateway_routes.FakeQuery` — answers `frappe.get_all` for the route-table builder off canned
  rows.
- `pathway`'s own `internal/repository/memory` — the in-memory store, with a `Fail[...]` switch
  per repository so "the store is unreachable" is a test case rather than a mystery.
