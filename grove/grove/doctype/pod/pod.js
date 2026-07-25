// Model-intrinsic vLLM config lives on the Model and is read in the backend
// (grove.serve_command.ServeCommand) at build time — the Pod no longer mirrors it. The Serving
// tab holds only per-pod tuning (serve port, dtype, gpu-mem-util, max_model_len, aliases,
// extra args). Nothing to copy on Model select.
frappe.ui.form.on('Pod', {
	refresh(frm) {
		if (frm.is_new()) return;

		// No provider pod yet → offer Spawn. Once spawned → Sync / Restart / Terminate.
		if (!frm.doc.pod_id) {
			frm.add_custom_button(__('Spawn'), () => {
				frm.call('spawn').then(() => frm.reload_doc());
			});
			return;
		}

		frm.add_custom_button(__('Sync'), () => {
			frm.call('sync').then(() => frm.reload_doc());
		});
		// Stop frees the GPU but keeps the pod + volume; Start resumes the same pod.
		if (frm.doc.status === 'Stopped') {
			frm.add_custom_button(__('Start'), () => {
				frm.call('start').then(() => frm.reload_doc());
			});
		} else {
			frm.add_custom_button(__('Stop'), () => {
				frappe.confirm(
					__('Stop this pod? The GPU is released (billing stops) but the volume is kept, so Start can resume it.'),
					() => frm.call('stop').then(() => frm.reload_doc()),
				);
			});
		}
		// Restart respawns (RunPod bakes the start command at create) → ports change, then sync.
		frm.add_custom_button(__('Restart'), () => {
			frappe.confirm(
				__('Restart respawns the pod to apply config — its ports will change (and weights re-download). Continue?'),
				() => frm.call('restart').then(() => frm.reload_doc()),
			);
		});
		frm.add_custom_button(__('Terminate'), () => {
			frappe.confirm(__('Terminate this pod? Frees GPU/disk/billing.'), () => {
				frm.call('terminate').then(() => frm.reload_doc());
			});
		}, __('Danger'));
	},
});
