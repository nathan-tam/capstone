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
import platform
import re
import sys
from functools import partial
from time import sleep, time

import pytest

# Save the Current Working Directory to find configuration files.
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
# Import topogen and topotest helpers
from lib.topogen import Topogen, TopoRouter, get_topogen
from lib.topolog import logger

from lib import topotest

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
    node.run("ip addr add %s/24 dev br1000" % svi_ip)
    # NOTE TO SELF: Is this the any gateway address for hosts to reach:
    node.run("ip addr add 192.168.0.1/24 dev br1000")
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


vtep_ips = {"vtep1": "10.10.10.10", "vtep2": "20.20.20.20", "vtep3": "30.30.30.30"}

svi_ips = {"vtep1": "192.168.0.11", "vtep2": "192.168.0.12", "vtep3": "192.168.0.13"}


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
    host_ip = "192.168.0." + host_id + "0/24"
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

    # Call a helper function to configure the bond on the host
    config_bond(host, bond_name, bond_members, "00:00:00:00:00:00", None)

    host_ip, host_mac = compute_host_ip_mac(host_name)

    # Assign the computed IP address and MAC address to the bonded interface
    host.run("ip addr add %s dev %s" % (host_ip, bond_name))
    host.run("ip link set dev %s address %s" % (bond_name, host_mac))


def config_hosts(tgen, hosts):
    for host_name in hosts:
        host = tgen.gears[host_name]
        config_host(host_name, host)


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

    # add LMEP controller
    lmep_server = tgen.add_router("lmep_server")


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

    router_list = tgen.routers()
    for rname, router in router_list.items():
        router.load_config(
            TopoRouter.RD_ZEBRA, os.path.join(CWD, "{}/zebra.conf".format(rname))
        )
        router.load_config(
            TopoRouter.RD_BGP, os.path.join(CWD, "{}/evpn.conf".format(rname))
        )
    tgen.start_router()

    lmep_node = tgen.gears["lmep_server"]
    lmep_node.run("ip addr add 192.168.1.1/24 dev lmep_server-eth0")
    lmep_node.run("ip link set dev lmep_server-eth0 up")
    lmep_node.run("ip route add 10.0.0.0/8 via 192.168.1.254")
    lmep_node.run("python3 /root/lmep_protocol.py > /tmp/lmep.log 2>&1 &")

    # Provide tgen as argument to each test function
    yield tgen

    # Teardown after last test runs
    tgen.stop_topology()


# Fixture that executes before each test
@pytest.fixture(autouse=True)
def skip_on_failure(tgen):
    if tgen.routers_have_failure():
        pytest.skip("skipped because of previous test failure")


# ===================
# The tests functions
# ===================
def start_background_ping(host, target_ip):
    """
    Start continuous ping in the background. Returns the PID of the ping.
    """
    # -D prints timestamp, very helpful for debugging mobility events.
    cmd = f"ping -i 0.1 -D {target_ip} > /tmp/ping_output.log 2>&1 & echo $!"
    pid = host.run(cmd).strip()
    return pid


def test_lmep_registration(tgen):
    lmep = tgen.gears["lmep_server"]
    vtep1 = tge.gears["vtep1"]

    log_content = lmep.run("cat /tmp/lmep.log")
    assert "Registered MAC" in log_content


def stop_background_ping(host, pid):
    host.run(f"kill {pid} || true")


import socket
import struct


def send_lmep_registration(target_host, mac_address, vtep_ip):
    mac_bytes = bytes.fromhex(mac_address.replace(":", ""))
    tlv_mac = struct.pack("!BB", 0x01, 6) + mac_bytes
    vtep_ip_bytes = socket.inet_aton(vtep_ip)
    tlv_vtep = struct.pack("!BB", 0x04, 4) + vtep_ip_bytes
    packet = tlv_mac + tlv_vtep

    # there is prolly a easier way to do this with frr but like eh
    cmd = f"python3 -c 'import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto({repr(packet)}, (\"{LMEP_SERVER_IP}\", {LMEP_SERVER_PORT}))'"
    target_host.run(cmd)


