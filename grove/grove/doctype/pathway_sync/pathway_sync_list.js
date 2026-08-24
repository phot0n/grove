frappe.listview_settings['Pathway Sync'] = {
	onload(listview) {
		listview.page.add_inner_button(__('Force Sync All'), () =>
			frappe.call('grove.grove.doctype.pathway_sync.pathway_sync.force_sync_all')
		);
	},
};
