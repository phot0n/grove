frappe.ui.form.on('Machine', {
	refresh(frm) {
		if (frm.is_new()) return;

		// Bare-metal only: on a cloud box the provider's instance type is the source of truth and
		// the GPU table is seeded from it at provision.
		if (frm.doc.public_ip && !frm.doc.cloud_provider) {
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
		// Either way the box's address changes, so both confirm: an Elastic IP replaces the
		// dynamic one, and releasing it has AWS hand out a fresh dynamic one.
		if (frm.doc.static_ip_allocation_id) {
			frm.add_custom_button(__('Release Static IP'), () => {
				frappe.confirm(
					__('Hand Elastic IP {0} back? {1} gets a new address, and anything pointing at this one — gateway routes, SSH config, DNS — stops reaching it.', [frm.doc.public_ip, frm.doc.name]),
					() => frm.call('release_static_ip').then(() => frm.reload_doc()),
				);
			}, __('AWS'));
		} else {
			frm.add_custom_button(__('Attach Static IP'), () => {
				frappe.confirm(
					__('Give {0} an Elastic IP? Its current address {1} is dropped, and the new one is billed for as long as the box holds it.', [frm.doc.name, frm.doc.public_ip]),
					() => frm.call('attach_static_ip').then(() => frm.reload_doc()),
				);
			}, __('AWS'));
		}
		frm.add_custom_button(__('Resize Root Volume'), () => {
			frappe.prompt(
				{
					fieldname: 'size_gb',
					fieldtype: 'Int',
					label: __('New size (GB)'),
					default: Math.max((frm.doc.root_volume_gb || 0) * 2, 100),
					reqd: 1,
					description: __(
						'Currently {0} GB. A volume can only grow, and AWS refuses another resize of the same volume for about six hours after one — so pick a size that covers every model this box will serve.',
						[frm.doc.root_volume_gb || 0],
					),
				},
				({size_gb}) => frm.call('resize_root_volume', {size_gb}),
				__('Resize Root Volume'),
			);
		}, __('AWS'));
		frm.add_custom_button(__('Terminate'), () => {
			frappe.confirm(
				__('Destroy instance {0}? Its root volume goes with it — the engine images and every model weight on this box are lost.', [frm.doc.instance_id]),
				() => frm.call('terminate'),
			);
		}, __('AWS'));
	},
});
