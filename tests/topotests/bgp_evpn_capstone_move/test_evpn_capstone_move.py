#!/usr/bin/env python
# -*- coding: utf-8 eval: (blacken-mode 1) -*-
# SPDX-License-Identifier: ISC
#
# test_evpn_capstone_move.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2017 by
# Network Device Education Foundation, Inc. ("NetDEF")
#

"""
Batch-based EVPN L2VNI endpoint mobility measurement.

Deploys a fixed pool of mobile VMs as MACVLANs, then moves batches of
increasing size via ``bridge fdb replace`` on VTEPs.  The relationship
between batch size and observed MP_REACH_NLRI count reveals how BGP
advertisement volume scales with move density.

Key differences from the tick-based simulation in test_evpn_capstone_asym:
  * Movement is via FDB manipulation (2 cmds/move) instead of MACVLAN
    lifecycle (8+ cmds/move).
  * Pcap captures start *after* initial deployment converges, so only
    move-related UPDATEs are measured.
  * Sweep axis is batch size (fixed VM pool) instead of VM count.
"""

import json
import os
import sys
import shlex
import random
import time
from collections import defaultdict
from time import sleep
import platform
import pytest

# Save the Current Working Directory to find configuration files.
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, CWD)  # Ensure same-directory imports (e.g. debug_tools) work.
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
# Import topogen and topotest helpers
from lib import topotest
from lib.topogen import Topogen, TopoRouter

# Import connectivity verification helpers.
from debug_tools import verify_post_migration_connectivity

# pytest module level markers
pytestmark = [
    pytest.mark.bgpd,
]

#####################################################
##   Configuration
#####################################################

# Test scaling parameters
NUM_VTEPS = 7
NUM_HOSTS = 7  # One host per VTEP for VM mobility testing

# Fixed pool of mobile VMs deployed once per measurement iteration.
try:
    NUM_POOL_VMS = max(1, int(os.getenv("NUM_POOL_VMS", "40")))
except ValueError:
    NUM_POOL_VMS = 40

# Controller VTEPs participate in topology/BGP but are excluded from endpoint mobility.
CONTROLLER_VTEPS = {"vtep1"}

# Static endpoint attached to the controller side (host on controller VTEP).
CONTROLLER_ENDPOINT_HOST = "host1"
CONTROLLER_ENDPOINT_IFACE = "controller"
CONTROLLER_ENDPOINT_IP = "192.168.100.254/16"
CONTROLLER_ENDPOINT_MAC = "00:aa:bb:dd:00:01"

# How long to wait after a batch move for BGP to converge (seconds).
# Should allow enough time for BGP UPDATE propagation across the topology.
try:
    CONVERGENCE_WAIT_SECONDS = max(1, int(os.getenv("CONVERGENCE_WAIT_SECONDS", "5")))
except ValueError:
    CONVERGENCE_WAIT_SECONDS = 5

# How long to wait after initial VM deployment for full BGP convergence.
try:
    INITIAL_CONVERGENCE_SECONDS = max(5, int(os.getenv("INITIAL_CONVERGENCE_SECONDS", "15")))
except ValueError:
    INITIAL_CONVERGENCE_SECONDS = 15

# Comma-separated batch sizes for the parametric scaling sweep.
_sweep_env = os.getenv("SWEEP_BATCH_SIZES", "1,5,10,20,40")
try:
    SWEEP_BATCH_SIZES = [max(1, int(x.strip())) for x in _sweep_env.split(",") if x.strip()]
except ValueError:
    SWEEP_BATCH_SIZES = [1, 5, 10, 20, 40]

# Reproducible RNG seed.  When unset a random seed is generated and logged so
# that any run can be replayed by setting SIMULATION_SEED=<value>.
SIMULATION_SEED = os.getenv("SIMULATION_SEED", None)


