-- Enhanced Visuals: the render config and shaders in res/ do the drawing (tools/build.py). This script
-- turns their parts on and off through renderer settings, live, sets the ambient occlusion's strength and
-- the screen dust through the planet's shading, and keeps the live options' rows in the pause menu's Mods
-- page. Options built into the deployed files (sharpening, ambient light, the shadow options, render
-- scale) are set in Obsidian only.
local plan = obsidian.module("src/plan")
local api = obsidian.import("obsidian.api")

local IDS = { "dust", "sharpen", "ambient", "ao", "ao_light", "shadows", "shadow_distance", "soft_shadows",
	"render_scale", "detail" }
local options = {}
for _, id in ipairs(IDS) do options[id] = obsidian.settings.get(id) end

local compare = false
local sent = nil

local function app()
	return rawget(_G, "Application")
end

local function send(all)
	local want = plan.of(options, compare)
	local change = plan.changes(not all and sent or nil, want)
	local a = app()
	local said = {}
	for name, value in pairs(change.renderer) do
		local ok, err = pcall(a.set_renderer_setting, name, value)
		said[#said + 1] = name .. "=" .. value .. (ok and "" or (" (" .. tostring(err) .. ")"))
	end
	if api and api.world and api.world.shading then
		for name, value in pairs(change.shading) do
			local ok, why
			if value then ok, why = api.world.shading(name, "scalar", value) else ok, why = api.world.shading(name, "scalar") end
			said[#said + 1] = name .. "=" .. tostring(value) .. ((ok or ok == nil) and "" or (" (" .. tostring(why) .. ")"))
		end
	end
	if #said > 0 then
		table.sort(said)
		obsidian.log("enhanced visuals: " .. table.concat(said, " "))
	end
	sent = want
end

local function set(id, value)
	options[id] = value
	send(false)
end

-- The rows of the options that apply at once, so they can change in game too.
if api and api.mod_settings and api.mod_settings.BINDS then
	local page = api.mod_settings.page("Enhanced Visuals")
	local function choices(list)
		local out = {}
		for _, c in ipairs(list) do out[#out + 1] = { id = c[1], label = c[2] } end
		return out
	end
	page:toggle("dust", { option = true, default = false, label = "Screen dust",
		info = "The layer of dust that drifts across the picture on most planets.",
		on_change = function(v) set("dust", v) end })
	page:choice("ao", { option = true, default = "strong", label = "Ambient occlusion",
		choices = choices({ { "off", "Off" }, { "subtle", "Subtle" }, { "strong", "Strong" } }),
		info = "Darkens the light where surfaces meet, such as at the foot of walls and under rocks.",
		on_change = function(v) set("ao", v) end })
	page:choice("ao_light", { option = true, default = "ambient", label = "Ambient occlusion affects",
		choices = choices({ { "ambient", "Ambient light" }, { "all", "All daylight" } }),
		info = "Ambient light shows mostly in shade. All daylight darkens sunlight too.",
		on_change = function(v) set("ao_light", v) end })
	page:choice("detail", { option = true, default = "x2", label = "Model detail distance",
		choices = choices({ { "game", "Default" }, { "x2", "2x" }, { "x4", "4x" } }),
		info = "How far from the camera models keep their most detailed version.",
		on_change = function(v) set("detail", v) end })
end

obsidian.keybind("compare", function()
	compare = not compare
	send(false)
	obsidian.toast(compare and "Enhanced Visuals Off" or "Enhanced Visuals On")
end)

send(true)

-- A new screen (the ship, a mission) sends everything again, in case the game set its own values.
local screen = nil
obsidian.on_frame(function()
	local ok, _, name = pcall(api.game.state)
	if ok and name ~= screen then
		screen = name
		send(true)
	end
end)
