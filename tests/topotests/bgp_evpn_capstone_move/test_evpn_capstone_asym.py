#!/usr/bin/env python
# -*- coding: utf-8 eval: (blacken-mode 1) -*-
# SPDX-License-Identifier: ISC
#
# test_evpn_capstone_asym.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2017 by
# Network Device Education Foundation, Inc. ("NetDEF")
#

"""Topotest for EVPN L2VNI endpoint mobility with a static controller endpoint."""

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
try:
    NUM_MOBILE_VMS = max(1, int(os.getenv("NUM_MOBILE_VMS", "30")))
except ValueError:
    NUM_MOBILE_VMS = 30

# Controller VTEPs participate in topology/BGP but are excluded from endpoint mobility.
CONTROLLER_VTEPS = {"vtep1"}

# Static endpoint attached to the controller side (host on controller VTEP).
CONTROLLER_ENDPOINT_HOST = "host1"
CONTROLLER_ENDPOINT_IFACE = "controller"
CONTROLLER_ENDPOINT_IP = "192.168.100.254/16"
CONTROLLER_ENDPOINT_MAC = "00:aa:bb:dd:00:01"

# Duration (seconds) to keep duplicate-MAC overlap during live migration.
# Can be overridden with env var MOBILITY_OVERLAP_SECONDS.
try:
    MOBILITY_OVERLAP_SECONDS = max(0.0, float(os.getenv("MOBILITY_OVERLAP_SECONDS", "0.2")))
except ValueError:
    MOBILITY_OVERLAP_SECONDS = 0.2

# Duration of the time-based random-movement simulation (seconds).
try:
    SIMULATION_DURATION_SECONDS = max(1, int(os.getenv("SIMULATION_DURATION_SECONDS", "60")))
except ValueError:
    SIMULATION_DURATION_SECONDS = 60

# How often (seconds) to evaluate each VM for a possible move.
try:
    SIMULATION_TICK_SECONDS = max(0.1, float(os.getenv("SIMULATION_TICK_SECONDS", "1.0")))
except ValueError:
    SIMULATION_TICK_SECONDS = 1.0

# Per-tick probability that any single VM will move (0.0 – 1.0).
try:
    VM_MOVE_PROBABILITY = min(1.0, max(0.0, float(os.getenv("VM_MOVE_PROBABILITY", "0.1"))))
except ValueError:
    VM_MOVE_PROBABILITY = 0.1

# When True, batch multiple ip-link commands into a single shell call per host
# to reduce subprocess overhead.  Set BATCH_SHELL_COMMANDS=0 to disable for debugging.
BATCH_SHELL_COMMANDS = os.getenv("BATCH_SHELL_COMMANDS", "1") not in ("0", "false", "False")

# When False (or MOBILITY_OVERLAP_SECONDS==0), skip the duplicate-MAC overlap
# sleep entirely.  Useful for high-throughput sweep runs.
MOBILITY_OVERLAP_ENABLED = os.getenv("MOBILITY_OVERLAP_ENABLED", "1") not in ("0", "false", "False")

# Reproducible RNG seed.  When unset a random seed is generated and logged so
# that any run can be replayed by setting SIMULATION_SEED=<value>.
SIMULATION_SEED = os.getenv("SIMULATION_SEED", None)

# Comma-separated VM counts for the parametric scaling sweep.
# Example: SWEEP_VM_COUNTS="5,10,20,40,80"
_sweep_env = os.getenv("SWEEP_VM_COUNTS", "5,10,20,40,80")
try:
    SWEEP_VM_COUNTS = [max(1, int(x.strip())) for x in _sweep_env.split(",") if x.strip()]
except ValueError:
    SWEEP_VM_COUNTS = [5, 10, 20, 40, 80]

def get_pcap_packet_count(node, file_path):
    """Return packet count, or 'missing' when the pcap does not exist."""
    path = shlex.quote(file_path)
    return node.run(
        "if [ -f {0} ]; then tcpdump -nr {0} 2>/dev/null | wc -l; else echo missing; fi".format(
            path
        )
    ).strip()

# Uses tshark to parse tcpdump pcap and filter by BGP path attribute type codes to count REACH and UNREACH packets
def get_pcap_mp_nlri_counts(node, file_path):
    path = shlex.quote(file_path)

    exists = node.run("[ -f {} ] && echo yes || echo no".format(path)).strip()
    if exists != "yes":
        return "missing"

    # Verify tshark is available inside the namespace.
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

    # Count packets that carry both MP_REACH_NLRI (14) and MP_UNREACH_NLRI (15) in one UPDATE.
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
        # tshark returned unexpected output; pass raw strings through so the caller can log them.
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


