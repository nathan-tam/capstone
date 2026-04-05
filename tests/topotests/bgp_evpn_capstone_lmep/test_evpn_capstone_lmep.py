#!/usr/bin/env python
# -*- coding: utf-8 eval: (blacken-mode 1) -*-
# SPDX-License-Identifier: ISC
#
# test_evpn_capstone_lmep.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2017 by
# Network Device Education Foundation, Inc. ("NetDEF")
#

"""Topotest for LMEP: Layer-2 Mapping & Encapsulation Protocol.

Simulates endpoint mobility across a 7-VTEP spine-leaf fabric, with the
LMEP Mapping Server handling MAC-to-VTEP registration via binary TLV over
UDP and Scapy-based packet forwarding.
"""

import os
import sys
import struct
import random
import socket
import shlex
import subprocess
import threading
from time import sleep, time, strftime, localtime
import platform
import pytest
import requests

mininet_lock = threading.Lock()

# Save the Current Working Directory to find configuration files.
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
# Import topogen and topotest helpers
from lib import topotest
from lib.topogen import Topogen, TopoRouter, get_topogen
from lib.topolog import logger
from debug_tools import verify_ping

# pytest module level markers
pytestmark = [
    pytest.mark.bgpd,
    pytest.mark.pimd,
]


#####################################################
##   Visualizer Configuration
#####################################################

VISUALIZER_URL = "http://127.0.0.1:5000/event"
VISUALIZER_HEALTH_URL = "http://127.0.0.1:5000/health"
PACKET_CHART_URL = os.getenv("PACKET_CHART_URL", "http://127.0.0.1:5000/packet-chart")


def env_flag(name, default="false"):
    """Parse a bool-like environment variable with safe defaults."""
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


ENABLE_LIVE_PACKET_GRAPH = env_flag("ENABLE_LIVE_PACKET_GRAPH", "true")
AUTO_OPEN_PACKET_CHART_WINDOW = env_flag("AUTO_OPEN_PACKET_CHART_WINDOW", "false")
AUTO_START_PACKET_CHART_SERVER = env_flag("AUTO_START_PACKET_CHART_SERVER", "false")

try:
    PACKET_SAMPLE_INTERVAL_SECONDS = max(
        0.2,
        float(os.getenv("PACKET_SAMPLE_INTERVAL_SECONDS", "1.0")),
    )
except ValueError:
    PACKET_SAMPLE_INTERVAL_SECONDS = 1.0

_LOCAL_VISUALIZER_PROCESS = None


def send_vis_event(action, **kwargs):
    """Safely send events to the visualizer server."""
    try:
        payload = {"action": action}
        payload.update(kwargs)
        requests.post(VISUALIZER_URL, json=payload, timeout=0.05)
    except Exception:
        pass


def visualizer_is_reachable():
    """Return True when the local visualizer HTTP server is responding."""
    try:
        response = requests.get(VISUALIZER_HEALTH_URL, timeout=0.2)
        return response.status_code == 200
    except Exception:
        return False


def maybe_start_local_visualizer_server():
    """Start visualizer_server.py when packet graph mode is enabled and no server is running."""
    if not (ENABLE_LIVE_PACKET_GRAPH and AUTO_START_PACKET_CHART_SERVER):
        return None
    if visualizer_is_reachable():
        return None

    server_script = os.path.join(CWD, "visualizer_server.py")
    if not os.path.exists(server_script):
        return None

    try:
        process = subprocess.Popen(
            [sys.executable, server_script],
            cwd=CWD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as error:
        print(f"WARNING: failed to start local visualizer server: {error}")
        return None

    deadline = time() + 4.0
    while time() < deadline:
        if visualizer_is_reachable():
            print("Started local visualizer server for packet chart")
            return process
        sleep(0.2)

    print("WARNING: visualizer server did not become reachable in time")
    return process


def maybe_open_packet_chart_window():
    """Open the standalone packet chart in a browser window/tab."""
    if not (ENABLE_LIVE_PACKET_GRAPH and AUTO_OPEN_PACKET_CHART_WINDOW):
        return

    system_name = platform.system()
    command_candidates = []

    if system_name == "Darwin":
        command_candidates = [
            ["open", "-n", PACKET_CHART_URL],
            ["open", PACKET_CHART_URL],
        ]
    elif system_name == "Linux":
        command_candidates = [["xdg-open", PACKET_CHART_URL]]
    elif system_name == "Windows":
        command_candidates = [["cmd", "/c", "start", "", PACKET_CHART_URL]]

    for command in command_candidates:
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"Live packet chart opened at: {PACKET_CHART_URL}")
            return
        except Exception:
            continue

    print(f"WARNING: unable to auto-open packet chart URL: {PACKET_CHART_URL}")


def parse_packet_count(packet_count):
    """Return an integer packet count when available; otherwise None."""
    try:
        return int(packet_count)
    except (TypeError, ValueError):
        return None


