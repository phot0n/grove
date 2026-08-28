frappe.ui.form.on('Inference Server', {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.machine) {
			frm.add_custom_button(__('Setup'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
			// The exporters listen on 9100/9400 for the Monitoring Agent above to scrape —
			// restrict those ports to that agent in the security group.
			frm.add_custom_button(__('Install Exporters'), () => frm.call('install_exporters'));
		}
		render_gpus(frm);
	},
});

// GPU allocation is derived, never stored: the server recomputes it from the Machine's cards
// and the Active Model Replicas on this box, so it is correct the moment one is deployed or
// torn down. Nothing to sync, nothing to go stale.
function render_gpus(frm) {
	frm.call('get_gpu_allocation').then((r) => {
		grove.render_gpu_table(frm.fields_dict.gpus_html?.$wrapper, r.message || [], {
			empty: __("No GPUs on this server's Machine — run Scan GPUs on it."),
			note: __("this box's Machine, live"),
		});
	});
}
