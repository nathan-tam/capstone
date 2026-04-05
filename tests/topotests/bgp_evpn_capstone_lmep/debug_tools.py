#!/usr/bin/env python

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