def start_live_packet_sampler(captures):
    """Stream live packet totals to the chart page while tcpdump is running."""
    if not ENABLE_LIVE_PACKET_GRAPH:
        return None, None

    interval = PACKET_SAMPLE_INTERVAL_SECONDS
    stop_event = threading.Event()
    state = {"last_total": 0}

    send_vis_event("PACKET_SAMPLE_RESET")

    def emit_one_sample():
        counts = {}
        total_packets = 0

        for capture_name, (node, capture_file) in captures.items():
            count_value = parse_packet_count(get_pcap_packet_count(node, capture_file))
            counts[capture_name] = count_value
            if count_value is not None:
                total_packets += count_value

        delta_packets = max(0, total_packets - state["last_total"])
        state["last_total"] = total_packets

        send_vis_event(
            "PACKET_SAMPLE",
            ts=strftime("%H:%M:%S", localtime()),
            total_packets=total_packets,
            delta_packets=delta_packets,
            counts=counts,
        )

    def sampler_loop():
        emit_one_sample()
        while not stop_event.wait(interval):
            emit_one_sample()

    thread = threading.Thread(target=sampler_loop, name="packet-sampler", daemon=True)
    thread.start()
    return stop_event, thread


#####################################################
##   Configuration — Scaling Parameters
#####################################################

NUM_VTEPS = 7
NUM_HOSTS = 7  # One host per VTEP

try:
    NUM_MOBILE_VMS = max(1, int(os.getenv("NUM_MOBILE_VMS", "30")))
except ValueError:
    NUM_MOBILE_VMS = 30

# Migration tuning — matches bgp_evpn_capstone_asym defaults.
try:
    MOBILITY_OVERLAP_SECONDS = max(0.0, float(os.getenv("MOBILITY_OVERLAP_SECONDS", "0.2")))
except ValueError:
    MOBILITY_OVERLAP_SECONDS = 0.2

try:
    MIGRATION_BATCH_SIZE = max(1, int(os.getenv("MIGRATION_BATCH_SIZE", "5")))
except ValueError:
    MIGRATION_BATCH_SIZE = 5

try:
    MIGRATION_REPEAT_COUNT = max(1, int(os.getenv("MIGRATION_REPEAT_COUNT", "5")))
except ValueError:
    MIGRATION_REPEAT_COUNT = 5

try:
    MIGRATION_BATCH_SETTLE_SECONDS = max(
        0.0,
        float(os.getenv("MIGRATION_BATCH_SETTLE_SECONDS", "0.6")),
    )
except ValueError:
    MIGRATION_BATCH_SETTLE_SECONDS = 0.6

# Hold time after each batch for reachability pings.
try:
    REACHABILITY_HOLD_SECONDS = max(0.0, float(os.getenv("REACHABILITY_HOLD_SECONDS", "2.0")))
except ValueError:
    REACHABILITY_HOLD_SECONDS = 2.0

# Static ping source — lives on host1 for the duration of the test.
# This does NOT restrict vtep1 from mobility; it's only a ping source.
CONTROLLER_ENDPOINT_HOST = "host1"
CONTROLLER_ENDPOINT_IFACE = "controller"
CONTROLLER_ENDPOINT_IP = "192.168.100.254/16"
CONTROLLER_ENDPOINT_MAC = "00:aa:bb:dd:00:01"


#####################################################
##   LMEP Server Configuration
#####################################################

def _default_lmep_server_host():
    return "127.0.0.1"


LMEP_SERVER_HOST = os.environ.get("LMEP_SERVER_HOST", _default_lmep_server_host())
LMEP_PORT = int(os.environ.get("LMEP_PORT", "6000"))
LMEP_VXLAN_PORT = int(os.environ.get("LMEP_VXLAN_PORT", "4789"))
LMEP_DEFAULT_VNI = int(os.environ.get("LMEP_VNI", "1000"))

# TLV type constants for binary LMEP registration (per LMEP Standard)
MAC_REGISTER_TYPE = 0x01
CLIENT_IP_TYPE = 0x02
VNI_TYPE = 0x03
VTEP_IP_TYPE = 0x04


def _send_lmep_registration_udp(server_host, server_port, mac, client_ip, vni, vtep_ip):
    """Send a binary TLV MAC registration to the external LMEP server over UDP.

    Builds a packet conforming to the LMEP Standard TLV format:
      Type 0x01 (6 bytes)  - Client MAC
      Type 0x02 (4 bytes)  - Client IP
      Type 0x03 (3 bytes)  - VNI
      Type 0x04 (4 bytes)  - VTEP IP

    The packet is sent from the host test process (not from a Topotest
    namespace) so the LMEP daemon remains external and independent.
    """
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    tlv_mac = struct.pack("!BB", MAC_REGISTER_TYPE, 6) + mac_bytes

    ip_bytes = socket.inet_aton(client_ip.split("/")[0])
    tlv_ip = struct.pack("!BB", CLIENT_IP_TYPE, 4) + ip_bytes

    tlv_vni = struct.pack("!BB", VNI_TYPE, 3) + vni.to_bytes(3, "big")

    vtep_bytes = socket.inet_aton(vtep_ip)
    tlv_vtep = struct.pack("!BB", VTEP_IP_TYPE, 4) + vtep_bytes

    packet = tlv_mac + tlv_ip + tlv_vni + tlv_vtep

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(packet, (server_host, server_port))


