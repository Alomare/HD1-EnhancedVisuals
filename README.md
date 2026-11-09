# Enhanced Visuals

Better graphics and extra visual effects for HELLDIVERS (2015), for the [Obsidian](https://github.com/Alomare/ObsidianModManager) mod manager.

The mod turns on the ambient occlusion pass that the game ships but never runs, adds sharpening and an ambient light effect, raises the sun's shadow resolution and distance, softens shadow edges, keeps detailed models farther from the camera, and can draw the scene above the screen's resolution. **F9** switches between Enhanced Visuals and the game's own look, to compare. The key can be changed in Obsidian.

## Options

Every option is in Obsidian. The ones that apply at once are also in game, under Mods in the pause menu; the others take effect at the next launch.

| Option | Choices (default in bold) | Applies |
|---|---|---|
| Screen dust | **Off**, On | At once |
| Sharpening | Off, **Normal**, Strong | Next launch |
| Ambient light | Off, **Subtle**, Strong | Next launch |
| Ambient occlusion | Off, Subtle, **Strong** | At once |
| Ambient occlusion affects | **Ambient light**, All daylight | At once |
| Shadow resolution | Default, **4096**, 8192 | Next launch |
| Shadow distance | **Default**, 35 m, 60 m | Next launch |
| Shadow edges | Default, Soft, **Softer** | Next launch |
| Render scale | **Default**, 1.5x, 2x | Next launch |
| Model detail distance | Default, **2x**, 4x | At once |

A shadow resolution of 8192 takes 256 MB of video memory for the sun alone. With Default, the Shadow quality in the game's Graphics menu applies as before.

## Requirements

Obsidian API 1.29.0 or later, which Obsidian installs with the mod. Enhanced Visuals is a client mod: it works in any lobby, and only this player sees the changes.

Photo Mode's sky fix is built in, so the two mods work together. Third Person Camera also uses F9 by default, so with both mods on, change one of the two keys in Obsidian.

For the game's textures at twice the size, see [Upscaled Textures](https://github.com/Alomare/HD1-UpscaledTextures), a separate mod that works with or without this one.

## Building

`python tools/build.py` writes `res/` from the game install and fills the manifest's resources. `python tools/check.py` checks the result offline, and `luajit tests/run.lua` runs the Lua tests. The build and the checks need Windows, numpy and Pillow. `docs/research.md` covers how the renderer works.

## License

MIT. See [LICENSE](LICENSE).