#####################################################
##   Pcap / tshark helpers
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
    """Use tshark to count MP_REACH and MP_UNREACH packets in a pcap."""
    path = shlex.quote(file_path)

    exists = node.run("[ -f {} ] && echo yes || echo no".format(path)).strip()
    if exists != "yes":
        return "missing"

    has_tshark = node.run("command -v tshark >/dev/null 2>&1 && echo yes || echo no").strip()
    if has_tshark != "yes":
        return "tshark-not-found"

    # Type 14: MP_REACH_NLRI
    mp_reach = node.run(
        "tshark -r {0} -Y 'bgp.update.path_attribute.type_code == 14' 2>/dev/null | wc -l".format(
            path
        )
    ).strip()

    # Type 15: MP_UNREACH_NLRI
    mp_unreach = node.run(
        "tshark -r {0} -Y 'bgp.update.path_attribute.type_code == 15' 2>/dev/null | wc -l".format(
            path
        )
    ).strip()

    # Count packets that carry both MP_REACH_NLRI (14) and MP_UNREACH_NLRI (15).
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


def macvlan_endpoint_exists(tgen, host_name, vm_name):
    """Return True when endpoint interface exists on the host."""
    host = tgen.gears[host_name]
    vm_name_quoted = shlex.quote(vm_name)
    result = host.run(
        "ip link show {0} >/dev/null 2>&1 && echo present || echo missing".format(
            vm_name_quoted
        )
    ).strip()
    return result == "present"


#####################################################
##   FDB-based movement engine
#####################################################

def _vm_mac(vm_idx):
    """Compute the deterministic MAC address for a given VM index."""
    return "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)


def move_batch_via_fdb(tgen, vm_indices, vm_locations, mobility_vtep_indices, rng):
    """Move multiple VMs in a single burst by manipulating bridge FDB entries.

    For each VM:
      1. Delete the static FDB entry from the old VTEP (if any).
      2. Add a static FDB entry on the new VTEP.

    The new VTEP's zebra notifies BGP → BGP advertises the MAC with an
    incremented MAC Mobility extended-community sequence number, which
    supersedes the old VTEP's advertisement.

    Returns a list of move descriptors.
    """
    plans = []
    for vm_idx in vm_indices:
        vm_name = f"vm{vm_idx}"
        _, old_vtep_idx = vm_locations[vm_name]

        candidates = [v for v in mobility_vtep_indices if v != old_vtep_idx]
        new_vtep_idx = rng.choice(candidates)

        plans.append({
            "vm_idx": vm_idx,
            "vm_name": vm_name,
            "vm_mac": _vm_mac(vm_idx),
            "old_vtep_idx": old_vtep_idx,
            "new_vtep_idx": new_vtep_idx,
        })

    # --- Delete old FDB entries (grouped by source VTEP) ---
    del_by_vtep = defaultdict(list)
    for p in plans:
        del_by_vtep[p["old_vtep_idx"]].append(p)

    for vtep_idx, vtep_plans in del_by_vtep.items():
        vtep = tgen.gears[vtep_name_from_index(vtep_idx)]
        cmds = [
            f"bridge fdb del {p['vm_mac']} dev hostbond1 master 2>/dev/null"
            for p in vtep_plans
        ]
        vtep.run(" ; ".join(cmds) + " ; true")

    # --- Add new FDB entries (grouped by destination VTEP) ---
    add_by_vtep = defaultdict(list)
    for p in plans:
        add_by_vtep[p["new_vtep_idx"]].append(p)

    for vtep_idx, vtep_plans in add_by_vtep.items():
        vtep = tgen.gears[vtep_name_from_index(vtep_idx)]
        cmds = [
            f"bridge fdb replace {p['vm_mac']} dev hostbond1 master static"
            for p in vtep_plans
        ]
        vtep.run(" && ".join(cmds))

    # --- Update tracking ---
    for p in plans:
        host_idx = vm_locations[p["vm_name"]][0]
        vm_locations[p["vm_name"]] = (host_idx, p["new_vtep_idx"])

    return plans