def send_lmep_registration(server_host, mac, client_ip, vni, vtep_ip, port=LMEP_PORT):
    """Send a binary TLV LMEP registration for a VM endpoint."""
    _send_lmep_registration_udp(server_host, port, mac, client_ip, vni, vtep_ip)

    # Visualizer hook: animate a registration flow to the LMEP server.
    # Derive the source host from the VTEP IP for visual clarity.
    vtep_name = None
    for vname, vip in vtep_ips.items():
        if vip == vtep_ip:
            vtep_name = vname
            break
    source = vtep_name if vtep_name else "unknown"
    send_vis_event("LMEP_REGISTER", source=source, vm=mac)


#####################################################
##   Computed Addressing
#####################################################

# VTEP loopback IPs: vtep1=10.10.10.10, vtep2=20.20.20.20, ...
vtep_ips = {
    f"vtep{i}": f"{i*10}.{i*10}.{i*10}.{i*10}"
    for i in range(1, NUM_VTEPS + 1)
}


def compute_svi_ip(vtep_index):
    """Compute the SVI IP for a VTEP."""
    if vtep_index <= 5:
        return f"192.168.0.{250 + vtep_index}"
    return f"192.168.200.{vtep_index - 5}"


svi_ips = {
    f"vtep{i}": compute_svi_ip(i)
    for i in range(1, NUM_VTEPS + 1)
}


def vtep_name_from_index(vtep_index):
    return f"vtep{vtep_index}"


def host_to_vtep_index(host_index):
    """Hosts are attached round-robin to VTEPs in build_topo()."""
    return ((host_index - 1) % NUM_VTEPS) + 1


def get_mobility_vtep_indices():
    """All VTEPs participate in mobility (no controller VTEP exclusion)."""
    return list(range(1, NUM_VTEPS + 1))


def get_mobility_host_indices():
    """All hosts participate in mobility."""
    return list(range(1, NUM_HOSTS + 1))


#####################################################
##   Network Plumbing Helpers
#####################################################

def config_bond(node, bond_name, bond_members, bond_ad_sys_mac, br):
    """Set up Linux bonds on VTEPs and hosts for MH."""
    node.run("ip link add dev %s type bond mode 802.3ad" % bond_name)
    node.run("ip link set dev %s type bond lacp_rate 1" % bond_name)
    node.run("ip link set dev %s type bond miimon 100" % bond_name)
    node.run("ip link set dev %s type bond xmit_hash_policy layer3+4" % bond_name)
    node.run("ip link set dev %s type bond min_links 1" % bond_name)
    node.run(
        "ip link set dev %s type bond ad_actor_system %s" % (bond_name, bond_ad_sys_mac)
    )

    for bond_member in bond_members:
        node.run("ip link set dev %s down" % bond_member)
        node.run("ip link set dev %s master %s" % (bond_member, bond_name))
        node.run("ip link set dev %s up" % bond_member)

    node.run("ip link set dev %s up" % bond_name)

    if br:
        node.run(" ip link set dev %s master %s" % (bond_name, br))
        node.run("/sbin/bridge link set dev %s priority 8" % bond_name)
        node.run("/sbin/bridge vlan del vid 1 dev %s" % bond_name)
        node.run("/sbin/bridge vlan del vid 1 untagged pvid dev %s" % bond_name)
        node.run("/sbin/bridge vlan add vid 1000 dev %s" % bond_name)
        node.run("/sbin/bridge vlan add vid 1000 untagged pvid dev %s" % bond_name)


def config_l2vni(node, svi_ip, vtep_ip):
    """Configure Linux bridge/VXLAN dataplane for L2VNI 1000 on one VTEP."""
    node.run("ip link add br1000 type bridge")
    node.run("ip addr add %s/16 dev br1000" % svi_ip)
    node.run("ip addr add 192.168.0.250/16 dev br1000")
    node.run("/sbin/sysctl net.ipv4.conf.br1000.arp_accept=1")

    node.run(
        "ip link add vni1000 type vxlan local %s dstport 4789 id 1000 nolearning"
        % vtep_ip
    )
    node.run("ip link set vni1000 master br1000 addrgenmode none")
    node.run("/sbin/bridge link set dev vni1000 learning off")
    node.run("ip link set vni1000 up")
    node.run("ip link set br1000 up")

    node.run("/sbin/bridge vlan del vid 1 dev vni1000")
    node.run("/sbin/bridge vlan del vid 1 untagged pvid dev vni1000")
    node.run("/sbin/bridge vlan add vid 1000 dev vni1000")
    node.run("/sbin/bridge vlan add vid 1000 untagged pvid dev vni1000")


def config_vtep(vtep_name, vtep, vtep_ip, svi_ip):
    """Configure host-facing bond plus VXLAN bridge on one EVPN VTEP."""
    config_l2vni(vtep, svi_ip, vtep_ip)

    vtep_index = int(vtep_name.split("vtep")[1])
    sys_mac = "44:38:39:ff:{:02x}:{:02x}".format(
        (vtep_index >> 8) & 0xFF,
        vtep_index & 0xFF,
    )

    bond_member = vtep_name + "-eth2"
    config_bond(vtep, "hostbond1", [bond_member], sys_mac, "br1000")


