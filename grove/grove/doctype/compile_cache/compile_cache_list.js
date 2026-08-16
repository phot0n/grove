// Rows mirror the bucket — Sync pulls the truth, deleting a row deletes its artifacts.
frappe.listview_settings['Compile Cache'] = {
	onload(listview) {
		listview.page.add_inner_button(__('Sync From Bucket'), () =>
			frappe
				.call('grove.grove.doctype.compile_cache.compile_cache.sync_from_bucket')
				.then((r) => {
					frappe.show_alert({ message: __('{0} cache entries', [r.message]), indicator: 'green' });
					listview.refresh();
				})
		);
	},
};
