frappe.ui.form.on('Monitoring Agent', {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.machine) return;

		frm.add_custom_button(__(frm.doc.status === 'Active' ? 'Re-install' : 'Install'), () => {
			frm.call('setup').then(() => frm.reload_doc());
		});

		// The target list is what this agent will actually scrape — reading it is the fastest
		// way to tell "nothing is assigned to me" from "the agent is broken".
		frm.add_custom_button(__('Show Targets'), () => {
			window.open(
				`/api/method/grove.monitoring.targets?agent=${encodeURIComponent(frm.doc.name)}&kind=engine`,
				'_blank',
			);
		});
	},
});
