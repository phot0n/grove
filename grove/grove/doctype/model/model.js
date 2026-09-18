// Head/layer counts drive the parallelism checks on Pod and Model Replica, so read them
// off the repo rather than trusting a hand-typed number.
frappe.ui.form.on('Model', {
	onload(frm) {
		// Blank means ours: show which provider that is, and let the fetch fill the mirror.
		if (!frm.is_new() || frm.doc.provider) return;
		frappe.db.get_value('Model Provider', { is_self_hosted: 1 }, 'name').then((r) => {
			if (r.message && r.message.name) frm.set_value('provider', r.message.name);
		});
	},

	refresh(frm) {
		// A vendor serves this one: there is no repo to read and no box holding its weights.
		if (frm.is_new() || !frm.doc.provider_is_self_hosted) return;

		frm.add_custom_button(__('Fetch Architecture'), () => {
			frm.call('fetch_architecture').then(() => frm.reload_doc());
		});

		frm.add_custom_button(__('Mirror Weights To S3'), () => {
			frm.call('mirror_weights');
		});
	},
});