def cleanup_fdb_entries(tgen, vm_locations, num_pool_vms):
    """Remove all static FDB entries added during the measurement."""
    fdb_by_vtep = defaultdict(list)
    for vm_idx in range(1, num_pool_vms + 1):
        vm_name = f"vm{vm_idx}"
        if vm_name in vm_locations:
            _, vtep_idx = vm_locations[vm_name]
            fdb_by_vtep[vtep_idx].append(_vm_mac(vm_idx))

    for vtep_idx, macs in fdb_by_vtep.items():
        vtep = tgen.gears[vtep_name_from_index(vtep_idx)]
        cmds = [f"bridge fdb del {mac} dev hostbond1 master 2>/dev/null" for mac in macs]
        vtep.run(" ; ".join(cmds) + " ; true")


#####################################################
##   Network helpers (unchanged from original)
#####################################################

vtep_ips = {
    f"vtep{i}": f"192.168.100.{14 + i}"
    for i in range(1, NUM_VTEPS + 1)
}


def vtep_name_from_index(vtep_index):
    return f"vtep{vtep_index}"


def host_to_vtep_index(host_index):
    # Hosts are attached round-robin to VTEPs in build_topo().
    return ((host_index - 1) % NUM_VTEPS) + 1


def get_mobility_vtep_indices():
    return [
        i
        for i in range(1, NUM_VTEPS + 1)
        if vtep_name_from_index(i) not in CONTROLLER_VTEPS
    ]


def get_mobility_host_indices():
    mobility_vtep_indices = set(get_mobility_vtep_indices())
    return [
        host_idx
        for host_idx in range(1, NUM_HOSTS + 1)
        if host_to_vtep_index(host_idx) in mobility_vtep_indices
    ]


def compute_svi_ip(vtep_index):
    if vtep_index <= 5:
        return f"192.168.0.{250 + vtep_index}"
    return f"192.168.200.{vtep_index - 5}"


svi_ips = {
    f"vtep{i}": compute_svi_ip(i)
    for i in range(1, NUM_VTEPS + 1)
}


def config_bond(node, bond_name, bond_members, bond_ad_sys_mac, br):
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
    node.run("ip link add br1000 type bridge")
    node.run("ip addr add %s/16 dev br1000" % svi_ip)
    node.run("ip addr add 192.168.0.250/16 dev br1000")
    node.run("/sbin/sysctl net.ipv4.conf.br1000.arp_accept=1")
    node.run(
        "ip link add vni1000 type vxlan local %s dstport 4789 id 1000" % vtep_ip
    )
    node.run("ip link set vni1000 master br1000 addrgenmode none")
    node.run("/sbin/bridge link set dev vni1000 learning on")
    node.run("ip link set vni1000 up")
    node.run("ip link set br1000 up")
    node.run("/sbin/bridge vlan del vid 1 dev vni1000")
    node.run("/sbin/bridge vlan del vid 1 untagged pvid dev vni1000")
    node.run("/sbin/bridge vlan add vid 1000 dev vni1000")
    node.run("/sbin/bridge vlan add vid 1000 untagged pvid dev vni1000")


def config_vtep(vtep_name, vtep, vtep_ip, svi_ip):
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
##   Network Topology Definition
#####################################################

def build_topo(tgen):
    """Build a 2-spine, N-VTEP, N-host Clos-style topology."""

    spine1 = tgen.add_router("spine1")
    spine2 = tgen.add_router("spine2")

    vteps = []
    for i in range(1, NUM_VTEPS + 1):
        vtep_name = f"vtep{i}"
        vtep = tgen.add_router(vtep_name)
        vteps.append(vtep_name)
        tgen.add_link(spine1, vtep)
        tgen.add_link(spine2, vtep)

    hosts = []
    for i in range(1, NUM_HOSTS + 1):
        host_name = f"host{i}"
        host = tgen.add_router(host_name)
        hosts.append(host_name)
        vtep_idx = (i - 1) % NUM_VTEPS
        vtep_name = f"vtep{vtep_idx + 1}"
        vtep = tgen.gears[vtep_name]
        tgen.add_link(vtep, host)


