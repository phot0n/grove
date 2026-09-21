

frappe.ui.form.on('Ansible Play', {
	refresh: function (frm) {
		if (frm.doc.status === 'Pending' || frm.doc.status === 'Running') {
			frm.add_custom_button(__('Stop'), () => {
				frappe.confirm(
					__('Stop this play? The box is left half-configured — tear down whatever it was deploying afterwards.'),
					() => frm.call('stop').then(() => frm.reload_doc()),
				);
			}).addClass('btn-danger');
		}
		// The other half of a stop: the engine the play left behind on the box.
		if (frm.doc.reference_doctype === 'Model Replica' && !['Pending', 'Running', 'Stopping'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Tear Down {0}', [frm.doc.reference_docname]), () => {
				frappe.confirm(
					__('Stop and remove this deployment from its box?'),
					() => frm.call('cleanup'),
				);
			});
		}
		frappe.realtime.on('ansible_play_progress', (data) => {
			if (data.progress && data.play === frm.doc.name) {
				const progress_title = __('Ansible Play Progress');
				frm.dashboard.show_progress(
					progress_title,
					(data.progress / data.total) * 100,
					`Ansible Play Progress (${data.progress} tasks completed out of ${data.total})`,
				);
				if (data.progress === data.total) {
					frm.dashboard.hide_progress(progress_title);
				}
			}
		});
	},
});
