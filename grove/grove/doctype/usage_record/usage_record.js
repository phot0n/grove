frappe.ui.form.on('Usage Record', {
	refresh(frm) {
		// Written by the usage pull and by nothing else. The gateway counters behind these
		// figures are drained on read, so a hand-edited total is one nothing can reconstruct.
		// disable_form: read-only fields, no Save. Must be called from refresh.
		frm.disable_form();
	},
});