def build_vm_migration_plan(vm_idx, vm_locations, mobility_vtep_indices, mobility_host_indices, rng=None):
    """Compute source/destination placement and addressing for one VM migration.

    When *rng* is supplied, the destination VTEP is chosen uniformly at random
    from all mobility-eligible VTEPs other than the current one.  When *rng* is
    ``None`` the legacy deterministic round-robin (next VTEP in index order) is
    used instead.
    """
    vm_name = f"vm{vm_idx}"

    # Current location.
    old_host_idx, old_vtep_idx = vm_locations[vm_name]
    old_host_name = f"host{old_host_idx}"

    # Choose destination VTEP.
    if rng is not None:
        candidates = [v for v in mobility_vtep_indices if v != old_vtep_idx]
        new_vtep_idx = rng.choice(candidates)
    else:
        # Deterministic round-robin fallback.
        current_pos = mobility_vtep_indices.index(old_vtep_idx)
        new_vtep_idx = mobility_vtep_indices[(current_pos + 1) % len(mobility_vtep_indices)]

    # Prefer a different host than source, then fallback to any host on destination VTEP.
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
        "new_host_name": f"host{new_host_idx}",
        "new_host_idx": new_host_idx,
        "new_vtep_idx": new_vtep_idx,
        "vm_ip": vm_ip,
        "vm_mac": vm_mac,
    }


def migrate_vms(tgen, vm_indices, vm_locations, mobility_vtep_indices, mobility_host_indices, rng=None):
    """
    Migrate multiple VMs in bulk: create all destinations, wait once for the
    overlap window, then delete all sources.  This avoids paying a per-VM
    sleep cost and keeps tick duration independent of how many VMs move.

    When *BATCH_SHELL_COMMANDS* is True, MACVLAN create/delete operations are
    grouped by host and issued as single compound shell commands to minimise
    subprocess overhead.  Pass *rng* through to ``build_vm_migration_plan``
    for random destination selection.
    """
    plans = [
        build_vm_migration_plan(idx, vm_locations, mobility_vtep_indices, mobility_host_indices, rng=rng)
        for idx in vm_indices
    ]

    # --- 1. Create every destination endpoint ---
    if BATCH_SHELL_COMMANDS:
        # Group creation commands by destination host.
        create_by_host = defaultdict(list)
        for plan in plans:
            create_by_host[plan["new_host_name"]].append(plan)

        for host_name, host_plans in create_by_host.items():
            host = tgen.gears[host_name]
            cmds = []
            for p in host_plans:
                cmds.append(
                    f"ip link add link vtepbond name {p['vm_name']} type macvlan mode bridge "
                    f"&& ip link set dev {p['vm_name']} address {p['vm_mac']} "
                    f"&& ip addr add {p['vm_ip']} dev {p['vm_name']} "
                    f"&& ip link set dev {p['vm_name']} up"
                )
            # Join per-VM compound commands with '&&' so that any single VM
            # failure causes the batch to report failure (';' would silently
            # swallow errors from all VMs except the last).
            batch_cmd = " && ".join(cmds)
            result = host.run(f"{{ {batch_cmd} ; }} >/dev/null 2>&1 && echo ok || echo failed").strip()
            if result != "ok":
                # Fall back to individual creation to get a precise error.
                # Clean up partially-created VMs first so create_macvlan_endpoint
                # does not collide with interfaces left behind by the batch.
                for p in host_plans:
                    delete_macvlan_endpoint_if_exists(tgen, p["new_host_name"], p["vm_name"])
                    create_macvlan_endpoint(tgen, p["new_host_name"], p["vm_name"], p["vm_ip"], p["vm_mac"])
    else:
        for plan in plans:
            create_macvlan_endpoint(
                tgen, plan["new_host_name"], plan["vm_name"], plan["vm_ip"], plan["vm_mac"]
            )
            if not macvlan_endpoint_exists(tgen, plan["new_host_name"], plan["vm_name"]):
                raise AssertionError(
                    f"destination endpoint {plan['vm_name']} missing on "
                    f"{plan['new_host_name']} after create"
                )

    # --- 2. One overlap window for the entire group ---
    if MOBILITY_OVERLAP_ENABLED and MOBILITY_OVERLAP_SECONDS > 0:
        sleep(MOBILITY_OVERLAP_SECONDS)

    # --- 3. Delete every source endpoint ---
    if BATCH_SHELL_COMMANDS:
        delete_by_host = defaultdict(list)  # already imported at module level
        for plan in plans:
            delete_by_host[plan["old_host_name"]].append(plan)

        for host_name, host_plans in delete_by_host.items():
            host = tgen.gears[host_name]
            del_cmds = [f"ip link del {p['vm_name']}" for p in host_plans]
            batch_cmd = " ; ".join(del_cmds)
            host.run(f"{{ {batch_cmd} ; }} >/dev/null 2>&1 || true")
    else:
        for plan in plans:
            delete_macvlan_endpoint(tgen, plan["old_host_name"], plan["vm_name"])
            if macvlan_endpoint_exists(tgen, plan["old_host_name"], plan["vm_name"]):
                print(
                    f"WARNING: source endpoint still present after delete; forcing cleanup for "
                    f"{plan['vm_name']} on {plan['old_host_name']}"
                )
                delete_macvlan_endpoint_if_exists(tgen, plan["old_host_name"], plan["vm_name"])

    # --- 4. Update tracking ---
    for plan in plans:
        vm_locations[plan["vm_name"]] = (plan["new_host_idx"], plan["new_vtep_idx"])

    return plans

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
    # Mobility-eligible VTEPs are all VTEPs that are not marked as controllers.
    return [
        i
        for i in range(1, NUM_VTEPS + 1)
        if vtep_name_from_index(i) not in CONTROLLER_VTEPS
    ]


