#!/usr/bin/env python3
"""Production WebSocket-to-TCP/UDP proxy for STK wasm.

Differs from the dev wasm/wsproxy.py:
  * TCP targets enforced against a strict allowlist (STK online API only).
  * UDP targets allowed broadly (ENet game servers vary), but the bogus
    "*.172.29:3478" STUN target produced by the wasm sockaddr bug is
    redirected to stun.cloudflare.com:3478.
  * Per-source-IP cap on concurrent connections + max payload size.
  * Logging to stdout/stderr for journald.

Binds to 127.0.0.1 only; nginx terminates wss:// and proxies to us.
"""

import asyncio
import base64
import hashlib
import os
import struct
import sys
from collections import defaultdict

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xa

# TCP allowlist — host:port. The STK wasm port only ever opens these.
ALLOWED_TCP = {
    "online.supertuxkart.net:443",
    "online.supertuxkart.net:80",
    "addons.supertuxkart.net:443",
    "addons.supertuxkart.net:80",
}

# Per-client cap (a "client" is the connecting IP reaching us from nginx —
# in practice this is always 127.0.0.1, but the header X-Forwarded-For
# carries the real one when nginx sets it).
MAX_CONNS_PER_CLIENT = 64
MAX_FRAME_BYTES = 256 * 1024  # 256 KB

# Active connections per client IP (used for the cap above).
_conn_counts = defaultdict(int)


async def read_http_request(reader):
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
    hdr = await reader.readexactly(2)
    opcode = hdr[0] & 0x0f
    masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7f
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    if length > MAX_FRAME_BYTES:
        # Skip overlarge frames to avoid memory blowup; signal close.
        return False, OP_CLOSE, b""
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return (hdr[0] & 0x80) != 0, opcode, payload


def build_frame(opcode, payload):
    n = len(payload)
    if n < 126:
        hdr = bytes([0x80 | opcode, n])
    elif n < 1 << 16:
        hdr = bytes([0x80 | opcode, 126]) + struct.pack("!H", n)
    else:
        hdr = bytes([0x80 | opcode, 127]) + struct.pack("!Q", n)
    return hdr + payload


# --- TCP bridge ---

async def ws_to_tcp(reader, writer):
    try:
        while True:
            fin, opcode, payload = await read_frame(reader)
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                continue
            if opcode in (OP_BIN, OP_TEXT, OP_CONT) and payload:
                writer.write(payload)
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def tcp_to_ws(reader, writer):
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


async def handle_tcp_bridge(client_reader, client_writer, headers, host, port, peer):
    target_key = f"{host}:{port}"
    if target_key not in ALLOWED_TCP:
        print(f"[wsproxy] {peer} TCP target {target_key} DENIED (allowlist)", flush=True)
        client_writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\ntarget not allowed\n")
        await client_writer.drain()
        client_writer.close()
        return
    try:
        tcp_reader, tcp_writer = await asyncio.open_connection(host, port)
    except OSError as e:
        print(f"[wsproxy] {peer} TCP connect {target_key} failed: {e}", flush=True)
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    client_writer.write(handshake_response(headers["sec-websocket-key"]))
    await client_writer.drain()

    await asyncio.gather(
        ws_to_tcp(client_reader, tcp_writer),
        tcp_to_ws(tcp_reader, client_writer),
        return_exceptions=True,
    )


# --- UDP bridge ---
#
# Key correctness requirement: STK does STUN on one socket to discover its
# public IP:port, then opens further connections to the game server. The
# server replies to the STUN-discovered port. If each WS got its own UDP
# socket on the proxy, the server's replies would land on a port nobody is
# listening on. So we keep one UDP socket per browser client (keyed by the
# client IP from X-Forwarded-For) and route inbound datagrams to the
# matching WS via (peer_ip, peer_port).

# client_ip -> _SharedUDP
_shared_udp = {}


class _SharedUDP:
    """One UDP socket shared by all of a client's UDP WSs."""

    def __init__(self, transport, client_ip):
        self.transport = transport
        self.client_ip = client_ip
        self.refcount = 0
        # (peer_ip, peer_port) -> asyncio.Queue for an active WS
        self.routes = {}

    def add_route(self, peer_ip, peer_port, queue):
        self.routes[(peer_ip, peer_port)] = queue
        self.refcount += 1

    def remove_route(self, peer_ip, peer_port):
        self.routes.pop((peer_ip, peer_port), None)
        self.refcount -= 1


class _SharedUDPProto(asyncio.DatagramProtocol):
    def __init__(self, shared):
        self.shared = shared

    def datagram_received(self, data, addr):
        queue = self.shared.routes.get(addr)
        if queue is None:
            # Most often: a late reply on a route we already tore down.
            # Occasionally: STK's NAT-punch peer expected a hostname-resolved
            # address but the reply came from a slightly different IP (rare).
            print(f"[wsproxy] {self.shared.client_ip} <- udp {addr} ({len(data)}B) NO ROUTE (have: {list(self.shared.routes)})", flush=True)
            return
        print(f"[wsproxy] {self.shared.client_ip} <- udp {addr} ({len(data)}B) -> route", flush=True)
        queue.put_nowait(data)

    def error_received(self, exc):
        pass


