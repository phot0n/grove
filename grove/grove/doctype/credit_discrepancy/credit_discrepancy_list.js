frappe.listview_settings['Credit Discrepancy'] = {
	get_indicator(doc) {
		return doc.resolved
			? [__('Resolved'), 'grey', 'resolved,=,1']
			: [__(doc.kind), 'red', 'resolved,=,0'];
	},
};
