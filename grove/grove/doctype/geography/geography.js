frappe.ui.form.on('Geography', {
	refresh(frm) {
		if (frm.doc.fleet_zone && !frm.is_new()) {
			frm.add_custom_button(__('Issue Fleet Certificate'), () => {
				frm.call('issue_fleet_certificate').then(() => frm.reload_doc());
			});
		}
	},
});
