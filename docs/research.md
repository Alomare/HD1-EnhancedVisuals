# Improving HELLDIVERS (2015) graphics: what modding can reach

Research from 2026-11-04/05, from the game files on disk (build in `Steam/steamapps/common/Helldivers`), the
decompiled game Lua in ObsidianModManager's `internal/research/lifted`, and the Obsidian API, then four
scripted probe runs in the game (2026-11-05, `internal/testing`, paused frames compared pixel by pixel).
Where a claim rests on an offline test only, it says so.

## How the game draws a frame

The engine is Bitsquid. Everything about the pipeline lives in one data resource, the render config
`core/rendering/renderer` (bundle `1a9c33dcd0301dfd`, 28 KB), plus the compiled shaders beside it.
`tools/datatree.py` reads that format and writes it back byte for byte (all six render configs and the
three `config` resources round-trip exactly), so the pipeline can be edited as a tree.

On PC, the default layer config runs, in order:

1. clear (mask, self_illumination, light_accumulation; albedo and normal are left alone, which is why a
   camera that sees past the level shows smears: Photo Mode's sky fix)
2. G-buffer: albedo, normal, mask (RGBA8 each), depth; then decals
3. `linearize_depth` into an R32F `depth` target (metres)
4. deferred shading: the sun's shadow map (one cascade per entry of `shadow_slice_depth_ranges`), the
   shadow mask into `mask.x`, `global_lighting` (sun times `mask.x`, ambient times `mask.a`), then point
   and spot lights
5. sky, reflections, fog volumes, fog, water, semi-transparent and HDR-transparent passes
6. tone mapping (fixed exposure from the planet's shading) into LDR
7. post processing: a four-level bloom pyramid, depth of field, colour grading
8. FXAA (already the PC-quality variant, 187 instructions), then world-space transparents and HUD

Graphics menu: brightness, Shadow quality (Low 512, Medium 1024, High 2048: `menu_screen_graphics.lua`),
FXAA, depth of field. Everything else is fixed.

## Levers, by what they can change

### Built into Enhanced Visuals

**Ambient occlusion.** The game ships a complete SSAO chain and never runs it: `vo_ssao` (volumetric
obscurance, 6 samples, half size), `ssao_blur:SEPARABLE_GAUSSIAN_13TAP_X/_Y` (a depth-aware blur that
reads depth packed into the AO target), `ssao_upscale` (blends `ao ^ ssao_strength` into `mask.a`, alpha
only). The render config still allocates `vo_rotation` (a 4x4 RG16F rotation texture) and the half-size
targets it needs, declares the render setting `ssao_enabled` (false), and every planet carries
`ssao_radius` and `ssao_strength`. No layer or generator references any of it.

Wiring the stock shaders alone would draw nothing: `vo_ssao` divides by a `max_distance` that no material
or shading environment sets, so it is 0, and every occluding sample fades out. The preview harness
(below) runs the game's own `vo_ssao` bytecode and confirms an AO of exactly 1.000 everywhere.

The mod puts the chain into the deferred shading generator after the shadow mask and before global
lighting, behind a dynamic branch on `ssao_enabled`, and replaces `vo_ssao`'s pixel shader with
`shaders/ssao.hlsl`: 12 samples on a spiral turned per pixel by `vo_rotation`, a normal rebuilt from
depth, a fall-off at a 1.25 m radius. It keeps `vo_ssao`'s bindings and output format, so the stock blur
and upscale work unchanged.

Two ways to apply it: on ambient light only, `ssao_upscale` blends it into `mask.a` as the game meant;
on all daylight (the default), a pass right after global lighting copies `light_accumulation` to the
spare `light_accumulation1` and multiplies it back by the occlusion (`shaders/ao_apply.hlsl`, in the
unused `luminance_adaption` slot), before point and spot lights add theirs. On HD1's planets the sun is
about five times the ambient light, so ambient-only occlusion barely shows outside shade.

What the probes found:
- A plain `"shader"` pass gets **no** shading-environment values: its constants stay 0. With `vo_ssao`
  run that way, `ssao_radius` read 0 and `ssao_upscale` raised the occlusion to the power 0, so the
  ambient path changed nothing at all. As a `"material"` pass, through the planet's shading material
  of the same name (the environment material lists `vo_ssao` and `ssao_upscale`), the values arrive.
- The planets' `ssao_radius` (0.1 to 0.92) and `ssao_strength` (0.5 to 5) are leftovers tuned to nothing
  visible, so the mod sets its own. `vo_ssao`'s only live value is `ssao_radius`, so it carries the
  strength (the occlusion is raised to ten times it); `ssao_strength` stays 1.
- On a city map, Normal changes 2.7% of the picture by more than 8 levels (out of 255), Strong 4.3%:
  along hedges, curbs, building edges, under the Helldiver and debris; open ground stays untouched.

**Screen dust.** The drifting dust over the picture is `tone_mapping_dust`, a branch of tone mapping
taken while the planet's `dust_opacity` isn't 0. The mod sets it to 0 through `api.world.shading`.

**Sharpening.** A new layer after FXAA, set up the way FXAA is (`output_rt` in and out), behind the
render setting `ev_sharpen`. The shader is contrast-adaptive sharpening (`shaders/sharpen.hlsl`), put in
the place of `filter:gaussian_blur_13tap`, which the render config never calls and no material or level
references (`tools/shader_users.py` searched every bundle). Three strengths, one shader each.

**Shadow resolution.** On PC the sun's shadow map takes its size from the `shadow_map_size` setting
(`size_from_render_setting`), and the menu stops at 2048. One cascade covers roughly 30 x 25 m of
ground, so 2048 is about one texel per pixel at 1080p and blurry above. The mod fixes the map at 4096 or
8192 and raises the point lights' shadow cube from 1024 to 2048.

**Renderer settings the engine reads but no menu sets:** `lod_object_multiplier` (how far models keep
their most detailed LOD; the API's `world.detail` already drives it) and `particles_cast_shadows`
(declared false). Set live with `Application.set_renderer_setting`, which the probes showed flips the
render config's dynamic branches at once (sharpening, AO and the AO mode all switched in paused frames).

**Sky clear.** The clear pass also clears albedo and normal, as Photo Mode's sky fix does, so the two
mods' render configs agree. Obsidian lets the last mod in the load order win a resource;
`load_after: alomare.photomode` keeps this one on top.

### Added later

**Ambient light.** In the manner of ReShade's AmbientLight.fx (a bright-weighted, very wide glow screen-blended
in, and darker dark tones). A bright-weighted copy of the picture at a quarter size (each pixel times the
square of the brightest channel, saturated; `shaders/ambient_extract.hlsl` in the unused
`filter:log_luminance`) is blurred at a sixteenth and a sixty-fourth by the game's own downsample and
Gaussian filters, in the bloom pyramid's targets once the pyramid has summed its levels, then added to the
bloom. The combine replaces `blend` (`shaders/ambient_combine.hlsl`): it keeps the game's exact add wherever
the first input is the smaller, which covers the pyramid's sums and the picture's pass with the inputs
turned round (what the render config runs while the option is off), and screen-blends with the dark-tone
curve `base * (1 - k (1 - luma)^2)` only on the picture's pass with the picture first.

**Flashlight and beacon shadows (removed before 1.0.0).** `Light.set_casts_shadows` on every light of the Helldivers' parts and of the
two newest stratagem beacons (`src/lights.lua`). The shadowed light shaders ship for PC; spot shadows
render into the sun's map at the Shadow resolution size, point shadows into the cube.

**Shadow edges.** The six pixel shaders of `global_shadow_mask` and `global_shadow_mask:cloud_shadows`
(plain, 3D Vision and smoke permutations) rebuilt from `shaders/shadow_mask.hlsl` with the game's own
math and a wider filter sized in metres on the ground. Under F9 the render setting
`g_tessellation_detail` (in `global_viewport`, read by no game shader) is 2, and the masks run the game's
2x2 filter.

**Shadow distance.** The far end of `shadow_slice_depth_ranges` rewritten in place in the shading
environments (17.9 to 21 m becomes 35 or 60 m), no engine call.

**Render scale.** `albedo` at 1.5 or 2 times the back buffer takes every scene target along; the passes
that draw into the back buffer get their own depth buffer at its size; the last picture goes through an
area-filter downsample (`shaders/downsample.hlsl`, in the editor-only `copy:encode_as_rgbm`) before color
grading. `transparent_in_world` (glass, such as the ship's shield) is hidden by the scene's depth, which the
back buffer's own depth buffer doesn't hold, so with a render scale it draws into `ev_world`: the finished
picture brought back up to the scene's size (nearest texel, the same shader), with the scene's depth
buffer, then down again. 33 pixel shaders divide by `back_buffer_size`, which stays the back buffer's in this layout
whichever way the engine fills it; `tools/bbsize.py` patches the 26 that bind a scene texture to divide by
that texture's size instead (one `resinfo`, the operands repointed, the container re-signed with the DXBC
checksum, which the tool reproduces for every game shader). Open: whether the engine sizes each layer's
viewport by the target it draws into.

### Leads not built yet

**Shadow distance.** `shadow_slice_depth_ranges` is `[0, 21]` on the planets checked: one cascade
ending 21 m from the camera. The mission camera sits 14 to 17 m back at 65 degrees with a 60 degree
vertical field of view (`camera_manager.lua`), so at full zoom-out the top edge of the screen is about
23 m deep: a thin strip at the top loses sun shadows. It matters far more for free cameras (Photo Mode,
third-person mods). The engine has `ShadingEnvironment.set_array_vector2`; the game's
`ViewportProxy.render` only passes scalars and vectors, so a hook on `ShadingEnvironment.apply` would be
needed, and the call's arguments aren't proven (a probe first: an engine call with wrong arguments has
crashed past `pcall` before). Patching the 220 shading environments is the other route.

**Auto exposure.** Also shipped and unused: `filter:log_luminance`, `luminance_adaption` (with
`adaption_settings`, `reset_adaption`) and `tone_mapping:luminance_adaption`. Needs small luminance
targets added to the global resources. It changes the art direction (every planet was lit for a fixed
exposure), so it fits an option rather than a default.

**Any other pixel shader.** The method used here works for any shader: an entry
(`tools/shaderlib.py`) is a header (constant buffers, variables by name and offset, textures, render
state, samplers) then size-prefixed DXBC stages, each followed by a binding table, with no offsets
pointing past the header (all 985 core entries check out). A new pixel shader that binds the same slots
and keeps variables at the same offsets drops in. Candidates: a filmic tone curve (`tone_mapping`),
softer and cleaner shadow edges (the `global_shadow_mask` materials' shader), colour grading, a
cheaper or better bloom. These replace shaders the game uses on every frame, so each needs its own
option and a careful test. Binding anything new (camera matrices, another texture) means editing the
header tables, which are mapped but not fully decoded.

**Better anti-aliasing.** FXAA is already the PC variant. SMAA needs two extra targets and two lookup
textures (both can be added: the global resources take plain `texture` entries, as `vo_rotation`
shows) and three new shaders. TAA is out of reach: no velocity target, and the camera jitter would need
engine help.

**Anisotropic filtering.** The exe has no setting for it. Sampler states sit in each shader library's
header records, whose encoding isn't decoded yet; changing them would mean rewriting about 2,600
entries. Forcing it in the GPU driver works today.

**Model and scatter density.** The exe also reads `lod_scatter_density` and `lod_decoration_density`.
Their ranges are unknown; worth a quick live test with `set_renderer_setting`.

**Planet looks.** Fog, sun, ambient, bloom offset, vignette, film dust and colour grading per planet are
all live through `api.world.shading` (Photo Mode's Look tab already uses them). A "clearer air" or
"no vignette" option is a few lines; whether that is an improvement is taste.

### Out of reach, or better done outside the game

- **Supersampling inside the engine.** The scene targets all follow the back buffer, so they could be
  doubled in the render config, and the final passes would downsample by sampling. But the world-space
  transparent and HUD passes draw to the back buffer with the scene's depth buffer, which would then be a
  different size. Driver supersampling (DSR, VSR) does it cleanly.
- **Higher-resolution textures by hand.** New art for 2,045 textures is out of scope; an upscaler is the
  practical route, taken by the separate Enhanced Textures download.
- **Engine features** (ray tracing, new light types, new G-buffer channels): need the exe.

## Tools in this repository

| Tool | What it does |
|---|---|
| `tools/stingray.py` | Bundles: murmur64, entries, lookup |
| `tools/survey.py` | Counts resources by type and dumps the render-side ones to `internal/dump` |
| `tools/datatree.py` | The compiled data tree, decoded and encoded byte for byte |
| `tools/shading_env.py` | Shading environments: variables by name, type, value |
| `tools/shader_names.py` | Names shader library entries by guessing their hashes |
| `tools/shader_users.py` | Which resources mention a shader |
| `tools/dxbc.py` | Disassembles and compiles DX11 shaders through `d3dcompiler_47.dll` |
| `tools/shaderlib.py` | Swaps a stage of a shader library entry |
| `tools/warp.py`, `tools/preview.py` | Runs the mod's and the game's pixel shaders on WARP over a ray-cast scene |
| `tools/build.py`, `tools/check.py` | Builds `res/` and the manifest's resources, then checks them offline |
| `internal/testing/` (not in git) | Photo Mode's scripted game harness, with probes that set renderer settings and shading values in a paused mission |

## What the probes settled

1. The engine loads shader library entries with a swapped pixel shader: all four replacements ran.
2. `Application.set_renderer_setting` flips dynamic branches live, so the compare key and the in-game
   rows apply at once.
3. Plain shader passes get no shading values; material passes do (above).
4. `ssao_upscale` into `mask.a` darkens ambient light, so its blend multiplies or replaces, either way
   doing what the ambient-only mode needs.

Still open: performance of 8192 shadows on a mid-range card, and how the AO looks to players on each
planet type.
