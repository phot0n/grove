-- agent /meter. Runs on EVERY request that was admitted (incl. client
-- disconnect / error), which is what releases the in-flight slot. Cosockets are banned in
-- the log phase, so the network call is deferred to a zero-delay timer.
local ctx = ngx.ctx
if not ctx.meter_id then
	return -- request was denied at /decide; nothing claimed, nothing to release
end

local cjson = require "cjson.safe"
local usage = ctx.usage_line or ""
usage = (usage:gsub("^data:%s*", "")) -- strip SSE prefix if present

local payload = cjson.encode({
	meter_id = ctx.meter_id,
	prefix = ctx.prefix or "",
	model = ctx.model or "",
	usage = usage,
	engine_url = ctx.engine_url or "",
	request_id = ctx.request_id or "",
})

local function send(premature, body)
	if premature then
		return
	end
	local http = require "resty.http"
	local httpc = http.new()
	httpc:set_timeout(10000)
	local ok, err = httpc:request_uri("http://127.0.0.1:9090/meter", {
		method = "POST",
		body = body,
		headers = { ["Content-Type"] = "application/json" },
	})
	if not ok then
		ngx.log(ngx.ERR, "grove meter failed: ", err)
	end
end

local ok, err = ngx.timer.at(0, send, payload)
if not ok then
	ngx.log(ngx.ERR, "grove meter timer failed: ", err)
end
