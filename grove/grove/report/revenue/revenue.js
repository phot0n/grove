frappe.query_reports['Revenue'] = {
	filters: [
		{ fieldname: 'from_date', label: __('From'), fieldtype: 'Date', reqd: 1, default: frappe.datetime.month_start() },
		{ fieldname: 'to_date', label: __('To'), fieldtype: 'Date', reqd: 1, default: frappe.datetime.get_today() },
		{
			fieldname: 'group_by',
			label: __('Group By'),
			fieldtype: 'Select',
			options: 'Model\nAPI Key\nGrove User\nDay',
			default: 'Model',
			reqd: 1,
		},
		{ fieldname: 'model', label: __('Model'), fieldtype: 'Link', options: 'Model' },
		{ fieldname: 'api_key', label: __('API Key'), fieldtype: 'Link', options: 'Grove API Key' },
		{ fieldname: 'grove_user', label: __('Grove User'), fieldtype: 'Link', options: 'Grove User' },
	],
};