def test_host_movement(tgen):
    """
    Test host movement between two TORs only a single host can move to a vtep at a time, the vtepbond interface can only hold 1 MAC address.
    The MAC address in the previous host do not have to be removed as sequence number is increased at the new vtep, so previous vtep will forward traffic to that new vtep that holds the host.
    This means that we cannot freely move both host1 and host2 to vtep1 at the same time as they will have the same MAC address on the same vtep bond interface.
    """

    # If any router has previously failed in another test, skip this one.
    if tgen.routers_have_failure():
        pytest.skip(f"skipped because of previous test failure\n {tgen.errors}")
    sleep(3)
    pid = start_background_ping(tgen.gears["host3"], "192.168.0.10")
    sleep(1)
    events = {}

    # ----------------------------------------------------------------------
    # Inner function: checks if OSPFv2 neighbor adjacency is FULL.
    # ----------------------------------------------------------------------
    def move_host_from(curr_hostname, target_hostname, targeted_ip, targeted_mac):
        # for host_name in hosts:
        curr_host = tgen.gears[curr_hostname]
        target_host = tgen.gears[target_hostname]

        output_before = tgen.gears["vtep1"].vtysh_cmd(
            "show evpn mac vni 1000 json", isjson=True
        )

        def change_addresses(bond_name, targeted_ip):
            """
            Move the host to a different interface to simulate failover
            """

            output = curr_host.run(f"ip addr show dev {bond_name}")
            if targeted_ip in output:
                # Add the removal of the host_ip from the previous owner. So that the previous owner does not contain the IP anymore.
                curr_host.run(f"ip addr del {targeted_ip}/24 dev {bond_name}")
                curr_host.run(f"ip link set dev {bond_name} address 00:00:00:00:ff:ff")

                target_host.run(f"ip addr add {targeted_ip}/24 dev {bond_name}")
                target_host.run(f"ip link set dev {bond_name} address {targeted_mac}")
                target_host.run("ip neigh flush all")

        def run_command_and_expect():
            # Run the FRRouting command via vtysh, output in JSON format.
            # Example command: `show ip ospf neighbor <neighbor> json`
            # Verify the connection has changed using vtep1

            print(
                f"Before movement show evpn mac:\n{output_before['macs'][targeted_mac]}"
            )
            events["movement_triggered"] = time()
            change_addresses("vtepbond", targeted_ip)

            # initial_host2 = host2

            output_after = tgen.gears["vtep1"].vtysh_cmd(
                "show evpn mac vni 1000 json", isjson=True
            )
            print(
                f"After movement show evpn mac:\n{output_after['macs'][targeted_mac]}"
            )

            # Check if a change between types has occurred (local -> remote or remote -> local)
            if (
                topotest.json_cmp(
                    output_before["macs"][targeted_mac]["type"],
                    output_after["macs"][targeted_mac]["type"],
                )
                is None
            ):
                # Check if the type is remote. If so, check if the remoteVtep has changed.
                if output_before["macs"][targeted_mac]["type"] == "remote":
                    if (
                        topotest.json_cmp(
                            output_before["macs"][targeted_mac]["remoteVtep"],
                            output_after["macs"][targeted_mac]["remoteVtep"],
                        )
                        is None
                    ):
                        return None
                # Return None to indicate success (If the mac stayed local (meaning it did not move)).
                return None
            events["movement_finished"] = time()
            # Otherwise, return the diff (meaning not yet FULL).
            return True

        # ------------------------------------------------------------------
        # Keep retrying the check until it succeeds or times out.
        # ------------------------------------------------------------------

        _, result = topotest.run_and_expect(
            run_command_and_expect,
            True,  # EXPECTED OUTPUT
            count=5,  # Try up to 30 times...
            wait=3,  # ...waiting 1 second between tries.
        )
        sleep(1)
        tgen.gears["host3"].run(
            f'echo "===== CHANGE STARTED at {events["movement_triggered"]} =====" >> /tmp/ping_output.log'
        )
        tgen.gears["host3"].run(
            f'echo "===== CHANGE STARTED at {events["movement_finished"]} =====" >> /tmp/ping_output.log'
        )

        # tgen.gears["host3"].run("echo \"===== CHANGE OCCURRED at $(date +%s.%N) =====\" >> /tmp/ping_output.log")

        # If the result is not None after all retries, OSPF didn't converge.
        assertmsg = f"The MAC and IP address in {curr_hostname} has not moved\n"

        assert result is True, assertmsg

    move_host_from("host1", "host2", "192.168.0.10", "00:00:00:00:00:01")
    # move_host_from("host2","host1", "192.168.0.10", "00:00:00:00:00:01")
    # move_host_from("host1","host2", "192.168.0.10", "00:00:00:00:00:01")
    stop_background_ping(tgen.gears["host3"], pid)

    # move_host_from("host1","host2", "192.168.0.10", "00:00:00:00:00:01")
    stop_background_ping(tgen.gears["host3"], pid)


def test_get_version(tgen):
    "Test the logs the FRR version"

    r1 = tgen.gears["vtep1"]
    version = r1.vtysh_cmd("show evpn mac vni 1000")
    logger.info("=-=-=-=-=-==-FRR version is: " + version)


# # def test_connectivity(tgen):
# #     "Test the logs the FRR version"

# #     r1 = tgen.gears["r1"]
# #     r2 = tgen.gears["r2"]
# #     output = r1.cmd_raises("ping -c1 192.168.1.2")
# #     output = r2.cmd_raises("ping -c1 192.168.3.1")


# @pytest.mark.xfail
# def test_expect_failure(tgen):
#     "A test that is current expected to fail but should be fixed"

#     assert False, "Example of temporary expected failure that will eventually be fixed"


# @pytest.mark.skip
# def test_will_be_skipped(tgen):
#     "A test that will be skipped"
#     assert False


# # Memory leak test template
# def test_memory_leak(tgen):
#     "Run the memory leak test and report results."

#     if not tgen.is_memleak_enabled():
#         pytest.skip("Memory leak test/report is disabled")

#     tgen.report_memory_leaks()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
