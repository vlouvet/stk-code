# SuperTuxKart WASM Port

Currently this port is still incomplete.

Working features:
- The game launches
- OpenGL ES 2 graphics (GLES 3 doesn't work)
- Audio
- Persistent user data
- Caching data and asset files in IndexedDB

TODO:
- Fix GLES 3 (maybe someone with more experience in webgl could help here)
- Lazy load assets during gameplay
- Networking

Caveats:
- The performance isn't great, probably because the legacy renderer is still being used
- Some options, like anything related to online multiplayer, may hang the game

## Building
1. First, get a copy of the emsdk (it might help to have emscripten already installed with `sudo apt install emscripten`):
```
wasm/get_emsdk.sh
```
2. Compile all the dependencies:
```
wasm/build_deps.sh
```
3. Compile STK:
```
wasm/build.sh
```
4. Compress and bundle the game data and assets:
```
sudo apt install imagemagick vorbis-tools pngquant advancecomp libjpeg-progs optipng
wasm/pack_assets.sh ../stk-assets
```
5. Host a web server:
```
(cd wasm/web && python3 ../run_server.py)
```

## Online features (news, login, addons, lobbies)

STK's online features talk to `online.supertuxkart.net` over TCP via curl,
which the browser can't do directly. Emscripten routes those TCP sockets
through a WebSocket-to-TCP proxy. A small dev proxy is included:

```
python3 wasm/wsproxy.py 8001
```

Then edit `wasm/web/config.json` to enable it:
```json
{
  "ws_enabled": true,
  "ws_proxy":   "ws://127.0.0.1:8001/"
}
```

Reload the page. The proxy logs each accepted connection to stdout.

**The proxy is dev-only.** It accepts a TCP target on the URL path
(`/host:port`) with no allowlist, so anyone who can connect can use it
as an open relay (SSRF). It binds to `127.0.0.1` and should never be
exposed publicly. For production deploys, run an allowlisted proxy
(e.g. `nginx stream` block, websockify with `--target-config`, or
ading2210's stk wsproxy) in front of the served wasm.

## Project Structure:
- /wasm/build - Files for building the dependencies
- /wasm/prefix - Headers and library files
- /wasm/web - Web server root directory
- /wasm/emsdk - Emscripten SDK
- /wasm/fragments - Patches for emscripten's generated JS