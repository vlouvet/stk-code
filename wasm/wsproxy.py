#!/usr/bin/env python3
"""Tiny WebSocket-to-TCP proxy for the STK wasm port (dev only).

The wasm build's curl-in-Emscripten makes TCP connections by opening a
WebSocket to `<ws_proxy>/<host>:<port>` (see wasm/fragments/force_wsproxy.js).
This script accepts those WS connections, parses the target out of the URL
path, opens a real TCP socket, and bridges bytes both ways.

Pure stdlib — no `websockets` library required, since macOS Python is
externally-managed and pip-install is awkward. Implements just enough of
RFC 6455 to forward binary frames between the browser and a TCP target.

## Usage

    python3 wasm/wsproxy.py 8001       # listens on ws://localhost:8001
    # then edit wasm/web/config.json:
    #   "ws_enabled": true,
    #   "ws_proxy":   "ws://localhost:8001/"

## Caveats — DO NOT EXPOSE PUBLICLY

  * No auth, no allowlist — anyone connecting can use you to TCP-connect
    anywhere on the internet (SSRF). Run on localhost only.
  * No TLS termination at the WS layer (`ws://` not `wss://`). The HTTPS
    traffic to online.supertuxkart.net is end-to-end-TLS *over* the WS
    socket; curl in the wasm build does its own TLS handshake.
  * No backpressure tuning, no message-size limits.
"""

import asyncio
import base64
import hashlib
import struct
import sys

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xa


async def read_http_request(reader):
    """Parse the WS upgrade request. Returns (path, headers) or None."""
    line = await reader.readline()
    if not line:
        return None
    try:
        method, path, _ = line.decode("latin-1").rstrip("\r\n").split(" ", 2)
    except ValueError:
        return None
    if method != "GET":
        return None
    headers = {}
    while True:
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        try:
            k, v = h.decode("latin-1").rstrip("\r\n").split(":", 1)
        except ValueError:
            continue
        headers[k.strip().lower()] = v.strip()
    return path, headers


def handshake_response(client_key):
    accept = base64.b64encode(
        hashlib.sha1(client_key.encode("ascii") + GUID).digest()
    ).decode("ascii")
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("latin-1")


async def read_frame(reader):
    """Read one WS frame; returns (fin, opcode, payload) or None on close."""
    hdr = await reader.readexactly(2)
    fin = (hdr[0] & 0x80) != 0
    opcode = hdr[0] & 0x0f
    masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7f
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def build_frame(opcode, payload):
    """Server frames are unmasked (RFC 6455 §5.1)."""
    n = len(payload)
    if n < 126:
        hdr = bytes([0x80 | opcode, n])
    elif n < 1 << 16:
        hdr = bytes([0x80 | opcode, 126]) + struct.pack("!H", n)
    else:
        hdr = bytes([0x80 | opcode, 127]) + struct.pack("!Q", n)
    return hdr + payload


async def ws_to_tcp(reader, writer, peer):
    try:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                # echo as pong via the back-channel; we don't have it here,
                # so just ignore. Browser-initiated pings are rare in practice.
                continue
            if opcode in (OP_BIN, OP_TEXT, OP_CONT):
                writer.write(payload)
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def tcp_to_ws(reader, writer, peer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(build_frame(OP_BIN, data))
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.write(build_frame(OP_CLOSE, b""))
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(client_reader, client_writer):
    peer = client_writer.get_extra_info("peername")
    parsed = await read_http_request(client_reader)
    if parsed is None:
        client_writer.close()
        return
    path, headers = parsed
    if (
        headers.get("upgrade", "").lower() != "websocket"
        or "sec-websocket-key" not in headers
    ):
        client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    target = path.lstrip("/")
    if ":" not in target:
        client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\nmissing host:port\n")
        await client_writer.drain()
        client_writer.close()
        return
    host, port_str = target.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\nbad port\n")
        await client_writer.drain()
        client_writer.close()
        return

    print(f"[wsproxy] {peer} -> {host}:{port}", flush=True)
    try:
        tcp_reader, tcp_writer = await asyncio.open_connection(host, port)
    except OSError as e:
        print(f"[wsproxy] connect {host}:{port} failed: {e}", flush=True)
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    # Complete the WS handshake, then bridge.
    client_writer.write(handshake_response(headers["sec-websocket-key"]))
    await client_writer.drain()

    await asyncio.gather(
        ws_to_tcp(client_reader, tcp_writer, peer),
        tcp_to_ws(tcp_reader, client_writer, peer),
        return_exceptions=True,
    )
    print(f"[wsproxy] {peer} -> {host}:{port} closed", flush=True)


async def main():
    port = int(sys.argv[1]) if len(sys.argv) >= 2 else 8001
    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    print(f"[wsproxy] listening on ws://127.0.0.1:{port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
