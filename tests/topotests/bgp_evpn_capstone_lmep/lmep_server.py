#!/usr/bin/env python3
"""Standalone LMEP mapping server with binary TLV registration and Scapy forwarding.

This process is intended to run outside the topotest process so the test can
interact with it as an external service.  It exposes:

- a UDP registration port that accepts binary TLV MAC registration messages
- a Scapy-based packet sniffer that intercepts controller-facing Ethernet
  frames, resolves the destination MAC via the mapping table, and encapsulates
  the frame in VXLAN toward the correct VTEP

The registration protocol uses the custom TLV format defined in the
LMEP Standard (see ``LMEP Standard.md``):

    Type 0x01 (6 bytes)  - Client MAC
    Type 0x02 (4 bytes)  - Client IP
    Type 0x03 (3 bytes)  - VNI
    Type 0x04 (4 bytes)  - VTEP IP

Usage::

    python3 lmep_server.py --port 6000 --iface eth0
"""

from __future__ import annotations

import argparse
import logging
import socket
import struct
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional

from scapy.all import IP, UDP, Ether, Raw, sendp, sniff

# ---------------------------------------------------------------------------
# TLV type constants (must match test_evpn_capstone_lmep.py)
# ---------------------------------------------------------------------------
MAC_REGISTER_TYPE = 0x01
CLIENT_IP_TYPE = 0x02
VNI_TYPE = 0x03
VTEP_IP_TYPE = 0x04

VXLAN_UDP_PORT = 4789


# ---------------------------------------------------------------------------
# Data store
# ---------------------------------------------------------------------------
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
    """Thread-safe MAC-to-VTEP mapping store."""

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


# ---------------------------------------------------------------------------
# Binary TLV registration parser
# ---------------------------------------------------------------------------
def parse_tlv_registration(data: bytes) -> dict:
    """Parse a binary TLV registration packet into a dict.

    Returns a dict with keys: ``mac``, ``client_ip``, ``vni``, ``vtep_ip``.
    Missing fields will be ``None``.
    """
    result: dict = {"mac": None, "client_ip": None, "vni": None, "vtep_ip": None}
    offset = 0

    while offset < len(data):
        if offset + 2 > len(data):
            break
        tlv_type = data[offset]
        tlv_length = data[offset + 1]
        if offset + 2 + tlv_length > len(data):
            break
        tlv_value = data[offset + 2 : offset + 2 + tlv_length]

        if tlv_type == MAC_REGISTER_TYPE and tlv_length == 6:
            result["mac"] = ":".join(f"{b:02x}" for b in tlv_value)
        elif tlv_type == CLIENT_IP_TYPE and tlv_length == 4:
            result["client_ip"] = socket.inet_ntoa(tlv_value)
        elif tlv_type == VNI_TYPE and tlv_length == 3:
            result["vni"] = int.from_bytes(tlv_value, "big")
        elif tlv_type == VTEP_IP_TYPE and tlv_length == 4:
            result["vtep_ip"] = socket.inet_ntoa(tlv_value)

        offset += 2 + tlv_length

    return result


# ---------------------------------------------------------------------------
# UDP registration listener
# ---------------------------------------------------------------------------
def listen_for_registrations(
    bind_host: str,
    port: int,
    store: LMEPStore,
    stop_event: threading.Event,
) -> None:
    """Listen on a UDP socket for binary TLV registration messages."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, port))
    sock.settimeout(1.0)
    logging.info("Registration listener on udp://%s:%s", bind_host, port)

    try:
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue

            parsed = parse_tlv_registration(data)
            mac = parsed.get("mac")
            vtep_ip = parsed.get("vtep_ip")

            if mac and vtep_ip:
                entry = store.register(
                    mac=mac,
                    client_ip=parsed.get("client_ip") or "",
                    vni=parsed.get("vni") or 1000,
                    vtep_ip=vtep_ip,
                )
                logging.info(
                    "Registered MAC %s -> VTEP %s (client_ip=%s vni=%s regs=%s) from %s",
                    entry.mac,
                    entry.vtep_ip,
                    entry.client_ip,
                    entry.vni,
                    entry.registrations,
                    addr,
                )
            else:
                logging.warning(
                    "Invalid registration from %s: parsed=%s raw=%s",
                    addr,
                    parsed,
                    data.hex(),
                )
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Scapy-based intercept & forward
# ---------------------------------------------------------------------------
def make_forwarder(store: LMEPStore, source_ip: str, vxlan_port: int, iface: str):
    """Return a Scapy packet callback that translates and forwards via VXLAN."""

    def translate_and_forward(packet):
        if Ether not in packet:
            return

        dst_mac = packet[Ether].dst.lower()
        entry = store.lookup(dst_mac)
        if entry is None:
            return

        vni = entry.vni
        vxlan_header = Raw(
            b"\x08\x00\x00\x00" + vni.to_bytes(3, "big") + b"\x00"
        )
        outer_udp = UDP(sport=12345, dport=vxlan_port)
        outer_ip = IP(src=source_ip, dst=entry.vtep_ip)
        outer_eth = Ether(src="00:11:22:33:44:55", dst="ff:ff:ff:ff:ff:ff")

        vxlan_packet = outer_eth / outer_ip / outer_udp / vxlan_header / packet[Ether]

        sendp(vxlan_packet, iface=iface, verbose=False)
        logging.info(
            "Forwarded packet dst_mac=%s -> VTEP %s (vni=%s)",
            dst_mac,
            entry.vtep_ip,
            vni,
        )

    return translate_and_forward


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone LMEP mapping server (binary TLV + Scapy)"
    )
    parser.add_argument(
        "--bind-host",
        default="0.0.0.0",
        help="Address to bind the UDP registration listener to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6000,
        help="UDP port for binary TLV registrations (default: 6000)",
    )
    parser.add_argument(
        "--iface",
        default="eth0",
        help="Network interface for Scapy sniffing and forwarding (default: eth0)",
    )
    parser.add_argument(
        "--vxlan-port",
        type=int,
        default=VXLAN_UDP_PORT,
        help="VXLAN UDP destination port for forwarded packets (default: 4789)",
    )
    parser.add_argument(
        "--source-ip",
        default="192.168.1.1",
        help="Outer source IP for VXLAN encapsulated packets (default: 192.168.1.1)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    store = LMEPStore()
    stop_event = threading.Event()

    # Start the UDP registration listener in a background thread
    reg_thread = threading.Thread(
        target=listen_for_registrations,
        args=(args.bind_host, args.port, store, stop_event),
        daemon=True,
    )
    reg_thread.start()

    forwarder = make_forwarder(store, args.source_ip, args.vxlan_port, args.iface)

    logging.info(
        "LMEP server ready: registration=udp://%s:%s iface=%s vxlan_port=%s source_ip=%s",
        args.bind_host,
        args.port,
        args.iface,
        args.vxlan_port,
        args.source_ip,
    )

    # Scapy sniff runs in the main thread (blocking)
    try:
        sniff(iface=args.iface, filter="ip", prn=forwarder, store=False)
    except KeyboardInterrupt:
        stop_event.set()
        logging.info("Stopping LMEP server")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
