-- Reads the request, asks the Go agent /decide, and on admit rewrites the
-- upstream + swaps the client key for the engine's internal key. Denials are
-- returned as OpenAI-shaped error JSON with the agent's status code.
local cjson = require "cjson.safe"
local http = require "resty.http"

local AGENT_DECIDE = "http://127.0.0.1:9090/decide"

-- Body is already read (lua_need_request_body on); fall back to the temp file.
local body = ngx.req.get_body_data()
if not body then
	local fpath = ngx.req.get_body_file()
	if fpath then
		local fh = io.open(fpath, "rb")
		if fh then
			body = fh:read("*a")
			fh:close()
		end
	end
end
body = body or ""

local model = ""
local session = ngx.var.http_x_grove_session
-- The endpoints whose vLLM schema carries stream_options and priority; the body is left
-- untouched everywhere else rather than risking a field the schema rejects.
local uri = ngx.var.uri
local completions = uri == "/v1/chat/completions" or uri == "/v1/completions"
local obj = cjson.decode(body)
local rewrite = false
if type(obj) == "table" then
	model = obj.model or ""
	if (not session or session == "") and type(obj.user) == "string" then
		session = obj.user
	end
	-- Guarantee a usage frame on OpenAI streaming so we can meter (§6 job 3).
	if obj.stream == true and completions then
		if type(obj.stream_options) ~= "table" then
			obj.stream_options = {}
		end
		obj.stream_options.include_usage = true
		rewrite = true
	end
end

if model ~= "" then
	ngx.var.grove_model = model
end

local decide_body = cjson.encode({
	authorization = ngx.var.http_authorization or "",
	model = model,
	bytes = #body,
	session = session or "",
})

local httpc = http.new()
httpc:set_timeout(10000)
local res, err = httpc:request_uri(AGENT_DECIDE, {
	method = "POST",
	body = decide_body,
	headers = { ["Content-Type"] = "application/json" },
})

if not res then
	ngx.log(ngx.ERR, "grove decide unreachable: ", err)
	ngx.status = 503
	ngx.header["Content-Type"] = "application/json"
	ngx.say('{"error":{"message":"gateway unavailable","type":"grove_agent_error"}}')
	return ngx.exit(503)
end

local d = cjson.decode(res.body) or {}
if not d.allow then
	ngx.status = d.status or 403
	ngx.header["Content-Type"] = "application/json"
	ngx.say(cjson.encode({ error = { message = d.reason or "request denied", type = "grove_gateway" } }))
	return ngx.exit(ngx.status)
end

-- Admitted. Point at the chosen engine (full URL incl. path) and swap the
-- client's gateway key for the engine's internal key.
ngx.var.upstream = d.engine_url .. ngx.var.request_uri
if d.internal_key and d.internal_key ~= "" then
	ngx.req.set_header("Authorization", "Bearer " .. d.internal_key)
end
ngx.ctx.meter_id = d.meter_id
ngx.ctx.prefix = d.prefix or ""
ngx.ctx.model = model  -- for per-model usage metering in the log phase
if d.prefix and d.prefix ~= "" then
	ngx.var.grove_prefix = d.prefix
end

-- Traceable request-id (gr-<gateway>-<server>-<keyprefix>-<rand>, built by the agent). Standard
-- X-Request-Id only: vLLM adopts it as its request_id (chatcmpl-<id>) and OpenAI-aware tooling
-- reads it; ours is canonical, overriding any client-supplied value. Also recorded in the
-- access log (rid=).
if d.request_id and d.request_id ~= "" then
	ngx.req.set_header("X-Request-Id", d.request_id)   -- to the engine
	ngx.header["X-Request-Id"] = d.request_id          -- back to the client
	ngx.var.grove_request_id = d.request_id            -- access-log rid=
end

-- Queueing rank, from the caller's Grove User Group and never from the caller: stamped
-- unconditionally so a client-supplied `priority` cannot elevate itself. Grove already
-- flipped the sign, so this is vLLM's convention (lowest served first) and 0 is the
-- baseline. Only engines running --scheduling-policy priority act on it.
if type(obj) == "table" and completions then
	obj.priority = d.priority or 0
	rewrite = true
end
if rewrite then
	local nb = cjson.encode(obj)
	if nb then
		ngx.req.set_body_data(nb)
	end
end
