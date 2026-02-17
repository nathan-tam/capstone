#!/usr/bin/env python
# -*- coding: utf-8 eval: (blacken-mode 1) -*-
# SPDX-License-Identifier: ISC
#
# <template>.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2017 by
# Network Device Education Foundation, Inc. ("NetDEF")
#

"""
<template>.py: Test <template>.
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

# TODO: select markers based on daemons used during test
# pytest module level markers
pytestmark = [
    # pytest.mark.babeld,
    # pytest.mark.bfdd,
    pytest.mark.bgpd,
    # pytest.mark.eigrpd,
    # pytest.mark.isisd,
    # pytest.mark.ldpd,
    # pytest.mark.nhrpd,
    # pytest.mark.ospf6d,
    # pytest.mark.ospfd,
    # pytest.mark.pathd,
    # pytest.mark.pbrd,
    pytest.mark.pimd,
    # pytest.mark.ripd,
    # pytest.mark.ripngd,
    # pytest.mark.sharpd,
    # pytest.mark.staticd,
    # pytest.mark.vrrpd,
]


def config_bond(node, bond_name, bond_members, bond_ad_sys_mac, br):
    """
    Used to setup bonds on the TORs and hosts for MH
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
    On torm1x amd torm21,
    Create a VxLAN device for VNI 1000 and add it to the bridge.
    VLAN-1000 is mapped to VNI-1000.

    Creates a Linux bridge br1000 tied to VRF vrf500.
    Assigns VLAN 1000 IP addresses to the bridge (SVI).
    Creates a VXLAN interface for VNI 1000 tied to the VTEP IP.
    Adds the VXLAN interface to the bridge.
    Disables MAC learning on VXLAN (because BGP EVPN handles MAC learning).
    Configures VLAN 1000 on the VXLAN interface.
    Brings interfaces up to start forwarding traffic.
    """
    # Create a bridge br1000 and assign SVI IP
    node.run("ip link add br1000 type bridge")
    # node.run("ip link set br1000 master vrf500")
    node.run("ip addr add %s/16 dev br1000" % svi_ip)
    # NOTE TO SELF: Is this the any gateway address for hosts to reach: 
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

dummy_to_host_map = {}
# Change this value to increase/decrease number of dummy hosts created
# This number of dummy interfaces will be divided among host1, host2, and host3
number_of_dummy=600

def config_vtep(vtep_name, vtep, vtep_ip, svi_pip):
    """
    Create the bond/vxlan-bridge on the TOR which acts as VTEP and EPN-PE
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
    Create the dual-attached bond on host nodes for MH
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

def compute_dummy_ip_mac(dummy_name):
    dummy_id = dummy_name.split("dummy")[1]
    # IP calculation: 192.168.(dummy_id // 256).(dummy_id % 256)
    octet_3 = int(dummy_id) // 256
    octet_4 = int(dummy_id) % 256
    dummy_ip = f"192.168.{octet_3}.{octet_4}/16"

    # MAC calculation: keep fixed prefix + last 2 bytes from dummy_id
    # Using ff as the 5th byte as before, and last byte = dummy_id mod 256
    # We can also encode dummy_id // 256 as the 4th byte for uniqueness
    mac_byte_4 = octet_3  # 0-3
    mac_byte_5 = 0xff     # fixed
    mac_byte_6 = octet_4  # 0-255
    dummy_mac = f"00:00:{mac_byte_4:02x}:00:{mac_byte_5:02x}:{mac_byte_6:02x}"
    return dummy_ip, dummy_mac

def config_dummy(dummy_name, host):
    dummy_ip, dummy_mac = compute_dummy_ip_mac(dummy_name)

    # Create macvlan interface on vtepbond
    host.run(f"ip link add link vtepbond name {dummy_name} type macvlan mode bridge")
    
    host.run(f"ip link set dev {dummy_name} address {dummy_mac}")
    host.run(f"ip addr add {dummy_ip} dev {dummy_name}")
    host.run(f"ip link set dev {dummy_name} up")

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

    # Create a p2p connection between r1 and r2
    tgen.add_link(spine1, vtep1)
    tgen.add_link(spine1, vtep2)
    tgen.add_link(spine1, vtep3)

    tgen.add_link(spine2, vtep1)
    tgen.add_link(spine2, vtep2)
    tgen.add_link(spine2, vtep3)

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

    vteps = []
    vteps.append("vtep1")
    vteps.append("vtep2")
    vteps.append("vtep3")
    config_vteps(tgen, vteps)

    hosts = []
    hosts.append("host1")
    hosts.append("host2")
    hosts.append("host3")
    config_hosts(tgen, hosts)

    for i in range(1,number_of_dummy+1):
        select_host = "host" + str(((i-1) % 3)+1)
        dummy_to_host_map["dummy" + str(i)] = select_host
        config_dummy("dummy" + str(i), tgen.gears[select_host] )
    
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