@pytest.fixture(scope="module")
def tgen(request):
    """Set up topology/configuration and provide Topogen to tests."""
    tgen = Topogen(build_topo, request.module.__name__)
    tgen.start_topology()

    krel = platform.release()
    if topotest.version_cmp(krel, "4.19") < 0:
        tgen.errors = "kernel 4.19 needed for multihoming tests"
        pytest.skip(tgen.errors)

    vteps = [f"vtep{i}" for i in range(1, NUM_VTEPS + 1)]
    config_vteps(tgen, vteps)

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

    original_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        tgen.stop_topology()
    finally:
        sys.stderr.close()
        sys.stderr = original_stderr


@pytest.fixture(autouse=True)
def skip_on_failure(tgen):
    if tgen.routers_have_failure():
        pytest.skip("skipped because of previous test failure")


#####################################################
##   MACVLAN endpoint lifecycle
#####################################################

def create_macvlan_endpoint(tgen, host_name, vm_name, ip, mac):
    """Create one MACVLAN endpoint on a host namespace."""
    host = tgen.gears[host_name]
    print(f"Creating MACVLAN {vm_name} on {host_name} with IP {ip} MAC {mac}")

    def run_checked(command, error_message):
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


def delete_macvlan_endpoint(tgen, host_name, vm_name):
    """Delete one MACVLAN endpoint from a host namespace."""
    host = tgen.gears[host_name]
    print(f"Deleting MACVLAN {vm_name} from {host_name}")
    result = host.run(f"ip link del {vm_name} >/dev/null 2>&1 && echo ok || echo failed").strip()
    assert result == "ok", f"failed to delete MACVLAN {vm_name} from {host_name}"


def create_controller_endpoint(tgen):
    """Create the static controller endpoint on the controller-side host."""
    delete_macvlan_endpoint_if_exists(
        tgen,
        CONTROLLER_ENDPOINT_HOST,
        CONTROLLER_ENDPOINT_IFACE,
    )
    create_macvlan_endpoint(
        tgen,
        CONTROLLER_ENDPOINT_HOST,
        CONTROLLER_ENDPOINT_IFACE,
        CONTROLLER_ENDPOINT_IP,
        CONTROLLER_ENDPOINT_MAC,
    )


def delete_macvlan_endpoint_if_exists(tgen, host_name, vm_name):
    """Delete a MACVLAN endpoint if present, ignoring missing-interface cases."""
    host = tgen.gears[host_name]
    host.run(f"ip link show {vm_name} >/dev/null 2>&1 && ip link del {vm_name} || true")


#####################################################
##   RNG helper
#####################################################

def _make_rng():
    """Create a seeded Random instance; log/return the seed for reproducibility."""
    if SIMULATION_SEED is not None:
        seed = int(SIMULATION_SEED)
    else:
        seed = int.from_bytes(os.urandom(8), "big")
    print(f"RNG seed: {seed}  (replay with SIMULATION_SEED={seed})")
    return random.Random(seed), seed


#####################################################
##   Pcap capture management
#####################################################

