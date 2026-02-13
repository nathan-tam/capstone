#!/usr/bin/env python

"""
test_evpn_capstone.py: Testing EVPN with L2VNI and L3VNI
"""

import os
import sys
import subprocess
import time
import pytest
import json
import platform
from functools import partial

# import topogen and topotest helpers
from lib import topotest
from lib.topogen import Topogen, TopoRouter, get_topogen

# select markers based on daemons used during test
pytestmark = [pytest.mark.bgpd, pytest.mark.pimd]

# Save the Current Working Directory
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

#####################################################
##   Network Topology Definition
#####################################################

# function we pass to Topogen to build our topology
def build_topo(tgen):
    tgen.add_router("spine1")
    tgen.add_router("spine2")
    tgen.add_router("torm11")
    tgen.add_router("torm12")
    tgen.add_router("torm21")
    tgen.add_router("torm22")
    tgen.add_router("hostd11")
    tgen.add_router("hostd12")
    tgen.add_router("hostd21")
    tgen.add_router("hostd22")

    # Connect Spine1
    switch = tgen.add_switch("sw1")
    switch.add_link(tgen.gears["spine1"])
    switch.add_link(tgen.gears["torm11"])
    switch = tgen.add_switch("sw2")
    switch.add_link(tgen.gears["spine1"])
    switch.add_link(tgen.gears["torm12"])
    switch = tgen.add_switch("sw3")
    switch.add_link(tgen.gears["spine1"])
    switch.add_link(tgen.gears["torm21"])
    switch = tgen.add_switch("sw4")
    switch.add_link(tgen.gears["spine1"])
    switch.add_link(tgen.gears["torm22"])

    # Connect Spine2
    switch = tgen.add_switch("sw5")
    switch.add_link(tgen.gears["spine2"])
    switch.add_link(tgen.gears["torm11"])
    switch = tgen.add_switch("sw6")
    switch.add_link(tgen.gears["spine2"])
    switch.add_link(tgen.gears["torm12"])
    switch = tgen.add_switch("sw7")
    switch.add_link(tgen.gears["spine2"])
    switch.add_link(tgen.gears["torm21"])
    switch = tgen.add_switch("sw8")
    switch.add_link(tgen.gears["spine2"])
    switch.add_link(tgen.gears["torm22"])

    # Connect Hosts to TORs
    switch = tgen.add_switch("sw9")
    switch.add_link(tgen.gears["torm11"])
    switch.add_link(tgen.gears["hostd11"])

    switch = tgen.add_switch("sw12")
    switch.add_link(tgen.gears["torm12"])
    switch.add_link(tgen.gears["hostd12"])

    switch = tgen.add_switch("sw13")
    switch.add_link(tgen.gears["torm21"])
    switch.add_link(tgen.gears["hostd21"])

    switch = tgen.add_switch("sw16")
    switch.add_link(tgen.gears["torm22"])
    switch.add_link(tgen.gears["hostd22"])

#####################################################
##   Configuration Functions
#####################################################

tor_ips = {
    "torm11": "192.168.100.15",
    "torm12": "192.168.100.16",
    "torm21": "192.168.100.17",
    "torm22": "192.168.100.18",
}

svi_ips = {
    "torm11": "45.0.0.2",
    "torm12": "45.0.0.3",
    "torm21": "45.0.0.4",
    "torm22": "45.0.0.5",
}

def config_bond(node, bond_name, bond_members, bond_ad_sys_mac, br):
    node.run("ip link add dev %s type bond mode 802.3ad" % bond_name)
    node.run("ip link set dev %s type bond lacp_rate 1" % bond_name)
    node.run("ip link set dev %s type bond miimon 100" % bond_name)
    node.run("ip link set dev %s type bond xmit_hash_policy layer3+4" % bond_name)
    node.run("ip link set dev %s type bond min_links 1" % bond_name)
    node.run("ip link set dev %s type bond ad_actor_system %s" % (bond_name, bond_ad_sys_mac))

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

def config_l3vni(tor_name, node, vtep_ip):
    node.run("ip link add vrf500 type vrf table 500")
    node.run("ip link set vrf500 up")
    node.run("ip link add br500 type bridge")
    node.run("ip link set br500 master vrf500 addrgenmode none")
    
    mac_map = {
        "torm11": "aa:bb:cc:00:00:11",
        "torm12": "aa:bb:cc:00:00:12",
        "torm21": "aa:bb:cc:00:00:21",
        "torm22": "aa:bb:cc:00:00:22"
    }
    if tor_name in mac_map:
        node.run("ip link set br500 addr %s" % mac_map[tor_name])

    node.run("ip link add vni500 type vxlan local %s dstport 4789 id 500 nolearning" % vtep_ip)
    node.run("ip link set vni500 master br500 addrgenmode none")
    node.run("/sbin/bridge link set dev vni500 learning off")
    node.run("ip link set vni500 up")
    node.run("ip link set br500 up")

