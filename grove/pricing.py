"""Prices per counter, and the prepaid balance they debit.

One rate table, joined at evaluation and never snapshotted onto usage: SELL on `Model Pricing`
(one enabled doc per model, priced by the day it was enabled). A counter with usage and no rate
bills 0; the Model form flags a model with no enabled pricing. The provider's cost card is
reference data, not read here.

`Grove User.spent` is the running USD total the pull increments; `settle` is the one writer of
the `rate_limited` verdict; `rebuild_spent` re-prices the day rows nightly and after a reprice."""

from decimal import Decimal

import frappe

from grove.grove.doctype.credit_discrepancy.credit_discrepancy import record, resolve
from grove.grove.doctype.grove_user.grove_user import set_rate_limited

NANO = 10**9
MICRO = 10**6

# Unit divisor per priced counter: a rate is USD per Mtok, per minute, per request. The gateway
# holds the same table (pathway internal/domain/price.go); the README lists both.
COUNTERS = {
	"input_tokens": 1_000_000,
	"cached_tokens": 1_000_000,
	"cache_write_tokens": 1_000_000,
	"cache_write_1h_tokens": 1_000_000,
	"completion_tokens": 1_000_000,
	"audio_seconds": 60,
	"request_count": 1,
}


def effective_rate(rows, counter, day):
	"""`rows` are (date, counter, rate, creation). The newest row for `counter` dated on or before
	`day`; the same date twice → the newer doc. None when nothing prices that day."""
	best = None
	for date, name, rate, creation in rows:
		if name != counter or date > day:
			continue
		if best is None or (date, creation) > best[0]:
			best = ((date, creation), rate)
	return None if best is None else best[1]


def nano(usd):
	"""USD → whole nano-USD, the gateway's unit. A float is read as its decimal text, so 0.3 is
	300 000 000 and not one short."""
	return int(Decimal(str(usd)) * NANO)


def micro_floor(nano_usd):
	"""Down to a whole µUSD, so sub-µUSD truncation drift does not re-hash a user's bucket."""
	return nano_usd - nano_usd % 1000


class PriceBook:
	"""Every sell rate the site holds, read once per run. Retired pricings with an `enabled_on`
	stay in: they price the days before their successor. A Scheduled one is not a rate yet."""

	def __init__(self):
		self.models = set()
		self.sell = {}
		self.cache = {}

	@classmethod
	def load(cls):
		book = cls()
		book.models = {model.name for model in frappe.get_all("Model", fields=["name"])}
		pricings = {
			p.name: p
			for p in frappe.get_all(
				"Model Pricing",
				filters={"enabled_on": ("is", "set"), "status": ("!=", "Scheduled")},
				fields=["name", "model", "enabled_on", "creation"],
			)
		}
		rates = frappe.get_all(
			"Model Pricing Rate",
			filters={"parent": ("in", list(pricings))},
			fields=["parent", "counter", "rate"],
			parent_doctype="Model Pricing",
		) if pricings else []
		for row in rates:
			pricing = pricings[row.parent]
			book.sell.setdefault(pricing.model, []).append(
				(pricing.enabled_on, row.counter, Decimal(str(row.rate)), pricing.creation)
			)
		return book

	def rate_for(self, model, counter, day):
		"""USD per unit: the model's sell rate for `day`, else None."""
		key = (model, counter, day)
		if key not in self.cache:
			self.cache[key] = effective_rate(self.sell.get(model, ()), counter, day)
		return self.cache[key]

	def rates_for(self, model, day):
		"""{counter: nano-USD per unit} — what a route row carries to the gateway."""
		rates = {}
		for counter in COUNTERS:
			rate = self.rate_for(model, counter, day)
			if rate is not None:
				rates[counter] = nano(rate)
		return rates

	def usage_cost(self, counts, model, day):
		"""USD for `{counter: amount}` of `model` on `day`. A counter with usage and no rate bills 0
		— the Model form flags a model with no enabled pricing. Never raises."""
		total = Decimal(0)
		for counter, amount in counts.items():
			if not amount or counter not in COUNTERS:
				continue
			rate = self.rate_for(model, counter, day)
			if rate is None:
				continue
			total += Decimal(amount) * rate / COUNTERS[counter]
		return total


