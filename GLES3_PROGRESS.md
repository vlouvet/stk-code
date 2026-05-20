# GLES3 WebAssembly Port — Progress Handoff

## Goal

Extend ading2210's WebAssembly port of SuperTuxKart ([upstream PR #5106](https://github.com/supertuxkart/stk-code/pull/5106)) to support **OpenGL ES 3.0 / WebGL2**. The PR ships GLES2/legacy only; its TODO explicitly says: *"Fix GLES 3 (maybe someone with more experience in webgl could help here)"*.

## Status — one-liner

**The game runs in WebGL2 with the full SP renderer and advanced lighting enabled.** All graphics settings can be cranked to max; the race renders correctly (track geometry, textures, shadows, particles, HUD). Frame rate at max is ~30 FPS on an M1/M4 in Firefox; expect 60+ at lower settings.

## Repo state

- **Working dir**: `/Users/v/code/stk` (macOS, M4 Mac mini, zsh)
- **Branch**: `wasm-gles3` (forked from `ading2210/wasm`)
- **Remotes**: `origin` → `supertuxkart/stk-code`, `ading2210` → `ading2210/stk-code`, `vlouvet` → `vlouvet/stk-code` (the working fork)
- **Last synced with `origin/master`**: merge commit `87c625a2c4`, May 2026. Backup tag `wasm-gles3-pre-merge-master` (`2f5a6cd4b1`) preserves the pre-merge tip in case of rollback.
- **External assets**: SVN clone at `../stk-assets` (~1.4 GB)
- **Build dir**: `cmake_build/Debug`
- **Web output**: `wasm/web/game/`

⚠️ **Line-number references throughout this doc may be stale** after the 352-commit merge from upstream. The file paths are correct; grep for the function/symbol if a `:NNN` doesn't land.

## The core insight (still the load-bearing fact)

The upstream wasm branch forces the legacy fixed-function renderer in two places. Removing both lets the OGLES2 path in `CIrrDeviceSDL::tryCreateOpenGLContext` run — it requests `SDL_GL_CONTEXT_MAJOR=3, MINOR=0`, which **is** GLES 3.0 / WebGL2:

1. **`-sGL_FFP_ONLY`** in [CMakeLists.txt](CMakeLists.txt) (Emscripten linker flag) — disables shader path.
2. **Unconditional `goto legacy`** in [CIrrDeviceSDL.cpp](lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp) — bypasses the `ForceLegacyDevice` check on Emscripten.

Confirmed: log shows `Using renderer: OpenGL ES 3.0 (WebGL 2.0)`, all SP-renderer shaders compile, GLSL is supported.

## What WebGL2 strictly enforces that native GL doesn't

These rules surface as wall-clock blockers on the browser; native GL silently tolerates the violations. Anyone touching graphics code on this branch needs them in working memory:

1. **`layout(std140)` UBO sizes are exact.** A `vec2` at the end of a UBO pads to 16 bytes. WebGL2 rejects every draw with `Buffer for uniform block is smaller than UNIFORM_BLOCK_DATA_SIZE`. STK's `Matrices` UBO is one example — GLSL is 592 bytes, C++ allocated 584. See [src/graphics/shared_gpu_objects.cpp](src/graphics/shared_gpu_objects.cpp) and [src/graphics/sp/sp_base.cpp](src/graphics/sp/sp_base.cpp).

2. **No feedback loops, ever.** A texture cannot be both an FBO attachment and bound as a sampler in the same draw, even if depth-test/depth-mask is off. STK's deferred renderer originally violated this in five places — see "Feedback-loop fixes" below.

3. **`glClientWaitSync` cannot transition to `SIGNALED` within a single tick.** Control must return to the JS event loop first. Native GL drivers don't have this restriction. Effect: any spin loop on `glClientWaitSync` is an infinite hang.

4. **Linear filtering of half-float textures requires extensions.** `EXT_color_buffer_float` lets you *render to* RGBA16F/R16F; `OES_texture_half_float_linear` is a *separate* extension that lets you *linearly filter* them. Firefox's WebGL2 on macOS exposes the first but not the second. Without the linear extension, every bilinear sample is rejected: `unit X is incomplete: filtering is not NEAREST..., format is not "texture-filterable"`. The HDR pipeline now requires both before enabling RGBA16F render targets — see [src/graphics/central_settings.cpp](src/graphics/central_settings.cpp).

