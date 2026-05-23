#!/bin/bash
# wasm/test_benchmark.sh
#
# Regression test for the "benchmark crash" reported in PR #5106 review:
# https://github.com/supertuxkart/stk-code/pull/5106#issuecomment-4525875591
#
# The benchmark flow activates the GPU profiler the moment the race countdown
# hits "GO", which triggers glGenQueriesEXT. Emscripten dispatches that to
# GLctx.disjointTimerQueryExt.createQueryEXT, which is undefined unless the
# WebGL extension was bound (and Firefox often refuses to bind it). The
# resulting "TypeError: ... is not a function" tears the page down.
#
# The fix is in src/graphics/glwrap.cpp: under __EMSCRIPTEN__, the
# ScopedGPUTimer/elapsedTimeus paths short-circuit before any glGenQueries/
# glBeginQuery/glEndQuery/glGetQueryObjectuiv call.
#
# What this script checks:
#   1. The wasm IMPORT section does not declare any timer-query entry points
#      (proof the C-side #ifdef is actually removing the call sites — the
#      JS glue keeps the symbols, but the wasm only imports what it calls).
#   2. The deployed page returns 200 and serves the wasm with the right MIME.
#   3. (Optional, manual) headless smoke instructions for triggering the
#      benchmark in a browser.
#
# Usage: bash wasm/test_benchmark.sh [host]
#   host defaults to stk.linuxcolorado.com.

set -eu

HOST="${1:-stk.linuxcolorado.com}"
WASM_LOCAL="${WASM_LOCAL:-wasm/web/game/supertuxkart.wasm}"
FAIL=0

step() { printf '\n=== %s ===\n' "$*"; }

step "1. Wasm import-section scan for timer-query symbols"
if [ ! -f "$WASM_LOCAL" ]; then
    echo "skipped: $WASM_LOCAL not present (build wasm first)"
else
    bad=$(python3 - "$WASM_LOCAL" <<'PY'
import sys
def leb128(d, i):
    v, s = 0, 0
    while True:
        b = d[i]; i += 1
        v |= (b & 0x7f) << s
        if not (b & 0x80): break
        s += 7
    return v, i
data = open(sys.argv[1],'rb').read()
assert data[:4] == b'\0asm'
i = 8; bad = []
while i < len(data):
    sid = data[i]; i += 1
    size, i = leb128(data, i)
    end = i + size
    if sid == 2:
        n, i = leb128(data, i)
        for _ in range(n):
            ml, i = leb128(data, i)
            mod = data[i:i+ml].decode('utf8','replace'); i += ml
            nl, i = leb128(data, i)
            nm = data[i:i+nl].decode('utf8','replace'); i += nl
            kind = data[i]; i += 1
            if kind == 0:
                _, i = leb128(data, i)
            elif kind in (1,2):
                if kind == 1: i += 1
                flg = data[i]; i += 1
                _, i = leb128(data, i)
                if flg & 1: _, i = leb128(data, i)
            elif kind == 3:
                i += 2
            elif kind == 4:
                i += 1; _, i = leb128(data, i)
            if any(k in nm for k in ("glGenQueries", "glBeginQuery", "glEndQuery", "glGetQuery", "glDeleteQueries", "TimerQuery")):
                bad.append(nm)
        break
    i = end
for b in bad: print(b)
PY
)
    if [ -n "$bad" ]; then
        echo "FAIL: wasm still imports timer-query entry points:"
        printf '  %s\n' $bad
        FAIL=1
    else
        echo "PASS: wasm has no timer-query imports"
    fi
fi

step "2. Public page health"
http_code=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/")
if [ "$http_code" = "200" ]; then
    echo "PASS: https://$HOST/ -> $http_code"
else
    echo "FAIL: https://$HOST/ -> $http_code (expected 200)"
    FAIL=1
fi

mime=$(curl -sI "https://$HOST/game/supertuxkart.wasm" | tr -d '\r' | awk -F': ' 'tolower($1)=="content-type"{print $2}')
if [ "$mime" = "application/wasm" ]; then
    echo "PASS: supertuxkart.wasm served as application/wasm"
else
    echo "FAIL: supertuxkart.wasm content-type is '$mime' (expected application/wasm)"
    FAIL=1
fi

step "3. Manual repro instructions"
cat <<EOF
To exercise the benchmark path end-to-end:

  1. Open https://$HOST/ in a fresh browser tab.
  2. Click Options -> Graphics -> "Run benchmark".
  3. The race countdown ("Ready... Set... Go!") should reach Go without
     a JS TypeError. The kart should render correctly (textured, lit).
  4. The benchmark should complete and write a profile log to the page
     download dir.

If you see "Uncaught TypeError: GLctx.disjointTimerQueryExt.createQueryEXT
is not a function" at the moment of Go, the fix has regressed and step 1
of this script should also fail on the next deploy.
EOF

exit $FAIL
