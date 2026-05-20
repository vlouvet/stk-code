#!/bin/bash

CORE_COUNT="$(getconf _NPROCESSORS_ONLN)"
BASE_DIR="$(realpath "$(dirname "$0")")"
EMSDK_DIR="$BASE_DIR/emsdk"

git clone https://github.com/emscripten-core/emsdk.git --depth=1 "$EMSDK_DIR"
cd $EMSDK_DIR

./emsdk update
./emsdk install latest
./emsdk activate latest