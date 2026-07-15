frappe.ui.form.on('Grove Settings', {
	refresh(frm) {
		frm.add_custom_button(__('Full Sync All Gateways'), () => {
			frm.call('full_sync_all');
		});
	},
});
