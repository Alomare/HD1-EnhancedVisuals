-- Offline tests for Enhanced Visuals' Lua. From the repository's root:
--   luajit tests/run.lua
local passed, failed = 0, 0

local function eq(got, want, what)
	if got ~= want then error((what or "value") .. ": expected " .. tostring(want) .. ", got " .. tostring(got), 2) end
end

local function test(name, fn)
	local ok, err = pcall(fn)
	if ok then passed = passed + 1 else failed = failed + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end

-- A game, a loader and an API that record what main.lua asks of them.
local function boot(opts)
	local world = { renderer = {}, shading = {}, rows = {}, toasts = {}, logs = {} }
	local keys, frames, state = {}, {}, "main_menu"
	local loaded = {}
	obsidian = {
		module = function(name)
			if loaded[name] == nil then loaded[name] = dofile(name .. ".lua") end
			return loaded[name]
		end,
		settings = { get = function(id) return opts[id] end },
		keybind = function(id, fn) keys[id] = fn end,
		on_frame = function(fn) frames[#frames + 1] = fn end,
		toast = function(s) world.toasts[#world.toasts + 1] = s end,
		log = function(s) world.logs[#world.logs + 1] = s end,
	}
	local page = {}
	function page:toggle(id, o) world.rows[id] = o end
	function page:choice(id, o) world.rows[id] = o end
	local api = {
		world = {
			shading = function(name, kind, value)
				assert(kind == "scalar", "kind")
				world.shading[name] = value == nil and "planet" or value
				return true
			end,
		},
		game = {
			state = function() return {}, state end,
		},
		mission = {
			params = function() return world.mission end,
		},
		mod_settings = { BINDS = true, page = function() return page end },
	}
	obsidian.import = function(id) assert(id == "obsidian.api"); return api end
	Application = { set_renderer_setting = function(name, value)
		assert(type(value) == "string", "renderer settings take strings")
		world.renderer[name] = value
	end }
	dofile("main.lua")
	world.press = function(id) keys[id]() end
	world.frame = function(s) state = s or state; for _, f in ipairs(frames) do f() end end
	world.frames = function(n) for _ = 1, n do world.frame() end end
	return world
end

test("the defaults turn the live parts on", function()
	local w = boot({})
	eq(w.renderer.ssao_enabled, "true", "ssao")
	eq(w.renderer.ev_sharpen, "true", "sharpen")
	eq(w.renderer.ev_ambient, "true", "ambient")
	eq(w.renderer.lod_object_multiplier, "2", "detail")
	eq(w.renderer.ev_ao_all, "false", "ambient light only")
	eq(w.shading.ssao_radius, 0.3, "strong rides in the radius")
	eq(w.shading.ssao_strength, 1.0, "upscale strength")
	eq(w.shading.dust_opacity, 0, "no dust")
	eq(w.renderer.particles_cast_shadows, nil, "smoke shadows left to the game")
end)

test("options off leave the game's look and give the shading back", function()
	local w = boot({ ao = "off", sharpen = "off", ambient = "off", detail = "game" })
	eq(w.renderer.ssao_enabled, "false")
	eq(w.renderer.ev_sharpen, "false")
	eq(w.renderer.ev_ambient, "false")
	eq(w.renderer.lod_object_multiplier, "1")
	eq(w.shading.ssao_strength, "planet")
	eq(w.shading.ssao_radius, "planet")
end)

test("screen dust on gives the planet's dust back; all-daylight AO", function()
	local w = boot({ dust = true, ao_light = "all" })
	eq(w.shading.dust_opacity, "planet")
	eq(w.renderer.ev_ao_all, "true")
end)

test("the compare key switches everything off and back", function()
	local w = boot({ detail = "x4", ao = "subtle" })
	eq(w.shading.ssao_radius, 0.1)
	w.press("compare")
	eq(w.renderer.ssao_enabled, "false")
	eq(w.renderer.ev_sharpen, "false")
	eq(w.renderer.ev_ambient, "false")
	eq(w.renderer.lod_object_multiplier, "1")
	eq(w.shading.ssao_radius, "planet")
	eq(w.shading.dust_opacity, "planet", "the game's dust back")
	eq(w.toasts[1], "Enhanced Visuals Off")
	w.press("compare")
	eq(w.renderer.ssao_enabled, "true")
	eq(w.renderer.ev_ambient, "true")
	eq(w.renderer.lod_object_multiplier, "4")
	eq(w.shading.ssao_radius, 0.1)
	eq(w.shading.dust_opacity, 0)
	eq(w.toasts[2], "Enhanced Visuals On")
end)

test("the compare key asks the soft shadow masks for the game's filter, unless they're off", function()
	local w = boot({})
	eq(w.renderer.g_tessellation_detail, "1", "the game's own value")
	w.press("compare")
	eq(w.renderer.g_tessellation_detail, "2", "compare value")
	w.press("compare")
	eq(w.renderer.g_tessellation_detail, "1")
	local off = boot({ soft_shadows = "off" })
	off.press("compare")
	eq(off.renderer.g_tessellation_detail, "1", "nothing to compare with the game's own masks")
end)

test("a row's change applies at once", function()
	local w = boot({})
	w.rows.ao.on_change("subtle")
	eq(w.shading.ssao_radius, 0.1)
	w.rows.dust.on_change(true)
	eq(w.shading.dust_opacity, "planet")
	w.rows.ao_light.on_change("all")
	eq(w.renderer.ev_ao_all, "true")
	w.rows.detail.on_change("x4")
	eq(w.renderer.lod_object_multiplier, "4")
	w.rows.ao.on_change("off")
	eq(w.renderer.ssao_enabled, "false")
end)

-- Options built into the deployed files have no row in game; the rest have one, with the manifest's
-- choices and default.
local LIVE = { dust = true, desert_ambient = true, ao = true, ao_light = true, detail = true }

test("ambient light goes off in daytime desert missions unless Desert ambient light is on", function()
	local w = boot({})
	eq(w.renderer.ev_ambient, "true", "on the ship")
	w.mission = { environment = "desert", time_of_day = "day" }
	w.frame("game")
	eq(w.renderer.ev_ambient, "false", "desert, day")
	w.rows.desert_ambient.on_change(true)
	eq(w.renderer.ev_ambient, "true", "option on")
	w.rows.desert_ambient.on_change(false)
	eq(w.renderer.ev_ambient, "false", "option off again")
	w.mission = nil
	w.frame("menu")
	eq(w.renderer.ev_ambient, "true", "back on the ship")
	for _, other in ipairs({ { "desert", "dusk" }, { "forest", "day" }, { "desert", "night" } }) do
		local o = boot({})
		o.mission = { environment = other[1], time_of_day = other[2] }
		o.frame("game")
		eq(o.renderer.ev_ambient, "true", other[1] .. " " .. other[2])
	end
end)

test("a mission read after the screen changes still turns desert ambient light off", function()
	local w = boot({})
	w.frame("game")
	eq(w.renderer.ev_ambient, "true", "mission not built yet")
	w.mission = { environment = "desert", time_of_day = "day" }
	w.frames(60)
	eq(w.renderer.ev_ambient, "false", "read a second later")
	local off = boot({ ambient = "off" })
	off.mission = { environment = "desert", time_of_day = "day" }
	off.frame("game")
	eq(off.renderer.ev_ambient, "false", "ambient off stays off")
end)

test("live options have rows matching the manifest, the others none", function()
	local f = assert(io.open("obsidian.json", "r"))
	local text = f:read("*a")
	f:close()
	local w = boot({})
	local seen = 0
	for id in text:gmatch('{"id": "([%w_]+)", "type": "[a-z]+"') do
		local row = w.rows[id]
		if not LIVE[id] then
			assert(not row, "row for deploy-time option " .. id)
		else
			seen = seen + 1
			assert(row, "no row for option " .. id)
			eq(row.option, true, id .. " bound")
			local default = text:match('{"id": "' .. id .. '", "type": "[a-z]+", "default": ([%w"]+)')
			eq(tostring(row.default), (default:gsub('"', "")), id .. " default")
			local line = text:match('{"id": "' .. id .. '", "type": "choice".-"choices": (%b[])')
			if line then
				for c in line:gmatch('"id": "([%w_]+)"') do
					local found = false
					for _, rc in ipairs(row.choices) do found = found or rc.id == c end
					assert(found, id .. ": row lacks choice " .. c)
				end
				eq(#row.choices, select(2, line:gsub('"id":', "")), id .. " choice count")
			end
		end
	end
	eq(seen, 5, "live rows")
end)

test("the plan's defaults match the manifest's", function()
	local f = assert(io.open("obsidian.json", "r"))
	local text = f:read("*a")
	f:close()
	local plan = dofile("src/plan.lua")
	for id, want in pairs(plan.DEFAULTS) do
		local default = assert(text:match('{"id": "' .. id .. '", "type": "[a-z]+", "default": ([%w"]+)'), id)
		eq(tostring(want), (default:gsub('"', "")), id .. " default")
	end
end)

test("a new screen sends the settings again", function()
	local w = boot({})
	w.renderer.ssao_enabled = nil
	w.frame("menu")
	eq(w.renderer.ssao_enabled, "true")
	w.renderer.ssao_enabled = nil
	w.frame("menu")
	eq(w.renderer.ssao_enabled, nil, "same screen: nothing sent")
end)

test("public text keeps the writing rules", function()
	for _, path in ipairs({ "obsidian.json", "main.lua", "README.md" }) do
		local f = io.open(path, "r")
		if f then
			local text = f:read("*a")
			f:close()
			-- Words players read: the manifest's and the rows' labels and infos, and the README.
			local shown = path == "main.lua" and table.concat({ text:match("(local function choices.-)\nobsidian.keybind") or "" }) or text
			for _, bad in ipairs({ "%f[%a][Ii]ts%f[%A]", "%f[%a]I%f[%A]", "%f[%a]my%f[%A]", "%f[%a]we%f[%A]" }) do
				for line in shown:gmatch("[^\n]+") do
					local strings = path == "README.md" and line or table.concat({ line:match('"(.-)"') or "" })
					assert(not strings:find(bad), path .. ": " .. strings)
				end
			end
		end
	end
end)

print(("%d passed, %d failed"):format(passed, failed))
os.exit(failed == 0 and 0 or 1)
