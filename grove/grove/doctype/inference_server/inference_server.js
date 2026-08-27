frappe.ui.form.on('Inference Server', {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.machine) {
			frm.add_custom_button(__('Setup'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
			// The exporters listen on 9100/9400 for the Monitoring Agent above to scrape —
			// restrict those ports to that agent in the security group.
			frm.add_custom_button(__('Install Exporters'), () => frm.call('install_exporters'));
		}
		render_gpus(frm);
	},
});

// GPU allocation is derived, never stored: the server recomputes it from the Machine's cards
// and the Active Model Replicas on this box, so it is correct the moment one is deployed or
// torn down. Nothing to sync, nothing to go stale.
function render_gpus(frm) {
	const field = frm.fields_dict.gpus_html;
	if (!field) return;
	const wrap = field.$wrapper;

	frm.call('get_gpu_allocation').then((r) => {
		const gpus = r.message || [];
		if (!gpus.length) {
			wrap.html(
				`<p class="text-muted">${__("No GPUs on this server's Machine — add them to the Machine's GPU table.")}</p>`,
			);
			return;
		}

		const esc = frappe.utils.escape_html;
		const colour = { Free: 'green', Allocated: 'blue', Conflict: 'red' };
		const rows = gpus
			.map((g) => {
				const used_by = (g.deployments || [])
					.map(
						(d) =>
							`<a href="/app/model-deployment/${encodeURIComponent(d.name)}">${esc(d.model || d.name)}</a>`,
					)
					.join(', ');
				return `<tr>
					<td style="text-align:right">${g.gpu_index}</td>
					<td>${esc(g.gpu_model || '')}</td>
					<td style="text-align:right">${g.vram_gb || ''}</td>
					<td><span class="indicator-pill ${colour[g.status]}">${__(g.status)}</span></td>
					<td>${used_by || `<span class="text-muted">—</span>`}</td>
				</tr>`;
			})
			.join('');

		const free = gpus.filter((g) => g.status === 'Free').length;
		const clash = gpus.some((g) => g.status === 'Conflict');

		wrap.html(`
			<div class="text-muted" style="margin-bottom:6px">
				${__('{0} of {1} free', [free, gpus.length])} ·
				${__('derived from Active Model Replicas')}
				${clash ? ` · <span style="color:var(--red-500)">${__('same GPU claimed twice')}</span>` : ''}
			</div>
			<table class="table table-bordered" style="margin:0">
				<thead><tr>
					<th style="text-align:right">${__('CUDA')}</th>
					<th>${__('Type')}</th>
					<th style="text-align:right">${__('VRAM GB')}</th>
					<th>${__('Status')}</th>
					<th>${__('Used By')}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table>`);
	});
}
