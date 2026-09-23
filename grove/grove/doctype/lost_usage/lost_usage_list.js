frappe.listview_settings['Lost Usage'] = {
	get_indicator(doc) {
		return doc.replayed ? [__('Replayed'), 'grey', 'replayed,=,1'] : [__('Pending'), 'red', 'replayed,=,0'];
	},
};
