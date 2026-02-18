#!/usr/bin/env python

def display_MAC_EVPN(tgen):
    """Display the EVPN MAC table on vtep1"""
    vtep1 = tgen.gears["vtep1"]                             # retrieve the first VTEP
    version = vtep1.vtysh_cmd("show evpn mac vni 1000")     # run a command to display the EVPN MAC table
    print("EVPN MAC table: " + version)                     # print the output to the console


def verify_ping(tgen, host_name, interface, target_ip, count=3, timeout_seconds=1):
    """
    Ping from a specific interface on a host.
    
    Args:
        tgen: Topogen instance
        host_name: Name of the host to ping from
        interface: Interface name to use for ping
        target_ip: IP address to ping
        count: Number of ping packets (default 3)
        timeout_seconds: Per-packet timeout in seconds (default 1)
    
    Returns:
        True if ping succeeds (0% packet loss), False otherwise
    """
    host = tgen.gears[host_name]
    # -W bounds wait time for each reply, so unreachable targets fail fast.
    cmd = f"ping -I {interface} -c {count} -W {timeout_seconds} {target_ip}"
    output = host.run(cmd)
    if "0% packet loss" in output:
        return True
    return False


def verify_initial_connectivity(tgen, vm_locations, gateway_ip, num_mobile_vms):
    """
    Verify connectivity from all VMs to gateway at initial locations.
    
    Args:
        tgen: Topogen instance
        vm_locations: dict mapping vm_name to (host_idx, vtep_idx)
        gateway_ip: Gateway IP to ping
        num_mobile_vms: Total number of VMs for logging
    
    Returns:
        Number of connectivity failures
    """
    connectivity_failures = 0
    
    for vm_idx in range(1, num_mobile_vms + 1):
        vm_name = f"vm{vm_idx}"
        host_idx, vtep_idx = vm_locations[vm_name]
        host_name = f"host{host_idx}"

        if verify_ping(tgen, host_name, vm_name, gateway_ip):
            if vm_idx % 10 == 0:
                print(f"  ✓ {vm_name} connectivity OK (on {host_name})")
        else:
            print(f"  ✗ {vm_name} connectivity FAILED")
            connectivity_failures += 1

    if connectivity_failures > 0:
        print(f"WARNING: {connectivity_failures}/{num_mobile_vms} VMs failed initial connectivity")
    else:
        print(f"SUCCESS: All {num_mobile_vms} VMs have connectivity from initial locations")
    
    return connectivity_failures


def verify_post_migration_connectivity(tgen, vm_locations, gateway_ip, num_mobile_vms):
    """
    Verify connectivity from all VMs to gateway after migration.
    Raises AssertionError if any VMs fail connectivity.
    
    Args:
        tgen: Topogen instance
        vm_locations: dict mapping vm_name to (host_idx, vtep_idx)
        gateway_ip: Gateway IP to ping
        num_mobile_vms: Total number of VMs for logging
    
    Raises:
        AssertionError if any VMs fail connectivity check
    """
    connectivity_failures = 0
    
    for vm_idx in range(1, num_mobile_vms + 1):
        vm_name = f"vm{vm_idx}"
        host_idx, vtep_idx = vm_locations[vm_name]
        host_name = f"host{host_idx}"

        if verify_ping(tgen, host_name, vm_name, gateway_ip):
            if vm_idx % 10 == 0:
                print(f"  ✓ {vm_name} connectivity OK (migrated to {host_name})")
        else:
            print(f"  ✗ {vm_name} connectivity FAILED at new location")
            connectivity_failures += 1

    assert connectivity_failures == 0, f"{connectivity_failures}/{num_mobile_vms} VMs failed connectivity at new locations"
    print(f"SUCCESS: All {num_mobile_vms} VMs have connectivity at new locations. Mobility simulation complete.")
