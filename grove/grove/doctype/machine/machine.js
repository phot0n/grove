frappe.ui.form.on('Machine', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__(frm.doc.status === 'Active' ? 'Re-provision' : 'Provision'), () => {
			frm.call('setup').then(() => frm.reload_doc());
		});

		// Cloud pods only: terminate frees GPU/disk/billing; recover re-reads endpoints
		// after a restart and redeploys models.
		if (frm.doc.cloud_provider && frm.doc.cloud_instance_id) {
			frm.add_custom_button(__('Recover Pod'), () => {
				frm.call('recover').then(() => frm.reload_doc());
			}, __('Cloud'));
			frm.add_custom_button(__('Terminate Pod'), () => {
				frappe.confirm(__('Terminate the pod? Frees GPU/disk/billing; models on it go offline.'), () => {
					frm.call('teardown').then(() => frm.reload_doc());
				});
			}, __('Cloud'));
		}
	},
});