def config_vteps(tgen, vteps):
    for vtep_name in vteps:
        vtep = tgen.gears[vtep_name]
        config_vtep(vtep_name, vtep, vtep_ips.get(vtep_name), svi_ips.get(vtep_name))


def compute_host_ip_mac(host_name):
    host_id = int(host_name.split("host")[1])
    if host_id <= 127:
        fourth_octet = host_id
        third_octet = 0
    else:
        offset = host_id - 128
        third_octet = 1 + (offset // 254)
        fourth_octet = 1 + (offset % 254)

    host_ip = f"192.168.{third_octet}.{fourth_octet}/16"
    host_mac = "00:00:00:{:02x}:{:02x}:{:02x}".format(
        (host_id >> 16) & 0xFF, (host_id >> 8) & 0xFF, host_id & 0xFF
    )
    return host_ip, host_mac


def config_host(host_name, host):
    """Configure the host-side bonded uplink used to attach endpoints."""
    bond_members = [host_name + "-eth0"]
    bond_name = "vtepbond"
    config_bond(host, bond_name, bond_members, "00:00:00:00:00:00", None)

    host_ip, host_mac = compute_host_ip_mac(host_name)
    host.run("ip addr add %s dev %s" % (host_ip, bond_name))
    host.run("ip link set dev %s address %s" % (bond_name, host_mac))


def config_hosts(tgen, hosts):
    for host_name in hosts:
        host = tgen.gears[host_name]
        config_host(host_name, host)


#####################################################
##   MACVLAN Endpoint Management
#####################################################

def create_macvlan_endpoint(tgen, host_name, vm_name, ip, mac):
    """Create one MACVLAN endpoint on a host namespace."""
    host = tgen.gears[host_name]

    def run_checked(command, error_message):
        with mininet_lock:
            result = host.run(f"{command} >/dev/null 2>&1 && echo ok || echo failed").strip()
        assert result == "ok", error_message

    run_checked(
        f"ip link add link vtepbond name {vm_name} type macvlan mode bridge",
        f"failed to create MACVLAN {vm_name} on {host_name}",
    )
    run_checked(
        f"ip link set dev {vm_name} address {mac}",
        f"failed to set MAC for {vm_name} on {host_name}",
    )
    run_checked(
        f"ip addr add {ip} dev {vm_name}",
        f"failed to set IP for {vm_name} on {host_name}",
    )
    run_checked(
        f"ip link set dev {vm_name} up",
        f"failed to bring up {vm_name} on {host_name}",
    )

    # VISUALIZER HOOK: Add VM node and link it to the host.
    send_vis_event("ADD_NODE", id=vm_name, type="vm")
    send_vis_event("ADD_LINK", source=vm_name, target=host_name)


def delete_macvlan_endpoint(tgen, host_name, vm_name):
    """Delete one MACVLAN endpoint from a host namespace."""
    host = tgen.gears[host_name]
    with mininet_lock:
        result = host.run(f"ip link del {vm_name} >/dev/null 2>&1 && echo ok || echo failed").strip()
    assert result == "ok", f"failed to delete MACVLAN {vm_name} from {host_name}"

    # VISUALIZER HOOK: Remove the link between the VM and the old host.
    send_vis_event("DEL_LINK", source=vm_name, target=host_name)


def delete_macvlan_endpoint_if_exists(tgen, host_name, vm_name):
    """Delete a MACVLAN endpoint if present, ignoring missing-interface cases."""
    host = tgen.gears[host_name]
    with mininet_lock:
        host.run(f"ip link show {vm_name} >/dev/null 2>&1 && ip link del {vm_name} || true")


def macvlan_endpoint_exists(tgen, host_name, vm_name):
    """Return True when endpoint interface exists on the host."""
    host = tgen.gears[host_name]
    with mininet_lock:
        result = host.run(
            "ip link show {0} >/dev/null 2>&1 && echo present || echo missing".format(
                shlex.quote(vm_name)
            )
        ).strip()
    return result == "present"


def create_controller_endpoint(tgen):
    """Create the static controller endpoint used as a ping source."""
    delete_macvlan_endpoint_if_exists(
        tgen, CONTROLLER_ENDPOINT_HOST, CONTROLLER_ENDPOINT_IFACE
    )
    create_macvlan_endpoint(
        tgen,
        CONTROLLER_ENDPOINT_HOST,
        CONTROLLER_ENDPOINT_IFACE,
        CONTROLLER_ENDPOINT_IP,
        CONTROLLER_ENDPOINT_MAC,
    )


def ping_batch_vms(tgen, migration_batch, vm_locations):
    """Ping each VM in the batch from the controller asynchronously to verify reachability.

    Waits REACHABILITY_HOLD_SECONDS for EVPN convergence in the background,
    then sends one ping per VM. Returns a Thread object.
    """
    def ping_task():
        if REACHABILITY_HOLD_SECONDS > 0:
            sleep(REACHABILITY_HOLD_SECONDS)

        for migration in migration_batch:
            vm_name = migration["vm_name"]
            vm_idx = int(vm_name.replace("vm", ""))
            target_ip = f"192.168.100.{vm_idx}"
            host_idx = vm_locations[vm_name][0]
            host_name = f"host{host_idx}"

            with mininet_lock:
                ok = verify_ping(
                    tgen,
                    CONTROLLER_ENDPOINT_HOST,
                    CONTROLLER_ENDPOINT_IFACE,
                    target_ip,
                    count=1,
                    timeout_seconds=1,
                )

            ok_str = "OK" if ok else "FAILED"
            print(
                f"    [Background] Ping {vm_name} ({target_ip}) on {host_name} "
                f"from {CONTROLLER_ENDPOINT_IFACE}@{CONTROLLER_ENDPOINT_HOST} ... {ok_str}",
                flush=True,
            )

            if ok:
                send_vis_event(
                    "PACKET_FLOW",
                    packet_type="PING",
                    source=CONTROLLER_ENDPOINT_IFACE,
                    target=vm_name,
                    vm=vm_name,
                )

    thread = threading.Thread(target=ping_task, daemon=True)
    thread.start()
    return thread




#####################################################
##   Migration Planning
#####################################################

def build_vm_migration_plan(vm_idx, vm_locations, mobility_vtep_indices, mobility_host_indices):
    """Compute source/destination placement and addressing for one VM migration."""
    vm_name = f"vm{vm_idx}"
    old_host_idx, old_vtep_idx = vm_locations[vm_name]
    old_host_name = f"host{old_host_idx}"

    # Destination is the next mobility-eligible VTEP.
    current_pos = mobility_vtep_indices.index(old_vtep_idx)
    new_vtep_idx = mobility_vtep_indices[(current_pos + 1) % len(mobility_vtep_indices)]

    # Prefer a different host than source.
    new_host_idx = None
    for potential_host in mobility_host_indices:
        if host_to_vtep_index(potential_host) == new_vtep_idx and potential_host != old_host_idx:
            new_host_idx = potential_host
            break

    if new_host_idx is None:
        for potential_host in mobility_host_indices:
            if host_to_vtep_index(potential_host) == new_vtep_idx:
                new_host_idx = potential_host
                break

    assert new_host_idx is not None, f"No destination host found for VTEP index {new_vtep_idx}"

    vm_ip = f"192.168.100.{vm_idx}/16"
    vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)

    return {
        "vm_name": vm_name,
        "old_host_name": old_host_name,
        "old_vtep_idx": old_vtep_idx,
        "old_vtep_name": vtep_name_from_index(old_vtep_idx),
        "new_host_name": f"host{new_host_idx}",
        "new_host_idx": new_host_idx,
        "new_vtep_idx": new_vtep_idx,
        "new_vtep_name": vtep_name_from_index(new_vtep_idx),
        "vm_ip": vm_ip,
        "vm_mac": vm_mac,
    }


