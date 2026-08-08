// Copyright (c) 2026, developers@frappe.io and contributors
// For license information, please see license.txt

frappe.ui.form.on('Grove API Key', {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== 'active') return;

		frm.add_custom_button(__('Revoke'), () => {
			frappe.confirm(
				__('Revoke this key? Gateways stop honouring it within the cache TTL, and it cannot be un-revoked.'),
				() => frm.call('revoke').then(() => frm.reload_doc()),
			);
		});
		frm.change_custom_button_type(__('Revoke'), null, 'danger');
	},
});