# ===================
# The tests functions
# ===================
def start_background_ping(host_name, target_ip):
    """
    Start continuous ping in the background. Returns the PID of the ping.
    """
    tgen = get_topogen()
    host = tgen.gears[host_name]
    # -D prints timestamp, very helpful for debugging mobility events.
    cmd = f"ping -i 0.1 -D {target_ip} > /tmp/outputs/ping_output_{host_name}_to_{target_ip}.log 2>&1 & echo $!"
    pid = host.run(cmd).strip()
    return pid

def start_packet_capture(server_name, capture_name='evpn_bgp_test_noname.pcap'):
    """
    Start continuous ping in the background. Returns the PID of the ping.
    """
    tgen = get_topogen()
    server = tgen.gears[server_name]
    cmd = f"sudo tcpdump -ni any '(port 179 or arp)' -ttt -w /tmp/outputs/{capture_name} > /dev/null 2>&1 & echo $!"
    pid = server.run(cmd).strip()
    return pid

def stop_background_ping(host_name, pid):
    tgen = get_topogen()
    host = tgen.gears[host_name]
    if host_name == "vtep1":
        host.run(f"kill {pid}")
    else:
        host.run(f"kill -2 {pid} || true")

def get_dummy_mac(dummy_name):
    """Return the MAC associated with a dummy interface name."""
    dummy_id = dummy_name.replace("dummy", "")
    return f"00:00:00:00:ff:{int(dummy_id):02x}"

def start_fdb_monitor_on_node(node, out_path="/tmp/outputs/fdb_vtep1_sorry6.txt", pid_path="/tmp/outputs/fdb_monitor.pid"):
    """
    Install and start a small Python script on the `node` that runs:
        bridge monitor fdb
    and prefixes each output line with a millisecond epoch timestamp.
    The monitor runs in background via nohup and the PID is written to pid_path.
    Returns the PID as a string (or None on failure).
    """
    # ensure outputs dir exists on node
    try:
        node.run("mkdir -p /tmp/outputs")
    except Exception:
        pass

    # small python monitor script; write it to the node
    monitor_py = r'''#!/usr/bin/env python3
import subprocess, sys, time
# Run bridge monitor fdb and prefix each line with ms epoch timestamp
p = subprocess.Popen(["bridge", "monitor", "fdb"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
for ln in p.stdout:
    try:
        sys.stdout.write(f"{int(time.time()*1000)} {ln}")
        sys.stdout.flush()
    except Exception:
        # on any error just continue to avoid monitor dying silently
        pass
'''
    # write the script to node
    node.run("cat > /tmp/outputs/fdb_monitor_vtep1.py <<'PY'\n" + monitor_py + "\nPY")
    node.run("chmod +x /tmp/outputs/fdb_monitor_vtep1.py || true")
    # truncate the output file before starting the script
    node.run(f"> {out_path}")
    # start it in background with nohup, save pid
    # echo pid into pid_path then output it so caller can capture
    start_cmd = (
        f"nohup python3 /tmp/outputs/fdb_monitor_vtep1.py >> {out_path} 2>&1 & echo $! > {pid_path} && cat {pid_path} || true"
    )
    try:
        pid = node.run(start_cmd).strip()
        if not pid:
            return None
        return pid
    except Exception:
        return None


def stop_fdb_monitor_on_node(node, pid_path="/tmp/outputs/fdb_monitor.pid"):
    """
    Kill the monitor whose PID is saved in pid_path on the node (if present).
    Removes pid file.
    """
    try:
        out = node.run(f"test -f {pid_path} && cat {pid_path} || true").strip()
        if out:
            node.run(f"kill {out} || true")
        node.run(f"rm -f {pid_path} || true")
    except Exception:
        pass