def migrate_macvlan_endpoints_live_batch(tgen, migration_batch):
    """Move a batch by creating all destinations first, then deleting all sources.

    For each migrated VM, an LMEP binary TLV registration is sent to the
    external Mapping Server so it updates its MAC-to-VTEP table.
    """
    created_destinations = []

    try:
        for migration in migration_batch:
            create_macvlan_endpoint(
                tgen,
                migration["new_host_name"],
                migration["vm_name"],
                migration["vm_ip"],
                migration["vm_mac"],
            )

            if not macvlan_endpoint_exists(
                tgen,
                migration["new_host_name"],
                migration["vm_name"],
            ):
                raise AssertionError(
                    f"destination endpoint {migration['vm_name']} missing on {migration['new_host_name']} after create"
                )

            created_destinations.append(
                (migration["new_host_name"], migration["vm_name"])
            )

            # Send LMEP binary TLV registration to update the Mapping Server.
            new_vtep_ip = vtep_ips[migration["new_vtep_name"]]
            try:
                send_lmep_registration(
                    LMEP_SERVER_HOST,
                    migration["vm_mac"],
                    migration["vm_ip"],
                    LMEP_DEFAULT_VNI,
                    new_vtep_ip,
                )
            except OSError as exc:
                logger.warning(
                    "LMEP registration failed for %s -> %s: %s",
                    migration["vm_name"],
                    new_vtep_ip,
                    exc,
                )
    except Exception as error:
        # Roll back already-created destination endpoints on failure.
        if created_destinations:
            print(
                "WARNING: batch migration destination create failed; "
                f"rolling back {len(created_destinations)} created endpoints. "
                f"Error: {error}"
            )
            for host_name, vm_name in reversed(created_destinations):
                delete_macvlan_endpoint_if_exists(tgen, host_name, vm_name)
        raise

    sleep(MOBILITY_OVERLAP_SECONDS)

    for migration in migration_batch:
        delete_macvlan_endpoint(
            tgen,
            migration["old_host_name"],
            migration["vm_name"],
        )
        if macvlan_endpoint_exists(
            tgen,
            migration["old_host_name"],
            migration["vm_name"],
        ):
            print(
                "WARNING: source endpoint still present after delete; forcing cleanup for "
                f"{migration['vm_name']} on {migration['old_host_name']}"
            )
            delete_macvlan_endpoint_if_exists(
                tgen,
                migration["old_host_name"],
                migration["vm_name"],
            )


