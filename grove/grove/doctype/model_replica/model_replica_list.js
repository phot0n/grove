// Without this, frappe.utils.guess_colour picks the colour off the status text, and it knows
// none of these words — Inactive and Broken both come out the same grey, which is the one pair
// that must never look alike: one is a deliberate pause, the other is an engine that needs you.
frappe.listview_settings['Model Replica'] = {
	get_indicator(doc) {
		const colour = {
			Draft: 'grey',
			Provisioning: 'orange',
			Active: 'green',
			Inactive: 'darkgrey',
			Broken: 'red',
		}[doc.status];
		return [__(doc.status), colour || 'grey', `status,=,${doc.status}`];
	},
};
