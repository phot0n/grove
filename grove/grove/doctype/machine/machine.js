frappe.ui.form.on('Machine', {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.public_ip) return;

		// nvidia-smi is the truth for what's in the box — these rows drive CUDA pinning, the
		// VRAM fit check and the Inference Server's GPU view, so don't hand-type them.
		frm.add_custom_button(__('Scan GPUs'), () => {
			frappe.confirm(
				__("Read this box's GPUs over SSH and replace the GPU table with what it reports?"),
				() => frm.call('scan_gpus'),
			);
		});
	},
});
