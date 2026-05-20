#!/usr/bin/env python3
"""Tiny WebSocket-to-TCP/UDP proxy for the STK wasm port (dev only).

The wasm build's Emscripten libsockfs opens a WebSocket per target socket.
URL form is `<ws_proxy>/<host>:<port>` for TCP and `<ws_proxy>/udp/<host>:<port>`
for UDP (see wasm/fragments/force_wsproxy.js). This script accepts those WS
connections, parses the target out of the URL, opens a real socket, and
bridges bytes both ways.

For UDP, libsockfs prepends a 10-byte `\xff\xff\xff\xff "port" <hi> <lo>`
preamble announcing the wasm side's bound local port. The proxy drops it —
we don't need it, since each WS is one socket-to-target bridge and our own
ephemeral source port is fine.

Pure stdlib — no `websockets` library required, since macOS Python is
externally-managed and pip-install is awkward. Implements just enough of
RFC 6455 to forward binary frames between the browser and the target.

## Usage

    python3 wasm/wsproxy.py 8001       # listens on ws://localhost:8001
    # then edit wasm/web/config.json:
    #   "ws_enabled": true,
    #   "ws_proxy":   "ws://localhost:8001/"

## Caveats — DO NOT EXPOSE PUBLICLY

  * No auth, no allowlist — anyone connecting can use you to TCP- or UDP-
    connect anywhere on the internet (SSRF). Run on localhost only.
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
    is_udp = False
    if target.startswith("udp/"):
        is_udp = True
        target = target[len("udp/"):]
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

    proto = "udp" if is_udp else "tcp"
    print(f"[wsproxy] {peer} -> {proto}://{host}:{port}", flush=True)

    if is_udp:
        await handle_udp_bridge(client_reader, client_writer, headers, host, port, peer)
    else:
        await handle_tcp_bridge(client_reader, client_writer, headers, host, port, peer)
    print(f"[wsproxy] {peer} -> {proto}://{host}:{port} closed", flush=True)


async def handle_tcp_bridge(client_reader, client_writer, headers, host, port, peer):
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


class _UDPProto(asyncio.DatagramProtocol):
    """Pipes inbound datagrams into a queue for the WS-writer coroutine."""

    def __init__(self, queue, expected_addr, peer):
        self.queue = queue
        self.expected_addr = expected_addr  # (host, port) tuple after resolution
        self.peer = peer
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        print(f"[wsproxy] {self.peer} <- udp {addr} ({len(data)} bytes)", flush=True)
        self.queue.put_nowait(data)

    def error_received(self, exc):
        print(f"[wsproxy] {self.peer} udp error: {exc}", flush=True)


def _is_port_preamble(data):
    return (
        len(data) == 10
        and data[0:4] == b"\xff\xff\xff\xff"
        and data[4:8] == b"port"
    )


async def ws_to_udp(reader, transport, target, peer):
    first = True
    try:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                continue
            if opcode in (OP_BIN, OP_TEXT, OP_CONT):
                # First wasm-side message may be the 10-byte port preamble
                # announcing the sender's bound local port. We don't need it.
                if first and _is_port_preamble(payload):
                    first = False
                    print(f"[wsproxy] {peer} dropped preamble", flush=True)
                    continue
                first = False
                if payload:
                    print(f"[wsproxy] {peer} -> udp {target} ({len(payload)} bytes)", flush=True)
                    transport.sendto(payload, target)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass


async def udp_to_ws(queue, writer, peer):
    try:
        while True:
            data = await queue.get()
            if data is None:
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


async def handle_udp_bridge(client_reader, client_writer, headers, host, port, peer):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    try:
        transport, _proto = await loop.create_datagram_endpoint(
            lambda: _UDPProto(queue, (host, port), peer),
            local_addr=("0.0.0.0", 0),
        )
    except OSError as e:
        print(f"[wsproxy] udp bind for {host}:{port} failed: {e}", flush=True)
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    client_writer.write(handshake_response(headers["sec-websocket-key"]))
    await client_writer.drain()

    t_ws = asyncio.create_task(ws_to_udp(client_reader, transport, (host, port), peer))
    t_udp = asyncio.create_task(udp_to_ws(queue, client_writer, peer))
    try:
        done, pending = await asyncio.wait(
            {t_ws, t_udp}, return_when=asyncio.FIRST_COMPLETED
        )
        # If the WS side ended, wake the UDP-reader so it exits cleanly.
        queue.put_nowait(None)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        try:
            transport.close()
        except Exception:
            pass


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
