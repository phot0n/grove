frappe.ui.form.on('Proxy Server', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__('Provision'), () => {
			frm.call('setup').then(() => frm.reload_doc());
		}, __('Gateway'));

		if (frm.doc.machine) {
			frm.add_custom_button(__('Deploy Latest Agent'), () => {
				frm.call('deploy_agent');
			}, __('Gateway'));
		}

		if (frm.doc.status === 'Active' && frm.doc.admin_url) {
			frm.add_custom_button(__('Full Sync'), () => {
				frm.call('full_sync');
			}, __('Gateway'));
		}
	},
});
