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
    node.run("ip addr add 192.168.0.254/24 dev br1000")
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
    "vtep1": "192.168.0.210",
    "vtep2": "192.168.0.220",
    "vtep3": "192.168.0.230"
}


def config_vtep(vtep_name, vtep, vtep_ip, svi_pip):
    """
    Create the bond/vxlan-bridge on the TOR which acts as VTEP and EPN-PE
    """
    # create l2vni, bridge and associated SVI
    config_l2vni(vtep_name, vtep, svi_pip, vtep_ip)


def config_vteps(tgen, vteps):
    for vtep_name in vteps:
        vtep = tgen.gears[vtep_name]
        config_vtep(vtep_name, vtep, vtep_ips.get(vtep_name), svi_ips.get(vtep_name))

def compute_host_ip_mac(host_name):
    host_id = host_name.split("dummy")[1]
    host_ip = "192.168.0." + host_id + "/24"
    host_mac = "00:00:00:00:00:" + host_id
    return host_ip, host_mac

def config_host(dummy_name, vtep):
    """
    Create the dual-attached bond on host nodes for MH
    """

    dummy_ip, dummy_mac = compute_host_ip_mac(dummy_name)

    vtep.run(f"ip link add name {dummy_name} type dummy")
    vtep.run(f"ip link set dev {dummy_name} address {dummy_mac}")
    vtep.run(f"ip link set dev {dummy_name} up")
    vtep.run(f"ip addr add {dummy_ip} dev {dummy_name}")
    vtep.run(f"ip link set {dummy_name} master br1000")


def config_hosts(tgen, dummyhosts, vteps):
    num_vteps = len(vteps)
    for i, dummy_name in enumerate(dummyhosts):
        vtep_name = vteps[i % num_vteps]  # cycle through vteps
        vtep = tgen.gears[vtep_name]
        config_host(dummy_name, vtep)

# Function we pass to Topogen to create the topology
def build_topo(tgen):
    "Build function"

    # Create spine, leaf, and hosts
    spine1 = tgen.add_router("spine1")
    spine2 = tgen.add_router("spine2")

    vtep1 = tgen.add_router("vtep1")
    vtep2 = tgen.add_router("vtep2")
    vtep3 = tgen.add_router("vtep3")

    # Create a p2p connection between r1 and r2
    tgen.add_link(spine1, vtep1)
    tgen.add_link(spine1, vtep2)
    tgen.add_link(spine1, vtep3)

    tgen.add_link(spine2, vtep1)
    tgen.add_link(spine2, vtep2)
    tgen.add_link(spine2, vtep3)


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

    dummyhosts = [f"dummy{i}" for i in range(1, 10)]
    config_hosts(tgen, dummyhosts, vteps)

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

def stop_background_ping(host, pid):
    host.run(f"kill {pid} || true")


def test_host_movement(tgen):

    """
    Test host movement between two TORs only a single host can move to a vtep at a time, the vtepbond interface can only hold 1 MAC address.
    The MAC address in the previous host do not have to be removed as sequence number is increased at the new vtep, so previous vtep will forward traffic to that new vtep that holds the host.
    This means that we cannot freely move both host1 and host2 to vtep1 at the same time as they will have the same MAC address on the same vtep bond interface.
    """
    
    # If any router has previously failed in another test, skip this one.
    if tgen.routers_have_failure():
        pytest.skip(f"skipped because of previous test failure\n {tgen.errors}")

    vtep_dummy_map = {}
    vteps = ["vtep1", "vtep2", "vtep3"]
    dummyhosts = [f"dummy{i}" for i in range(1, 10)]
    num_vteps = len(vteps)
    for i, dummy_name in enumerate(dummyhosts):
        vtep_name = vteps[i % num_vteps]  # cycle through vteps
        vtep_dummy_map[dummy_name] = vtep_name
    # print(initial_vtep_dummy_map)
    pid = start_background_ping(tgen.gears["vtep2"], "192.168.0.3")
    sleep(2)  # wait for some pings to be sent
    print(tgen.gears["vtep2"].run("ip -s link show vni1000"))
    def move_host_from(curr_hostname, target_hostname, targeted_dummy):
        # Re assign the host to the new vtep in the map
        vtep_dummy_map[targeted_dummy] = target_hostname

        # Get vtep nodes
        curr_vtep = tgen.gears[curr_hostname]
        target_vtep = tgen.gears[target_hostname]

        def run_command_and_expect():
            # Delete dummy from previous owner and assign it to the new one
            config_host(targeted_dummy, target_vtep)
            curr_vtep.run(f"ip link delete {targeted_dummy}")
            return True
        
        _, result = topotest.run_and_expect(
            run_command_and_expect,
            True,   # EXPECTED OUTPUT
            count=5,  # Try up to 30 times...
            wait=3     # ...waiting 1 second between tries.
        )
        
        sleep(1)  # wait for some pings to be sent
        print(tgen.gears["vtep2"].run("ip -s link show vni1000"))
        # If the result is not None after all retries, OSPF didn't converge.
        assertmsg = (
            f"The MAC and IP address in {curr_hostname} has not moved\n"
        )

        assert result is True, assertmsg
    
    move_host_from(vtep_dummy_map["dummy3"], "vtep2", "dummy3")
    stop_background_ping(tgen.gears["vtep2"], pid)

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