def config_l2vni(tor_name, node, svi_ip, vtep_ip):
    # Setup VNI 1000 (Subnet 45.0.0.0/24)
    node.run("ip link add br1000 type bridge")
    node.run("ip link set br1000 master vrf500")
    node.run("ip addr add %s/24 dev br1000" % svi_ip)
    node.run("ip addr add 45.0.0.1/24 dev br1000") # Anycast GW
    node.run("/sbin/sysctl net.ipv4.conf.br1000.arp_accept=1")

    node.run("ip link add vni1000 type vxlan local %s dstport 4789 id 1000 nolearning" % vtep_ip)
    node.run("ip link set vni1000 master br1000 addrgenmode none")
    node.run("/sbin/bridge link set dev vni1000 learning off")
    node.run("ip link set vni1000 up")
    node.run("ip link set br1000 up")

    node.run("/sbin/bridge vlan del vid 1 dev vni1000")
    node.run("/sbin/bridge vlan del vid 1 untagged pvid dev vni1000")
    node.run("/sbin/bridge vlan add vid 1000 dev vni1000")
    node.run("/sbin/bridge vlan add vid 1000 untagged pvid dev vni1000")

    # Setup VNI 2000 (Subnet 20.0.0.0/24) only for Rack 2
    if "torm2" in tor_name:
        node.run("ip link add br2000 type bridge")
        node.run("ip link set br2000 master vrf500")
        node.run("ip addr add 20.0.0.20/24 dev br2000")
        node.run("/sbin/sysctl net.ipv4.conf.br2000.arp_accept=1")
        node.run("ip link add vni2000 type vxlan local %s dstport 4789 id 2000 nolearning" % vtep_ip)
        node.run("ip link set vni2000 master br2000 addrgenmode none")
        node.run("/sbin/bridge link set dev vni2000 learning off")
        node.run("ip link set vni2000 up")
        node.run("ip link set br2000 up")
        node.run("/sbin/bridge vlan del vid 1 dev vni2000")
        node.run("/sbin/bridge vlan del vid 1 untagged pvid dev vni2000")
        node.run("/sbin/bridge vlan add vid 1000 dev vni2000")
        node.run("/sbin/bridge vlan add vid 1000 untagged pvid dev vni2000")

def config_tor(tor_name, tor, tor_ip, svi_pip):
    config_l3vni(tor_name, tor, tor_ip)
    config_l2vni(tor_name, tor, svi_pip, tor_ip)

    if "torm1" in tor_name:
        sys_mac = "44:38:39:ff:ff:01"
    else:
        sys_mac = "44:38:39:ff:ff:02"

    bond_member = tor_name + "-eth2"
    
    # --- FIXED LOGIC FROM ORIGINAL FILE ---
    # Assign hostbond to correct bridge based on rack/subnet
    if "torm11" in tor_name:
        config_bond(tor, "hostbond1", [bond_member], sys_mac, "br1000")
    elif "torm12" in tor_name:
        config_bond(tor, "hostbond1", [bond_member], sys_mac, "br1000")
    elif "torm21" in tor_name:  # <--- FIXED TYPO (Was torm12)
        config_bond(tor, "hostbond1", [bond_member], sys_mac, "br1000")
    else:
        # torm22 uses VNI 2000
        config_bond(tor, "hostbond1", [bond_member], sys_mac, "br2000")

def config_tors(tgen, tors):
    for tor_name in tors:
        tor = tgen.gears[tor_name]
        config_tor(tor_name, tor, tor_ips.get(tor_name), svi_ips.get(tor_name))

def compute_host_ip_mac(host_name):
    host_id = host_name.split("hostd")[1]
    if host_name == "hostd22":
        host_ip = "20.0.0." + host_id + "/24"
    else:
        host_ip = "45.0.0." + host_id + "/24"
    host_mac = "00:00:00:00:00:" + host_id
    return host_ip, host_mac

def config_host(host_name, host):
    bond_members = [host_name + "-eth0"]
    bond_name = "torbond"
    config_bond(host, bond_name, bond_members, "00:00:00:00:00:00", None)

    host_ip, host_mac = compute_host_ip_mac(host_name)
    host.run("ip addr add %s dev %s" % (host_ip, bond_name))
    host.run("ip link set dev %s address %s" % (bond_name, host_mac))

def config_hosts(tgen, hosts):
    for host_name in hosts:
        host = tgen.gears[host_name]
        config_host(host_name, host)

