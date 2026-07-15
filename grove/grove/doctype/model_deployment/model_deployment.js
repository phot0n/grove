frappe.ui.form.on('Model Deployment', {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.model && frm.doc.inference_server) {
			frm.add_custom_button(__('Deploy'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
			// Fast re-render of the engine unit (dtype / gpu mem / attention backend /
			// port) without a full re-install. Only meaningful once served.
			if (frm.doc.status === 'Active' || frm.doc.status === 'Broken') {
				frm.add_custom_button(__('Update Engine Config'), () => {
					frappe.confirm(
						__('Restart the engine to apply config changes? In-flight requests will be dropped.'),
						() => frm.call('apply_engine_config').then(() => frm.reload_doc()),
					);
				});
				// Multi-tenant box: remove just THIS instance's unit + key (shared
				// venv/weights stay for other instances).
				frm.add_custom_button(__('Tear Down'), () => {
					frappe.confirm(
						__('Stop and remove this instance from its box? The model will stop serving here.'),
						() => frm.call('teardown').then(() => frm.reload_doc()),
					);
				}).addClass('btn-danger');
			}
		}
	},
});
