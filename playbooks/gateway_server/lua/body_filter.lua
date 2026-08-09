-- capture the response's usage frame without
-- touching the stream. CPU only (cosockets are banned here); the actual /meter
-- call happens in log.lua's timer. Keeps the last newline-delimited line that
-- contains "usage" (the final OpenAI stream frame, or the whole non-streaming
-- body), carrying a partial line across chunk boundaries.
local chunk, eof = ngx.arg[1], ngx.arg[2]
local ctx = ngx.ctx

if chunk and chunk ~= "" then
	local data = (ctx.carry or "") .. chunk
	local start = 1
	while true do
		local nl = string.find(data, "\n", start, true)
		if not nl then
			ctx.carry = string.sub(data, start)
			break
		end
		local line = string.sub(data, start, nl - 1)
		if string.find(line, '"usage"', 1, true) then
			ctx.usage_line = line
		end
		start = nl + 1
	end
	-- Bound the carry (a huge single-line non-streaming body); keep the tail.
	if ctx.carry and #ctx.carry > 262144 then
		ctx.carry = string.sub(ctx.carry, -262144)
	end
end

if eof then
	local c = ctx.carry
	if c and c ~= "" and string.find(c, '"usage"', 1, true) then
		ctx.usage_line = c
	end
end
-- ngx.arg[1] left unmodified → response streams through untouched.
