frappe.ui.form.on('Ingress Server', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__('Provision'), () => {
			frm.call('setup').then(() => frm.reload_doc());
		}, __('Ingress'));

		if (frm.doc.machine) {
			frm.add_custom_button(__('Deploy Latest Agent'), () => {
				frm.call('deploy_agent');
			}, __('Ingress'));

			// Config only — no agent restart, and Redis keeps its replica table.
			frm.add_custom_button(__('Deploy OpenResty Config'), () => {
				frm.call('deploy_openresty');
			}, __('Ingress'));

			frm.add_custom_button(__('Install Exporters'), () => frm.call('install_exporters'));

			// The replica table: every Active replica in this ingress's Network, dialled privately.
			frm.add_custom_button(__('Sync Replicas'), () => {
				frm.call('sync_replicas');
			}, __('Ingress'));

			frm.add_custom_button(__('Sync DNS Records'), () => {
				frm.call('sync_dns_records');
			}, __('TLS'));

			frm.add_custom_button(__('Deploy Fleet Certificate'), () => {
				frm.call('deploy_tls');
			}, __('TLS'));
		}
	},
});
