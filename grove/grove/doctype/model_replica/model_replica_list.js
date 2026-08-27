// Without this, frappe.utils.guess_colour picks the colour off the status text, and it knows
// none of these words — Inactive and Broken both come out the same grey, which is the one pair
// that must never look alike: one is a deliberate pause, the other is an engine that needs you.
frappe.listview_settings['Model Replica'] = {
	get_indicator(doc) {
		const colour = {
			// Nothing on a box yet.
			Draft: 'grey',
			// A play owns this instance right now.
			Provisioning: 'orange',
			// Serving, and in the gateway's route table.
			Active: 'green',
			// Stopped on purpose. Its container, run script, port and key are all still on the
			// box, so Start brings the same engine back — blue, not red, because nothing is wrong.
			Inactive: 'blue',
			// A play failed, or the engine never came up. Nothing routes here.
			Broken: 'red',
		}[doc.status];
		// Anything not named above — Terminated — is deliberately the default grey: it is a
		// dead row, and giving it a colour of its own only competes with the ones that matter.
		// Clicking the dot filters the list to that status.
		return [__(doc.status), colour || 'grey', `status,=,${doc.status}`];
	},
};
