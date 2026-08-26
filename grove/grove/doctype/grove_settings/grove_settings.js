frappe.ui.form.on('Grove Settings', {
	refresh(frm) {
		if (frm.doc.fleet_zone && frm.doc.dns_provider) {
			frm.add_custom_button(__('Issue Fleet Certificate'), () => {
				frm.call('issue_fleet_certificate').then(() => frm.reload_doc());
			});
		}
	},
});