def _start_pcap_captures(tgen, controller_vtep_name):
    """Start tcpdump on spine1, vtep2, vtep3, and the controller VTEP.

    Returns a dict of ``{label: (node, pcap_file, pidfile)}``.
    """
    captures = {}

    spine = tgen.gears["spine1"]
    pcap_dir = os.path.join(tgen.logdir, "spine1")
    pcap_file = os.path.join(pcap_dir, "spine1_evpn_mobility.pcap")
    spine.run("mkdir -p {}".format(shlex.quote(pcap_dir)))
    spine.run(
        "tcpdump -nni any -s 0 -w {} port 179 2>/dev/null & echo $! > /tmp/tcpdump_evpn.pid".format(
            shlex.quote(pcap_file)
        ),
        stdout=None,
    )
    captures["spine1"] = (spine, pcap_file, "/tmp/tcpdump_evpn.pid")

    controller_vtep = tgen.gears[controller_vtep_name]
    cdir = os.path.join(tgen.logdir, controller_vtep_name)
    cfile = os.path.join(cdir, f"{controller_vtep_name}_evpn_controller_mobility.pcap")
    controller_vtep.run("mkdir -p {}".format(shlex.quote(cdir)))
    controller_vtep.run(
        "tcpdump -nni any -s 0 -w {} port 179 2>/dev/null & echo $! > /tmp/tcpdump_evpn_controller.pid".format(
            shlex.quote(cfile)
        ),
        stdout=None,
    )
    captures[controller_vtep_name] = (controller_vtep, cfile, "/tmp/tcpdump_evpn_controller.pid")

    vtep2 = tgen.gears["vtep2"]
    v2dir = os.path.join(tgen.logdir, "vtep2")
    v2file = os.path.join(v2dir, "vtep2_evpn_mobility.pcap")
    vtep2.run("mkdir -p {}".format(shlex.quote(v2dir)))
    vtep2.run(
        "tcpdump -nni any -s 0 -w {} port 179 2>/dev/null & echo $! > /tmp/tcpdump_evpn_vtep2.pid".format(
            shlex.quote(v2file)
        ),
        stdout=None,
    )
    captures["vtep2"] = (vtep2, v2file, "/tmp/tcpdump_evpn_vtep2.pid")

    vtep3 = tgen.gears["vtep3"]
    v3dir = os.path.join(tgen.logdir, "vtep3")
    v3file = os.path.join(v3dir, "vtep3_evpn_mobility.pcap")
    vtep3.run("mkdir -p {}".format(shlex.quote(v3dir)))
    vtep3.run(
        "tcpdump -nni any -s 0 -w {} port 179 2>/dev/null & echo $! > /tmp/tcpdump_evpn_vtep3.pid".format(
            shlex.quote(v3file)
        ),
        stdout=None,
    )
    captures["vtep3"] = (vtep3, v3file, "/tmp/tcpdump_evpn_vtep3.pid")

    print("tcpdump started on:", ", ".join(captures.keys()))
    for label, (_, pf, _) in captures.items():
        print(f"  {label} capture file: {pf}")
    sleep(1)  # Give tcpdump a brief startup window.
    return captures


def _stop_pcap_captures(captures):
    """Kill tcpdump processes via their PID files."""
    for label, (node, _, pidfile) in captures.items():
        node.run(f"if [ -f {pidfile} ]; then kill $(cat {pidfile}) 2>/dev/null; rm -f {pidfile}; fi")
    next(iter(captures.values()))[0].run("sleep 1")


def _collect_pcap_stats(captures):
    """Return per-node packet and NLRI counts from pcap files."""
    stats = {}
    for label, (node, pcap_file, _) in captures.items():
        pkt_count = get_pcap_packet_count(node, pcap_file)
        nlri = get_pcap_mp_nlri_counts(node, pcap_file)
        stats[label] = {"total_packets": pkt_count, "nlri": nlri}
    return stats


def _fmt_nlri(counts):
    """Format MP NLRI counts for display."""
    if isinstance(counts, str):
        return counts
    total_nlri = counts["mp_reach"] + counts["mp_unreach"] - counts["both"]
    return (
        f"mp_reach={counts['mp_reach']}  "
        f"mp_unreach={counts['mp_unreach']}  "
        f"both={counts['both']}  "
        f"total_nlri={total_nlri}"
    )


def _print_pcap_summary(stats):
    """Print a human-readable summary of pcap results."""
    print("Packet captures saved:")
    for label, data in stats.items():
        print(f"  {label}: total_packets={data['total_packets']}")
        print(f"    BGP UPDATE NLRI: {_fmt_nlri(data['nlri'])}")
        print("")


