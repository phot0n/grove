-- Frees the in-flight slot the pick claimed. The ingress's counterpart to the gateway's /meter,
-- minus the metering: usage belongs to a tenant, and this box has no idea which one.
--
-- Best effort, like the gateway's. A lost release is not retried — the slot ages out of the
-- sorted set on its own, which is why in-flight is a scored set and not a counter.
local cjson = require "cjson.safe"
local http = require "resty.http"

local engine = ngx.var.grove_engine
if not engine or engine == "" or engine == "-" then
	return
end

local client = http.new()
if not client then
	return
end
client:set_timeout(1000)

local ok, err = client:request_uri("http://127.0.0.1:9090/release", {
	method = "POST",
	headers = { ["Content-Type"] = "application/json" },
	body = cjson.encode({ engine_url = engine, request_id = ngx.var.grove_request_id }),
})
if not ok then
	ngx.log(ngx.WARN, "grove ingress: release failed for ", ngx.var.grove_request_id, ": ", err)
end