#####################################################
##   Packet Capture Helpers
#####################################################

def get_pcap_packet_count(node, file_path):
    """Return packet count, or 'missing' when the pcap does not exist."""
    path = shlex.quote(file_path)
    return node.run(
        "if [ -f {0} ]; then tcpdump -nr {0} 2>/dev/null | wc -l; else echo missing; fi".format(
            path
        )
    ).strip()


def get_pcap_mp_nlri_counts(node, file_path):
    """Count MP_REACH_NLRI and MP_UNREACH_NLRI messages in a capture using tshark."""
    path = shlex.quote(file_path)

    exists = node.run("[ -f {} ] && echo yes || echo no".format(path)).strip()
    if exists != "yes":
        return "missing"

    has_tshark = node.run("command -v tshark >/dev/null 2>&1 && echo yes || echo no").strip()
    if has_tshark != "yes":
        return "tshark-not-found"

    mp_reach = node.run(
        "tshark -r {0} -Y 'bgp.update.path_attribute.type_code == 14' 2>/dev/null | wc -l".format(
            path
        )
    ).strip()
    mp_unreach = node.run(
        "tshark -r {0} -Y 'bgp.update.path_attribute.type_code == 15' 2>/dev/null | wc -l".format(
            path
        )
    ).strip()
    both = node.run(
        "tshark -r {0} -Y 'bgp.update.path_attribute.type_code == 14 && "
        "bgp.update.path_attribute.type_code == 15' 2>/dev/null | wc -l".format(path)
    ).strip()

    try:
        return {
            "mp_reach": int(mp_reach),
            "mp_unreach": int(mp_unreach),
            "both": int(both),
        }
    except ValueError:
        return {"mp_reach": mp_reach, "mp_unreach": mp_unreach, "both": both}


def format_nlri_counts(counts):
    """Format NLRI counts for logs."""
    if isinstance(counts, str):
        return counts
    total_nlri = counts["mp_reach"] + counts["mp_unreach"] - counts["both"]
    return "total={} (reach={}, unreach={}, both={})".format(
        total_nlri,
        counts["mp_reach"],
        counts["mp_unreach"],
        counts["both"],
    )


def start_bgp_capture(node, pcap_file, pid_file):
    """Start a detached tcpdump capture for BGP control-plane traffic."""
    node.run(
        "tcpdump -nni any -s 0 -w {} port 179 2>/dev/null & echo $! > {}".format(
            shlex.quote(pcap_file),
            shlex.quote(pid_file),
        ),
        stdout=None,
    )


def stop_bgp_capture(node, pid_file):
    """Stop a detached tcpdump capture if it is running."""
    node.run(
        "if [ -f {0} ]; then kill $(cat {0}) 2>/dev/null || true; fi".format(
            shlex.quote(pid_file)
        )
    )


#####################################################
##   Network Topology Definition
#####################################################

def build_topo(tgen):
    """Build a 2-spine, N-VTEP, N-host Clos-style topology."""

    spine1 = tgen.add_router("spine1")
    spine2 = tgen.add_router("spine2")
    send_vis_event("ADD_NODE", id="spine1", type="spine")
    send_vis_event("ADD_NODE", id="spine2", type="spine")

    # Create VTEPs and connect to both spines.
    for i in range(1, NUM_VTEPS + 1):
        vtep_name = f"vtep{i}"
        tgen.add_router(vtep_name)
        send_vis_event("ADD_NODE", id=vtep_name, type="vtep")

        tgen.add_link(spine1, tgen.gears[vtep_name])
        send_vis_event("ADD_LINK", source="spine1", target=vtep_name)

        tgen.add_link(spine2, tgen.gears[vtep_name])
        send_vis_event("ADD_LINK", source="spine2", target=vtep_name)

    # Create hosts and distribute them across VTEPs.
    for i in range(1, NUM_HOSTS + 1):
        host_name = f"host{i}"
        tgen.add_router(host_name)
        send_vis_event("ADD_NODE", id=host_name, type="host")

        vtep_idx = (i - 1) % NUM_VTEPS
        vtep_name = f"vtep{vtep_idx + 1}"
        tgen.add_link(tgen.gears[vtep_name], tgen.gears[host_name])
        send_vis_event("ADD_LINK", source=vtep_name, target=host_name)

    # Add the external LMEP Mapping Server as a visualizer-only node.
    send_vis_event("ADD_NODE", id="lmep_server", type="lmep_server")


