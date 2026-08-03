frappe.ui.form.on('Machine', {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.public_ip) {
			// nvidia-smi is the truth for what's in the box — these rows drive CUDA pinning, the
			// VRAM fit check and the Inference Server's GPU view, so don't hand-type them.
			frm.add_custom_button(__('Scan GPUs'), () => {
				frappe.confirm(
					__("Read this box's GPUs over SSH and replace the GPU table with what it reports?"),
					() => frm.call('scan_gpus'),
				);
			});
		}

		// A box with no provider — or a non-AWS one — shows nothing below. Gated on the
		// provider's TYPE, not merely on one being set, so a RunPod machine stays clean.
		if (frm.doc.provider_type !== 'aws') return;

		if (!frm.doc.instance_id) {
			frm.add_custom_button(__('Provision'), () => frm.call('provision'), __('AWS'));
			return;
		}
		for (const action of ['Sync', 'Stop', 'Start']) {
			frm.add_custom_button(__(action), () => frm.call(action.toLowerCase()), __('AWS'));
		}
		frm.add_custom_button(__('Terminate'), () => {
			frappe.confirm(
				__('Destroy instance {0}? Its root volume goes with it — the engine images and every model weight on this box are lost.', [frm.doc.instance_id]),
				() => frm.call('terminate'),
			);
		}, __('AWS'));
	},
});