#####################################################
##   Core batch measurement
#####################################################

def _safe_nlri_field(stats_dict, node_label, field):
    """Safely extract an NLRI field from pcap stats."""
    nlri = stats_dict.get(node_label, {}).get("nlri", {})
    if isinstance(nlri, dict):
        return nlri.get(field, 0)
    return 0


def _run_batch_measurement(tgen, num_pool_vms, batch_size, rng, tag=""):
    """Deploy a fixed VM pool, move a batch via FDB, and measure BGP UPDATEs.

    Workflow:
      1. Create controller endpoint.
      2. Deploy *num_pool_vms* MACVLANs across mobility-eligible hosts.
      3. Wait for initial BGP convergence (all type-2 routes settle).
      4. Start pcap capture.
      5. Move *batch_size* randomly-chosen VMs via ``bridge fdb replace``.
      6. Wait for post-move convergence.
      7. Stop pcap; collect MP_REACH / MP_UNREACH counts.
      8. Clean up all VMs and FDB entries.

    Returns a results dict suitable for JSON serialisation.
    """
    mobility_vtep_indices = get_mobility_vtep_indices()
    mobility_host_indices = get_mobility_host_indices()
    controller_host_idx = int(CONTROLLER_ENDPOINT_HOST.replace("host", ""))
    controller_vtep_name = vtep_name_from_index(host_to_vtep_index(controller_host_idx))

    assert controller_vtep_name in CONTROLLER_VTEPS
    assert len(mobility_vtep_indices) >= 2
    assert mobility_host_indices

    prefix = f"[{tag}] " if tag else ""
    captures = None
    pcap_stats = {}
    vm_locations = {}
    total_moves = 0
    move_elapsed = 0.0

    try:
        # --- 1. Controller endpoint ---
        create_controller_endpoint(tgen)

        # --- 2. Deploy VM pool ---
        print(f"\n{prefix}=== Deploying {num_pool_vms} VMs ===")
        for vm_idx in range(1, num_pool_vms + 1):
            vm_name = f"vm{vm_idx}"
            vm_ip = f"192.168.100.{vm_idx}/16"
            vm_mac = _vm_mac(vm_idx)

            host_idx = mobility_host_indices[(vm_idx - 1) % len(mobility_host_indices)]
            host_name = f"host{host_idx}"
            vtep_idx = host_to_vtep_index(host_idx)

            create_macvlan_endpoint(tgen, host_name, vm_name, vm_ip, vm_mac)
            vm_locations[vm_name] = (host_idx, vtep_idx)

            if vm_idx % 5 == 0:
                sleep(1)

        # --- 3. Wait for initial convergence ---
        print(f"{prefix}Waiting {INITIAL_CONVERGENCE_SECONDS}s for initial BGP convergence...")
        sleep(INITIAL_CONVERGENCE_SECONDS)

        # Optional baseline connectivity check (informational).
        print(f"{prefix}Verifying baseline connectivity...")
        try:
            verify_post_migration_connectivity(
                tgen, vm_locations, "192.168.0.250", num_pool_vms
            )
        except (AssertionError, Exception) as exc:
            print(f"  {prefix}Baseline connectivity: {exc}")

        # --- 4. Start pcap (AFTER convergence — captures only move-related UPDATEs) ---
        captures = _start_pcap_captures(tgen, controller_vtep_name)

        # --- 5. Move batch via FDB ---
        actual_batch = min(batch_size, num_pool_vms)
        movers = rng.sample(range(1, num_pool_vms + 1), actual_batch)

        print(f"\n{prefix}=== Moving {actual_batch} VMs via FDB ===")
        t0 = time.time()
        plans = move_batch_via_fdb(
            tgen, movers, vm_locations, mobility_vtep_indices, rng
        )
        move_elapsed = time.time() - t0
        total_moves = len(plans)

        for p in plans:
            print(
                f"  {p['vm_name']}: vtep{p['old_vtep_idx']} -> vtep{p['new_vtep_idx']}"
            )
        print(f"{prefix}FDB moves completed in {move_elapsed:.3f}s")

        # --- 6. Wait for post-move convergence ---
        print(f"{prefix}Waiting {CONVERGENCE_WAIT_SECONDS}s for post-move convergence...")
        sleep(CONVERGENCE_WAIT_SECONDS)

    finally:
        # --- 7. Stop pcap ---
        if captures:
            _stop_pcap_captures(captures)
            pcap_stats = _collect_pcap_stats(captures)
            _print_pcap_summary(pcap_stats)

        # --- 8. Cleanup ---
        cleanup_fdb_entries(tgen, vm_locations, num_pool_vms)

        for vm_idx in range(1, num_pool_vms + 1):
            vm_name = f"vm{vm_idx}"
            if vm_name in vm_locations:
                host_idx, _ = vm_locations[vm_name]
                delete_macvlan_endpoint_if_exists(tgen, f"host{host_idx}", vm_name)

        delete_macvlan_endpoint_if_exists(
            tgen,
            CONTROLLER_ENDPOINT_HOST,
            CONTROLLER_ENDPOINT_IFACE,
        )

        sleep(2)

    # --- Build results ---
    results = {
        "num_pool_vms": num_pool_vms,
        "batch_size": min(batch_size, num_pool_vms),
        "total_moves": total_moves,
        "move_seconds": round(move_elapsed, 3),
        "convergence_wait_seconds": CONVERGENCE_WAIT_SECONDS,
        "mp_reach_spine1": _safe_nlri_field(pcap_stats, "spine1", "mp_reach"),
        "mp_unreach_spine1": _safe_nlri_field(pcap_stats, "spine1", "mp_unreach"),
        "mp_reach_controller": _safe_nlri_field(
            pcap_stats, controller_vtep_name, "mp_reach"
        ),
        "pcap_stats": {
            label: {
                "total_packets": data["total_packets"],
                "mp_reach": _safe_nlri_field(pcap_stats, label, "mp_reach"),
                "mp_unreach": _safe_nlri_field(pcap_stats, label, "mp_unreach"),
                "both": _safe_nlri_field(pcap_stats, label, "both"),
            }
            for label, data in pcap_stats.items()
        },
    }

    # Derived metrics.
    if total_moves > 0:
        results["mp_reach_per_move"] = round(
            results["mp_reach_spine1"] / total_moves, 2
        )
    else:
        results["mp_reach_per_move"] = 0.0

    return results