@pytest.fixture(scope="module")
def tgen(request):
    """Setup/Teardown the environment and provide tgen argument to tests."""

    global _LOCAL_VISUALIZER_PROCESS

    # Start the visualizer server before topology build so it captures ADD_NODE events.
    _LOCAL_VISUALIZER_PROCESS = maybe_start_local_visualizer_server()
    maybe_open_packet_chart_window()

    tgen = Topogen(build_topo, request.module.__name__)
    tgen.start_topology()

    krel = platform.release()
    if topotest.version_cmp(krel, "4.19") < 0:
        tgen.errors = "kernel 4.19 needed for multihoming tests"
        pytest.skip(tgen.errors)

    # Configure all VTEPs
    vteps = [f"vtep{i}" for i in range(1, NUM_VTEPS + 1)]
    config_vteps(tgen, vteps)

    # Configure all hosts
    hosts = [f"host{i}" for i in range(1, NUM_HOSTS + 1)]
    config_hosts(tgen, hosts)

    router_list = tgen.routers()
    for rname, router in router_list.items():
        router.load_config(
            TopoRouter.RD_ZEBRA, os.path.join(CWD, "{}/zebra.conf".format(rname))
        )
        router.load_config(
            TopoRouter.RD_BGP, os.path.join(CWD, "{}/evpn.conf".format(rname))
        )
    tgen.start_router()

    yield tgen

    # Suppress the memory allocation report during topology teardown.
    original_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        tgen.stop_topology()
    finally:
        sys.stderr.close()
        sys.stderr = original_stderr
        if _LOCAL_VISUALIZER_PROCESS is not None:
            try:
                _LOCAL_VISUALIZER_PROCESS.terminate()
                _LOCAL_VISUALIZER_PROCESS.wait(timeout=2)
            except Exception:
                pass


@pytest.fixture(autouse=True)
def skip_on_failure(tgen):
    if tgen.routers_have_failure():
        pytest.skip("skipped because of previous test failure")


#####################################################
##   Test Functions
#####################################################

