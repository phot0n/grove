// One renderer for the GPU table, shared by the Machine form (the box that owns the cards) and
// the Inference Server form (the role that serves from them). Both draw the same rows, so drawing
// them twice was two copies to keep in step — this is loaded app-wide via app_include_js.
frappe.provide('grove');

grove.render_gpu_table = function (wrapper, gpus, opts = {}) {
	if (!wrapper) return;
	if (!gpus.length) {
		wrapper.html(`<p class="text-muted">${opts.empty || __('No GPUs recorded.')}</p>`);
		return;
	}

	const esc = frappe.utils.escape_html;
	const free = gpus.filter((g) => !g.held_by).length;

	const rows = gpus
		.map((g) => {
			// A holder is a Model Replica; the deployments list is what the allocation view adds
			// so the model name is readable without opening the replica.
			const holder = g.held_by
				? `<a href="/app/model-replica/${encodeURIComponent(g.held_by)}">${esc(
						(g.deployments || [])[0]?.model || g.held_by,
					)}</a>`
				: `<span class="text-muted">—</span>`;
			return `<tr>
				<td style="text-align:right">${g.gpu_index}</td>
				<td>${esc(g.gpu_type || '')}</td>
				<td style="text-align:right">${g.vram_gb || ''}</td>
				<td><code>${esc(g.device_id || '')}</code></td>
				<td><span class="indicator-pill ${g.held_by ? 'blue' : 'green'}">${
					g.held_by ? __('Held') : __('Free')
				}</span></td>
				<td>${holder}</td>
			</tr>`;
		})
		.join('');

	wrapper.html(`
		<div class="text-muted" style="margin-bottom:6px">
			${__('{0} of {1} free', [free, gpus.length])}${opts.note ? ` · ${opts.note}` : ''}
		</div>
		<table class="table table-bordered" style="margin:0">
			<thead><tr>
				<th style="text-align:right">${__('CUDA')}</th>
				<th>${__('Type')}</th>
				<th style="text-align:right">${__('VRAM GB')}</th>
				<th>${__('Device')}</th>
				<th>${__('Status')}</th>
				<th>${__('Held By')}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
	`);
};
