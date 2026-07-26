// Head/layer counts drive the parallelism checks on Pod and Model Deployment, so read them
// off the repo rather than trusting a hand-typed number.
frappe.ui.form.on('Model', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__('Fetch Architecture'), () => {
			frm.call('fetch_architecture').then(() => frm.reload_doc());
		});
	},
});
