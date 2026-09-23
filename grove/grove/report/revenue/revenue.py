"""Revenue per model, API key, user or day: the day rows priced at today's tables, so a reprice
changes history here exactly as it does in `spent`."""

from decimal import Decimal

import frappe

from grove.pricing import PriceBook

GROUP_BY = {
	"Model": ("c.model", "Link", "Model"),
	"API Key": ("r.api_key", "Link", "Grove API Key"),
	"Grove User": ("r.user", "Link", "Grove User"),
	"Day": ("r.day", "Date", None),
}
FILTERS = {"model": "c.model", "api_key": "r.api_key", "grove_user": "r.user"}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	group_by = filters.group_by or "Model"
	expr, fieldtype, options = GROUP_BY[group_by]
	columns = [
		{"fieldname": "label", "label": group_by, "fieldtype": fieldtype, "options": options, "width": 260},
		{"fieldname": "requests", "label": "Requests", "fieldtype": "Int", "width": 120},
		{"fieldname": "revenue", "label": "Revenue (USD)", "fieldtype": "Currency", "width": 140},
	]
	return columns, rows(filters, expr)


def rows(filters, expr):
	"""One grouped read of the counter rows, priced per (day, model, counter); requests are the
	`request_count` counter every request emits."""
	conditions = " ".join(f"and {column} = %({name})s" for name, column in FILTERS.items() if filters.get(name))
	usage = frappe.db.sql(
		f"""select {expr} as label, r.day, c.model, c.counter, sum(c.amount) as amount
		from `tabUsage Counter Row` c join `tabUsage Record` r on r.name = c.parent
		where r.day between %(from_date)s and %(to_date)s {conditions}
		group by label, r.day, c.model, c.counter""",
		filters,
		as_dict=True,
	)
	book = PriceBook.load()
	totals = {}
	for row in usage:
		total = totals.setdefault(row.label, {"label": row.label, "requests": 0, "revenue": Decimal(0)})
		total["revenue"] += book.usage_cost({row.counter: row.amount}, row.model, row.day)
		if row.counter == "request_count":
			total["requests"] += int(row.amount)
	ranked = sorted(totals.values(), key=lambda total: total["revenue"], reverse=True)
	return [{**total, "revenue": float(total["revenue"])} for total in ranked]