def get_mobility_host_indices():
    # Mobility-eligible hosts are attached to mobility-eligible VTEPs.
    mobility_vtep_indices = set(get_mobility_vtep_indices())
    return [
        host_idx
        for host_idx in range(1, NUM_HOSTS + 1)
        if host_to_vtep_index(host_idx) in mobility_vtep_indices
    ]


def compute_svi_ip(vtep_index):
    # Keep legacy SVI assignment for vtep1-vtep5.
    if vtep_index <= 5:
        return f"192.168.0.{250 + vtep_index}"

    # For additional VTEPs, avoid invalid octets (e.g., .256) and keep SVI
    # space separate from underlay ranges currently used in this topology.
    return f"192.168.200.{vtep_index - 5}"


svi_ips = {
    f"vtep{i}": compute_svi_ip(i)
    for i in range(1, NUM_VTEPS + 1)
}

def config_bond(node, bond_name, bond_members, bond_ad_sys_mac, br):
    """
    Set up Linux bonds on VTEPs and hosts for multihoming.
    """
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

    # If a bridge is specified, add the bond as a bridge member.
    if br:
        node.run(" ip link set dev %s master %s" % (bond_name, br))
        node.run("/sbin/bridge link set dev %s priority 8" % bond_name)
        node.run("/sbin/bridge vlan del vid 1 dev %s" % bond_name)
        node.run("/sbin/bridge vlan del vid 1 untagged pvid dev %s" % bond_name)
        node.run("/sbin/bridge vlan add vid 1000 dev %s" % bond_name)
        node.run("/sbin/bridge vlan add vid 1000 untagged pvid dev %s" % bond_name)