def usage_by_day_model_counter(user):
	"""One grouped read of everything `user` ever used: (day, model, counter, amount)."""
	return frappe.db.sql(
		"""select r.day, c.model, c.counter, sum(c.amount) as amount
		from `tabUsage Counter Row` c join `tabUsage Record` r on r.name = c.parent
		where r.user = %s group by r.day, c.model, c.counter""",
		[user],
		as_dict=True,
	)


def priced_history(user, book):
	"""What the day rows cost at today's tables — the ledger truth `spent` is checked against."""
	by_day_model = {}
	for row in usage_by_day_model_counter(user):
		by_day_model.setdefault((row.day, row.model), {})[row.counter] = row.amount
	return sum((book.usage_cost(counts, model, day) for (day, model), counts in by_day_model.items()), Decimal(0))


def rebuild_spent(user, book=None):
	"""`spent` re-priced from the day rows, written, then the verdict re-decided — the only thing
	that can ever clear a blocked user who has no traffic. → the rebuilt total."""
	spent = priced_history(user, book or PriceBook.load())
	frappe.db.set_value("Grove User", user, "spent", spent, update_modified=False)
	settle(user)
	return spent


def allocated(user, lock=False):
	"""Σ the user's Grove Credit ledger. Locked while a verdict is being decided."""
	suffix = " for update" if lock else ""
	total = frappe.db.sql(
		f"select coalesce(sum(amount), 0) from `tabGrove Credit` where grove_user = %s{suffix}", [user]
	)[0][0]
	return Decimal(str(total))


def settle(user):
	"""The one writer of the verdict and of `balance`: allocated − spent, written to the user, and
	`rate_limited` = not free and nothing left, both directions. A negative balance is an Overspend
	row, cleared by the entry that covers it. → actual balance."""
	doc = frappe.db.get_value("Grove User", user, ["free", "spent"], as_dict=True, for_update=True)
	actual = allocated(user, lock=True) - Decimal(str(doc.spent or 0))
	frappe.db.set_value("Grove User", user, "balance", float(actual), update_modified=False)
	set_rate_limited(user, int(not doc.free and actual <= 0))
	if actual < 0:
		record("Overspend", grove_user=user, expected_value=0, gateway_value=actual, delta=actual)
	else:
		resolve("Overspend", grove_user=user)
	return actual


def credit_summary(user):
	"""{allocated, spent, remaining}, summed live rather than read off `balance`."""
	spent = Decimal(str(frappe.db.get_value("Grove User", user, "spent") or 0))
	total = allocated(user)
	return {"allocated": total, "spent": spent, "remaining": total - spent}


def tolerance(request_count):
	"""One µUSD per request: the gateway truncates per counter in nano-USD, so anything beyond
	this is a discrepancy, not rounding."""
	return Decimal(request_count or 0) / MICRO


def verify_balances():
	"""Nightly: every paying user's `spent` rebuilt from the day rows. Beyond tolerance is a
	Ledger Drift row; the join wins either way, then `settle` re-decides."""
	book = PriceBook.load()
	for user in frappe.get_all("Grove User", filters={"free": 0}, pluck="name"):
		materialised = Decimal(str(frappe.db.get_value("Grove User", user, "spent") or 0))
		requests = frappe.db.sql(
			"""select coalesce(sum(c.amount), 0) from `tabUsage Counter Row` c
			join `tabUsage Record` r on r.name = c.parent where r.user = %s and c.counter = 'request_count'""",
			[user],
		)[0][0]
		rebuilt = rebuild_spent(user, book)
		if abs(rebuilt - materialised) > tolerance(requests):
			record(
				"Ledger Drift", grove_user=user, gateway_value=materialised, expected_value=rebuilt,
				delta=rebuilt - materialised,
			)
		frappe.db.commit()


def validate_price_rows(rows, key):
	"""No blank or negative rate; no two rows `key` reads the same. 0 is a price: free."""
	seen = set()
	for row in rows:
		if row.rate is None:
			frappe.throw(f"Row {row.idx}: a blank rate is not a price — 0 means free.")
		if row.rate < 0:
			frappe.throw(f"Row {row.idx}: a rate cannot be negative.")
		k = key(row)
		if k in seen:
			frappe.throw(f"Row {row.idx} repeats {k} — one rate per counter per date.")
		seen.add(k)
