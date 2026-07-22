frappe.ui.form.on('Cloud Provider', {
	refresh(frm) {
		if (frm.is_new() || frm.doc.provider_type !== 'runpod') return;
		frm.add_custom_button(__('Fetch GPU Types'), () => {
			frappe.dom.freeze(__('Fetching GPU types…'));
			frm.call('fetch_gpu_types')
				.then((r) => {
					frappe.dom.unfreeze();
					show_gpu_types_dialog(r.message || []);
				})
				.catch(() => frappe.dom.unfreeze());
		});
	},
});

function show_gpu_types_dialog(rows) {
	const esc = frappe.utils.escape_html;
	const tbody = rows
		.map(
			(g) => `<tr>
				<td><code>${esc(g.id || '')}</code></td>
				<td>${esc(g.displayName || '')}</td>
				<td style="text-align:right">${g.memoryInGb || ''}</td>
				<td style="text-align:center">${g.secureCloud ? '✓' : ''}</td>
				<td style="text-align:center">${g.communityCloud ? '✓' : ''}</td>
			</tr>`,
		)
		.join('');

	const d = new frappe.ui.Dialog({
		title: __('RunPod GPU Types'),
		size: 'large',
	});
	d.$body.html(`
		<p class="text-muted">Put the <b>ID</b> into a Machine's GPU row <code>gpu_model</code>.</p>
		<input class="form-control gpu-filter" placeholder="${__('Filter…')}" style="margin-bottom:8px">
		<div style="max-height:60vh;overflow:auto">
			<table class="table table-bordered" style="margin:0">
				<thead><tr>
					<th>ID (gpu_model)</th><th>Name</th><th>VRAM GB</th><th>Secure</th><th>Community</th>
				</tr></thead>
				<tbody>${tbody}</tbody>
			</table>
		</div>`);
	d.$body.find('.gpu-filter').on('input', function () {
		const q = this.value.toLowerCase();
		d.$body.find('tbody tr').each(function () {
			this.style.display = this.innerText.toLowerCase().includes(q) ? '' : 'none';
		});
	});
	d.show();
}
