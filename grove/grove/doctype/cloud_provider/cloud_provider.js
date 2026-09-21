frappe.ui.form.on('Cloud Provider', {
	refresh(frm) {
		render_gpu_types(frm);
		if (frm.is_new() || frm.doc.provider_type !== 'runpod') return;
		frm.add_custom_button(__('Fetch GPU Types'), () => {
			frm.call('fetch_gpu_types'); // enqueues a bg job; server msgprints "reload in a bit"
		});
	},

	provider_type(frm) {
		render_gpu_types(frm);
	},
});

// Render the cached gpu_types JSON (refreshed by the Fetch GPU Types bg job) into the
// read-only HTML field, with a client-side filter. IDs go into a Machine GPU row's
// GPU Type / a Pod's gpu_type_id.
function render_gpu_types(frm) {
	const field = frm.fields_dict.gpu_types_html;
	if (!field) return;
	const wrap = field.$wrapper;

	let rows = [];
	try {
		rows = JSON.parse(frm.doc.gpu_types || '[]') || [];
	} catch (e) {
		rows = [];
	}

	if (!rows.length) {
		wrap.html(
			`<p class="text-muted">${__('No GPU types cached yet — click “Fetch GPU Types”, then reload.')}</p>`,
		);
		return;
	}

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

	const updated = frm.doc.gpu_types_updated
		? __('Last fetched: {0}', [frappe.datetime.str_to_user(frm.doc.gpu_types_updated)])
		: '';

	wrap.html(`
		<div class="text-muted" style="margin-bottom:6px">
			${__('Put the ID into a Pod (gpu_type_id). A Machine gets its cards from a scan.')}
			<span style="float:right">${esc(updated)}</span>
		</div>
		<input class="form-control gpu-filter" placeholder="${__('Filter…')}" style="margin-bottom:8px">
		<div style="max-height:60vh;overflow:auto">
			<table class="table table-bordered" style="margin:0">
				<thead><tr>
					<th>ID</th><th>Name</th><th>VRAM GB</th><th>Secure</th><th>Community</th>
				</tr></thead>
				<tbody>${tbody}</tbody>
			</table>
		</div>`);

	wrap.find('.gpu-filter').on('input', function () {
		const q = this.value.toLowerCase();
		wrap.find('tbody tr').each(function () {
			this.style.display = this.innerText.toLowerCase().includes(q) ? '' : 'none';
		});
	});
}