#####################################################
##   Tests
#####################################################

def test_mobility(tgen):
    """
    Single-batch measurement with the default pool and batch sizes.

    Verifies that FDB-based movement triggers observable BGP advertisements,
    then writes detailed results to JSON.
    """
    controller_host_idx = int(CONTROLLER_ENDPOINT_HOST.replace("host", ""))
    controller_vtep_name = vtep_name_from_index(host_to_vtep_index(controller_host_idx))
    assert controller_vtep_name in CONTROLLER_VTEPS

    mobility_vtep_indices = get_mobility_vtep_indices()
    mobility_host_indices = get_mobility_host_indices()
    assert mobility_vtep_indices and len(mobility_vtep_indices) >= 2
    assert mobility_host_indices

    print("\n===== Batch-Based Mobility Measurement =====")
    print(f"Pool VMs: {NUM_POOL_VMS}")
    print(f"Default batch size: {SWEEP_BATCH_SIZES[-1] if SWEEP_BATCH_SIZES else NUM_POOL_VMS}")
    print(f"MRAI (advertisement-interval): default (datacenter)")
    print(f"Post-move convergence wait: {CONVERGENCE_WAIT_SECONDS}s")
    print(f"Initial convergence wait: {INITIAL_CONVERGENCE_SECONDS}s")

    rng, _seed = _make_rng()

    batch_size = SWEEP_BATCH_SIZES[-1] if SWEEP_BATCH_SIZES else NUM_POOL_VMS
    results = _run_batch_measurement(
        tgen,
        num_pool_vms=NUM_POOL_VMS,
        batch_size=batch_size,
        rng=rng,
    )

    # --- Assertions ---
    assert results["total_moves"] > 0, "No moves occurred"
    assert results["mp_reach_spine1"] > 0, (
        f"No MP_REACH_NLRI on spine1 after {results['total_moves']} moves"
    )
    assert results["mp_reach_controller"] > 0, (
        f"No MP_REACH_NLRI on controller after {results['total_moves']} moves"
    )

    # Write results.
    results_file = os.path.join(tgen.logdir, "mobility_results.json")
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {results_file}")

    # Drop into CLI if requested.
    if os.getenv("MUNET_CLI") == "1":
        tgen.mininet_cli()


