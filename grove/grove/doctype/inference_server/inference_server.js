frappe.ui.form.on('Inference Server', {
	refresh(frm) {
		show_front_warning(frm);
		if (frm.is_new()) return;

		if (frm.doc.status !== 'Terminated') {
			if (!frm.doc.is_provisioned) {
				frm.add_custom_button(__('Setup'), () => {
					frm.call('setup').then(() => frm.reload_doc());
				});
			}
			if (frm.doc.is_standalone) {
				frm.add_custom_button(__('Sync DNS Records'), () => frm.call('sync_dns_records'), __('TLS'));
				frm.add_custom_button(__('Deploy Fleet Certificate'), () => frm.call('deploy_tls'), __('TLS'));
			}
			// The exporters listen on 9100/9400 for the agent above to scrape.
			frm.add_custom_button(__('Update Scrape Auth'), () => frm.call('update_scrape_auth'));
			frm.add_custom_button(__('Archive'), () => {
				frappe.confirm(
					__('Archive {0}? Its Machine is terminated — the box and everything on its disk are gone — and this server leaves the fleet. Refused while anything still depends on it.', [frm.doc.name]),
					() => frm.call('archive').then(() => frm.reload_doc()),
				);
			}, __('Danger'));
		}
		render_gpus(frm);
	},

	ingress: show_front_warning,

	is_standalone(frm) {
		// The Ingress field hides once this is ticked, and a hidden value would fail the save.
		if (frm.doc.is_standalone && frm.doc.ingress) frm.set_value('ingress', '');
		show_front_warning(frm);
	},
});

// A warning, not a save error: Setup is what refuses a box with no front. A box set up before the
// choice existed still serves by IP, so it keeps the warning with its own wording.
function show_front_warning(frm) {
	const has_no_front = frm.doc.status !== 'Terminated' && !frm.doc.ingress && !frm.doc.is_standalone;
	const message = frm.doc.is_provisioned
		? __('Set up with no Ingress Server and not Standalone — the gateways dial it by IP, and Standalone is fixed on a set-up box.')
		: __('No Ingress Server and not Standalone — Setup will refuse this box. Pick the ingress that fronts it, or tick Standalone to have the gateways dial it directly.');
	frm.set_intro(has_no_front ? message : '', 'orange');
}

// Derived, never stored: recomputed from the Machine's cards and the Active replicas on this box,
// so it is correct the moment one is deployed or torn down.
function render_gpus(frm) {
	frm.call('get_gpu_allocation').then((r) => {
		grove.render_gpu_table(frm.fields_dict.gpus_html?.$wrapper, r.message || [], {
			empty: __("No GPUs on this server's Machine — run Scan GPUs on it."),
			note: __("this box's Machine, live"),
		});
	});
}
