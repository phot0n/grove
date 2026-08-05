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

		// machine_type says which server doctype this box backs — works for bare-metal too,
		// so this sits above the AWS-only gate below. public_ip/region are fetch_from on both
		// doctypes, so setting `machine` on the new doc is all that's needed to fill them in.
		if (frm.doc.machine_type) {
			frappe.db.get_value(frm.doc.machine_type, {machine: frm.doc.name}, 'name').then(({message}) => {
				if (message && message.name) {
					frm.add_custom_button(__('Open {0}', [frm.doc.machine_type]), () =>
						frappe.set_route('Form', frm.doc.machine_type, message.name));
				} else {
					frm.add_custom_button(__('Create {0}', [frm.doc.machine_type]), () =>
						frappe.new_doc(frm.doc.machine_type, {machine: frm.doc.name}));
				}
			});
		}

		// A box with no provider — or a non-AWS one — shows nothing below. Gated on the
		// provider's TYPE, not merely on one being set, so a RunPod machine stays clean.
		if (frm.doc.provider_type !== 'aws') return;

		if (frm.doc.status === 'Provisioning') {
			// launch() sets status before instance_id lands — without this, reloading mid-launch
			// still shows Provision and a second click would enqueue a second real EC2 instance.
			frm.add_custom_button(__('Sync'), () => frm.call('sync'), __('AWS'));
			return;
		}

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
