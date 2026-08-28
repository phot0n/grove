// The log viewer here is the twin of the one in pod.js — same realtime payload ({lines, done}),
// same 'grove_log' event. Kept per-doctype rather than shared: a shared helper would need an
// asset bundle, and doctype JS is served straight off disk.
const LOG_LINE_LIMIT = 2000;
// Well inside the server's 45s liveness TTL, so one slow ping doesn't cut the stream.
const LOG_PING_INTERVAL = 15000;

frappe.ui.form.on('Model Replica', {
	fetch_engine_logs(frm) {
		// A running stream is stopped, not left to append onto the tail this is about to fetch:
		// it owns the same pane, and it holds a background worker, an SSH connection and a
		// `docker logs -f` on the box for as long as it runs.
		stop_log_stream(frm);
		// Both readers write the same pane, so each starts from an empty one — otherwise a fetch
		// after a stream (or the other way round) reads as one log with a jump in the middle.
		clear_logs(frm);
		frm.call('get_engine_logs', { lines: frm.doc.log_lines || 200 }).then((r) => {
			const text = r.message || __('Nothing came back — the engine has not started on the box.');
			frm.log_lines = text.split('\n');
			render_logs(frm, true);
		});
	},

	toggle_log_stream(frm) {
		if (frm.log_streaming) {
			stop_log_stream(frm);
		} else {
			clear_logs(frm);
			frm.call('stream_engine_logs').then(() => set_log_button(frm, true));
		}
	},

	refresh(frm) {
		if (frm.is_new()) return;
		setup_log_view(frm);
		if (!(frm.doc.model && frm.doc.inference_server)) return;
		const status = frm.doc.status;
		const served = status === 'Active' || status === 'Broken';
		// Provisioning: a play already owns this instance, and a second one would race it over
		// the same files. Terminated: teardown took the container, the port and the GPU claims
		// off the box for good — a new deployment is how the model comes back.
		if (!['Provisioning', 'Terminated', 'Active'].includes(status)) {
			frm.add_custom_button(__('Deploy'), () => {
				frm.call('setup').then(() => frm.reload_doc());
			});
		}
		// Fast re-render of the container's config (kv cache dtype / gpu mem / batch caps /
		// attention backend / env rows) with none of the deploy around it. Offered while
		// Provisioning as well, because that is where a wrong flag shows up: the deploy is
		// stuck on its health gate and this restarts the engine with the corrected argv
		// instead of waiting the gate out.
		if (served || status === 'Provisioning') {
			frm.add_custom_button(__('Update Engine Config'), () => {
				frappe.confirm(
					status === 'Provisioning'
						? __('Restart the engine with the current config while its deploy is still running? The deploy keeps waiting for the engine to answer, and will pass or fail on this one.')
						: __('Restart the engine to apply config changes? In-flight requests will be dropped.'),
					() => frm.call('apply_engine_config').then(() => frm.reload_doc()),
				);
			});
		}
		// Pause without giving the box back: the container, its config, its port and its
		// GPUs stay claimed, so Start is a `docker start` rather than a redeploy.
		if (served) {
			frm.add_custom_button(__('Stop'), () => {
				frappe.confirm(
					__('Stop this engine? It stops serving and stays on the box until you start it again.'),
					() => frm.call('stop').then(() => frm.reload_doc()),
				);
			});
		}
		if (status === 'Inactive') {
			frm.add_custom_button(__('Start'), () => {
				frm.call('start').then(() => frm.reload_doc());
			});
		}
		// Multi-tenant box: remove just THIS instance's container + key (shared weights and
		// image stay for other instances). Also the cleanup for a failed deploy — a container
		// carries --restart unless-stopped, so a crash-looping engine keeps coming back until
		// this removes it. Not while Provisioning: it would race the running play.
		if (served || status === 'Inactive') {
			frm.add_custom_button(__('Tear Down'), () => {
				frappe.confirm(
					__('Stop and remove this instance from its box? The model stops serving here, and this deployment cannot be redeployed — make a new one.'),
					() => frm.call('teardown').then(() => frm.reload_doc()),
				);
			}).addClass('btn-danger');
		}
	},
});

function setup_log_view(frm) {
	frm.log_lines = frm.log_lines || [];
	set_log_button(frm, frm.log_streaming);

	frappe.realtime.off('grove_log');
	frappe.realtime.on('grove_log', ({ lines, done }) => {
		// The job takes a moment to notice Stop, and its last batch rides the 'done' event —
		// drop both once we are no longer streaming. The pane belongs to the fetch by then, and
		// appending to it is the jump-in-the-middle that clearing was meant to prevent. The
		// button still follows 'done', which is the one thing worth hearing after a stop.
		if (frm.log_streaming) {
			frm.log_lines = frm.log_lines.concat(lines || []).slice(-LOG_LINE_LIMIT);
			render_logs(frm);
		}
		if (done) set_log_button(frm, false);
	});
}

// The job takes a moment to notice Stop; flip the button now so it doesn't look stuck. Safe to
// call when nothing is streaming, which is what lets Fetch call it unconditionally.
function stop_log_stream(frm) {
	if (!frm.log_streaming) return;
	frm.call('stop_engine_logs');
	set_log_button(frm, false);
}

function clear_logs(frm) {
	frm.log_lines = [];
	render_logs(frm, true);
}

function render_logs(frm, force_scroll) {
	const $wrapper = frm.get_field('engine_logs').$wrapper;
	if (!$wrapper.find('pre').length) {
		$wrapper.html(
			$('<pre>').css({
				height: '60vh', overflow: 'auto', margin: 0, padding: '8px',
				background: 'var(--fg-color)', border: '1px solid var(--border-color)',
				'border-radius': 'var(--border-radius)', 'font-size': '11px',
				'white-space': 'pre-wrap',
			}),
		);
	}
	// Newest last, like the terminal. Only follow the tail if the reader is already at the
	// bottom — don't yank them back mid-scroll.
	const pre = $wrapper.find('pre')[0];
	const follow = force_scroll || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
	pre.textContent = frm.log_lines.join('\n');
	if (follow) pre.scrollTop = pre.scrollHeight;
}

function set_log_button(frm, streaming) {
	frm.log_streaming = streaming;
	set_log_heartbeat(frm, streaming);
	frm.get_field('toggle_log_stream').set_label(
		streaming ? __('Stop Streaming') : __('Start Streaming'),
	);
}

// The stream lives while this ping keeps its key alive, so it must stop the moment this form
// is no longer on screen — a job left running holds one of the site's few background workers,
// plus an SSH connection and a `docker logs -f` on the box. Leaving the route stops it here;
// closing the tab stops it by never firing again.
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
