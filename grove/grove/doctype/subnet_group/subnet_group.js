frappe.ui.form.on('Subnet Group', {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.vpc_id) return;
		if (frm.doc.proxy_security_group_ids && frm.doc.inference_security_group_ids) return;

		frm.add_custom_button(__('Create Security Groups'), () => frm.call('create_security_groups'));
	},
});
