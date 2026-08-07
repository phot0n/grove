// Model-intrinsic vLLM config lives on the Model and is read in the backend
// (grove.serve_command.ServeCommand) at build time — the Pod no longer mirrors it. The Serving
// tab holds only per-pod tuning (serve port, kv cache dtype, gpu-mem-util, max_model_len, aliases,
// extra args). Nothing to copy on Model select.
// Keep the viewer bounded — a loading vLLM emits far more than anyone scrolls back through.
const LOG_LINE_LIMIT = 2000;
// Well inside the server's 45s liveness TTL, so one slow ping doesn't cut the stream.
const LOG_PING_INTERVAL = 15000;

frappe.ui.form.on('Pod', {
	refresh(frm) {
		if (frm.is_new()) return;
		setup_log_view(frm);

		// No provider pod yet → offer Spawn. Once spawned → Sync / Restart / Terminate.
		if (!frm.doc.pod_id && (frm.doc.status === 'Pending' || frappe.boot.developer_mode)) {
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
		// Restart updates the pod in place (RunPod resets the container) → ports may move, then sync.
		frm.add_custom_button(__('Restart'), () => {
			frappe.confirm(
				__('Restart applies this config to the running pod — it resets, keeping its volume, so weights are not re-downloaded. Its ports may move. An edited GPU type or count cannot be applied to a live pod and is refused: that needs Terminate, then Spawn. Continue?'),
				() => frm.call('restart').then(() => frm.reload_doc()),
			);
		});
		frm.add_custom_button(__('Terminate'), () => {
			frappe.confirm(__('Terminate this pod? Frees GPU/disk/billing.'), () => {
				frm.call('terminate').then(() => frm.reload_doc());
			});
		}, __('Danger'));
	},

	toggle_log_stream(frm) {
		if (frm.log_streaming) {
			// The job publishes a final 'done' event; flip the button now so it doesn't look stuck.
			frm.call('stop_logs');
			set_log_button(frm, false);
		} else {
			frm.call('stream_logs').then(() => set_log_button(frm, true));
		}
	},
});

function setup_log_view(frm) {
	const $wrapper = frm.get_field('log_output').$wrapper;
	if (!$wrapper.find('pre').length) {
		$wrapper.html(
			`<pre class="pod-log-output" style="height: 60vh; overflow: auto; margin: 0; padding: 8px;
				background: var(--fg-color); border: 1px solid var(--border-color);
				border-radius: var(--border-radius); font-size: 11px; white-space: pre-wrap;"></pre>`,
		);
	}
	frm.log_lines = frm.log_lines || [];
	set_log_button(frm, frm.log_streaming);

	frappe.realtime.off('grove_log');
	frappe.realtime.on('grove_log', ({ lines, done }) => {
		// The job takes a moment to notice Stop — drop what it publishes in the meantime.
		if (!frm.log_streaming && !done) return;
		frm.log_lines = frm.log_lines.concat(lines || []).slice(-LOG_LINE_LIMIT);
		const pre = frm.get_field('log_output').$wrapper.find('pre')[0];
		// Only follow the tail if the reader is already at the bottom — don't yank them back.
		const follow = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
		pre.textContent = frm.log_lines.join('\n');
		if (follow) pre.scrollTop = pre.scrollHeight;
		if (done) set_log_button(frm, false);
	});
}

function set_log_button(frm, streaming) {
	frm.log_streaming = streaming;
	set_log_heartbeat(frm, streaming);
	frm.get_field('toggle_log_stream').set_label(
		streaming ? __('Stop Streaming') : __('Start Streaming'),
	);
}

// The stream lives while this ping keeps its key alive, so it must stop the moment this form
// is no longer on screen — a job left running holds one of the site's few background workers.
// Leaving the route stops it here; closing the tab stops it by never firing again.
function set_log_heartbeat(frm, streaming) {
	clearInterval(frm.log_heartbeat);
	if (!streaming) return;
	frm.log_heartbeat = setInterval(() => {
		const route = frappe.get_route();
		const showing = route[0] === 'Form' && route[1] === frm.doctype && route[2] === frm.docname;
		if (!showing || !frm.log_streaming) {
			clearInterval(frm.log_heartbeat);
			return;
		}
		frappe.xcall('grove.log_relay.keep_streaming', {
			doctype: frm.doctype, docname: frm.docname,
		});
	}, LOG_PING_INTERVAL);
}
