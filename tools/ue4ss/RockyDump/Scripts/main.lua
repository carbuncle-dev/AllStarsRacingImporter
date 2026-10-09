-- UE4SS diagnostic mod for the Rocky Coaster port: press F7 in a race to dump runtime state.
-- Writes dump_<n>.txt: karts, then (CAMERA_PP_ONLY) camera pose, fogs and post-process volumes, or
-- the lighting / gameplay sections from earlier investigations.
-- Output folder (must exist): ROCKYDUMP_OUT_DIR, else relative to the game's Binaries/Win64 folder.

local OUT_DIR = os.getenv("ROCKYDUMP_OUT_DIR") or "ue4ss/Mods/RockyDump/out/"
local MAX_DEPTH = 4
local MAX_ARRAY = 24
local MAX_INSTANCES = 6

-- Lighting of the course being raced (reference for the port: a stock day course such as Metal
-- Harbor): every light, sky, fog and post-process setting the renderer uses, plus the camera's own
-- post-process (the game adds exposure there).
local LIGHTING = {
    "DirectionalLightComponent", "SkyLightComponent", "PointLightComponent", "SpotLightComponent",
    "RectLightComponent", "ExponentialHeightFogComponent", "SkyAtmosphereComponent",
    "VolumetricCloudComponent", "PostProcessVolume", "PostProcessComponent", "CameraComponent",
    "PlayerCameraManager", "LightmassImportanceVolume",
}
-- Classes dumped in full (short names, matched exactly).
local TARGETS = {
    "LevelStreamingDynamic", "LevelStreamingAlwaysLoaded", "LevelStreaming", "LevelInstance",
    "RaceSequenceStateLoadAssets", "UnionRingSystemManager", "UnionNavLoader", "UnionRaceInfo",
    "UnionRaceDataManager", "UnionRacer", "UnionVehicleModelInfo", "RingInstanceObject",
    "BP_TravelRingMainEnterSpawner_C", "BP_TravelDecidePanelSpawner_C", "BP_FX_TravelRing_Sequence_C",
    "SL_FX_TravelRing_Main_C", "SL_FX_TravelRing_Another_C", "AreaDataDividedCourseCollisions",
}
-- Any live class whose name contains one of these is also dumped (first few instances).
local PATTERNS = { "Domain", "CourseCol", "RaceManager" }

local LIGHTING_ONLY = true  -- the gameplay sections below are from earlier investigations
local dumpIndex = 0
local out

local function w(s) out:write(s, "\n") end

local function safe(f, ...)
    local ok, r = pcall(f, ...)
    if ok then return r end
    return nil
end

local function propName(p) return safe(function() return p:GetFName():ToString() end) or "?" end
local function propType(p) return safe(function() return p:GetClass():GetFName():ToString() end) or "?" end

local dumpValue

-- Reading some property kinds from Lua crashes natively (pcall cannot catch it): only read these.
local READABLE = {
    BoolProperty = true, ByteProperty = true, EnumProperty = true, IntProperty = true, Int8Property = true,
    Int16Property = true, Int64Property = true, UInt16Property = true, UInt32Property = true,
    UInt64Property = true, FloatProperty = true, DoubleProperty = true, NameProperty = true,
    StrProperty = true, ObjectProperty = true, ClassProperty = true, StructProperty = true,
    ArrayProperty = true,
}

local function dumpStructFields(struct, value, indent, depth)
    local s = struct
    while s ~= nil and safe(function() return s:IsValid() end) do
        safe(function()
            s:ForEachProperty(function(p)
                local name = propName(p)
                local t = propType(p)
                if not READABLE[t] then
                    w(indent .. name .. " (" .. t .. ", skipped)")
                    return
                end
                if t == "ArrayProperty" then
                    local it = safe(function() return propType(p:GetInner()) end)
                    if not READABLE[it] then
                        w(indent .. name .. " (array of " .. tostring(it) .. ", skipped)")
                        return
                    end
                end
                local v = safe(function() return value[name] end)
                dumpValue(p, name, v, indent, depth)
            end)
        end)
        s = safe(function() return s:GetSuperStruct() end)
    end
end