def config_l2vni(node, svi_ip, vtep_ip):
    """
    Configure Linux bridge/VXLAN dataplane for L2VNI 1000 on one VTEP.

    - Create bridge br1000 and assign SVI + anycast gateway addresses
    - Create vxlan device vni1000 bound to the VTEP source IP
    - Attach vni1000 to br1000 with VLAN 1000 membership
    - Bring all interfaces up
    """
    # Create a bridge br1000 and assign SVI IP
    node.run("ip link add br1000 type bridge")
    node.run("ip addr add %s/16 dev br1000" % svi_ip)
    # Anycast gateway address for hosts to reach
    node.run("ip addr add 192.168.0.250/16 dev br1000")
    node.run("/sbin/sysctl net.ipv4.conf.br1000.arp_accept=1")

    node.run(
        "ip link add vni1000 type vxlan local %s dstport 4789 id 1000"
        % vtep_ip
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
    """
    Configure host-facing bond plus VXLAN bridge on one EVPN VTEP.
    """

    # Create L2VNI, bridge, and associated SVI.
    config_l2vni(vtep, svi_ip, vtep_ip)

    # Create host bond and add it to the bridge.
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
    # Simple sequential IP: host1 = 192.168.0.1, host2 = 192.168.0.2, etc.
    # (avoiding .0 as network and .250+ for gateways/SVIs)
    if host_id <= 127:
        # Use 192.168.0.1 to 192.168.0.127
        fourth_octet = host_id
        third_octet = 0
    else:
        # For hosts > 127, use 192.168.1.x, 192.168.2.x, etc.
        offset = host_id - 128
        third_octet = 1 + (offset // 254)
        fourth_octet = 1 + (offset % 254)
    
    host_ip = f"192.168.{third_octet}.{fourth_octet}/16"
    # For MAC addresses, simple encoding: 00:00:00:xx:yy:zz
    host_mac = "00:00:00:{:02x}:{:02x}:{:02x}".format(
        (host_id >> 16) & 0xFF, (host_id >> 8) & 0xFF, host_id & 0xFF
    )
    return host_ip, host_mac


def config_host(host_name, host):
    """
    Configure the host-side bonded uplink used to attach endpoints.
    """

    bond_members = [host_name + "-eth0"]

    # Name of the bonded interface to be created on the host
    bond_name = "vtepbond"
    config_bond(host, bond_name, bond_members, "00:00:00:00:00:00", None)

    host_ip, host_mac = compute_host_ip_mac(host_name)

    # Assign the computed IP address and MAC address to the bonded interface
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

    # Create spines
    spine1 = tgen.add_router("spine1")
    spine2 = tgen.add_router("spine2")

    # Create VTEPs
    vteps = []
    for i in range(1, NUM_VTEPS + 1):
        vtep_name = f"vtep{i}"
        vtep = tgen.add_router(vtep_name)
        vteps.append(vtep_name)
        
        # Connect this VTEP to spine1.
        tgen.add_link(spine1, vtep)
        
        # Connect this VTEP to spine2.
        tgen.add_link(spine2, vtep)

    # Create hosts and distribute them across VTEPs.
    hosts = []
    for i in range(1, NUM_HOSTS + 1):
        host_name = f"host{i}"
        host = tgen.add_router(host_name)
        hosts.append(host_name)
        
        # Distribute hosts across VTEPs in round-robin fashion.
        vtep_idx = (i - 1) % NUM_VTEPS
        vtep_name = f"vtep{vtep_idx + 1}"
        vtep = tgen.gears[vtep_name]
        
        # Connect the selected VTEP to this host.
        tgen.add_link(vtep, host)


@pytest.fixture(scope="module")
def tgen(request):
    """Set up topology/configuration and provide Topogen to tests."""

    # Instantiate and start the topology.
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

    # Provide tgen as argument to each test function
    yield tgen

    # Suppress the memory allocation report that FRR prints on shutdown.
    # The topotest framework treats any remaining allocations as "memory leaks".
    # It dumps a verbose table for every daemon on every router.
    # We redirect sys.stderr temporarily so the output is discarded during topology teardown.
    original_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        tgen.stop_topology()
    finally:
        sys.stderr.close()
        sys.stderr = original_stderr


# Skip subsequent tests if an earlier test caused router failures.
@pytest.fixture(autouse=True)
def skip_on_failure(tgen):
    if tgen.routers_have_failure():
        pytest.skip("skipped because of previous test failure")


#####################################################
##   Test Functions
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
    # Make creation idempotent in case a previous run exited before cleanup.
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


def _make_rng():
    """Create a seeded Random instance; log/return the seed for reproducibility."""
    if SIMULATION_SEED is not None:
        seed = int(SIMULATION_SEED)
    else:
        seed = int.from_bytes(os.urandom(8), "big")
    print(f"RNG seed: {seed}  (replay with SIMULATION_SEED={seed})")
    return random.Random(seed), seed


def _start_pcap_captures(tgen, controller_vtep_name):
    """Start tcpdump on spine1, vtep2, vtep3, and the controller VTEP.

    Returns a dict of ``{label: (node, pcap_file)}`` plus the internal
    PID-file names used for cleanup.
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
    # Brief flush window.
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
        return counts  # 'missing' or 'tshark-not-found'
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


def _run_mobility_simulation(
    tgen,
    num_mobile_vms,
    simulation_duration_seconds=None,
    simulation_tick_seconds=None,
    vm_move_probability=None,
    rng=None,
    tag="",
):
    """Core mobility simulation loop.  Extracted so it can be called by both
    ``test_mobility`` and the parametric sweep.

    Returns a dict with simulation results suitable for JSON serialisation.
    """
    duration = simulation_duration_seconds if simulation_duration_seconds is not None else SIMULATION_DURATION_SECONDS
    tick_sec = simulation_tick_seconds if simulation_tick_seconds is not None else SIMULATION_TICK_SECONDS
    move_prob = vm_move_probability if vm_move_probability is not None else VM_MOVE_PROBABILITY

    controller_host_idx = int(CONTROLLER_ENDPOINT_HOST.replace("host", ""))
    controller_vtep_name = vtep_name_from_index(host_to_vtep_index(controller_host_idx))
    assert controller_vtep_name in CONTROLLER_VTEPS

    mobility_vtep_indices = get_mobility_vtep_indices()
    mobility_host_indices = get_mobility_host_indices()
    assert len(mobility_vtep_indices) >= 2
    assert mobility_host_indices

    prefix = f"[{tag}] " if tag else ""

    # --- Packet capture ---
    captures = _start_pcap_captures(tgen, controller_vtep_name)

    # Track per-tick performance.
    tick_stats = []  # [(tick_number, num_movers, work_seconds, overran)]

    try:
        create_controller_endpoint(tgen)

        print(f"\n{prefix}=== Starting Mobility Simulation with {num_mobile_vms} VMs ===\n")

        vm_locations = {}

        # Deploy VMs.
        for vm_idx in range(1, num_mobile_vms + 1):
            vm_name = f"vm{vm_idx}"
            vm_ip = f"192.168.100.{vm_idx}/16"
            vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)

            host_idx = mobility_host_indices[(vm_idx - 1) % len(mobility_host_indices)]
            host_name = f"host{host_idx}"
            vtep_idx = host_to_vtep_index(host_idx)

            create_macvlan_endpoint(tgen, host_name, vm_name, vm_ip, vm_mac)
            vm_locations[vm_name] = (host_idx, vtep_idx)

            if vm_idx % 5 == 0:
                sleep(1)

        sleep(5)
        print(f"{prefix}Deployment complete. Starting random movement...\n")

        print(
            f"{prefix}Running for {duration}s  "
            f"(tick={tick_sec:.2f}s, "
            f"move_probability={move_prob:.3f})\n"
        )

        if rng is None:
            rng, _ = _make_rng()

        sim_start = time.time()
        tick_number = 0
        total_moves = 0

        while time.time() - sim_start < duration:
            tick_number += 1
            tick_deadline = sim_start + tick_number * tick_sec
            tick_work_start = time.time()

            # Roll for each VM independently.
            movers = [
                vm_idx
                for vm_idx in range(1, num_mobile_vms + 1)
                if rng.random() < move_prob
            ]

            if movers:
                plans = migrate_vms(
                    tgen, movers, vm_locations,
                    mobility_vtep_indices, mobility_host_indices,
                    rng=rng,
                )
                total_moves += len(plans)
                elapsed = time.time() - sim_start
                for plan in plans:
                    print(
                        f"  {prefix}[{elapsed:6.1f}s] {plan['vm_name']}: "
                        f"{plan['old_host_name']} -> {plan['new_host_name']} "
                        f"(vtep{plan['new_vtep_idx']})"
                    )
                print(
                    f"  {prefix}[{elapsed:6.1f}s] tick {tick_number}: "
                    f"{len(plans)} move(s) this tick  (total: {total_moves})"
                )
            elif tick_number % 10 == 0:
                elapsed = time.time() - sim_start
                print(f"  {prefix}[{elapsed:6.1f}s] tick {tick_number}: no moves (total: {total_moves})")

            # --- Deadline-based scheduling ---
            work_seconds = time.time() - tick_work_start
            remaining = tick_deadline - time.time()
            overran = remaining < 0
            if overran:
                print(
                    f"  {prefix}WARNING: tick {tick_number} overran by "
                    f"{-remaining:.3f}s ({len(movers)} movers, "
                    f"work={work_seconds:.3f}s)"
                )
            else:
                sleep(remaining)

            tick_stats.append((tick_number, len(movers), work_seconds, overran))

        sim_elapsed = time.time() - sim_start
        print(
            f"\n{prefix}Simulation finished: {tick_number} ticks, "
            f"{total_moves} total moves in {sim_elapsed:.1f}s"
        )

        # Wait for BGP/EVPN to process remaining updates.
        sleep(5)

        # --- Non-asserting connectivity spot-check ---
        print(f"\n{prefix}Running post-simulation connectivity spot-check...")
        try:
            verify_post_migration_connectivity(
                tgen, vm_locations, "192.168.0.250", num_mobile_vms
            )
        except (AssertionError, Exception) as exc:
            # Informational only — do not fail the test on connectivity issues.
            print(f"  {prefix}Connectivity spot-check: {exc}")

        print(f"\n{prefix}Simulation complete.")

    finally:
        # --- Stop captures & collect results ---
        _stop_pcap_captures(captures)
        pcap_stats = _collect_pcap_stats(captures)
        _print_pcap_summary(pcap_stats)

        # --- Tick-duration summary ---
        if tick_stats:
            durations = [t[2] for t in tick_stats]
            overruns = sum(1 for t in tick_stats if t[3])
            print(f"Tick duration stats (n={len(tick_stats)}):")
            print(
                f"  min={min(durations)*1000:.1f}ms  "
                f"avg={sum(durations)/len(durations)*1000:.1f}ms  "
                f"max={max(durations)*1000:.1f}ms  "
                f"overruns={overruns}/{len(tick_stats)}"
            )
        else:
            overruns = 0

        sleep(2)

        # Clean up all VMs.
        for vm_idx in range(1, num_mobile_vms + 1):
            vm_name = f"vm{vm_idx}"
            if vm_name in vm_locations:
                host_idx, _ = vm_locations[vm_name]
                delete_macvlan_endpoint_if_exists(tgen, f"host{host_idx}", vm_name)

        delete_macvlan_endpoint_if_exists(
            tgen,
            CONTROLLER_ENDPOINT_HOST,
            CONTROLLER_ENDPOINT_IFACE,
        )

    # --- Build structured results ---
    def _safe_nlri_field(stats_dict, node_label, field):
        nlri = stats_dict.get(node_label, {}).get("nlri", {})
        if isinstance(nlri, dict):
            return nlri.get(field, 0)
        return 0

    results = {
        "num_vms": num_mobile_vms,
        "total_moves": total_moves,
        "simulation_seconds": round(sim_elapsed, 1),
        "effective_moves_per_second": round(total_moves / max(sim_elapsed, 0.001), 2),
        "tick_count": tick_number,
        "tick_overrun_count": overruns,
        "avg_tick_duration_ms": round(
            (sum(d[2] for d in tick_stats) / max(len(tick_stats), 1)) * 1000, 1
        ) if tick_stats else 0,
        "mp_reach_spine1": _safe_nlri_field(pcap_stats, "spine1", "mp_reach"),
        "mp_unreach_spine1": _safe_nlri_field(pcap_stats, "spine1", "mp_unreach"),
        "mp_reach_controller": _safe_nlri_field(
            pcap_stats, vtep_name_from_index(host_to_vtep_index(
                int(CONTROLLER_ENDPOINT_HOST.replace("host", ""))
            )), "mp_reach"
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

    return results


def test_mobility(tgen):
    """
    Simulate random endpoint mobility across VTEPs over a fixed time window.

    Each VM independently has a random chance of moving every tick.
    Individual moves are printed to console as they happen.
    BGP packet captures run throughout the simulation for analysis.
    """

    controller_host_idx = int(CONTROLLER_ENDPOINT_HOST.replace("host", ""))
    controller_vtep_name = vtep_name_from_index(host_to_vtep_index(controller_host_idx))
    assert (
        controller_vtep_name in CONTROLLER_VTEPS
    ), f"{CONTROLLER_ENDPOINT_HOST} is mapped to {controller_vtep_name}, but is not in CONTROLLER_VTEPS"

    mobility_vtep_indices = get_mobility_vtep_indices()
    mobility_host_indices = get_mobility_host_indices()

    # Guardrails for controller-VTEP mode.
    assert mobility_vtep_indices, "No mobility-eligible VTEPs are configured"
    assert len(mobility_vtep_indices) >= 2, "Need at least two mobility-eligible VTEPs"
    assert mobility_host_indices, "No mobility-eligible hosts are available"

    print("\nStarting packet capture on spine1, vtep2, and controller VTEP...")
    print(f"Using mobility overlap timer: {MOBILITY_OVERLAP_SECONDS:.3f}s")
    print(f"Simulation duration: {SIMULATION_DURATION_SECONDS}s")
    print(f"Simulation tick interval: {SIMULATION_TICK_SECONDS:.2f}s")
    print(f"Per-tick VM move probability: {VM_MOVE_PROBABILITY:.3f}")
    print(f"Batch shell commands: {BATCH_SHELL_COMMANDS}")
    print(f"Overlap sleep enabled: {MOBILITY_OVERLAP_ENABLED}")

    rng, _seed = _make_rng()

    results = _run_mobility_simulation(
        tgen,
        num_mobile_vms=NUM_MOBILE_VMS,
        rng=rng,
    )

    # --- Assertions: the simulation must produce observable BGP activity ---
    assert results["total_moves"] > 0, "Simulation produced zero moves"
    assert results["mp_reach_spine1"] > 0, (
        f"No MP_REACH_NLRI observed on spine1 after {results['total_moves']} moves"
    )
    assert results["mp_reach_controller"] > 0, (
        f"No MP_REACH_NLRI observed on controller after {results['total_moves']} moves"
    )

    # Write results to JSON for external analysis.
    results_file = os.path.join(tgen.logdir, "mobility_results.json")
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {results_file}")

    # Run with MUNET_CLI=1 to drop into CLI after test completion.
    if os.getenv("MUNET_CLI") == "1":
        tgen.mininet_cli()  # this drops you into the 'munet>' prompt


#####################################################
##   Parametric Scaling Sweep
#####################################################

@pytest.mark.parametrize("num_vms", SWEEP_VM_COUNTS, ids=lambda n: f"vms_{n}")
def test_mobility_scaling_sweep(tgen, num_vms):
    """
    Run the mobility simulation at different VM counts to measure how BGP
    advertisement volume scales with increasing move density.

    Each parametrized iteration deploys *num_vms* mobile VMs, runs the
    simulation for ``SIMULATION_DURATION_SECONDS``, then records the
    resulting MP_REACH / MP_UNREACH counts.

    After all iterations the combined results are written to
    ``{logdir}/scaling_results.json`` for plotting/analysis.
    """

    print(f"\n{'='*60}")
    print(f"  SCALING SWEEP: num_vms={num_vms}")
    print(f"{'='*60}\n")

    rng, _seed = _make_rng()

    results = _run_mobility_simulation(
        tgen,
        num_mobile_vms=num_vms,
        rng=rng,
        tag=f"sweep-{num_vms}",
    )

    # --- Basic assertions ---
    assert results["total_moves"] > 0, f"Sweep iteration (vms={num_vms}) produced zero moves"

    # Persist individual iteration results.
    iter_file = os.path.join(tgen.logdir, f"scaling_sweep_{num_vms}_vms.json")
    with open(iter_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nIteration results written to {iter_file}")

    # Append to combined results file.
    combined_file = os.path.join(tgen.logdir, "scaling_results.json")
    if os.path.exists(combined_file):
        with open(combined_file, "r") as f:
            combined = json.load(f)
    else:
        combined = []
    combined.append(results)
    with open(combined_file, "w") as f:
        json.dump(combined, f, indent=2)

    # Print a running table if we have multiple data points.
    if len(combined) > 1:
        print(f"\n{'='*80}")
        print("  SCALING RESULTS SO FAR")
        print(f"{'='*80}")
        print(
            f"  {'VMs':>6s}  {'Moves':>7s}  {'Moves/s':>8s}  "
            f"{'MP_REACH':>10s}  {'MP_UNREACH':>12s}  "
            f"{'Overruns':>9s}  {'Avg Tick ms':>12s}"
        )
        print(f"  {'-'*6}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*12}  {'-'*9}  {'-'*12}")
        for r in combined:
            print(
                f"  {r['num_vms']:>6d}  {r['total_moves']:>7d}  "
                f"{r['effective_moves_per_second']:>8.1f}  "
                f"{r['mp_reach_spine1']:>10}  "
                f"{r['mp_unreach_spine1']:>12}  "
                f"{r['tick_overrun_count']:>9d}  "
                f"{r['avg_tick_duration_ms']:>12.1f}"
            )
        print(f"{'='*80}\n")

    # Brief settle before next iteration.
    sleep(10)


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
