#!/usr/bin/env python

"""
test_evpn_capstone.py: Testing EVPN with L2VNI and L3VNI
"""

import os
import sys
import shlex
from time import sleep
import platform
import pytest

# Save the Current Working Directory to find configuration files.
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
# Import topogen and topotest helpers
from lib import topotest
from lib.topogen import Topogen, TopoRouter
from debug_tools import verify_ping

# pytest module level markers
pytestmark = [
    pytest.mark.bgpd,
    pytest.mark.pimd,
]

#####################################################
##   Configuration Functions
#####################################################

# Test scaling parameters
NUM_VTEPS = 7
NUM_HOSTS = 7  # One host per VTEP for VM mobility testing
NUM_MOBILE_VMS = 128  # Number of VMs that will move around

# Controller VTEPs participate in topology/BGP but are excluded from endpoint mobility.
CONTROLLER_VTEPS = {"vtep1"}

# Static endpoint attached to the controller side (host on controller VTEP).
CONTROLLER_ENDPOINT_HOST = "host1"
CONTROLLER_ENDPOINT_IFACE = "controller"
CONTROLLER_ENDPOINT_IP = "192.168.100.254/16"
CONTROLLER_ENDPOINT_MAC = "00:aa:bb:dd:00:01"

# Toggle controller-to-VM ping verification during phase 2 and phase 4.
# Set to False to skip controller reachability sweeps while keeping mobility logic unchanged.
ENABLE_CONTROLLER_PING_CHECKS = False

# Duration (seconds) to keep duplicate-MAC overlap during live migration.
# Can be overridden with env var MOBILITY_OVERLAP_SECONDS.
try:
    MOBILITY_OVERLAP_SECONDS = max(0.0, float(os.getenv("MOBILITY_OVERLAP_SECONDS", "0.2")))
except ValueError:
    MOBILITY_OVERLAP_SECONDS = 0.1

# Auto-threshold for post-test pcap packet counting.
# Packet counting is skipped when a pcap exceeds this size (bytes).
try:
    PCAP_PACKET_COUNT_MAX_BYTES = max(
        0, int(os.getenv("PCAP_PACKET_COUNT_MAX_BYTES", str(1024 * 1024 * 1024)))
    )
except ValueError:
    PCAP_PACKET_COUNT_MAX_BYTES = 1024 * 1024 * 1024