def test_host_movement(tgen):

    """
    Test host movement between two TORs using macvlan interfaces.
    Only a single host can move to a VTEP at a time, because the vtepbond
    interface can only hold one MAC address per macvlan interface.
    When the host moves, the MAC address appears at the new VTEP with an
    updated sequence number, so the previous VTEP forwards traffic to the new VTEP.
    This means two hosts cannot be moved to the same VTEP simultaneously
    if they share the same MAC on the vtepbond interface.
    """

    # If any router has previously failed in another test, skip this one.
    if tgen.routers_have_failure():
        pytest.skip(f"skipped because of previous test failure\n {tgen.errors}")
    
    output = "/"
    # print(tester.vtysh_cmd("show evpn mac vni 1000"))
    # pdb.set_trace()
    # Start continuous ping from host3 to monitor connectivity during movement
    
    hosts = ["host1", "host2"]
    delay_data = {}
    pre_movement_details = []
    after_movement_details = []
    def move_host_from(targeted_if,delay=0):
        
        sleep(delay)
        
        # Get the current host name and randomly select a different target host
        current_hostname = dummy_to_host_map[targeted_if]
        possible_targets = [h for h in hosts if h != current_hostname and h != "host3"]
        target_hostname = random.choice(possible_targets)
        # target_hostname = "host1"  # Forcing movement to host2 for easier debugging
        dummy_to_host_map[targeted_if] = target_hostname

        # Get the current and target host nodes
        curr_host = tgen.gears[current_hostname]
        target_host = tgen.gears[target_hostname]

        def run_command_and_expect():
            config_dummy(targeted_if, target_host)
            # Delete the macvlan interface from the previous host
            curr_host.run(f"ip link delete {targeted_if}")
            move_ts_ms = int(time() * 1000)
            dummy_mac = get_dummy_mac(targeted_if)
            if dummy_mac == "00:00:00:00:ff:01":
                after_movement_details.append({
                    "mac": dummy_mac,
                    "time": move_ts_ms,
                    "interface": targeted_if,
                    "from": current_hostname,
                    "to": target_hostname,
                    "delay": delay
                })
            return True

        _, result = topotest.run_and_expect(
        run_command_and_expect,
        True,   # EXPECTED OUTPUT
        count=5,  # Try up to 5 times
        wait=3     # waiting 3 seconds between tries
        )

        # print(f"--- Host moved from {current_hostname} to {target_hostname} at {time()} ---")
        assert result is True, (
        f"The MAC and IP address in {current_hostname} has not moved\n"
        )

    sleep(5)

    fdb_pid = start_fdb_monitor_on_node(tgen.gears["vtep3"], out_path="/tmp/outputs/fdb_vtep3.txt", pid_path="/tmp/outputs/fdb_monitor_vtep3.pid")
    logger.info(f"Started FDB monitor on vtep3 with PID {fdb_pid}")
    
    sleep(5)  # wait for some pings to be sent

    # delays = [2, 1, 0.8, 0.5, 0.2, 0.1, 0]
    delays = [0.01]
    moves = 20 * number_of_dummy

    # delays = [1]
    for delay in delays:
        for i in range(moves):
            select_dummy = "dummy" + str((i % number_of_dummy) + 1)
            # select_dummy = "dummy1"
            move_host_from(select_dummy,delay)
        sleep(5)
    
    sleep(15)

    stop_fdb_monitor_on_node(tgen.gears["vtep3"], pid_path="/tmp/outputs/fdb_monitor_vtep3.pid")
    logger.info("Stopped FDB monitor on vtep3")

    with open("/tmp/outputs/after_movement_details.json", "w") as f:
        json.dump(after_movement_details, f, indent=2)


# def test_host_movement(tgen):

#     """
#     Test host movement between two TORs using macvlan interfaces.
#     Only a single host can move to a VTEP at a time, because the vtepbond
#     interface can only hold one MAC address per macvlan interface.
#     When the host moves, the MAC address appears at the new VTEP with an
#     updated sequence number, so the previous VTEP forwards traffic to the new VTEP.
#     This means two hosts cannot be moved to the same VTEP simultaneously
#     if they share the same MAC on the vtepbond interface.
#     """

#     # If any router has previously failed in another test, skip this one.
#     if tgen.routers_have_failure():
#         pytest.skip(f"skipped because of previous test failure\n {tgen.errors}")
    
#     tester = tgen.gears["vtep2"]
#     output = "/"
#     # print(tester.vtysh_cmd("show evpn mac vni 1000"))
#     # pdb.set_trace()
#     # Start continuous ping from host3 to monitor connectivity during movement
    
#     hosts = ["host1", "host2"]
#     delay_data = {}
#     pre_movement_details = []
#     after_movement_details = []
#     def move_host_from(targeted_if,delay=0):
        
#         sleep(delay)
#         raw = tester.vtysh_cmd("show evpn mac vni 1000 mac 00:00:00:00:ff:01 json")
#         parsed = json.loads(raw)
        
#         if delay not in delay_data:
#             delay_data[delay] = []
#         delay_data[delay].append(parsed)
        
#         # Get the current host name and randomly select a different target host
#         current_hostname = dummy_to_host_map[targeted_if]
#         possible_targets = [h for h in hosts if h != current_hostname and h != "host3"]
#         target_hostname = random.choice(possible_targets)
#         # target_hostname = "host1"  # Forcing movement to host2 for easier debugging
#         dummy_to_host_map[targeted_if] = target_hostname
#         move_ts_ms = int(time() * 1000)
#         dummy_mac = get_dummy_mac(targeted_if)
#         if dummy_mac == "00:00:00:00:ff:01":
#             pre_movement_details.append({
#                 "mac": dummy_mac,
#                 "time": move_ts_ms,
#                 "interface": targeted_if,
#                 "from": current_hostname,
#                 "to": target_hostname,
#                 "delay": delay
#             })

