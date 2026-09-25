frappe.ui.form.on('Lost Usage', {
	refresh(frm) {
		frm.disable_form();
		if (frm.doc.replayed) return;
		frm.add_custom_button(__('Replay Now'), () => {
			frm.call('replay').then((r) => {
				frappe.show_alert(r.message ? __('Landed') : __('Failed again — see Last Error'));
				frm.reload_doc();
			});
		});
	},
});
