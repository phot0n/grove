frappe.ui.form.on('Pathway Update', {
	setup(frm) {
		// Only the ticked types may be added as rows.
		frm.set_query('server_type', 'servers', () => ({
			filters: { name: ['in', [frm.doc.ingresses && 'Ingress Server', frm.doc.gateways && 'Gateway Server'].filter(Boolean)] },
		}));
		frm.set_query('server', 'servers', () => ({ filters: { status: 'Active' } }));
	},

	onload(frm) {
		if (!frm.is_new()) return;
		// Shown before saving; the server copies them from Grove Settings at insert either way.
		for (const [field, setting] of [['release', 'pathway_release'], ['repo', 'pathway_repo']]) {
			frappe.db.get_single_value('Grove Settings', setting).then((value) => frm.set_value(field, value));
		}
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const status = frm.doc.status;
		// Pending with a start time: Stop was pressed and the current server is still deploying.
		const stopping = status === 'Pending' && frm.doc.started_at;
		if (status === 'Running' || stopping) {
			frm.disable_save();
		} else {
			frm.enable_save();
		}
		if (frm.is_dirty()) return;

		const pending = frm.doc.servers.filter((row) => row.status === 'Pending').length;
		const act = (label, method, message) => frm.add_custom_button(__(label), () => {
			frappe.confirm(message, () => frm.call(method).then(() => frm.reload_doc()));
		});

		if (status === 'Pending' && !stopping) {
			act('Start', 'start', __('Deploy pathway {0} to {1} servers, one at a time?', [frm.doc.release, pending]));
		}
		if (['Stopped', 'Failure', 'Success'].includes(status) && pending) {
			act('Continue', 'resume', __('Deploy pathway {0} to the {1} Pending servers? Failed rows are passed over.', [frm.doc.release, pending]));
		}
		if (status === 'Running') {
			act('Stop', 'stop', __('Stop once the server being deployed is done? Nothing is stopped mid-deploy.'));
		}

		if (stopping) {
			frm.set_intro(__('Stopping — the server being deployed finishes first.'), 'blue');
		} else if (status === 'Stopped') {
			frm.set_intro(__('Stopped. Continue carries on with the Pending rows.'), 'orange');
		} else if (status === 'Failure') {
			frm.set_intro(__('A deploy failed. The failed server was put in maintenance to debug: End Maintenance on it when done. Continue carries on past it; add its server again as a new row to retry.'), 'red');
		}
	},
});
