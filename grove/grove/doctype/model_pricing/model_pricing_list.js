frappe.listview_settings['Model Pricing'] = {
	get_indicator(doc) {
		const color = { Enabled: 'green', Scheduled: 'orange' }[doc.status] || 'grey';
		return [__(doc.status), color, `status,=,${doc.status}`];
	},
};