dumpValue = function(p, name, v, indent, depth)
    local t = propType(p)
    if v == nil then
        w(indent .. name .. " (" .. t .. ") = <unreadable>")
        return
    end
    local lt = type(v)
    if lt == "number" or lt == "boolean" or lt == "string" then
        w(indent .. name .. " = " .. tostring(v))
        return
    end
    if t == "StructProperty" then
        if depth >= MAX_DEPTH then w(indent .. name .. " = {...}"); return end
        w(indent .. name .. " {")
        local st = safe(function() return p:GetStruct() end)
        if st then dumpStructFields(st, v, indent .. "  ", depth + 1) end
        w(indent .. "}")
        return
    end
    if t == "ArrayProperty" then
        local n = safe(function() return v:GetArrayNum() end) or -1
        w(indent .. name .. " [" .. n .. "]")
        if depth >= MAX_DEPTH or n <= 0 then return end
        local inner = safe(function() return p:GetInner() end)
        for i = 1, math.min(n, MAX_ARRAY) do
            local e = safe(function() return v[i] end)
            if inner then dumpValue(inner, "[" .. i .. "]", e, indent .. "  ", depth + 1) end
        end
        return
    end
    if t == "ObjectProperty" or t == "ClassProperty" or t == "WeakObjectProperty" or t == "SoftObjectProperty" then
        local fn = safe(function() return v:GetFullName() end)
        w(indent .. name .. " -> " .. tostring(fn))
        return
    end
    if t == "NameProperty" or t == "StrProperty" then
        w(indent .. name .. " = " .. tostring(safe(function() return v:ToString() end)))
        return
    end
    if t == "EnumProperty" or t == "ByteProperty" then
        w(indent .. name .. " = " .. tostring(safe(function() return v end)))
        return
    end
    w(indent .. name .. " (" .. t .. ")")
end

local function dumpObject(obj, header)
    out:flush()
    w("")
    w("=== " .. header .. ": " .. tostring(safe(function() return obj:GetFullName() end)))
    local loc = safe(function() return obj:K2_GetActorLocation() end)
    if loc then w(string.format("  ActorLocation = (%.1f, %.1f, %.1f)", loc.X, loc.Y, loc.Z)) end
    local cls = safe(function() return obj:GetClass() end)
    if cls then dumpStructFields(cls, obj, "  ", 0) end
end

