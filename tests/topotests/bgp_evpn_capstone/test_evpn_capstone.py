#!/usr/bin/env python
# -*- coding: utf-8 eval: (blacken-mode 1) -*-
# SPDX-License-Identifier: ISC
#
# test_evpn_capstone.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2017 by
# Network Device Education Foundation, Inc. ("NetDEF")
#

"""
test_evpn_capstone.py: Testing EVPN with L2VNI
"""

import os
import re
import sys
import pdb
import random
import json
from functools import partial
from time import sleep, time
import platform
import pytest

# Save the Current Working Directory to find configuration files.
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
# Import topogen and topotest helpers
from lib import topotest
from lib.topogen import Topogen, TopoRouter, get_topogen
from lib.topolog import logger

# pytest module level markers
pytestmark = [
    pytest.mark.bgpd,
    pytest.mark.pimd,
]

#####################################################
##   Configuration Functions
#####################################################

vtep_ips = {
    "vtep1": "10.10.10.10",
    "vtep2": "20.20.20.20",
    "vtep3": "30.30.30.30"
}

svi_ips = {
    "vtep1": "192.168.0.251",
    "vtep2": "192.168.0.252",
    "vtep3": "192.168.0.253"
}

def config_bond(node, bond_name, bond_members, bond_ad_sys_mac, br):
    """
    Used to setup bonds on the VTEPs and hosts for MH
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

    # if bridge is specified add the bond as a bridge member
    if br:
        node.run(" ip link set dev %s master %s" % (bond_name, br))
        node.run("/sbin/bridge link set dev %s priority 8" % bond_name)
        node.run("/sbin/bridge vlan del vid 1 dev %s" % bond_name)
        node.run("/sbin/bridge vlan del vid 1 untagged pvid dev %s" % bond_name)
        node.run("/sbin/bridge vlan add vid 1000 dev %s" % bond_name)
        node.run("/sbin/bridge vlan add vid 1000 untagged pvid dev %s" % bond_name)


def config_l2vni(vtep_name, node, svi_ip, vtep_ip):
    """
    Create a VxLAN device for VNI 1000 and add it to the bridge.
    VLAN-1000 is mapped to VNI-1000.

    Creates a Linux bridge br1000.
    Assigns VLAN 1000 IP addresses to the bridge (SVI).
    Creates a VXLAN interface for VNI 1000 tied to the VTEP IP.
    Adds the VXLAN interface to the bridge.
    Disables MAC learning on VXLAN (because BGP EVPN handles MAC learning).
    Configures VLAN 1000 on the VXLAN interface.
    Brings interfaces up to start forwarding traffic.
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


def config_vtep(vtep_name, vtep, vtep_ip, svi_pip):
    """
    Create the bond/vxlan-bridge on the VTEP which acts as EVPN-PE
    """

    # create l2vni, bridge and associated SVI
    config_l2vni(vtep_name, vtep, svi_pip, vtep_ip)

    # create hostbonds and add them to the bridge
    vtep_id = vtep_name.split("vtep")[1]
    sys_mac = "44:38:39:ff:ff:0" + vtep_id

    bond_member = vtep_name + "-eth2"
    config_bond(vtep, "hostbond1", [bond_member], sys_mac, "br1000")


def config_vteps(tgen, vteps):
    for vtep_name in vteps:
        vtep = tgen.gears[vtep_name]
        config_vtep(vtep_name, vtep, vtep_ips.get(vtep_name), svi_ips.get(vtep_name))


def compute_host_ip_mac(host_name):
    host_id = host_name.split("host")[1]
    host_ip = "192.168.0.24" + host_id + "/16"
    host_mac = "00:00:00:00:00:0" + host_id
    return host_ip, host_mac


def config_host(host_name, host):
    """
    Create the bond on host nodes for MH
    """

    bond_members = []
    bond_members.append(host_name + "-eth0")

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

