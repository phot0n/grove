// Which doctype a Machine Type backs. The Select reads as the role ('Inference'); the doctype
// carries the word Server. Pinned against the Select and the doctypes by test_machine_types.py.
const SERVER_DOCTYPE = {
	'Gateway': 'Gateway Server',
	'Ingress': 'Ingress Server',
	'Inference': 'Inference Server',
	'Monitoring Agent': 'Monitoring Agent',
};

frappe.ui.form.on('Machine', {
	refresh(frm) {
		if (frm.is_new()) return;

		// Bare-metal only: on a cloud box the provider's instance type is the source of truth and
		// the cards are seeded from it at provision.
		if (frm.doc.public_ip && !frm.doc.cloud_provider) {
			// nvidia-smi is the truth for what's in the box — these records drive CUDA pinning,
			// the VRAM fit check and the Inference Server's GPU view, so don't hand-type them.
			frm.add_custom_button(__('Scan GPUs'), () => {
				frappe.confirm(
					__("Read this box's GPUs over SSH? A card still there keeps its record and whatever holds it; one that is gone is removed."),
					() => frm.call('scan_gpus'),
				);
			});
		}

		// Reads nvidia-smi and never writes, so unlike Scan GPUs it is offered on cloud boxes too.
		if ((frm.doc.gpus || []).length) {
			frm.add_custom_button(__('GPU Memory'), () => show_gpu_memory(frm));
		}

		// machine_type says which server doctype this box backs — works for bare-metal too,
		// so this sits above the AWS-only gate below. public_ip/region are fetch_from on both
		// doctypes, so setting `machine` on the new doc is all that's needed to fill them in.
		const server_doctype = SERVER_DOCTYPE[frm.doc.machine_type];
		if (server_doctype) {
			frappe.db.get_value(server_doctype, {machine: frm.doc.name}, 'name').then(({message}) => {
				if (message && message.name) {
					frm.add_custom_button(__('Open {0}', [server_doctype]), () =>
						frappe.set_route('Form', server_doctype, message.name));
				} else {
					frm.add_custom_button(__('Create {0}', [server_doctype]), () =>
						frappe.new_doc(server_doctype, {machine: frm.doc.name}));
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

// Live nvidia-smi memory, in a table. The call SSHes to the box, so freeze while it runs —
// without it the button looks dead for the ten seconds Ansible takes.
function show_gpu_memory(frm) {
	frappe.dom.freeze(__('Reading nvidia-smi on {0}…', [frm.doc.name]));
	frm.call('gpu_memory')
		.then(({message}) => {
			frappe.dom.unfreeze();
			new frappe.ui.Dialog({
				title: __('GPU Memory — {0}', [frm.doc.name]),
				fields: [{fieldtype: 'HTML', fieldname: 'table', options: gpu_memory_table(message || [])}],
			}).show();
		})
		.catch(() => frappe.dom.unfreeze());
}

function gpu_memory_table(rows) {
	if (!rows.length) return `<p class="text-muted">${__('nvidia-smi reported no usable GPU memory figures.')}</p>`;
	const gb = (mib) => (mib / 1024).toFixed(1);
	const body = rows
		.map(
			(row) => `<tr>
				<td>${row.gpu_index}</td>
				<td>${frappe.utils.escape_html(row.gpu_model)}</td>
				<td class="text-right">${gb(row.used_mib)}</td>
				<td class="text-right">${gb(row.free_mib)}</td>
				<td class="text-right">${gb(row.total_mib)}</td>
				<td class="text-right">${Math.round((row.used_mib / row.total_mib) * 100)}%</td>
			</tr>`,
		)
		.join('');
	return `<table class="table table-bordered">
		<thead><tr>
			<th>${__('#')}</th><th>${__('GPU')}</th>
			<th class="text-right">${__('Used (GB)')}</th>
			<th class="text-right">${__('Free (GB)')}</th>
			<th class="text-right">${__('Total (GB)')}</th>
			<th class="text-right">${__('Used')}</th>
		</tr></thead>
		<tbody>${body}</tbody>
	</table>`;
}
