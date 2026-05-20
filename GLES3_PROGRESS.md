# GLES3 WebAssembly Port — Progress Handoff

## Goal

Extend ading2210's WebAssembly port of SuperTuxKart ([upstream PR #5106](https://github.com/supertuxkart/stk-code/pull/5106)) to support **OpenGL ES 3.0** (WebGL2). The PR ships GLES2 only; its TODO explicitly says: *"Fix GLES 3 (maybe someone with more experience in webgl could help here)"*.

## Repo state

- **Working dir**: `/Users/v/code/stk` (macOS, M4 Mac mini, zsh)
- **Branch**: `wasm-gles3`, originally tracking `ading2210/wasm`
- **Remotes**: `origin` → `supertuxkart/stk-code`, `ading2210` → `ading2210/stk-code`
- **External assets**: cloned via SVN at `../stk-assets` (~1.4 GB)
- **Build dir**: `cmake_build/Debug`
- **Web output**: `wasm/web/game/`

## The GLES3 hypothesis (the core insight)

The upstream wasm branch forces the legacy renderer in two places:

1. **`-sGL_FFP_ONLY`** in [CMakeLists.txt:107](CMakeLists.txt#L107) — Emscripten flag that disables shader path, forces fixed-function emulation.
2. **Unconditional `goto legacy`** in [lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp:557-560](lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp#L557-L560) — bypasses the `ForceLegacyDevice` check on Emscripten.

Removing both lets the OGLES2 path at [CIrrDeviceSDL.cpp:574-585](lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp#L574-L585) run — it requests `SDL_GL_CONTEXT_MAJOR=3, MINOR=0`, which **is** GLES 3.0 / WebGL2. Confirmed working: log shows `Using renderer: OpenGL ES 3.0 (WebGL 2.0)`, all 30+ SP-renderer shaders compile, GLSL is supported.

## Current state

**Where the game is now:** the user can load the page, sees the menu, sets a username, picks a race, clicks Start, and the loading screen completes. The race itself **renders** — track geometry, kart silhouettes, and the minimap show up. Visual fidelity is wrong (white road textures, depth-buffer feedback-loop glitches), but the engine is running on WebGL2.

## Code changes — committed on `wasm-gles3`

All committed; one logical change per commit. Run `git log ading2210/wasm..HEAD --oneline` to see the list.

### Build environment (macOS portability)

- `wasm/get_emsdk.sh`, `wasm/build.sh`, `data/optimize_data.sh`, `wasm/build_deps.sh`, `android/generate_assets.sh` — `nproc --all` → `getconf _NPROCESSORS_ONLN` (BSD doesn't ship `nproc`)
- `wasm/build.sh` — drop the `sdl2_image_jpg sdl2_image_png` embuilder subtargets (rejected by current emsdk)
- `wasm/build_deps.sh` — BSD `sed -i` needs an extension arg; bypass zlib's Makefile (its libtool can't read wasm bitcode); add `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` for freetype
- `android/generate_assets.sh` — parallelize find-while loops with `xargs -P`, auto-detect ImageMagick 6/7 and use input-first arg order for IM7, `mktemp` for parallel-safe temp files
- `wasm/pack_assets.sh` — `COPYFILE_DISABLE=1 tar --format=ustar --exclude='._*'` so macOS bsdtar doesn't embed AppleDouble/Pax entries that break js-untar in the browser; clean stale chunks before split; switch BSD-incompatible `du -b` / `split --numeric-suffixes` to `wc -c` / `split -d`; comment out mid/high tiers during iteration

### GLES3 enablement

- [CMakeLists.txt:107](CMakeLists.txt#L107) — removed `-sGL_FFP_ONLY`
- [CIrrDeviceSDL.cpp:557-560](lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp#L557-L560) — dropped the `#ifndef __EMSCRIPTEN__` so the `ForceLegacyDevice` gate behaves the same as native builds

### Emscripten harness rework

The PR's `var real_run = run; run = () => {...}` override doesn't work with current Emscripten — `run` is lexically scoped inside the module IIFE. Replaced with the canonical `Module.noInitialRun = true; Module.onRuntimeInitialized = ...` pattern + explicit `Module.callMain()`:

- `wasm/web/index.html` — `var Module = { noInitialRun: true, onRuntimeInitialized: () => globalThis.ready = true }` set **before** `<script src="/game/supertuxkart.js">`
- `wasm/web/script.js:244` — `run()` → `Module.callMain()`
- [CMakeLists.txt:107](CMakeLists.txt#L107) — `-sEXPORTED_RUNTIME_METHODS=callMain,FS,IDBFS` (modern Emscripten doesn't auto-expose them)

### CMake configure determinism

[CMakeLists.txt:65-89](CMakeLists.txt#L65-L89) — all the `set(JPEG_INCLUDE_DIR ...)` etc. became `set(... CACHE PATH "" FORCE)` / `CACHE FILEPATH "" FORCE`. Plain `set()` creates a normal variable; `find_package()` internally calls `find_path()` which creates a CACHE variable; on subsequent runs the CACHE shadows the normal var. The upstream comment "cmake fails the first few times you run it then works fine without changing anything" was exactly this race. With `FORCE`, the configure works on the first try every time.

### WebGL2 strictness fixes

These are the actual GLES3-specific bugs in STK's SP renderer:

- **[CMakeLists.txt:107](CMakeLists.txt#L107)** — added `-sPTHREAD_POOL_SIZE=32` so shader compilation doesn't deadlock on a too-small worker pool
- **Matrices UBO size** ([shared_gpu_objects.cpp:144](src/graphics/shared_gpu_objects.cpp#L144), [sp_base.cpp:465](src/graphics/sp/sp_base.cpp#L465)) — GLSL's `layout(std140) uniform Matrices { ... vec2 u_screen; }` is 592 bytes because the trailing `vec2` is padded to 16-byte alignment. C++ allocated 584 bytes (146 floats). Native GL was lenient; WebGL2 rejected every `drawArraysInstanced`/`drawElementsInstanced` with `Buffer for uniform block is smaller than UNIFORM_BLOCK_DATA_SIZE`. Allocate 148 floats / 592 bytes; the host array stays 146 floats (see "ABI coupling" TODO below); the trailing 8 bytes of the GL buffer stay zero from `glBufferData(NULL, ...)`.
- **Persistent UBO bindings** ([sp_base.cpp:478-486](src/graphics/sp/sp_base.cpp#L478-L486)) — `ShaderBasedRenderer::renderScene` binds slots 0/1/2 every frame, but only runs during gameplay. Menu-state SP draws (kart preview) had nothing at slot 0 → null buffer → undersized → WebGL2 no-op → lavender screen. Bind `sp_mat_ubo[0][0]` and `lighting_data_ubo` at `SP::init()` so menu draws have valid buffers.
- **checkForGLCommand busy-wait** ([sp_texture_manager.cpp:106-130](src/graphics/sp/sp_texture_manager.cpp#L106-L130)) — the old code, called with `before_scene=true`, looped on `std::this_thread::sleep_for(1ms)` waiting for the queue to drain. On Emscripten the main thread MUST return to the JS event loop for worker messages (texture-ready signals) to be delivered — the loop deadlocked exactly because it never yielded. Replaced with a single pass over a snapshot; anything returning `false` is re-queued for the next frame. The `before_scene` arg is now ignored (kept for ABI).

### Misc

- [kart_model.cpp:711-713](src/karts/kart_model.cpp#L711-L713) — skip headlight `<object>` entries that have no model file (the spotlight type) instead of falling through to `getMesh("")` and logging a misleading "file format unsupported" error for the kart directory.

## Outstanding TODOs

1. **Visual glitches in-race**. The screenshot shows the track loaded but with white road textures and what looks like depth-buffer feedback artifacts. The log shows:

   ```
   drawArraysInstanced: Texture level 0 would be read by TEXTURE_2D unit 1,
   but written by framebuffer attachment DEPTH_ATTACHMENT,
   which would be illegal feedback.
   ```

   The deferred renderer's G-buffer/SSAO/lighting composite passes sample a depth texture while it's still attached as `DEPTH_ATTACHMENT`. Native GL tolerates this when depth writes are masked off; WebGL2 doesn't, ever. Affected draws no-op. **Quickest test**: turn off "Dynamic lighting" in the in-game graphics settings (toggles `enable_dynamic_lights="false"` in `~/.config/supertuxkart/config-0.10/config.xml`), which routes to the forward renderer and should sidestep the feedback loops entirely. Proper fix is to copy the depth texture before sampling, or detach it from the FBO while sampling. The forward path is the right interim ship target.

2. **Host `m_mat_ubo[148]` breaks display init** — *a real mystery*. When the host-side `shadow_matrices.hpp` array is grown to 148 floats to mirror the GL buffer size, display init fails (4× "Could not initialize display!") even on a clean rebuild. With 146, init succeeds. Nothing in the changeset should touch the display-init path. Current workaround: host array stays 146 floats, GL buffer is 148 floats, `glBufferSubData` uploads only 146 floats and the trailing 8 bytes stay as the zero-init from `glBufferData(NULL, ...)`. Worth tracking down — probably a hidden `static_assert` on `sizeof(ShadowMatrices)` or an ABI-coupled allocation elsewhere.

3. **World-delete flush is best-effort now.** The one caller of `checkForGLCommand(before_scene=true)` in `main_loop.cpp:509` used the (former) blocking behavior to "flush all GL commands before deleting a world." With the new non-blocking semantics, anything still queued at world-delete time will fire its callback against an already-deleted SPMeshBuffer/SPDynamicDrawCall. Use-after-free in theory. Doesn't matter for the current "boot to first race" demo path. To revisit before any "race → return to menu → race again" loop.

## Pending build artifacts

- `wasm/prefix/lib/` — all C++ deps built: libcrypto, libcurl, libfreetype, libharfbuzz, libjpeg, libogg, libpng, libssl, libturbojpeg, libvorbis*, libz.
- `wasm/web/game/data_low.tar.gz.{00..06}` + manifest — ~128 MB low-quality assets, ustar format.
- `wasm/web/game/supertuxkart.{js,wasm}` — current build (see top of HEAD log).

## Useful files when picking back up

- [CMakeLists.txt:61-115](CMakeLists.txt#L61-L115) — Emscripten branch (prefix paths, linker flags)
- [lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp:540-700](lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp#L540) — GL context creation (OGLES2 → legacy paths)
- [src/graphics/sp/sp_base.cpp:438-490](src/graphics/sp/sp_base.cpp#L438) — SP renderer init (UBO allocation, persistent bindings)
- [src/graphics/sp/sp_texture_manager.cpp:100-140](src/graphics/sp/sp_texture_manager.cpp#L100) — the patched GL command queue
- [src/graphics/shader_based_renderer.cpp:78-105](src/graphics/shader_based_renderer.cpp#L78) — LightingData upload (the std140 layout reference)
- [data/shaders/header.txt](data/shaders/header.txt) — the GLSL UBO declarations that all the C++ buffer-sizing math has to match
- [wasm/fragments/fix_webgl.js](wasm/fragments/fix_webgl.js) — JS-level workaround for client-side vertex arrays in legacy path (still applied; harmless on SP path)
- [wasm/web/script.js](wasm/web/script.js), [wasm/web/index.html](wasm/web/index.html) — JS loader, Module bootstrap

## Build / run cheatsheet

```zsh
# full build (5–15 min)
rm -rf cmake_build/Debug && wasm/build.sh Debug

# incremental (a single .cpp edit)
wasm/build.sh Debug

# repack assets (low quality only — fast on M4 thanks to parallelization)
wasm/pack_assets.sh ../stk-assets

# serve
cd wasm/web && python3 -m http.server 8000
```

In the browser: hard-reload (Cmd+Shift+R), select Low quality, click Start Game. To wipe persisted state, open devtools → Application/Storage → IndexedDB → `stk_db` → delete.

## Notes

- Don't re-run `wasm/build_deps.sh` unless you wipe `wasm/prefix/` — it's idempotent via existence checks but heavy.
- Asset re-pack with the parallelization on M4 is a few minutes for the low tier.
- If you change texture-resize parameters in `generate_assets.sh`, delete `wasm/web/game/data_low/` before re-running `wasm/pack_assets.sh`.
