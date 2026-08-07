frappe.ui.form.on('Grove Settings', {
	refresh(frm) {
		frm.add_custom_button(__('Full Sync All Gateways'), () => {
			frm.call('full_sync_all');
		});

		if (frm.doc.proxy_zone && frm.doc.dns_provider) {
			frm.add_custom_button(__('Issue Fleet Certificate'), () => {
				frm.call('issue_fleet_certificate').then(() => frm.reload_doc());
			});
		}
	},
});