# Function we pass to Topogen to create the topology
def build_topo(tgen):
    "Build function"

    # Create spine, leaf, and hosts
    spine1 = tgen.add_router("spine1")
    spine2 = tgen.add_router("spine2")

    vtep1 = tgen.add_router("vtep1")
    vtep2 = tgen.add_router("vtep2")
    vtep3 = tgen.add_router("vtep3")

    host1 = tgen.add_router("host1")
    host2 = tgen.add_router("host2")
    host3 = tgen.add_router("host3")

    # Create links between spine1 and VTEPs
    tgen.add_link(spine1, vtep1)
    tgen.add_link(spine1, vtep2)
    tgen.add_link(spine1, vtep3)

    # Create links between spine2 and VTEPs
    tgen.add_link(spine2, vtep1)
    tgen.add_link(spine2, vtep2)
    tgen.add_link(spine2, vtep3)

    # Create links between VTEPs and hosts
    tgen.add_link(vtep1, host1)
    tgen.add_link(vtep2, host2)
    tgen.add_link(vtep3, host3)


# New form of setup/teardown using pytest fixture
@pytest.fixture(scope="module")
def tgen(request):
    "Setup/Teardown the environment and provide tgen argument to tests"

    # This function initiates the topology build with Topogen...
    tgen = Topogen(build_topo, request.module.__name__)
    tgen.start_topology()

    krel = platform.release()
    if topotest.version_cmp(krel, "4.19") < 0:
        tgen.errors = "kernel 4.19 needed for multihoming tests"
        pytest.skip(tgen.errors)

    vteps = ["vtep1", "vtep2", "vtep3"]
    config_vteps(tgen, vteps)

    hosts = ["host1", "host2", "host3"]
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
    tgen.stop_topology()


# Fixture that executes before each test
@pytest.fixture(autouse=True)
def skip_on_failure(tgen):
    if tgen.routers_have_failure():
        pytest.skip("skipped because of previous test failure")


#####################################################
##   Test Functions
#####################################################

def create_macvlan_endpoint(tgen, host_name, vm_name, ip, mac):
    """Creates a MACVLAN interface on the specified host to simulate a VM/Container"""
    host = tgen.gears[host_name]
    print(f"Creating MACVLAN {vm_name} on {host_name} with IP {ip} MAC {mac}")

    # 1. Add link of type macvlan linked to the physical bond (vtepbond)
    host.run(f"ip link add link vtepbond name {vm_name} type macvlan mode bridge")

    # 2. Set MAC address
    host.run(f"ip link set dev {vm_name} address {mac}")

    # 3. Set IP address
    host.run(f"ip addr add {ip} dev {vm_name}")

    # 4. Bring up
    host.run(f"ip link set dev {vm_name} up")


def delete_macvlan_endpoint(tgen, host_name, vm_name):
    """Deletes the MACVLAN interface to simulate VM departure"""
    host = tgen.gears[host_name]
    print(f"Deleting MACVLAN {vm_name} from {host_name}")
    host.run(f"ip link del {vm_name}")


def verify_ping(tgen, host_name, interface, target_ip, count=3):
    """Pings from the specific interface"""
    host = tgen.gears[host_name]
    cmd = f"ping -I {interface} -c {count} {target_ip}"
    output = host.run(cmd)
    if "0% packet loss" in output:
        return True
    return False


