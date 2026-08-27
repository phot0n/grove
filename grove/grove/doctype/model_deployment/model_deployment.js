frappe.ui.form.on('Model Deployment', {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__('Place a Replica'), () => place_replica(frm));
		frm.add_custom_button(__('Add Replica'), () => add_replica(frm));
		show_replicas(frm);
	},
});

// The scheduler picks the box and the cards off this deployment's placement_policy. It throws
// naming why every box was rejected when nothing fits, so the failure is readable here rather
// than being a silent "no capacity".
function place_replica(frm) {
	frappe.confirm(__('Let the scheduler choose a box for one more replica?'), () => {
		frm.call('add_replica').then((r) => {
			if (r.message) frappe.set_route('Form', 'Model Replica', r.message);
		});
	});
}

// The manual path, still here: naming the box and the cards is how an operator overrides the
// scheduler for one replica. Any Active box is offered — a deployment is not tied to a region,
// and deploy:<model> unions its replicas wherever they sit. The box's live GPU allocation is
// shown so a card another deployment already holds is visible before the deploy refuses it.
function add_replica(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __('Add Replica'),
		fields: [
			{
				fieldname: 'inference_server',
				fieldtype: 'Link',
				label: __('Inference Server'),
				options: 'Inference Server',
				reqd: 1,
				get_query: () => ({ filters: { status: 'Active' } }),
				onchange: () => show_gpu_allocation(dialog),
			},
			{ fieldname: 'allocation', fieldtype: 'HTML', label: __('GPUs on this box') },
			{
				fieldname: 'gpus',
				fieldtype: 'Data',
				label: __('GPU Indices'),
				description: __(
					'CUDA indices to pin, comma separated — {0} of them. Blank means single-GPU, unpinned.',
					[frm.doc.gpus_per_replica]
				),
			},
		],
		primary_action_label: __('Add and Deploy'),
		primary_action({ inference_server, gpus }) {
			dialog.hide();
			frm.call('add_replica', { inference_server, gpus }).then((r) => {
				if (r.message) frappe.set_route('Form', 'Model Replica', r.message);
			});
		},
	});
	dialog.show();
}

function show_gpu_allocation(dialog) {
	const server = dialog.get_value('inference_server');
	const field = dialog.get_field('allocation');
	if (!server) return field.$wrapper.empty();
	frappe.xcall('run_doc_method', {
		dt: 'Inference Server',
		dn: server,
		method: 'get_gpu_allocation',
	}).then((gpus) => {
		const rows = (gpus || [])
			.map((gpu) => {
				const held = (gpu.deployments || []).map((d) => d.name).join(', ');
				return `<tr><td>${gpu.gpu_index}</td><td>${frappe.utils.escape_html(
					gpu.gpu_model || ''
				)}</td><td>${gpu.vram_gb || ''}</td><td>${gpu.status}</td>
				<td class="text-muted small">${frappe.utils.escape_html(held)}</td></tr>`;
			})
			.join('');
		field.$wrapper.html(
			`<table class="table table-bordered small"><thead><tr>
			<th>${__('Index')}</th><th>${__('Model')}</th><th>${__('VRAM')}</th>
			<th>${__('Status')}</th><th>${__('Held by')}</th></tr></thead>
			<tbody>${rows}</tbody></table>`
		);
	});
}

// The count is what a reconciler will drive later, so it is shown the way it will be computed:
// Provisioning is capacity already bought and is counted alongside Active.
function show_replicas(frm) {
	frappe.db
		.get_list('Model Replica', {
			filters: { model_deployment: frm.doc.name },
			fields: ['name', 'inference_server', 'status'],
			limit: 100,
		})
		.then((replicas) => {
			const live = replicas.filter((r) => ['Active', 'Provisioning'].includes(r.status));
			frm.dashboard.clear_headline();
			frm.dashboard.set_headline(
				replicas.length
					? __('{0} replicas — {1} serving or provisioning', [replicas.length, live.length])
					: __('No replicas yet. Add Replica places one on any Active box.')
			);
		});
}