5. **Workers can't deliver messages to a main thread that doesn't yield.** Emscripten worker pool (used for texture decode) parks workers on `cvwait`. The main thread must return to the JS event loop between frames for those workers to be polled. Busy-waits hold the main thread and deadlock.

## Code changes — committed on `wasm-gles3`

Logical-change-per-commit. Run `git log ading2210/wasm..HEAD --oneline` to see the list. Grouped by category:

### Build environment (macOS portability)

- `nproc --all` → `getconf _NPROCESSORS_ONLN` (BSD has no `nproc`) across `wasm/get_emsdk.sh`, `wasm/build.sh`, `wasm/build_deps.sh`, `data/optimize_data.sh`.
- `wasm/build.sh` — drop the `sdl2_image_jpg/png` embuilder subtargets (rejected by current emsdk).
- `wasm/build_deps.sh` — BSD `sed -i` extension arg; bypass zlib's Makefile (its libtool can't read wasm bitcode); `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` for freetype.
- `android/generate_assets.sh` — parallelize find-while loops with `xargs -P`, auto-detect ImageMagick 6/7 (use `$MAGICK` consistently), `mktemp` for parallel-safe temp files.
- `wasm/pack_assets.sh` — `COPYFILE_DISABLE=1 tar --format=ustar --exclude='._*'` so macOS bsdtar doesn't embed AppleDouble/Pax entries that break js-untar; clean stale chunks before split; BSD-portable `wc -c` / `split -d`. Mid/high quality tiers commented out during iteration — re-enable to ship.

### GLES3 enablement

- `CMakeLists.txt` Emscripten branch — removed `-sGL_FFP_ONLY`.
- `CIrrDeviceSDL.cpp` — dropped the `#ifndef __EMSCRIPTEN__` so the `ForceLegacyDevice` gate behaves like native.

### Emscripten harness rework

The PR's `var real_run = run; run = () => {...}` override doesn't work with current Emscripten — `run` is lexically scoped inside the module IIFE. Replaced with `Module.noInitialRun = true; Module.onRuntimeInitialized = ...` + explicit `Module.callMain()`:

