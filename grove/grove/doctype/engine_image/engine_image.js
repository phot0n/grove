// A box's disk has to hold this image before it holds a single weight, so read what it
// weighs off the registry rather than guessing at it when sizing a Machine.
frappe.ui.form.on('Engine Image', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__('Fetch Size'), () => {
			frm.call('fetch_size').then(() => frm.reload_doc());
		});
	},
});
