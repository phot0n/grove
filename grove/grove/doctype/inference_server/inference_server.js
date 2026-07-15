frappe.ui.form.on('Inference Server', {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.machine) {
			frm.add_custom_button(
				__(frm.doc.is_provisioned ? 'Re-provision' : 'Provision'),
				() => {
					frm.call('setup').then(() => frm.reload_doc());
				},
			);
		}
	},
});