#####################################################
##   Parametric Scaling Sweep (batch size axis)
#####################################################

@pytest.mark.parametrize(
    "batch_size", SWEEP_BATCH_SIZES, ids=lambda b: f"batch_{b}"
)
def test_batch_scaling_sweep(tgen, batch_size):
    """
    Sweep over increasing batch sizes with a fixed VM pool.

    Each iteration deploys *NUM_POOL_VMS* endpoints, moves *batch_size*
    of them via FDB manipulation, waits for convergence, and records
    the MP_REACH / MP_UNREACH counts.

    With the default FRR datacenter MRAI (0), each move triggers
    individual UPDATEs immediately, so MP_REACH should scale
    roughly linearly with batch size.
    """
    print(f"\n{'=' * 60}")
    print(f"  SCALING SWEEP: batch_size={batch_size}  (pool={NUM_POOL_VMS})")
    print(f"{'=' * 60}\n")

    rng, _seed = _make_rng()

    results = _run_batch_measurement(
        tgen,
        num_pool_vms=NUM_POOL_VMS,
        batch_size=batch_size,
        rng=rng,
        tag=f"batch-{batch_size}",
    )

    # Basic assertion.
    assert results["total_moves"] > 0, (
        f"Sweep iteration (batch_size={batch_size}) produced zero moves"
    )

    # Persist individual iteration results.
    iter_file = os.path.join(tgen.logdir, f"batch_sweep_{batch_size}.json")
    with open(iter_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nIteration results written to {iter_file}")

    # Append to combined results file.
    combined_file = os.path.join(tgen.logdir, "batch_scaling_results.json")
    if os.path.exists(combined_file):
        with open(combined_file, "r") as f:
            combined = json.load(f)
    else:
        combined = []
    combined.append(results)
    with open(combined_file, "w") as f:
        json.dump(combined, f, indent=2)

    # Print running table.
    if len(combined) > 1:
        print(f"\n{'=' * 90}")
        print("  BATCH SCALING RESULTS SO FAR")
        print(f"{'=' * 90}")
        print(
            f"  {'Batch':>6s}  {'Moves':>6s}  {'Move(s)':>8s}  "
            f"{'MP_REACH':>10s}  {'MP_UNREACH':>12s}  "
            f"{'REACH/Move':>11s}"
        )
        print(
            f"  {'-' * 6}  {'-' * 6}  {'-' * 8}  {'-' * 10}  "
            f"{'-' * 12}  {'-' * 11}"
        )
        for r in combined:
            print(
                f"  {r['batch_size']:>6d}  {r['total_moves']:>6d}  "
                f"{r['move_seconds']:>8.3f}  "
                f"{r['mp_reach_spine1']:>10}  "
                f"{r['mp_unreach_spine1']:>12}  "
                f"{r.get('mp_reach_per_move', 0):>11.2f}"
            )
        print(f"{'=' * 90}\n")

    # Settle before next iteration.
    sleep(10)


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