def test_host_movement(tgen):
    """Simulate endpoint mobility across VTEPs with LMEP registration.

    All 7 VTEPs participate in mobility (no controller VTEP exclusion).
    At each migration step, a binary TLV LMEP registration is sent to the
    external Mapping Server.

    Steps:
    1. Deploy mobile VMs distributed across all hosts
    2. Live-migrate the full VM set for MIGRATION_REPEAT_COUNT rounds
       (brief duplicate-MAC window with LMEP registration at each move)
    3. Capture BGP packet data during migrations
    """
    if tgen.routers_have_failure():
        pytest.skip(f"skipped because of previous test failure\n {tgen.errors}")

    mobility_vtep_indices = get_mobility_vtep_indices()
    mobility_host_indices = get_mobility_host_indices()

    assert mobility_vtep_indices, "No mobility-eligible VTEPs are configured"
    assert len(mobility_vtep_indices) >= 2, "Need at least two mobility-eligible VTEPs"
    assert mobility_host_indices, "No mobility-eligible hosts are available"

    # Fail fast if the LMEP server is not reachable.
    try:
        _send_lmep_registration_udp(
            LMEP_SERVER_HOST, LMEP_PORT,
            "ff:ff:ff:ff:ff:ff", "0.0.0.0", 0, "0.0.0.0"
        )
    except OSError as exc:
        pytest.fail(
            f"LMEP server unreachable at {LMEP_SERVER_HOST}:{LMEP_PORT}: {exc}\n"
            "Start lmep_server.py or set LMEP_SERVER_HOST/LMEP_PORT."
        )

    #####################################################
    # SECTION: Packet Capture Setup
    #####################################################
    spine1 = tgen.gears["spine1"]
    vtep2 = tgen.gears["vtep2"]
    vtep3 = tgen.gears["vtep3"]

    pcap_plan = {
        "spine1": (
            spine1,
            os.path.join(tgen.logdir, "spine1", "spine1_lmep_mobility.pcap"),
            "/tmp/tcpdump_lmep_spine1.pid",
        ),
        "vtep1": (
            tgen.gears["vtep1"],
            os.path.join(tgen.logdir, "vtep1", "vtep1_lmep_mobility.pcap"),
            "/tmp/tcpdump_lmep_vtep1.pid",
        ),
        "vtep2": (
            vtep2,
            os.path.join(tgen.logdir, "vtep2", "vtep2_lmep_mobility.pcap"),
            "/tmp/tcpdump_lmep_vtep2.pid",
        ),
        "vtep3": (
            vtep3,
            os.path.join(tgen.logdir, "vtep3", "vtep3_lmep_mobility.pcap"),
            "/tmp/tcpdump_lmep_vtep3.pid",
        ),
    }

    print("\nStarting BGP control-plane captures for LMEP test...")
    print(f"Mobility overlap timer: {MOBILITY_OVERLAP_SECONDS:.3f}s")
    print(f"Migration batch size: {MIGRATION_BATCH_SIZE}")
    print(f"Migration repeat count: {MIGRATION_REPEAT_COUNT}")
    print(f"Batch settle timer: {MIGRATION_BATCH_SETTLE_SECONDS:.3f}s")

    for capture_name, (node, pcap_file, pid_file) in pcap_plan.items():
        start_bgp_capture(node, pcap_file, pid_file)
        print(f"  {capture_name} capture: {pcap_file}")

    sleep(1)

    # Start live packet sampler for the visualizer chart.
    packet_sampler_stop = None
    packet_sampler_thread = None
    pcap_sampler_captures = {
        name: (node, pcap_file)
        for name, (node, pcap_file, _) in pcap_plan.items()
    }
    packet_sampler_stop, packet_sampler_thread = start_live_packet_sampler(
        pcap_sampler_captures
    )

    # Create a static controller endpoint on host1 as the ping source.
    create_controller_endpoint(tgen)

    try:

        #####################################################
        # SECTION: Mobility Simulation
        #####################################################
        print(f"\n=== Starting LMEP Mobility Test with {NUM_MOBILE_VMS} VMs ===\n")

        # Track VM locations: {vm_name: (current_host_idx, current_vtep_idx)}
        vm_locations = {}
        ping_threads = []

        # --- Phase 1: Deploy VMs on initial hosts ---
        print(f"Phase 1: Deploying {NUM_MOBILE_VMS} VMs on hosts...")

        for vm_idx in range(1, NUM_MOBILE_VMS + 1):
            vm_name = f"vm{vm_idx}"
            vm_ip = f"192.168.100.{vm_idx}/16"
            vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)

            host_idx = mobility_host_indices[(vm_idx - 1) % len(mobility_host_indices)]
            host_name = f"host{host_idx}"
            vtep_idx = host_to_vtep_index(host_idx)

            create_macvlan_endpoint(tgen, host_name, vm_name, vm_ip, vm_mac)
            vm_locations[vm_name] = (host_idx, vtep_idx)

            # Send initial LMEP registration.
            vtep_ip = vtep_ips[vtep_name_from_index(vtep_idx)]
            try:
                send_lmep_registration(
                    LMEP_SERVER_HOST, vm_mac, vm_ip, LMEP_DEFAULT_VNI, vtep_ip
                )
            except OSError:
                pass  # Best-effort during initial deployment.

            if vm_idx % 5 == 0:
                sleep(1)

        sleep(5)

        # --- Phase 2: Post-deployment settle ---
        print("\nPhase 2: Initial deployment complete; proceeding to mobility...")

        # --- Phase 3: Migrate VMs to different VTEPs ---
        print(
            f"\nPhase 3: Moving {NUM_MOBILE_VMS} VMs to different locations "
            f"for {MIGRATION_REPEAT_COUNT} round(s)..."
        )

        for migration_round in range(1, MIGRATION_REPEAT_COUNT + 1):
            print(f"  Starting migration round {migration_round}/{MIGRATION_REPEAT_COUNT}")

            for batch_start in range(1, NUM_MOBILE_VMS + 1, MIGRATION_BATCH_SIZE):
                batch_end = min(batch_start + MIGRATION_BATCH_SIZE - 1, NUM_MOBILE_VMS)
                migration_batch = []

                for vm_idx in range(batch_start, batch_end + 1):
                    migration_batch.append(
                        build_vm_migration_plan(
                            vm_idx,
                            vm_locations,
                            mobility_vtep_indices,
                            mobility_host_indices,
                        )
                    )

                migrate_macvlan_endpoints_live_batch(tgen, migration_batch)

                for migration in migration_batch:
                    vm_locations[migration["vm_name"]] = (
                        migration["new_host_idx"],
                        migration["new_vtep_idx"],
                    )

                moved_count = batch_end
                if moved_count % 20 == 0 or moved_count == NUM_MOBILE_VMS:
                    print(
                        f"  Round {migration_round}/{MIGRATION_REPEAT_COUNT} progress: "
                        f"{moved_count}/{NUM_MOBILE_VMS}"
                    )

                if MIGRATION_BATCH_SETTLE_SECONDS > 0.0:
                    sleep(MIGRATION_BATCH_SETTLE_SECONDS)

                # Reachability check: hold VMs in place and ping from controller.
                # Runs asynchronously in a background thread.
                t = ping_batch_vms(tgen, migration_batch, vm_locations)
                ping_threads.append(t)

        print("  Waiting for background ping tasks to finish...")
        for t in ping_threads:
            t.join()

        sleep(2)

        # --- Phase 4: Post-migration ---
        print("\nPhase 4: Post-migration checks complete.")

    finally:
        if packet_sampler_stop is not None:
            packet_sampler_stop.set()
        if packet_sampler_thread is not None:
            packet_sampler_thread.join(timeout=2)

        # Clean up the controller endpoint.
        delete_macvlan_endpoint_if_exists(
            tgen, CONTROLLER_ENDPOINT_HOST, CONTROLLER_ENDPOINT_IFACE
        )

        print("\nStopping BGP captures and collecting packet metrics...")
        for _, (node, _, pid_file) in pcap_plan.items():
            stop_bgp_capture(node, pid_file)

        spine1.run("sleep 1")

        for capture_name, (node, pcap_file, _) in pcap_plan.items():
            packet_count = get_pcap_packet_count(node, pcap_file)
            nlri_counts = get_pcap_mp_nlri_counts(node, pcap_file)
            print(f"  {capture_name} BGP packets: {packet_count}")
            print(f"  {capture_name} MP_NLRI: {format_nlri_counts(nlri_counts)}")
            print()

        sleep(5)

    if os.getenv("MUNET_CLI") == "1":
        tgen.mininet_cli()


def test_get_version(tgen):
    """Sanity check that queries the EVPN MAC table."""
    r1 = tgen.gears["vtep1"]
    version = r1.vtysh_cmd("show evpn mac vni 1000")


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
