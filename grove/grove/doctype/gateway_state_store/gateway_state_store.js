frappe.ui.form.on('Gateway State Store', {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status !== 'Terminated') {
			frm.add_custom_button(__('Setup'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
			frm.add_custom_button(__('Archive'), () => {
				frappe.confirm(
					__('Archive {0}? Its Machine is terminated — the box and everything on its disk are gone. Refused while any gateway still runs on it.', [frm.doc.name]),
					() => frm.call('archive').then(() => frm.reload_doc()),
				);
			}, __('Danger'));
		}
	},
});
