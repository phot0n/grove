-- Asks the Go agent /pick for a replica in this Network, then rewrites the upstream and swaps
-- the gateway's internal key for the engine's.
--
-- Deliberately smaller than the gateway's access.lua. No body is read and no body is rewritten:
-- the model comes from a header the gateway already parsed it into, and priority and
-- stream_options were stamped upstream. An ingress does not need to understand the payload it is
-- forwarding, and not parsing it is what keeps this hop cheap.
local cjson = require "cjson.safe"
local http = require "resty.http"

local AGENT_PICK = "http://127.0.0.1:9090/pick"

-- Everything the pick needs, and nothing that identifies a tenant. session_key is opaque here:
-- the gateway derived it from the api key, the model and a jittered time bucket, and the ingress
-- only hashes it.
local model = ngx.var.http_x_grove_model or ""
local session_key = ngx.var.http_x_grove_session_key or ""
local request_id = ngx.var.http_x_request_id or ""

local function fail(status, reason)
	-- Named so the gateway can tell a broken ingress from a model with nowhere to go here: it
	-- ejects a whole network on a connection failure or a 502/504, and must NOT eject one on a
	-- no-replica 503, or one unplaced model takes the network out of rotation for every other.
	ngx.header["X-Grove-Reason"] = reason
	ngx.status = status
	ngx.header["Content-Type"] = "application/json"
	ngx.say(cjson.encode({ error = { message = reason, type = "grove_ingress" } }))
	return ngx.exit(status)
end

if model == "" then
	return fail(400, "no-model")
end

local client, err = http.new()
if not client then
	return fail(503, "agent-unavailable")
end
client:set_timeout(2000)

local res
res, err = client:request_uri(AGENT_PICK, {
	method = "POST",
	headers = { ["Content-Type"] = "application/json" },
	body = cjson.encode({ model = model, session_key = session_key, request_id = request_id }),
})
if not res then
	return fail(503, "agent-unavailable")
end

local decision = cjson.decode(res.body)
if type(decision) ~= "table" then
	return fail(503, "agent-unavailable")
end
if not decision.allow then
	return fail(decision.status or 503, decision.reason or "no-replica")
end

-- Stamped in the access phase, before the request leaves, so it is set whatever status the engine
-- comes back with. The gateway reads it off the response to attribute usage to a placement it
-- never chose — an ingress picked it, and nothing else downstream knows which.
ngx.header["X-Grove-Engine"] = decision.deployment or "-"

-- Kept for the log phase: together they name the in-flight slot /pick just claimed.
ngx.var.grove_engine = decision.engine_url
ngx.var.grove_deployment = decision.deployment or "-"
ngx.var.grove_model = model
ngx.var.grove_request_id = request_id

ngx.var.upstream = decision.engine_url
if decision.internal_key and decision.internal_key ~= "" then
	ngx.req.set_header("Authorization", "Bearer " .. decision.internal_key)
end
-- Not forwarded to the engine: it is this hop's routing input and means nothing to vLLM.
ngx.req.clear_header("X-Grove-Session-Key")
