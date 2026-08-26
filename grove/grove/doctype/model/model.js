// Head/layer counts drive the parallelism checks on Pod and Model Deployment, so read them
// off the repo rather than trusting a hand-typed number.
frappe.ui.form.on('Model', {
	refresh(frm) {
		// A provider serves this one: there is no repo to read and no box holding its weights.
		if (frm.is_new() || frm.doc.provider_base_url) return;

		frm.add_custom_button(__('Fetch Architecture'), () => {
			frm.call('fetch_architecture').then(() => frm.reload_doc());
		});

		frm.add_custom_button(__('Mirror Weights To S3'), () => {
			frm.call('mirror_weights');
		});
	},
});