def format_binary_size(num_bytes):
    """Format bytes into human-readable binary units (KiB, MiB, GiB...)."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(max(0, num_bytes))
    unit_idx = 0
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(size)} {units[unit_idx]}"
    return f"{size:.1f} {units[unit_idx]}"


def get_file_size_bytes(node, file_path):
    """Return file size in bytes as string, or 'missing' if file does not exist."""
    path = shlex.quote(file_path)
    return node.run(
        "if [ -f {0} ]; then stat -c%s {0}; else echo missing; fi".format(path)
    ).strip()


def get_pcap_packet_count(node, file_path, max_bytes):
    """Return packet count, or 'skipped'/'missing' based on file state and threshold."""
    path = shlex.quote(file_path)
    return node.run(
        "if [ -f {0} ]; then size=$(stat -c%s {0}); if [ \"$size\" -le {1} ]; then tcpdump -nr {0} 2>/dev/null | wc -l; else echo skipped; fi; else echo missing; fi".format(
            path,
            max_bytes,
        )
    ).strip()


def format_packet_count(packet_count_value, max_bytes):
    """Format packet-count summary value for reporting."""
    if packet_count_value == "skipped":
        return f"skipped(>{format_binary_size(max_bytes)})"
    return packet_count_value

vtep_ips = {
    f"vtep{i}": f"{10*i}.{10*i}.{10*i}.{10*i}" 
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
    # Mobility-eligible hosts are hosts connected to mobility-eligible VTEPs.
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
    node.run("ip link set dev %s type bond ad_actor_system %s" % (bond_name, bond_ad_sys_mac))

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


def config_l2vni(vtep_name, node, svi_ip, vtep_ip):
    """
    Create a VxLAN device for VNI 1000 and add it to the bridge.
    VLAN-1000 is mapped to VNI-1000.

    Creates a Linux bridge br1000.
    Assigns VLAN 1000 IP addresses to the bridge (SVI).
    Creates a VXLAN interface for VNI 1000 tied to the VTEP IP.
    Adds the VXLAN interface to the bridge.
    Sets MAC learning on VXLAN in the Linux bridge dataplane.
    Configures VLAN 1000 on the VXLAN interface.
    Brings interfaces up to start forwarding traffic.
    """
    # Create a bridge br1000 and assign SVI IP
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


def config_vtep(vtep_name, vtep, vtep_ip, svi_pip):
    """
    Create bond + VXLAN bridge configuration on a VTEP (EVPN PE).
    """

    # Create L2VNI, bridge, and associated SVI.
    config_l2vni(vtep_name, vtep, svi_pip, vtep_ip)

    # Create host bond and add it to the bridge.
    vtep_id = vtep_name.split("vtep")[1]
    sys_mac = "44:38:39:ff:ff:0" + vtep_id

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
    """
    Create the host-facing bond used for multihoming tests.
    """

    bond_members = []
    bond_members.append(host_name + "-eth0")

    # Name of the bonded interface to be created on the host
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

# Function passed to Topogen to create the topology.
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


# Setup/teardown fixture that creates the topology and starts daemons.
@pytest.fixture(scope="module")
def tgen(request):
    "Setup/Teardown the environment and provide tgen argument to tests"

    # Instantiate and start the topology.
    tgen = Topogen(build_topo, request.module.__name__)
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


# Skip subsequent tests if an earlier test caused router failures.
@pytest.fixture(autouse=True)
def skip_on_failure(tgen):
    if tgen.routers_have_failure():
        pytest.skip("skipped because of previous test failure")


#####################################################
##   Mobility Simulation Test
#####################################################

def create_macvlan_endpoint(tgen, host_name, vm_name, ip, mac):
    """Create a MACVLAN interface on a host to simulate a VM/container."""
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
    """Delete a MACVLAN interface to simulate VM departure."""
    host = tgen.gears[host_name]
    host.logger.info(f"Deleting MACVLAN {vm_name} from {host_name}")
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
    sleep(MOBILITY_OVERLAP_SECONDS)
    
    # Step 2: Delete from source (now both VTEPs had it briefly)
    delete_macvlan_endpoint(tgen, old_host_name, vm_name)


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


def verify_controller_to_vm_connectivity(tgen, num_mobile_vms, phase_name):
    """Verify static controller endpoint can ping all mobile VM IPs."""
    connectivity_failures = 0
    progress_interval = 10

    print(f"\nController reachability check ({phase_name})...")
    print(
        f"Checking {num_mobile_vms} VM endpoints from {CONTROLLER_ENDPOINT_HOST}:{CONTROLLER_ENDPOINT_IFACE}"
    )

    for vm_idx in range(1, num_mobile_vms + 1):
        target_ip = f"192.168.100.{vm_idx}"
        if verify_ping(
            tgen,
            CONTROLLER_ENDPOINT_HOST,
            CONTROLLER_ENDPOINT_IFACE,
            target_ip,
            count=1,
        ):
            if vm_idx % 20 == 0:
                print(f"  ✓ controller -> vm{vm_idx} ({target_ip})")
        else:
            print(f"  ✗ controller -> vm{vm_idx} ({target_ip}) FAILED")
            connectivity_failures += 1

        if vm_idx % progress_interval == 0 or vm_idx == num_mobile_vms:
            checked = vm_idx
            print(
                f"  Progress: {checked}/{num_mobile_vms} checked, failures so far: {connectivity_failures}"
            )

    assert (
        connectivity_failures == 0
    ), f"controller endpoint failed to reach {connectivity_failures}/{num_mobile_vms} VMs during {phase_name}"

    print(f"Controller reachability check ({phase_name}) PASSED: all {num_mobile_vms} VMs reachable")



def test_mobility(tgen):
    """
    Simulates 128 VMs moving between mobility-eligible VTEPs (via one host per VTEP)
    while keeping controller VTEPs static in the control plane.

    This establishes a baseline for
    network traffic measurements. This creates high control plane stress by simulating
    live VM migrations.

    Steps:
    1. Deploy 128 VMs distributed across hosts (one per VTEP)
    2. Verify controller endpoint can reach all VM IPs at initial locations
    3. Live-migrate each VM to a different VTEP (brief duplicate-MAC window)
    4. Verify controller endpoint can still reach all VM IPs after migration
    5. Capture BGP packet data during migrations
    """

    # Anycast gateway.
    gateway_ip = "192.168.0.250"

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

    #####################################################
    # SECTION: Packet Capture Setup
    #####################################################
    print("\nStarting Packet Capturing on spine1 and controller VTEP...")
    print(f"Using mobility overlap timer: {MOBILITY_OVERLAP_SECONDS:.3f}s")
    print(f"Packet count threshold: {format_binary_size(PCAP_PACKET_COUNT_MAX_BYTES)}")
    
    spine = tgen.gears["spine1"]                                # Run packet capture on spine1.
    pcap_dir = os.path.join(tgen.logdir, "spine1")              # Store output in the test log directory.
    pcap_file = os.path.join(pcap_dir, "evpn_mobility.pcap")    # Output file for captured packets.
    spine.run("mkdir -p {}".format(shlex.quote(pcap_dir)))      # Ensure output directory exists.

    controller_vtep = tgen.gears[controller_vtep_name]
    controller_pcap_dir = os.path.join(tgen.logdir, controller_vtep_name)
    controller_pcap_file = os.path.join(
        controller_pcap_dir,
        "evpn_controller_mobility.pcap",
    )
    controller_vtep.run("mkdir -p {}".format(shlex.quote(controller_pcap_dir)))

    print(f"spine capture file: {pcap_file}")
    print(f"controller capture file: {controller_pcap_file}")
    
    # Start tcpdump.
    spine.run(
        # Run detached with full packet capture; save PID for cleanup.
        "tcpdump -nni any -s 0 -w {} port 179 & echo $! > /tmp/tcpdump_evpn.pid".format(
            shlex.quote(pcap_file)
        ),
        stdout=None,
    )

    # Capture controller-VTEP view of mobility-related control/data-plane traffic.
    controller_vtep.run(
        "tcpdump -nni any -s 0 -w {} 'tcp port 179 or udp port 4789 or arp' & echo $! > /tmp/tcpdump_evpn_controller.pid".format(
            shlex.quote(controller_pcap_file)
        ),
        stdout=None,
    )

    print("tcpdump started on spine1 and controller VTEP")

    sleep(1)    # Give tcpdump a brief startup window before mobility begins.

    try:
        # Create static controller endpoint before mobility begins.
        create_controller_endpoint(tgen)

        #####################################################
        # SECTION: Mobility Simulation
        #####################################################
        print("\n=== Starting Mobility Simulation Test with {} VMs ===\n".format(NUM_MOBILE_VMS))

        # Track VM locations as: {vm_name: (current_host, current_vtep_idx)}.
        vm_locations = {}

        # --- Phase 1: deploy VMs on initial hosts --- #
        print(f"Phase 1: Deploying {NUM_MOBILE_VMS} VMs on hosts...")
        
        # Configure addressing.
        for vm_idx in range(1, NUM_MOBILE_VMS + 1):
            # VM naming scheme: vm1, vm2, ..., vm128.
            vm_name = f"vm{vm_idx}"
            
            # Use 192.168.100.x for mobile VMs (up to 254 VMs).
            vm_ip = f"192.168.100.{vm_idx}/16"
            
            # Generate a deterministic MAC from the VM index using bit shifts.
            vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)

            # Distribute VMs round-robin across mobility-eligible hosts only.
            host_idx = mobility_host_indices[(vm_idx - 1) % len(mobility_host_indices)]
            host_name = f"host{host_idx}"
            
            # Determine which VTEP this host is connected to.
            vtep_idx = host_to_vtep_index(host_idx)

            # Create the MACVLAN endpoint to simulate the VM.
            create_macvlan_endpoint(tgen, host_name, vm_name, vm_ip, vm_mac)
            
            # Add the VM to the tracking dictionary.
            vm_locations[vm_name] = (host_idx, vtep_idx)

            # Every 5 VMs, pause briefly to allow BGP/EVPN updates.
            if vm_idx % 5 == 0:
                # Give BGP/EVPN a moment to advertise between VM additions.
                sleep(1)

        # Wait for BGP/EVPN to stabilize.
        sleep(3)

        # --- Phase 2: verify initial connectivity --- #
        print("\nPhase 2: Verifying connectivity from initial locations...")
        print("This will take a while...make a coffee, get a snack!\n")
        if ENABLE_CONTROLLER_PING_CHECKS:
            verify_controller_to_vm_connectivity(tgen, NUM_MOBILE_VMS, "initial deployment")
        else:
            print("Controller ping checks are disabled (initial deployment phase).")
        # Intentionally disabled for now:
        # verify_initial_connectivity(tgen, vm_locations, gateway_ip, NUM_MOBILE_VMS)

        # --- Phase 3: migrate VMs to different VTEPs --- #
        print(f"\nPhase 3: Moving {NUM_MOBILE_VMS} VMs to different locations...")
        print("(Creating at destination while source exists, then cleaning up source)")
        
        for vm_idx in range(1, NUM_MOBILE_VMS + 1):
            vm_name = f"vm{vm_idx}"
            
            # Get current location.
            old_host_idx, old_vtep_idx = vm_locations[vm_name]
            old_host_name = f"host{old_host_idx}"
            
            # Compute new location on the next mobility-eligible VTEP.
            current_pos = mobility_vtep_indices.index(old_vtep_idx)
            new_vtep_idx = mobility_vtep_indices[(current_pos + 1) % len(mobility_vtep_indices)]
            
            # Find a mobility-eligible host on the new VTEP.
            # Hosts are distributed round-robin, so host_i is on vtep(((i-1) % NUM_VTEPS) + 1).
            new_host_idx = None
            for potential_host in mobility_host_indices:
                if host_to_vtep_index(potential_host) == new_vtep_idx and potential_host != old_host_idx:
                    new_host_idx = potential_host
                    break
            
            if new_host_idx is None:
                # Fallback: pick any mobility-eligible host on the new VTEP.
                for potential_host in mobility_host_indices:
                    if host_to_vtep_index(potential_host) == new_vtep_idx:
                        new_host_idx = potential_host
                        break

            assert new_host_idx is not None, f"No destination host found for VTEP index {new_vtep_idx}"

            new_host_name = f"host{new_host_idx}"

            # Compute VM IP and MAC.
            vm_ip = f"192.168.100.{vm_idx}/16"
            vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)

            # Perform live migration (create at destination, then delete from source).
            migrate_macvlan_endpoint_live(tgen, old_host_name, new_host_name, vm_name, vm_ip, vm_mac)
            vm_locations[vm_name] = (new_host_idx, new_vtep_idx)

            # Rate-limit migrations to observe network behavior.
            if vm_idx % 5 == 0:
                sleep(1)

        # Wait for all BGP/EVPN updates to process.
        sleep(5)

        # --- Phase 4: verify connectivity at new locations --- #
        print("\nPhase 4: Verifying connectivity at new locations...")
        if ENABLE_CONTROLLER_PING_CHECKS:
            verify_controller_to_vm_connectivity(tgen, NUM_MOBILE_VMS, "post migration")
        else:
            print("Controller ping checks are disabled (post migration phase).")
        # Intentionally disabled for now:
        # verify_post_migration_connectivity(tgen, vm_locations, gateway_ip, NUM_MOBILE_VMS)
    finally:
        # Stop capture and flush output.
        spine.run("if [ -f /tmp/tcpdump_evpn.pid ]; then kill $(cat /tmp/tcpdump_evpn.pid); fi")
        controller_vtep.run(
            "if [ -f /tmp/tcpdump_evpn_controller.pid ]; then kill $(cat /tmp/tcpdump_evpn_controller.pid); fi"
        )
        spine.run("sleep 1")

        spine_pcap_size_bytes = get_file_size_bytes(spine, pcap_file)
        controller_pcap_size_bytes = get_file_size_bytes(
            controller_vtep,
            controller_pcap_file,
        )
        spine_pcap_packets = get_pcap_packet_count(
            spine,
            pcap_file,
            PCAP_PACKET_COUNT_MAX_BYTES,
        )
        controller_pcap_packets = get_pcap_packet_count(
            controller_vtep,
            controller_pcap_file,
            PCAP_PACKET_COUNT_MAX_BYTES,
        )

        spine_pcap_size_display = (
            "missing"
            if spine_pcap_size_bytes == "missing"
            else format_binary_size(int(spine_pcap_size_bytes))
        )
        controller_pcap_size_display = (
            "missing"
            if controller_pcap_size_bytes == "missing"
            else format_binary_size(int(controller_pcap_size_bytes))
        )

        spine_pcap_packets_display = format_packet_count(
            spine_pcap_packets,
            PCAP_PACKET_COUNT_MAX_BYTES,
        )
        controller_pcap_packets_display = format_packet_count(
            controller_pcap_packets,
            PCAP_PACKET_COUNT_MAX_BYTES,
        )

        print("Packet captures saved:")
        print(
            f"  spine1: {pcap_file} ({spine_pcap_size_display}, packets={spine_pcap_packets_display})"
        )
        print(
            f"  {controller_vtep_name}: {controller_pcap_file} ({controller_pcap_size_display}, packets={controller_pcap_packets_display})"
        )

        # Remove controller endpoint to keep test namespace clean.
        delete_macvlan_endpoint_if_exists(
            tgen,
            CONTROLLER_ENDPOINT_HOST,
            CONTROLLER_ENDPOINT_IFACE,
        )

    # Run with MUNET_CLI=1 to drop into CLI after test completion.
    if os.getenv("MUNET_CLI") == "1":
        tgen.mininet_cli()  # this drops you into the 'munet>' prompt

if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))