def test_mobility(tgen):
    """
    Simulates a host moving from VTEP 1 (vtep1) to VTEP 2 (vtep2).

    Requirement:
    1. Create dummy1 on host1. Ping GW.
    2. Delete dummy1 on host1.
    3. Create dummy1 on host2. Ping GW.
    """

    # If any router has previously failed in another test, skip this one.
    if tgen.routers_have_failure():
        pytest.skip(f"skipped because of previous test failure\n {tgen.errors}")

    # define our roaming "VM" parameters
    vm_name = "dummy1"
    vm_ip = "192.168.0.99/16"      # using unused IP in VNI 1000 subnet
    vm_mac = "00:aa:bb:cc:dd:99"
    gateway_ip = "192.168.0.250"   # anycast Gateway

    # start our packet capturing
    print("\nStarting Packet Capturing on spine1...")
    spine = tgen.gears["spine1"]
    pcap_dir = os.path.join(tgen.logdir, "spine1")
    pcap_file = os.path.join(pcap_dir, "evpn_mobility.pcap")
    spine.run("mkdir -p {}".format(pcap_dir))
    # capture on spine for BGP (TCP/179). Use full flags so tcpdump
    # runs detached and writes immediately (-s 0). Save pid for cleanup.
    spine.run(
        "tcpdump -nni any -s 0 -w {} port 179 & echo $! > /tmp/tcpdump_evpn.pid".format(
            pcap_file
        ),
        stdout=None,
    )

    # give tcpdump a moment to start
    sleep(1)

    print("\n=== Starting Mobility Simulation Test ===\n")

    # --- Step 1: Deploy on host1 (VTEP 1) --- #
    create_macvlan_endpoint(tgen, "host1", vm_name, vm_ip, vm_mac)

    # give BGP/EVPN a moment to advertise
    sleep(2)

    # verify connectivity
    print("Testing connectivity from Location A (host1)...")

    success = verify_ping(tgen, "host1", vm_name, gateway_ip)

    # Check EVPN state on vtep1 before migration
    vtep1 = tgen.gears["vtep1"]
    print("EVPN MAC state on vtep1 BEFORE migration:")
    evpn_state = vtep1.vtysh_cmd("show evpn mac vni 1000")
    print(evpn_state)

    # verifies 'success' is True, otherwise raises an AssertionError, halts test
    assert success, "Ping failed from Location A (host1)"

    print("SUCCESS: Location A connectivity established.")

    # --- Step 2: Migrate (Delete from A) --- #
    print("Migrating... Deleting from Location A.")
    delete_macvlan_endpoint(tgen, "host1", vm_name)

    # give EVPN time to process the withdrawal
    sleep(2)

    # Check EVPN state on vtep1 after migration
    print("EVPN MAC state on vtep1 AFTER deletion:")
    evpn_state = vtep1.vtysh_cmd("show evpn mac vni 1000")
    print(evpn_state)

    # --- Step 3: Arrive on host2 (VTEP 2) --- #
    print("Arriving... Creating on Location B (host2).")
    create_macvlan_endpoint(tgen, "host2", vm_name, vm_ip, vm_mac)

    # give eVPN time to update routes
    sleep(5)

    # Check EVPN state on vtep2 after arrival
    vtep2 = tgen.gears["vtep2"]
    print("EVPN MAC state on vtep2 AFTER arrival:")
    evpn_state = vtep2.vtysh_cmd("show evpn mac vni 1000")
    print(evpn_state)

    # --- Step 4: Verify New Connectivity --- #
    print("Testing connectivity from Location B (host2)...")

    # force an ARP update (gratuitous ARP usually handles this, sending a ping triggers traffic)
    success = verify_ping(tgen, "host2", vm_name, gateway_ip)
    assert success, "Ping failed from Location B (host2) after migration"

    print("SUCCESS: Location B connectivity established. Mobility simulation complete.")

    # stop capture and flush output
    spine.run("if [ -f /tmp/tcpdump_evpn.pid ]; then kill $(cat /tmp/tcpdump_evpn.pid); fi")
    spine.run("sleep 1")

    # run test with MUNET_CLI=1 to drop into CLI after test completes
    if os.getenv("MUNET_CLI") == "1":
        tgen.mininet_cli()  # this drops you into the 'munet>' prompt


def test_get_version(tgen):
    "Test that logs the FRR version"

    vtep1 = tgen.gears["vtep1"]
    version = vtep1.vtysh_cmd("show evpn mac vni 1000")
    print("EVPN MAC table: " + version)


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
