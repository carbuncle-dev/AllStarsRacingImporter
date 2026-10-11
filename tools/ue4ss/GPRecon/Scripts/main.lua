-- GPRecon: read-only reconnaissance for a custom Grand Prix mod.
-- Logs calls to the Grand Prix / course-select / save functions listed in ../hooks.txt (generated from the
-- UNION headers) while the player walks through the menus: which ones run through Blueprint calls (and so
-- can be hooked from UE4SS) and in which order. Changes nothing: no return values or parameters are touched.
-- Output: out/gp_<time>.log (calls, first MAX_LINES per function, with plain-value arguments) and
-- out/gp_<time>_counts.txt (call counts, rewritten every 10 s). F9 writes a marker line.

local DIR = os.getenv("GPRECON_DIR") or "ue4ss/Mods/GPRecon/"
local MAX_LINES = 40

local stamp = os.date("%H%M%S")
local log = io.open(DIR .. "out/gp_" .. stamp .. ".log", "w")
if not log then
    print("[GPRecon] cannot open " .. DIR .. "out/ (create the folder)")
    return
end
local function w(s)
    log:write(os.date("%H:%M:%S ") .. s .. "\n")
    log:flush()
end

-- Only plain values are read (numbers, booleans, strings): reading object or struct pointers from
-- UE4SS has crashed this game before.
local function fmt(param)
    local ok, v = pcall(function() return param:get() end)
    if not ok then return "?" end
    local t = type(v)
    if t == "number" or t == "boolean" or t == "string" then return tostring(v) end
    return t
end

local counts, order = {}, {}
local function writeCounts()
    local f = io.open(DIR .. "out/gp_" .. stamp .. "_counts.txt", "w")
    if not f then return end
    for _, path in ipairs(order) do
        f:write(string.format("%6d  %s\n", counts[path], path))
    end
    f:close()
end

local hooked, failed = 0, 0
for path in io.lines(DIR .. "hooks.txt") do
    if path ~= "" then
        local name = path:match(":(.+)$")
        local ok, err = pcall(RegisterHook, path, function(ctx, ...)
            if not counts[path] then
                counts[path] = 0
                order[#order + 1] = path
            end
            counts[path] = counts[path] + 1
            if counts[path] <= MAX_LINES then
                local args = {}
                for i, p in ipairs({ ... }) do args[i] = fmt(p) end
                w(string.format("%s(%s)%s", path:match("%.([%w_]+:[%w_]+)$") or name, table.concat(args, ", "),
                    counts[path] == MAX_LINES and "   [further calls only counted]" or ""))
            end
        end)
        if ok then
            hooked = hooked + 1
        else
            failed = failed + 1
            w("not hooked: " .. path .. " (" .. tostring(err) .. ")")
        end
    end
end
w(string.format("GPRecon ready: %d functions hooked, %d not hookable", hooked, failed))
print(string.format("[GPRecon] %d hooked, %d failed; F9 = marker", hooked, failed))

local marks = 0
RegisterKeyBind(Key.F9, function()
    marks = marks + 1
    w("========== MARKER " .. marks .. " ==========")
    writeCounts()
end)

LoopAsync(10000, function()
    pcall(writeCounts)
    return false
end)