- `wasm/web/index.html` sets `var Module = { noInitialRun: true, onRuntimeInitialized: ... }` **before** the `<script src="/game/supertuxkart.js">`.
- `wasm/web/script.js` calls `Module.callMain()`.
- `CMakeLists.txt` Emscripten branch — `-sEXPORTED_RUNTIME_METHODS=callMain,FS,IDBFS` (modern Emscripten doesn't auto-expose them).

### CMake configure determinism

`set(JPEG_INCLUDE_DIR ...)` etc. became `set(... CACHE PATH "" FORCE)` / `CACHE FILEPATH "" FORCE`. Plain `set()` creates a normal variable, `find_package()` internally creates a CACHE variable, and on subsequent runs the CACHE shadows the normal var. The upstream comment "cmake fails the first few times you run it" was exactly this race.

### Std140 / UBO fixes

- `shared_gpu_objects.cpp` and `sp_base.cpp` — allocate 148 floats (592 bytes) for the Matrices UBO, not 146. The trailing 8 bytes stay zero from `glBufferData(NULL, ...)`. See the WebGL2-strictness rule #1 above.
- `sp_base.cpp` `SP::init()` — bind UBO slots 0 (`sp_mat_ubo[0][0]`) and 1 (`lighting_data_ubo`) persistently at init. The renderer's per-frame binding only runs during gameplay; menu-state SP draws (kart preview) had no buffer at slot 0 → undersized → WebGL2 no-op → lavender screen.

### Texture-queue / yield fixes

- `sp_texture_manager.cpp` `checkForGLCommand` — replaced the busy-wait (`sleep_for(1ms); continue;`) with a single pass over a snapshot of the queue. Anything returning `false` is re-queued for the next frame. `before_scene` arg ignored (kept for ABI). See rule #5 above.
- `draw_calls.hpp` `setFenceSync()` — no-op on `__EMSCRIPTEN__` so `m_sync` stays 0 and the spin loop in `prepareDrawCalls()` (which polls `glClientWaitSync`) is skipped. See rule #3.

### Feedback-loop fixes

Each of these added either a `bindWithoutDepth()` to the target FBO or wrapped the offending pass so the depth-stencil texture isn't simultaneously a sampler and an attachment:

- `frame_buffer.hpp` — added `FrameBuffer::bindWithoutDepth()` which detaches the depth attachment, plus made `bind()` auto-reattach. State tracked via `mutable bool m_depth_detached`.
- `shader_based_renderer.cpp` — switched `FBO_COMBINED_DIFFUSE_SPECULAR` (`renderLights`, samples `dtex` at unit 1) and `FBO_COLORS` (`CombineDiffuseColor`, samples `depth_stencil` at unit 4) to `bindWithoutDepth()`. Re-bind `FBO_COLORS` with depth before skybox/transparent/particles since those need depth-test. Also detach for the soft-particle pass (`simple_particle.frag` samples `dtex`).
- `post_processing.cpp` — `DepthOfFieldShader::render` and `renderMotionBlur` use `bindWithoutDepth()` on their output FBO. Both sample `dtex` while writing into a depth-attached target.

### HDR pipeline gating

- `central_settings.cpp` — `hasColorBufferFloat` now requires `EXT_color_buffer_float` AND `OES_texture_float_linear` AND `OES_texture_half_float_linear`. Without the last, the fallback path in [src/graphics/rtts.cpp](src/graphics/rtts.cpp) downgrades all HDR targets to `RGBA8`, which is filterable. See rule #4.

### Misc

- `kart_model.cpp` — skip headlight `<object>` entries with empty filename (the spotlight type) instead of falling through to `getMesh("")` and logging a misleading error.

### Merge with `origin/master` (May 2026)

Pulled 352 upstream commits via merge commit `87c625a2c4`. Eleven conflicts resolved manually. Additional Emscripten-portability fixes brought forward through the merge (the new upstream code assumes Vulkan is available):

- Guard new Vulkan-only sources (`ge_vulkan_deferred_fbo.cpp`, `_attachment_texture`, `_environment_map`, `_hiz_depth`, `_light_handler`, `_animated_mesh_scene_node`, `_mesh_scene_node`) with `#ifdef _IRR_COMPILE_WITH_VULKAN_` — wasm build defines `NO_IRR_COMPILE_WITH_VULKAN_` so their bodies are skipped.
- `ge_material_manager.cpp` — added `using namespace irr;` and `#include "IXMLReader.h" / "IFileSystem.h" / "irrXML.h"`. These used to come transitively from `ge_vulkan_driver.hpp`'s Vulkan-only `using` directive.
- `irr_driver.cpp` — qualify bare `EDT_VULKAN` as `video::EDT_VULKAN`.
- `race_paused_dialog.cpp`, `debug.cpp`, `custom_video_settings.cpp`, `options_screen_video.cpp`, `options_screen_display.cpp` — wrap `GE::getVKDriver()` call sites in `#ifdef _IRR_COMPILE_WITH_VULKAN_`.

Assets had to be repacked after the merge — upstream added new shaders (`sp_displace_ssr.frag`, `sunlightshadowpcss.frag`, the `ge_shaders/` directory) and new data files. Run `wasm/pack_assets.sh ../stk-assets` whenever a shader file appears in `data/shaders/` that wasn't there before.

## Open work

### 1. New 22× feedback-loop after merge

The post-merge log shows ~22 `drawElementsInstanced` rejections per frame:

```
drawElementsInstanced: Texture level 0 would be read by TEXTURE_2D unit 1,
but written by framebuffer attachment DEPTH_ATTACHMENT
```

This is new — pre-merge logs were clean. Suspect candidates from the upstream changes:

- `sp_displace_ssr.frag` — SP-pipeline shader for screen-space reflections in displacement passes. Samples a `sampler2DShadow` named `u_depth`. SP transparent draws go to `FBO_COLORS`, which has depth-stencil attached after our reattach. If `u_depth` resolves to the depth-stencil texture, that's the feedback.
- `sunlightshadowpcss.frag` — new PCSS shadow variant for sun light. Used inside `renderLights`, which already binds `FBO_COMBINED_DIFFUSE_SPECULAR` without depth, so this *should* be fine — but worth confirming.

To diagnose: temporarily comment out one of the new SP shader paths or print which `bind*` call precedes the failing draw, then apply the same `bindWithoutDepth()` pattern. Each rejected draw still costs validation time so fixing this should claw back some FPS.

### 2. Host `m_mat_ubo[148]` ABI mystery

When the host-side mirror in `shadow_matrices.hpp` is grown to 148 floats to match the GL buffer size, display init fails (4× "Could not initialize display!") even on a clean rebuild. With 146 it succeeds. The current workaround keeps the host array at 146 floats and the GL buffer at 148; `glBufferSubData` uploads only 146 floats and the trailing 8 bytes stay zero from the initial `glBufferData(NULL, ...)`. Probably a hidden `static_assert` on `sizeof(ShadowMatrices)` or an ABI-coupled allocation elsewhere. Worth tracking down — would simplify the buffer-size accounting.

### 3. World-delete flush is best-effort

`main_loop.cpp` used to call `checkForGLCommand(before_scene=true)` to "flush all GL commands before deleting a world." With the new non-blocking semantics, anything still queued at world-delete time fires its callback against an already-deleted `SPMeshBuffer`/`SPDynamicDrawCall`. Use-after-free in theory. Doesn't matter for boot-to-first-race; revisit before any "race → menu → race again" loop is exercised in anger.

### 4. Frame rate at max settings is ~30 FPS

Expected for browser/WebGL2 at this fidelity (native on the same M-series GPU sits at 60-120). Two cheap wins available if anyone wants to chase: fix the 22× feedback above (clawback unknown but non-zero), and double-check whether the menu-state path is also running the full deferred pipeline when it doesn't need to. Below `max`, FPS scales well — drop shadows from Very High to High, disable DoF/Godrays/LightScatter, and 60+ is easy.

### 5. Mid + high quality tiers are not built

`wasm/pack_assets.sh` only generates `data_low` (256-px textures). Lines for `data_mid` and `data_high` are commented out. Re-enable when shipping; each tier roughly doubles the asset size and pack time.

## Useful files when picking back up

Symbol-level pointers (line numbers may have shifted post-merge):

- `CMakeLists.txt` — Emscripten branch, prefix paths, linker flags.
- `lib/irrlicht/source/Irrlicht/CIrrDeviceSDL.cpp` — `tryCreateOpenGLContext` (OGLES2 → legacy paths). The `ForceLegacyDevice` ifndef trick lives here.
- `src/graphics/sp/sp_base.cpp` — SP renderer init (UBO allocation, persistent slot bindings).
- `src/graphics/sp/sp_texture_manager.cpp` — the patched GL command queue (`checkForGLCommand`).
- `src/graphics/frame_buffer.hpp` — `bind()` / `bindWithoutDepth()` detach-reattach toggle.
- `src/graphics/shader_based_renderer.cpp` — `renderSceneDeferred` (the feedback-loop fixes live here).
- `src/graphics/draw_calls.hpp` — `setFenceSync()` no-op on Emscripten.
- `src/graphics/central_settings.cpp` — `hasColorBufferFloat` gate including the half-float-linear extension check.
- `data/shaders/header.txt` — the GLSL UBO declarations that all the C++ buffer-sizing math has to match.
- `wasm/fragments/fix_webgl.js` — JS-level workaround for client-side vertex arrays in legacy path (harmless on SP path).
- `wasm/web/script.js`, `wasm/web/index.html` — JS loader, Module bootstrap.

## Build / run cheatsheet

```zsh
# full build (5–15 min from clean)
rm -rf cmake_build/Debug && wasm/build.sh Debug

# incremental
wasm/build.sh Debug

# repack assets — REQUIRED whenever data/shaders/ gains a new file
wasm/pack_assets.sh ../stk-assets

# serve
cd wasm/web && python3 -m http.server 8000
```

Browser: hard-reload (Cmd+Shift+R), select Low quality, click Start Game. To wipe persisted state, devtools → Application/Storage → IndexedDB → `stk_db` → delete.

## Gotchas

- **Don't re-run `wasm/build_deps.sh`** unless you wipe `wasm/prefix/` — it's idempotent via existence checks but heavy.
- **Repack assets when shader files change.** The wasm bundle is a static tarball; new files in `data/shaders/` don't appear in the served `data_low.tar.gz.*` chunks unless `pack_assets.sh` is re-run. The merge from upstream missed two new shaders this way and the game failed to link them at runtime.
- **DevTools open hurts perf.** Firefox warns about this directly in the console; expect 10-30% recovery when closed.
- **Two backup tags exist**: `wasm-gles3-pre-merge-master` (`2f5a6cd4b1`) is the pre-merge tip. Use `git reset --hard wasm-gles3-pre-merge-master` to roll back if the merge introduces a regression you can't immediately resolve.
- **The `mid`/`high` asset tiers aren't built.** See open work #5.
