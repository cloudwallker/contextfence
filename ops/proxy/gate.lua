-- Read the directory bind mount on every request: atomic replacement must be visible.
-- Accept only our exact two-key schema. Missing, truncated and unknown-key files fail closed.
core.register_fetches("proxy_gate_open", function(txn)
    local file = io.open("/runtime/proxy-gate.json", "r")
    if not file then return false end
    local text = file:read(512)
    local excess = file:read(1)
    file:close()
    if not text or excess then return false end
    -- Whitespace is allowed between tokens, never inside a quoted state value.
    return text:match('^%s*{%s*"format_version"%s*:%s*1%s*,%s*"state"%s*:%s*"OPEN"%s*}%s*$') ~= nil
end)