def setup_module(module):
    "Setup topology"
    tgen = Topogen(build_topo, module.__name__)
    tgen.start_topology()

    krel = platform.release()
    if topotest.version_cmp(krel, "4.19") < 0:
        tgen.errors = "kernel 4.19 needed for multihoming tests"
        pytest.skip(tgen.errors)

    tors = ["torm11", "torm12", "torm21", "torm22"]
    config_tors(tgen, tors)

    hosts = ["hostd11", "hostd12", "hostd21", "hostd22"]
    config_hosts(tgen, hosts)

    router_list = tgen.routers()
    for rname, router in router_list.items():
        router.load_config(TopoRouter.RD_ZEBRA, os.path.join(CWD, "{}/zebra.conf".format(rname)))
        router.load_config(TopoRouter.RD_BGP, os.path.join(CWD, "{}/evpn.conf".format(rname)))
    tgen.start_router()

def teardown_module(_mod):
    tgen = get_topogen()
    tgen.stop_topology()

#####################################################
##   Mobility Simulation Test
#####################################################

def create_macvlan_endpoint(tgen, host_name, vm_name, ip, mac):
    """Creates a MACVLAN interface on the specified host to simulate a VM/Container"""
    host = tgen.gears[host_name]
    logger = host.logger
    logger.info(f"Creating MACVLAN {vm_name} on {host_name} with IP {ip} MAC {mac}")
    
    # 1. Add link of type macvlan linked to the physical bond (torbond)
    host.run(f"ip link add link torbond name {vm_name} type macvlan mode bridge")
    
    # 2. Set MAC address
    host.run(f"ip link set dev {vm_name} address {mac}")
    
    # 3. Set IP address
    host.run(f"ip addr add {ip} dev {vm_name}")
    
    # 4. Bring up
    host.run(f"ip link set dev {vm_name} up")

def delete_macvlan_endpoint(tgen, host_name, vm_name):
    """Deletes the MACVLAN interface to simulate VM departure"""
    host = tgen.gears[host_name]
    host.logger.info(f"Deleting MACVLAN {vm_name} from {host_name}")
    host.run(f"ip link del {vm_name}")

def verify_ping(tgen, host_name, interface, target_ip, count=3):
    """Pings from the specific interface"""
    host = tgen.gears[host_name]
    cmd = f"ping -I {interface} -c {count} {target_ip}"
    output = host.run(cmd)
    if "0% packet loss" in output:
        return True
    return False

def test_mobility():
    """
    Simulates a host moving from VTEP 1 (torm11) to VTEP 2 (torm21).
    
    Requirement: 
    1. Create dummy1 on hostd11. Ping GW.
    2. Delete dummy1 on hostd11.
    3. Create dummy1 on hostd21. Ping GW.
    """

    tgen = get_topogen()
    
    # define our roaming "VM" parameters
    vm_name = "dummy1"
    vm_ip = "45.0.0.99/24"      # using unused IP in VNI 1000 subnet
    vm_target_ip = "45.0.0.99"
    vm_mac = "00:aa:bb:cc:dd:99"
    gateway_ip = "45.0.0.1"     # anycast Gateway

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

    # give tcpdump a moment to start (necessary?)
    time.sleep(1)
    
    print("\n=== Starting Mobility Simulation Test ===\n")

    # --- Step 1: Deploy on hostd11 (VTEP 1) --- #
    create_macvlan_endpoint(tgen, "hostd11", vm_name, vm_ip, vm_mac)
    
    # give BGP/EVPN a moment to advertise (necessary?)
    time.sleep(2)
    
    # verify connectivity
    print("Testing connectivity from Location A (hostd11)...")
    
    success = verify_ping(tgen, "hostd11", vm_name, gateway_ip)
    
    # verifies 'success' is True. otherwise raises an AssertionError, halts test
    assert success, "Ping failed from Location A (hostd11)"
    
    print("SUCCESS: Location A connectivity established.")

    # --- Step 2: Migrate (Delete from A) --- #
    print("Migrating... Deleting from Location A.")
    delete_macvlan_endpoint(tgen, "hostd11", vm_name)
    
    # --- Step 3: Arrive on hostd21 (VTEP 2) --- #
    print("Arriving... Creating on Location B (hostd21).")
    create_macvlan_endpoint(tgen, "hostd21", vm_name, vm_ip, vm_mac)
    
    # give eVPN time to update routes (necessary?)
    time.sleep(5) 
    
    # --- Step 4: Verify New Connectivity --- #
    print("Testing connectivity from Location B (hostd21)...")
    
    # force an ARP update (gratuitous ARP usually handles this, sending a ping triggers traffic)
    success = verify_ping(tgen, "hostd21", vm_name, gateway_ip)
    assert success, "Ping failed from Location B (hostd21) after migration"
    
    print("SUCCESS: Location B connectivity established. Mobility simulation complete.")

    # stop capture and flush output
    spine.run("if [ -f /tmp/tcpdump_evpn.pid ]; then kill $(cat /tmp/tcpdump_evpn.pid); fi")
    spine.run("sleep 1")

    # run test with MUNET_CLI=1 to drop into CLI after test completes
    if os.getenv("MUNET_CLI") == "1":
        tgen.mininet_cli()  # this drops you into the 'munet>' prompt

if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))