-- What the options ask of the engine: renderer settings (Application.set_renderer_setting takes a name and
-- a string) and shading values (api.world.shading). Pure, so tests/run.lua can check it without the game.
local M = {}

-- The mod's vo_ssao raises the occlusion to ten times ssao_radius (the one live value that shader's
-- tables offer), and ssao_upscale raises it again to ssao_strength, held at 1. The planets carry
-- leftover values for both from an SSAO that never ran on PC, so the mod always sets its own.
M.AO_STRENGTH = { subtle = 1.0, strong = 3.0 }

M.DETAIL = { game = 1, x2 = 2, x4 = 4 }

M.DEFAULTS = { dust = false, sharpen = "normal", ambient = "subtle", ao = "strong", ao_light = "ambient",
	soft_shadows = "softer", detail = "x2" }

local function pick(t, k)
	local v = t[k]
	if v == nil then return M.DEFAULTS[k] end
	return v
end

-- options: what obsidian.settings.get answers, by id. compare: true while the game's own look is shown.
-- Answers { renderer = { name = string }, shading = { name = number or false } }; false gives the
-- value back to the planet.
function M.of(options, compare)
	local ao_level = M.AO_STRENGTH[pick(options, "ao")]
	local ao = ao_level ~= nil and not compare
	local sharpen = pick(options, "sharpen") ~= "off" and not compare
	local ambient = pick(options, "ambient") ~= "off" and not compare
	local no_dust = not pick(options, "dust") and not compare
	local detail = compare and 1 or (M.DETAIL[pick(options, "detail")] or 1)
	local out = {
		renderer = {
			ssao_enabled = tostring(ao),
			ev_ao_all = tostring(pick(options, "ao_light") == "all"),
			ev_sharpen = tostring(sharpen),
			ev_ambient = tostring(ambient),
			lod_object_multiplier = tostring(detail),
			-- No game shader reads it; the soft shadow masks run the game's filter at exactly 2.
			g_tessellation_detail = (compare and pick(options, "soft_shadows") ~= "off") and "2" or "1",
		},
		shading = {},
	}
	if ao then
		out.shading.ssao_radius = ao_level / 10
		out.shading.ssao_strength = 1.0
	else
		out.shading.ssao_radius = false
		out.shading.ssao_strength = false
	end
	out.shading.dust_opacity = no_dust and 0 or false
	return out
end

-- The entries of `want` that differ from `had` (both as M.of answers), to send only what changed.
function M.changes(had, want)
	local out = { renderer = {}, shading = {} }
	for _, part in ipairs({ "renderer", "shading" }) do
		for k, v in pairs(want[part]) do
			if not had or had[part][k] ~= v then out[part][k] = v end
		end
	end
	return out
end

return M
