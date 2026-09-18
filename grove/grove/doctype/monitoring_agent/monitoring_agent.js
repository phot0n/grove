frappe.ui.form.on('Monitoring Agent', {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status !== 'Terminated') {
			frm.add_custom_button(__('Archive'), () => {
				frappe.confirm(
					__('Archive {0}? Its Machine is terminated — the box and everything on its disk are gone — and this server leaves the fleet. Refused while anything still depends on it.', [frm.doc.name]),
					() => frm.call('archive').then(() => frm.reload_doc()),
				);
			}, __('Danger'));
		}
		if (!frm.doc.machine) return;

		if (frm.doc.status === 'Active') {
			// The binary is already on the box; re-downloading it to change an interval only
			// makes the change slower to land.
			frm.add_custom_button(__('Update Config'), () => {
				frm.call('update_config').then(() => frm.reload_doc());
			});
			frm.add_custom_button(__('Rotate Token'), () => {
				frappe.prompt(
					{
						fieldname: 'token',
						fieldtype: 'Password',
						label: __('New Metrics Token'),
						reqd: 1,
						description: __('Minted by the ingestion service. Saved here and written to the box in one go — pushes fail until both match.'),
					},
					({token}) => frm.call('rotate_metrics_token', {token}).then(() => frm.reload_doc()),
					__('Rotate Metrics Token'),
					__('Rotate'),
				);
			});
		} else {
			frm.add_custom_button(__('Install'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
		}

		// developer_mode only: the box cannot reach this Grove, so nothing re-fetches and this is
		// the only way its target list moves. Off a dev site http_sd does it by itself.
		if (frappe.boot.developer_mode && frm.doc.status === 'Active') {
			frm.add_custom_button(__('Push Targets'), () => {
				frm.call('push_targets').then(() => frm.reload_doc());
			});
		}

		// The fastest way to tell "nothing is assigned to me" from "the agent is broken".
		frm.add_custom_button(__('Show Targets'), () => {
			window.open(
				`/api/method/grove.monitoring.targets?agent=${encodeURIComponent(frm.doc.name)}&kind=engine`,
				'_blank',
			);
		});
	},
});
