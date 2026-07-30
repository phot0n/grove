frappe.ui.form.on('Model Deployment', {
	fetch_engine_logs(frm) {
		frm.call('get_engine_logs', { lines: frm.doc.log_lines || 200 }).then((r) => {
			// Newest last, like the terminal — scrolled to the bottom, where a crash is.
			const $wrapper = frm.get_field('engine_logs').$wrapper;
			$wrapper.html(
				$('<pre>')
					.css({ 'max-height': '60vh', overflow: 'auto', 'white-space': 'pre-wrap' })
					.text(r.message || __('Nothing came back — the engine has not started on the box.')),
			);
			const el = $wrapper.find('pre')[0];
			if (el) el.scrollTop = el.scrollHeight;
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.model && frm.doc.inference_server) {
			frm.add_custom_button(__('Deploy'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
			const served = frm.doc.status === 'Active' || frm.doc.status === 'Broken';
			// Fast re-render of the engine unit (dtype / gpu mem / attention backend /
			// port) without a full re-install. Only meaningful once served.
			if (served) {
				frm.add_custom_button(__('Update Engine Config'), () => {
					frappe.confirm(
						__('Restart the engine to apply config changes? In-flight requests will be dropped.'),
						() => frm.call('apply_engine_config').then(() => frm.reload_doc()),
					);
				});
			}
			// Multi-tenant box: remove just THIS instance's container/unit + key (shared
			// venv/weights stay for other instances). Also the cleanup for a failed deploy —
			// a container carries --restart unless-stopped, so a crash-looping engine keeps
			// coming back until this removes it. Offered while Provisioning too: that is
			// where a deploy whose worker died leaves the doc, with the container still up.
			if (served || frm.doc.status === 'Provisioning') {
				frm.add_custom_button(__('Tear Down'), () => {
					frappe.confirm(
						frm.doc.status === 'Provisioning'
							? __('Remove this instance from its box? Check its Ansible Play first — if a deploy is still running, this will race it.')
							: __('Stop and remove this instance from its box? The model will stop serving here.'),
						() => frm.call('teardown').then(() => frm.reload_doc()),
					);
				}).addClass('btn-danger');
			}
		}
	},
});