-- Every boost pad's trigger: where it is, its domain, and whether the game considers it live.
local function staticBodies(cls, ownerFilter, limit)
    local objs = safe(function() return FindAllOf(cls) end) or {}
    w("")
    w("##### " .. cls .. " triggers (" .. #objs .. " live, owner filter '" .. ownerFilter .. "')")
    local n = 0
    for _, c in ipairs(objs) do
        local owner = safe(function() return c:GetOwner() end)
        local oname = owner and safe(function() return owner:GetFullName() end) or "?"
        if oname:find(ownerFilter) and n < limit then
            n = n + 1
            local loc = owner and safe(function() return owner:K2_GetActorLocation() end)
            local tr = safe(function() return c:GetTransform() end)
            local tl = tr and safe(function() return tr.Translation end)
            w(string.format("%s | actor (%.0f, %.0f, %.0f) | body (%s) | domain %s | enabled %s | available %s | kinematic %s",
                oname:match("[^%.:]+$") or oname,
                loc and loc.X or 0, loc and loc.Y or 0, loc and loc.Z or 0,
                tl and string.format("%.0f, %.0f, %.0f", tl.X, tl.Y, tl.Z) or "?",
                tostring(safe(function() return c.DomainNumber end)),
                tostring(safe(function() return c:IsCollisionEnabled() end)),
                tostring(safe(function() return c:IsCollisionAvailable() end)),
                tostring(safe(function() return c.bIsKinematic end))))
            out:flush()
        end
    end
end

-- Full reflection dump of our own stage's actors of a class (our domain sits around (-900000, -900000)).
local function ourActors(cls, limit)
    local objs = safe(function() return FindAllOf(cls) end) or {}
    local n = 0
    w("")
    w("##### " .. cls .. " in our domain")
    for _, a in ipairs(objs) do
        local loc = safe(function() return a:K2_GetActorLocation() end)
        if loc and loc.X < -500000 and loc.Y < -500000 and n < limit then
            n = n + 1
            safe(dumpObject, a, cls)
        end
    end
    w("(" .. n .. " dumped)")
end

local function census()
    local counts = {}
    ForEachUObject(function(obj)
        local cn = safe(function() return obj:GetClass():GetFullName() end)
        if cn and (cn:find("/Script/Union") or cn:find("/Script/UNION") or cn:find("/Game/")) then
            counts[cn] = (counts[cn] or 0) + 1
        end
    end)
    local names = {}
    for k in pairs(counts) do names[#names + 1] = k end
    table.sort(names)
    w("##### CENSUS (" .. #names .. " game classes with live objects)")
    for _, k in ipairs(names) do w(string.format("%6d  %s", counts[k], k)) end
    return names
end

-- Camera post-process: only the settings a camera actually overrides (bOverride_X = true), read field by
-- field -- reading whole camera components crashed UE4SS. Press F7 during the stage intro and in race.
local CAMERA_PP_ONLY = true
local CAMERA_CLASSES_TO_READ = {}

local function findProperty(cls, wanted)
    local s, found = cls, nil
    while s ~= nil and not found and safe(function() return s:IsValid() end) do
        safe(function()
            s:ForEachProperty(function(p) if not found and propName(p) == wanted then found = p end end)
        end)
        s = safe(function() return s:GetSuperStruct() end)
    end
    return found
end

local function dumpOverrides(obj, indent, propname)
    propname = propname or "PostProcessSettings"
    local cls = safe(function() return obj:GetClass() end)
    local pp = cls and findProperty(cls, propname)
    if not pp then w(indent .. "(no " .. propname .. ")"); return end
    local st = safe(function() return pp:GetStruct() end)
    local v = safe(function() return obj[propname] end)
    if not st or not v then w(indent .. "(unreadable PostProcessSettings)"); return end
    local on = {}
    safe(function()
        st:ForEachProperty(function(p)
            local n = propName(p)
            if n:sub(1, 10) == "bOverride_" and safe(function() return v[n] end) == true then on[#on + 1] = n:sub(11) end
        end)
    end)
    w(indent .. #on .. " overrides")
    for _, n in ipairs(on) do
        local p = findProperty(st, n)
        if p then
            local t = propType(p)
            if t == "ArrayProperty" or t == "ObjectProperty" then
                w(indent .. n .. " (" .. t .. ", see below)")
            else
                safe(dumpValue, p, n, safe(function() return v[n] end), indent, 2)
            end
        end
        out:flush()
    end
end

local camLoc = nil

local function dumpCameras()
    -- reading camera components' settings crashed UE4SS (some cine cameras); race cameras override
    -- nothing (dump_112001_1.txt), so only the active view's pose is recorded.
    for _, cls in ipairs(CAMERA_CLASSES_TO_READ) do
        local objs = safe(function() return FindAllOf(cls) end) or {}
        w("")
        w("##### " .. cls .. ": " .. #objs .. " live")
        for i = 1, math.min(#objs, 40) do
            local c = objs[i]
            w("")
            w("=== " .. tostring(safe(function() return c:GetFullName() end)))
            w("  active " .. tostring(safe(function() return c:IsActive() end)) .. ", blend weight " ..
              tostring(safe(function() return c.PostProcessBlendWeight end)))
            safe(dumpOverrides, c, "  ")
            out:flush()
        end
    end
    local pcm = safe(function() return FindFirstOf("PlayerCameraManager") end)
    out:flush()
    if pcm then
        local vt = safe(function() return pcm.ViewTarget.Target end)
        w("")
        w("##### view target: " .. tostring(vt and safe(function() return vt:GetFullName() end)))
        local l = safe(function() return pcm:GetCameraLocation() end)
        camLoc = l
        local r = safe(function() return pcm:GetCameraRotation() end)
        local f = safe(function() return pcm:GetFOVAngle() end)
        if l and r then
            w(string.format("##### camera: location (%.2f, %.2f, %.2f) rotation pitch %.3f yaw %.3f roll %.3f fov %s",
                l.X, l.Y, l.Z, r.Pitch, r.Yaw, r.Roll, tostring(f)))
        end
    end
    -- fog and post-process volumes of every loaded stage (only simple fields / enabled overrides)
    local fogs = safe(function() return FindAllOf("ExponentialHeightFogComponent") end) or {}
    w("")
    w("##### ExponentialHeightFogComponent: " .. #fogs .. " live")
    for i = 1, math.min(#fogs, 12) do
        local c = fogs[i]
        w("=== " .. tostring(safe(function() return c:GetFullName() end)))
        for _, k in ipairs({ "bVisible", "bHiddenInGame", "FogDensity", "FogHeightFalloff", "FogMaxOpacity",
                             "StartDistance", "FogCutoffDistance", "DirectionalInscatteringExponent" }) do
            w("  " .. k .. " = " .. tostring(safe(function() return c[k] end)))
        end
        local col = safe(function() return c.FogInscatteringLuminance end)
        if col then
            w(string.format("  FogInscatteringLuminance = (%s, %s, %s)", tostring(safe(function() return col.R end)),
                tostring(safe(function() return col.G end)), tostring(safe(function() return col.B end))))
        end
        out:flush()
    end
    local ppvs = safe(function() return FindAllOf("PostProcessVolume") end) or {}
    w("")
    w("##### PostProcessVolume: " .. #ppvs .. " live")
    for i = 1, math.min(#ppvs, 20) do
        local v = ppvs[i]
        w("=== " .. tostring(safe(function() return v:GetFullName() end)))
        local loc = safe(function() return v:K2_GetActorLocation() end)
        if loc then w(string.format("  location (%.1f, %.1f, %.1f)", loc.X, loc.Y, loc.Z)) end
        for _, k in ipairs({ "bEnabled", "bUnbound", "Priority", "BlendRadius", "BlendWeight" }) do
            w("  " .. k .. " = " .. tostring(safe(function() return v[k] end)))
        end
        -- does the game consider the camera inside this volume? (AVolume::EncompassesPoint)
        if camLoc then
            local res = safe(function() return v:EncompassesPoint(camLoc, 0.0, {}) end)
            w("  EncompassesPoint(camera) = " .. tostring(res))
        end
        local bc = safe(function() return v.BrushComponent end)
        local bs = bc and safe(function() return bc.BrushBodySetup end)
        w("  BrushBodySetup -> " .. tostring(bs and safe(function() return bs:GetFullName() end)))
        out:flush()
        local n = bs and safe(function() return bs.AggGeom.ConvexElems:GetArrayNum() end)
        w("  convex elems = " .. tostring(n))
        out:flush()
        safe(dumpOverrides, v, "  ", "Settings")
        out:flush()
    end
end

local function dumpAll()
    dumpIndex = dumpIndex + 1
    out = io.open(OUT_DIR .. "dump_" .. os.date("%H%M%S") .. "_" .. dumpIndex .. ".txt", "w")
    if not out then print("[RockyDump] cannot open output file in " .. OUT_DIR); return end
    local names = {}  -- census crashed once mid-race; it is not needed any more
    -- Every kart's position (press during the countdown to capture the starting grid).
    local karts = safe(function() return FindAllOf("BP_VehicleInRacePawn_C") end) or {}
    w("##### KARTS (" .. #karts .. ")")
    for _, k in ipairs(karts) do
        local l = safe(function() return k:K2_GetActorLocation() end)
        local r = safe(function() return k:K2_GetActorRotation() end)
        if l then
            w(string.format("%s | (%.1f, %.1f, %.1f) | yaw %.1f", tostring(safe(function() return k:GetFName():ToString() end)),
                l.X, l.Y, l.Z, r and r.Yaw or 0))
        end
    end
    out:flush()
    if CAMERA_PP_ONLY then
        safe(dumpCameras)
        out:close()
        print("[RockyDump] wrote camera dump " .. dumpIndex)
        return
    end
    w("")
    w("##### LIGHTING")
    for _, cls in ipairs(LIGHTING) do
        local objs = safe(function() return FindAllOf(cls) end)
        w("")
        w("##### " .. cls .. ": " .. (objs and #objs or 0) .. " live")
        if objs then
            for i = 1, math.min(#objs, 12) do
                safe(dumpObject, objs[i], cls)
                out:flush()
            end
        end
    end
    if LIGHTING_ONLY then
        out:close()
        print("[RockyDump] wrote lighting dump " .. dumpIndex)
        return
    end
    local done = {}
    w("")
    w("##### TARGETS")
    for _, cls in ipairs(TARGETS) do
        local objs = safe(function() return FindAllOf(cls) end)
        w("")
        w("##### " .. cls .. ": " .. (objs and #objs or 0) .. " live")
        if objs then
            for i = 1, math.min(#objs, MAX_INSTANCES) do safe(dumpObject, objs[i], cls) end
        end
        done[cls] = true
    end
    safe(ourActors, "BP_DashPanel_C", 30)
    safe(ourActors, "BP_Ring_C", 6)
    safe(staticBodies, "GimmickStaticBodyComponent", "DashPanel", 40)
    safe(staticBodies, "GimmickStaticBodyComponent", "", 12)
    safe(staticBodies, "StaticBodyComponent", "DashPanel", 100)
    safe(staticBodies, "ItemStaticBodyComponent", "", 12)
    safe(staticBodies, "ItemStaticBodyComponent", "Ring", 200)
    safe(staticBodies, "StaticBodyComponent", "Ring", 1000)
    w("")
    w("##### PATTERN MATCHES")
    for _, full in ipairs(names) do
        local short = full:match("[%.]([%w_]+)$") or full
        for _, pat in ipairs(PATTERNS) do
            if short:find(pat) and not done[short] then
                done[short] = true
                local objs = safe(function() return FindAllOf(short) end)
                w("")
                w("##### " .. short .. ": " .. (objs and #objs or 0) .. " live")
                if objs then
                    for i = 1, math.min(#objs, 3) do safe(dumpObject, objs[i], short) end
                end
                break
            end
        end
    end
    out:close()
    print("[RockyDump] wrote dump " .. dumpIndex)
end

RegisterKeyBind(Key.F7, function()
    ExecuteInGameThread(function()
        local ok, err = pcall(dumpAll)
        if not ok then print("[RockyDump] failed: " .. tostring(err)) end
    end)
end)
print("[RockyDump] loaded: press F7 during a race")

-- Path recorder: logs the player's kart (world position + velocity) every 100 ms while a pawn
-- exists, to out/runtime/path_<time>.csv, so the exact spot and manner of falling off can be seen.
local rec = nil
local recTicks = 0
local STATUS_FIELDS = {
    "PresentDomainNumber", "PresentNextDomainNumber", "bPresentValidTravelRing", "bPresentNoticeRingOn",
    "bPresentSelectStartOn", "bPresentSelectingA", "bPresentSelectingB", "bPresentAnotherConfirmEndOn",
    "bPresentJustBeforeOn", "bPresentIsSameDomainNumber", "InRingDomainNumber", "bInRingValidTravelRing",
    "bInRingJustBeforeOn",
}
local RACER_FIELDS = {
    "CurrentLapCount", "ElapsedLapCount", "CurrentElapsedRatio", "ElapsedRatio", "RunningDistance",
    "bInReverseRun", "bInCourseOut", "bInFootCutting", "bInGoal", "CurrentDomainNumber", "BackRunDistance",
    "ReverseRunSeconds", "RingCount",
}
local playerStatus, playerRacer

local function findPlayer()
    if not (playerStatus and safe(function() return playerStatus:IsValid() end)) then
        playerStatus = nil
        for _, ps in ipairs(safe(function() return FindAllOf("UnionRacePlayerStatus") end) or {}) do
            if safe(function() return ps.PlayerControllerIndex end) == 0 and safe(function() return ps.bIndexInited end) then
                playerStatus = ps
                break
            end
        end
    end
    if not (playerRacer and safe(function() return playerRacer:IsValid() end)) then
        playerRacer = nil
        for _, r in ipairs(safe(function() return FindAllOf("UnionRacer") end) or {}) do
            local st = safe(function() return r.RacerStatus end)
            if st and safe(function() return st.PlayerControllerIndex end) == 0 then
                playerRacer = r
                break
            end
        end
    end
end

local function statusColumns()
    findPlayer()
    local cols = {}
    for _, f in ipairs(STATUS_FIELDS) do
        local v = playerStatus and safe(function() return playerStatus[f] end)
        if type(v) == "boolean" then v = v and 1 or 0 end
        cols[#cols + 1] = tostring(v)
    end
    local lap = playerRacer and safe(function() return playerRacer.RaceLapInfo.m_LapCount end)
    cols[#cols + 1] = tostring(lap)
    local rs = playerRacer and safe(function() return playerRacer.RacerStatus end)
    for _, f in ipairs(RACER_FIELDS) do
        local v = rs and safe(function() return rs[f] end)
        if type(v) == "boolean" then v = v and 1 or 0 end
        if type(v) == "number" and math.floor(v) ~= v then v = string.format("%.4f", v) end
        cols[#cols + 1] = tostring(v)
    end
    return table.concat(cols, ",")
end
LoopAsync(100, function()
    ExecuteInGameThread(function()
        local pc = safe(function() return FindFirstOf("PlayerController") end)
        local pawn = pc and safe(function() return pc.Pawn end)
        if not pawn or not safe(function() return pawn:IsValid() end) then return end
        local loc = safe(function() return pawn:K2_GetActorLocation() end)
        if not loc then return end
        local vel = safe(function() return pawn:GetVelocity() end)
        if not rec then
            rec = io.open(OUT_DIR .. "path_" .. os.date("%H%M%S") .. ".csv", "w")
            if not rec then return end
            rec:write("t,x,y,z,vx,vy,vz," .. table.concat(STATUS_FIELDS, ",") .. ",lap,"
                .. table.concat(RACER_FIELDS, ",") .. "\n")
        end
        recTicks = recTicks + 1
        rec:write(string.format("%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,", recTicks * 0.1,
            loc.X, loc.Y, loc.Z, vel and vel.X or 0, vel and vel.Y or 0, vel and vel.Z or 0)
            .. (safe(statusColumns) or "") .. "\n")
        rec:flush()
    end)
    return false
end)