#         # Get the current and target host nodes
#         curr_host = tgen.gears[current_hostname]
#         target_host = tgen.gears[target_hostname]

#         def run_command_and_expect():
#             config_dummy(targeted_if, target_host)
#             # Delete the macvlan interface from the previous host
#             curr_host.run(f"ip link delete {targeted_if}")
#             move_ts_ms = int(time() * 1000)
#             dummy_mac = get_dummy_mac(targeted_if)
#             if dummy_mac == "00:00:00:00:ff:01":
#                 after_movement_details.append({
#                     "mac": dummy_mac,
#                     "time": move_ts_ms,
#                     "interface": targeted_if,
#                     "from": current_hostname,
#                     "to": target_hostname,
#                     "delay": delay
#                 })
#             return True

#         _, result = topotest.run_and_expect(
#         run_command_and_expect,
#         True,   # EXPECTED OUTPUT
#         count=5,  # Try up to 5 times
#         wait=3     # waiting 3 seconds between tries
#         )
        

#         # print(f"--- Host moved from {current_hostname} to {target_hostname} at {time()} ---")
#         assert result is True, (
#         f"The MAC and IP address in {current_hostname} has not moved\n"
#         )

#     sleep(5)
#     pid_capture1 = start_packet_capture("vtep1", "vtep1_capture.pcap")
#     # # pid_capture2 = start_packet_capture("spine1", "spine1_capture_move_from_vtep2_to_vtep1.pcap")
#     pid_capture2 = start_packet_capture("vtep2", "vtep2_capture.pcap")
#     # pid_capture3 = start_packet_capture("vtep3", "vtep3_various_delays.pcap")
#     # pid_capture4 = start_packet_capture("vtep4", "vtep4_various_delays.pcap")
#     # pid1 = start_background_ping("host4", "192.168.0.1")
#     # pid2 = start_background_ping("host4", "192.168.0.2")
#     # pid3 = start_background_ping("host4", "192.168.0.3")
#     fdb_pid = start_fdb_monitor_on_node(tgen.gears["vtep1"], out_path="/tmp/outputs/fdb_vtep1.txt", pid_path="/tmp/outputs/fdb_monitor_vtep1.pid")
#     logger.info(f"Started FDB monitor on vtep1 with PID {fdb_pid}")
#     fdb_pid = start_fdb_monitor_on_node(tgen.gears["vtep2"], out_path="/tmp/outputs/fdb_vtep2.txt", pid_path="/tmp/outputs/fdb_monitor_vtep2.pid")
#     logger.info(f"Started FDB monitor on vtep2 with PID {fdb_pid}")
    
#     sleep(5)  # wait for some pings to be sent

#     # delays = [2, 1, 0.8, 0.5, 0.2, 0.1, 0]
#     delays = [0.2]
#     moves = 20 * number_of_dummy

#     # delays = [1]
#     for delay in delays:
#         for i in range(moves):
#             select_dummy = "dummy" + str((i % number_of_dummy) + 1)
#             # select_dummy = "dummy1"
#             move_host_from(select_dummy,delay)
#         sleep(5)
    
#     sleep(15)
        
#     # tester = tgen.gears["vtep1"]
#     # print(tester.vtysh_cmd("show evpn mac vni 1000"))
#     # sleep(5)
#     # stop_background_ping("host4", pid1)
#     # stop_background_ping("host4", pid2)
#     # stop_background_ping("host4", pid3)
#     stop_background_ping("vtep1", pid_capture1)
#     # # stop_background_ping("spine1", pid_capture2)
#     stop_background_ping("vtep2", pid_capture2)
#     # stop_background_ping("vtep3", pid_capture3)
#     # stop_background_ping("vtep4", pid_capture4)
#     stop_fdb_monitor_on_node(tgen.gears["vtep1"], pid_path="/tmp/outputs/fdb_monitor_vtep1.pid")
#     logger.info("Stopped FDB monitor on vtep1")
#     stop_fdb_monitor_on_node(tgen.gears["vtep2"], pid_path="/tmp/outputs/fdb_monitor_vtep2.pid")
#     logger.info("Stopped FDB monitor on vtep2")

#     with open("/tmp/outputs/evpn_show_results.json", "w") as f:
#         json.dump(delay_data, f, indent=2)
#     with open("/tmp/outputs/pre_movement_details.json", "w") as f:
#         json.dump(pre_movement_details, f, indent=2)
#     with open("/tmp/outputs/after_movement_details.json", "w") as f:
#         json.dump(after_movement_details, f, indent=2)


def test_get_version(tgen):
    "Test the logs the FRR version"

    r1 = tgen.gears["vtep1"]
    version = r1.vtysh_cmd("show evpn mac vni 1000")
    # logger.info("=-=-=-=-=-==-FRR version is: " + version)

if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
