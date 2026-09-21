frappe.ui.form.on('Ingress Server', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__('Provision'), () => {
			frm.call('setup').then(() => frm.reload_doc());
		}, __('Ingress'));

		if (frm.doc.admin_url) {
			frm.add_custom_button(__('Ping'), () => frm.call('ping'), __('Ingress'));
		}

		if (frm.doc.status === 'Active' && frm.doc.admin_url) {
			const start = !frm.doc.is_in_maintenance;
			frm.add_custom_button(start ? __('Start Maintenance') : __('End Maintenance'), () => {
				frappe.confirm(
					start
						? __('Requests the gateways send through {0} get 503 until maintenance ends; running ones finish.', [frm.doc.name])
						: __('{0} serves new requests again.', [frm.doc.name]),
					() => frm.call('set_maintenance', { on: start ? 1 : 0 }).then(() => frm.reload_doc()),
				);
			}, __('Ingress'));
		}
		if (frm.doc.is_in_maintenance) {
			frm.set_intro(__('In maintenance — new requests get 503.'), 'orange');
		}

		if (frm.doc.machine) {
			frm.add_custom_button(__('Deploy Latest Agent'), () => {
				frm.call('deploy_agent');
			}, __('Ingress'));


			frm.add_custom_button(__('Update Scrape Auth'), () => frm.call('update_scrape_auth'));

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

		if (frm.doc.status !== 'Terminated') {
			frm.add_custom_button(__('Archive'), () => {
				frappe.confirm(
					__('Archive {0}? Its Machine is terminated — the box and everything on its disk are gone — and this server leaves the fleet. Refused while anything still depends on it.', [frm.doc.name]),
					() => frm.call('archive').then(() => frm.reload_doc()),
				);
			}, __('Danger'));
		}
	},
});
