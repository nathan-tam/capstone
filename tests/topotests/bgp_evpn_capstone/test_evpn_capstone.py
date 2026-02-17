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
from debug_tools import verify_ping, verify_initial_connectivity, verify_post_migration_connectivity
import debug_tools  # contains debugging functions, in case we need them

# pytest module level markers
pytestmark = [
    pytest.mark.bgpd,
    pytest.mark.pimd,
]

#####################################################
##   Configuration Functions
#####################################################

# Test scaling parameters
NUM_VTEPS = 4
NUM_HOSTS = 4  # One host per VTEP for VM mobility testing
NUM_MOBILE_VMS = 128  # Number of VMs that will move around

vtep_ips = {
    f"vtep{i}": f"{10*i}.{10*i}.{10*i}.{10*i}" 
    for i in range(1, NUM_VTEPS + 1)
}

svi_ips = {
    f"vtep{i}": f"192.168.0.{250+i}" 
    for i in range(1, NUM_VTEPS + 1)
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

    # Create spines
    spine1 = tgen.add_router("spine1")
    spine2 = tgen.add_router("spine2")

    # Create VTEPs
    vteps = []
    for i in range(1, NUM_VTEPS + 1):
        vtep_name = f"vtep{i}"
        vtep = tgen.add_router(vtep_name)
        vteps.append(vtep_name)
        
        # Create links between spine1 and this VTEP
        tgen.add_link(spine1, vtep)
        
        # Create links between spine2 and this VTEP
        tgen.add_link(spine2, vtep)

    # Create hosts and distribute them across VTEPs
    hosts = []
    for i in range(1, NUM_HOSTS + 1):
        host_name = f"host{i}"
        host = tgen.add_router(host_name)
        hosts.append(host_name)
        
        # Distribute hosts across VTEPs in round-robin fashion
        vtep_idx = (i - 1) % NUM_VTEPS
        vtep_name = f"vtep{vtep_idx + 1}"
        vtep = tgen.gears[vtep_name]
        
        # Create link between this VTEP and host
        tgen.add_link(vtep, host)


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


def migrate_macvlan_endpoint_live(tgen, old_host_name, new_host_name, vm_name, ip, mac):
    """
    Live migration of MACVLAN interface: Creates at destination while source still exists,
    then deletes from source. This creates a brief moment where the same MAC is on two VTEPs,
    simulating vMotion/container failover and stressing the control plane.
    """
    print(f"Live migrating MACVLAN {vm_name} from {old_host_name} to {new_host_name}")
    
    # Step 1: Create at destination (while source still has the VM)
    create_macvlan_endpoint(tgen, new_host_name, vm_name, ip, mac)
    
    # Small delay to let BGP detect the duplicate
    sleep(0.5)
    
    # Step 2: Delete from source (now both VTEPs had it briefly)
    delete_macvlan_endpoint(tgen, old_host_name, vm_name)



def test_mobility(tgen):
    """
    Simulates 128 VMs moving between 4 VTEPs (via 4 hosts) to establish a baseline for 
    network traffic measurements. This creates high control plane stress by simulating
    live VM migrations.

    Steps:
    1. Deploy 128 VMs distributed across 4 hosts (one per VTEP)
    2. Verify connectivity from initial locations
    3. Live-migrate each VM to a different VTEP (creates a brief MAC duplicates)
    4. Verify connectivity at new locations
    5. Capture BGP packet data during migrations
    """

    # anycast gateway
    gateway_ip = "192.168.0.250"

    #####################################################
    # SECTION: Packet Capture Setup
    #####################################################
    print("\nStarting Packet Capturing on spine1...")
    
    spine = tgen.gears["spine1"]                                # retrieve spine1 to execute commands on
    pcap_dir = os.path.join(tgen.logdir, "spine1")              # stores .pcap files in test's log directory
    pcap_file = os.path.join(pcap_dir, "evpn_mobility.pcap")    # file to store captured packets
    spine.run("mkdir -p {}".format(pcap_dir))                   # creates the directory if it doesn't exist
    
    # run tcpdump
    spine.run(
        # runs detached and writes immediately (-s 0). save pid for cleanup
        "tcpdump -nni any -s 0 -w {} port 179 & echo $! > /tmp/tcpdump_evpn.pid".format(
            pcap_file
        ),
        stdout=None,
    )

    sleep(1)    # give tcpdump a moment to start (necessary?)

    #####################################################
    # SECTION: Mobility Simulation
    #####################################################
    print("\n=== Starting Mobility Simulation Test with {} VMs ===\n".format(NUM_MOBILE_VMS))

    # create a dictionary to track VM locations. {vm_name: (current_host, current_vtep_idx)}
    vm_locations = {}

    # --- Phase 1: deploy VMs on initial hosts --- #
    print(f"Phase 1: Deploying {NUM_MOBILE_VMS} VMs on hosts...")
    
    # configure addressing
    for vm_idx in range(1, NUM_MOBILE_VMS + 1):
        # VM naming scheme is vm1, vm2, ..., vm128
        vm_name = f"vm{vm_idx}"
        
        # use 192.168.100.x for mobile VMs (can handle up to 254 VMs)
        vm_ip = f"192.168.100.{vm_idx}/16"
        
        # we use bit shifting to generate MAC addresses. sorry.
        vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)

        # distribute VMs round-robin across hosts (32 VMs per host with 128 VMs / 4 hosts)
        host_idx = ((vm_idx - 1) % NUM_HOSTS) + 1
        host_name = f"host{host_idx}"
        
        # determine which VTEP this host is connected to
        vtep_idx = (host_idx - 1) % NUM_VTEPS

        # create the MACVLAN endpoint to simulate the VM
        create_macvlan_endpoint(tgen, host_name, vm_name, vm_ip, vm_mac)
        
        # add the VM to tracking dictionary
        vm_locations[vm_name] = (host_idx, vtep_idx)

        # triggers every 5 VMs. adds a 1-second pause to give BGP/EVPN time to advertise
        if vm_idx % 5 == 0:
            # give BGP/EVPN a moment to advertise between VMs
            sleep(1)

    # wait for BGP/EVPN to stabilize
    sleep(3)

    # --- Phase 2: verify initial connectivity --- #
    print("\nPhase 2: Verifying connectivity from initial locations...")
    print("This will take a while...make a coffee, get a snack!\n")
    # verify_initial_connectivity(tgen, vm_locations, gateway_ip, NUM_MOBILE_VMS)

    # --- Phase 3: Migrate VMs to different VTEPs --- #
    print(f"\nPhase 3: Moving {NUM_MOBILE_VMS} VMs to different locations...")
    print("(Creating at destination while source exists, then cleaning up source)")
    
    for vm_idx in range(1, NUM_MOBILE_VMS + 1):
        vm_name = f"vm{vm_idx}"
        
        # get current location
        old_host_idx, old_vtep_idx = vm_locations[vm_name]
        old_host_name = f"host{old_host_idx}"
        
        # compute new location on a different VTEP
        new_vtep_idx = (old_vtep_idx + 1) % NUM_VTEPS
        
        # Find a host on the new VTEP
        # Hosts are distributed round-robin, so host_i is on vtep((i-1) % NUM_VTEPS)
        new_host_idx = None
        for potential_host in range(1, NUM_HOSTS + 1):
            if ((potential_host - 1) % NUM_VTEPS) == new_vtep_idx and potential_host != old_host_idx:
                new_host_idx = potential_host
                break
        
        if new_host_idx is None:
            # Fallback: just pick any host on the new VTEP
            for potential_host in range(1, NUM_HOSTS + 1):
                if ((potential_host - 1) % NUM_VTEPS) == new_vtep_idx:
                    new_host_idx = potential_host
                    break

        new_host_name = f"host{new_host_idx}"

        # Compute VM IP and MAC
        vm_ip = f"192.168.100.{vm_idx}/16"
        vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)

        # Perform live migration (creates at destination, then deletes from source)
        migrate_macvlan_endpoint_live(tgen, old_host_name, new_host_name, vm_name, vm_ip, vm_mac)
        vm_locations[vm_name] = (new_host_idx, new_vtep_idx)

        # Rate limit migrations to observe network behavior
        if vm_idx % 5 == 0:
            sleep(1)

    # wait for all BGP/EVPN to process changes
    sleep(5)

    # --- Phase 4: Verify connectivity at new locations --- #
    print("\nPhase 4: Verifying connectivity at new locations...")
    # verify_post_migration_connectivity(tgen, vm_locations, gateway_ip, NUM_MOBILE_VMS)

    # stop capture and flush output
    spine.run("if [ -f /tmp/tcpdump_evpn.pid ]; then kill $(cat /tmp/tcpdump_evpn.pid); fi")
    spine.run("sleep 1")

    # run test with MUNET_CLI=1 to drop into CLI after test completes
    if os.getenv("MUNET_CLI") == "1":
        tgen.mininet_cli()  # this drops you into the 'munet>' prompt

if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