def _is_port_preamble(data):
    return len(data) == 10 and data[0:4] == b"\xff\xff\xff\xff" and data[4:8] == b"port"


async def _get_or_create_shared_udp(client_ip):
    shared = _shared_udp.get(client_ip)
    if shared is not None:
        return shared
    loop = asyncio.get_running_loop()
    proto_holder = {}
    def factory():
        proto = _SharedUDPProto(None)
        proto_holder["proto"] = proto
        return proto
    transport, _ = await loop.create_datagram_endpoint(factory, local_addr=("0.0.0.0", 0))
    shared = _SharedUDP(transport, client_ip)
    proto_holder["proto"].shared = shared
    _shared_udp[client_ip] = shared
    sockname = transport.get_extra_info("sockname")
    print(f"[wsproxy] shared UDP for {client_ip} bound at {sockname}", flush=True)
    return shared


async def ws_to_udp(reader, transport, target):
    first = True
    try:
        while True:
            fin, opcode, payload = await read_frame(reader)
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                continue
            if opcode in (OP_BIN, OP_TEXT, OP_CONT):
                if first and _is_port_preamble(payload):
                    first = False
                    continue
                first = False
                if payload:
                    transport.sendto(payload, target)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass


async def udp_to_ws(queue, writer):
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
    # Redirect the wasm sockaddr-corruption STUN target to a real STUN server.
    if host.endswith(".172.29") and port == 3478:
        print(f"[wsproxy] {peer} redirect bogus STUN {host}:{port} -> stun.cloudflare.com:3478", flush=True)
        host, port = "stun.cloudflare.com", 3478

    loop = asyncio.get_running_loop()
    # Resolve the target up front so we can route inbound packets by IP.
    # Force IPv4 — the proxy's shared socket is bound to 0.0.0.0 (AF_INET),
    # so an AAAA result would silently fail on sendto.
    import socket as _socket
    try:
        infos = await loop.getaddrinfo(host, port, family=_socket.AF_INET, type=_socket.SOCK_DGRAM)
    except OSError as e:
        print(f"[wsproxy] {peer} resolve {host}:{port} failed: {e}", flush=True)
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return
    target_ip, target_port = infos[0][4][0], infos[0][4][1]
    print(f"[wsproxy] {peer} udp {host}:{port} resolved to {target_ip}:{target_port}", flush=True)

    client_ip = peer[0] if peer else "unknown"
    try:
        shared = await _get_or_create_shared_udp(client_ip)
    except OSError as e:
        print(f"[wsproxy] {peer} UDP bind failed: {e}", flush=True)
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    queue = asyncio.Queue()
    shared.add_route(target_ip, target_port, queue)

    client_writer.write(handshake_response(headers["sec-websocket-key"]))
    await client_writer.drain()

    t_ws = asyncio.create_task(ws_to_udp(client_reader, shared.transport, (target_ip, target_port)))
    t_udp = asyncio.create_task(udp_to_ws(queue, client_writer))
    try:
        done, pending = await asyncio.wait({t_ws, t_udp}, return_when=asyncio.FIRST_COMPLETED)
        queue.put_nowait(None)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        shared.remove_route(target_ip, target_port)
        if shared.refcount == 0:
            _shared_udp.pop(client_ip, None)
            try:
                shared.transport.close()
            except Exception:
                pass
            print(f"[wsproxy] shared UDP for {client_ip} torn down", flush=True)


# --- dispatcher ---

async def handle_client(client_reader, client_writer):
    peer = client_writer.get_extra_info("peername")
    client_ip = peer[0] if peer else "unknown"

    if _conn_counts[client_ip] >= MAX_CONNS_PER_CLIENT:
        client_writer.write(b"HTTP/1.1 429 Too Many Requests\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return
    _conn_counts[client_ip] += 1

    try:
        parsed = await read_http_request(client_reader)
        if parsed is None:
            client_writer.close()
            return
        path, headers = parsed

        # Trust X-Forwarded-For from nginx; logs use it.
        xff = headers.get("x-forwarded-for", "")
        if xff:
            peer = (xff.split(",")[0].strip(), peer[1] if peer else 0)

        if headers.get("upgrade", "").lower() != "websocket" or "sec-websocket-key" not in headers:
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
    finally:
        _conn_counts[client_ip] -= 1
        if _conn_counts[client_ip] <= 0:
            _conn_counts.pop(client_ip, None)


async def main():
    bind_host = os.environ.get("WSPROXY_BIND", "127.0.0.1")
    bind_port = int(os.environ.get("WSPROXY_PORT", "8001"))
    server = await asyncio.start_server(handle_client, bind_host, bind_port)
    print(f"[wsproxy] listening on ws://{bind_host}:{bind_port}", flush=True)
    print(f"[wsproxy] TCP allowlist: {sorted(ALLOWED_TCP)}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
