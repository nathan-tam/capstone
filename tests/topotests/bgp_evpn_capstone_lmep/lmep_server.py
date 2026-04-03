#!/usr/bin/env python3
"""Standalone LMEP mapping server and VXLAN forwarder.

This process is intended to run outside the topotest process so the test can
interact with it as an external service. It exposes:
- a control port for MAC registration updates
- a data port for controller-facing frame forwarding requests

The implementation is intentionally small and uses only the Python standard
library so it can be started from tmux or any other shell session.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional


VXLAN_FLAGS_I = 0x08000000
DEFAULT_VXLAN_PORT = 4789


@dataclass
class LMEPEntry:
    mac: str
    client_ip: str
    vni: int
    vtep_ip: str
    registrations: int = 0

    def as_dict(self) -> dict:
        return {
            "mac": self.mac,
            "client_ip": self.client_ip,
            "vni": self.vni,
            "vtep_ip": self.vtep_ip,
            "registrations": self.registrations,
        }


@dataclass
class LMEPStore:
    entries: Dict[str, LMEPEntry] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, mac: str, client_ip: str, vni: int, vtep_ip: str) -> LMEPEntry:
        normalized_mac = mac.lower()
        with self.lock:
            entry = self.entries.get(normalized_mac)
            if entry is None:
                entry = LMEPEntry(
                    mac=normalized_mac,
                    client_ip=client_ip,
                    vni=vni,
                    vtep_ip=vtep_ip,
                    registrations=1,
                )
                self.entries[normalized_mac] = entry
            else:
                entry.client_ip = client_ip
                entry.vni = vni
                entry.vtep_ip = vtep_ip
                entry.registrations += 1
            return entry

    def lookup(self, mac: str) -> Optional[LMEPEntry]:
        with self.lock:
            return self.entries.get(mac.lower())

    def snapshot(self) -> Dict[str, dict]:
        with self.lock:
            return {mac: entry.as_dict() for mac, entry in self.entries.items()}


def mac_to_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def normalize_ethertype(value) -> int:
    if value is None:
        return 0x0800
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    return int(text, 16) if any(ch in text for ch in "abcdef") else int(text)


def build_ethernet_frame(dst_mac: str, src_mac: str, payload: bytes, ethertype=0x0800) -> bytes:
    return struct.pack(
        "!6s6sH",
        mac_to_bytes(dst_mac),
        mac_to_bytes(src_mac),
        normalize_ethertype(ethertype),
    ) + payload


def build_vxlan_packet(vni: int, inner_frame: bytes) -> bytes:
    # VXLAN header: 8 bytes total, I flag set and VNI in the upper 24 bits of the
    # second 32-bit word.
    return struct.pack("!I", VXLAN_FLAGS_I) + struct.pack("!I", int(vni) << 8) + inner_frame


def send_vxlan_packet(outer_src_ip: Optional[str], outer_dst_ip: str, vxlan_port: int, packet: bytes) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if outer_src_ip:
            sock.bind((outer_src_ip, 0))
        return sock.sendto(packet, (outer_dst_ip, vxlan_port))
    finally:
        sock.close()


def send_json_line(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


def read_json_lines(conn: socket.socket):
    buffer = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            yield json.loads(line.decode("utf-8"))


def handle_control_connection(conn: socket.socket, store: LMEPStore) -> None:
    with conn:
        for request in read_json_lines(conn):
            action = request.get("action")
            if action == "register":
                logging.info(
                    "control register mac=%s client_ip=%s vni=%s vtep=%s",
                    request.get("mac"),
                    request.get("client_ip", ""),
                    request.get("vni", 1000),
                    request.get("vtep_ip"),
                )
                entry = store.register(
                    mac=request["mac"],
                    client_ip=request.get("client_ip", ""),
                    vni=int(request.get("vni", 1000)),
                    vtep_ip=request["vtep_ip"],
                )
                send_json_line(conn, {"ok": True, "entry": entry.as_dict()})
                logging.info(
                    "control register complete mac=%s registrations=%s resolved_vtep=%s",
                    entry.mac,
                    entry.registrations,
                    entry.vtep_ip,
                )
            elif action == "lookup":
                entry = store.lookup(request["mac"])
                send_json_line(conn, {"ok": True, "entry": entry.as_dict() if entry else None})
                logging.info(
                    "control lookup mac=%s hit=%s",
                    request.get("mac"),
                    bool(entry),
                )
            elif action == "dump":
                send_json_line(conn, {"ok": True, "entries": store.snapshot()})
                logging.info("control dump entries=%s", len(store.entries))
            else:
                send_json_line(conn, {"ok": False, "error": f"unsupported action: {action}"})
                logging.warning("control unsupported action=%s payload=%s", action, request)


def handle_data_connection(conn: socket.socket, store: LMEPStore, outer_src_ip: Optional[str], vxlan_port: int) -> None:
    with conn:
        for request in read_json_lines(conn):
            dst_mac = request["dst_mac"]
            src_mac = request.get("src_mac", "02:00:00:00:00:01")
            ethertype = request.get("ethertype", 0x0800)
            vni = int(request.get("vni", 1000))
            payload_hex = request.get("payload_hex")
            payload_b64 = request.get("payload_b64")
            payload_text = request.get("payload_text")

            if payload_hex:
                payload = bytes.fromhex(payload_hex)
            elif payload_b64:
                payload = base64.b64decode(payload_b64)
            elif payload_text is not None:
                payload = str(payload_text).encode("utf-8")
            else:
                payload = b"LMEP"

            entry = store.lookup(dst_mac)
            if entry is None:
                send_json_line(conn, {"ok": False, "error": f"unknown destination MAC {dst_mac}"})
                logging.warning("data forward miss dst_mac=%s src_mac=%s vni=%s", dst_mac, src_mac, vni)
                continue

            inner_frame = build_ethernet_frame(dst_mac, src_mac, payload, ethertype=ethertype)
            vxlan_packet = build_vxlan_packet(vni, inner_frame)
            bytes_sent = send_vxlan_packet(outer_src_ip, entry.vtep_ip, vxlan_port, vxlan_packet)
            send_json_line(
                conn,
                {
                    "ok": True,
                    "mac": entry.mac,
                    "resolved_vtep": entry.vtep_ip,
                    "vni": vni,
                    "bytes_sent": bytes_sent,
                },
            )
            logging.info(
                "data forward dst_mac=%s src_mac=%s vni=%s resolved_vtep=%s bytes_sent=%s",
                dst_mac,
                src_mac,
                vni,
                entry.vtep_ip,
                bytes_sent,
            )


def serve_tcp(bind_host: str, port: int, handler, stop_event: threading.Event, label: str, *handler_args) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((bind_host, port))
    server.listen(16)
    server.settimeout(1.0)
    logging.info("%s listening on %s:%s", label, bind_host, port)
    try:
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handler, args=(conn, *handler_args), daemon=True).start()
    finally:
        server.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone LMEP mapping server")
    parser.add_argument("--bind-host", default="0.0.0.0", help="Address to bind the TCP listeners to")
    parser.add_argument("--control-port", type=int, default=6000, help="TCP control-plane port")
    parser.add_argument("--data-port", type=int, default=6001, help="TCP data-plane port")
    parser.add_argument("--vxlan-port", type=int, default=DEFAULT_VXLAN_PORT, help="VXLAN UDP destination port")
    parser.add_argument("--outer-src-ip", default="", help="Optional outer source IP for VXLAN UDP packets")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    store = LMEPStore()
    stop_event = threading.Event()

    control_thread = threading.Thread(
        target=serve_tcp,
        args=(args.bind_host, args.control_port, handle_control_connection, stop_event, "control-plane", store),
        daemon=True,
    )
    data_thread = threading.Thread(
        target=serve_tcp,
        args=(args.bind_host, args.data_port, handle_data_connection, stop_event, "data-plane", store, args.outer_src_ip or None, args.vxlan_port),
        daemon=True,
    )

    control_thread.start()
    data_thread.start()

    logging.info(
        "LMEP server ready: control=%s:%s data=%s:%s vxlan=%s outer-src-ip=%s",
        args.bind_host,
        args.control_port,
        args.bind_host,
        args.data_port,
        args.vxlan_port,
        args.outer_src_ip or "auto",
    )

    try:
        control_thread.join()
        data_thread.join()
    except KeyboardInterrupt:
        stop_event.set()
        logging.info("Stopping LMEP server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
