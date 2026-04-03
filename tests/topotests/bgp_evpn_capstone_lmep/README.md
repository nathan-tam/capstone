# LMEP
This folder contains the topotest setup for the Layer-2 Mapping & Encapsulation Protocol (LMEP), the proposed control-plane reduction mechanism for the capstone project.

LMEP is designed for standard data center fabrics that still need VXLAN-based overlay forwarding, but want to avoid the broadcast-heavy behavior of traditional MAC learning. Instead of letting every VTEP independently learn remote endpoints through flood-and-learn or broad EVPN dissemination, LMEP uses a centralized mapping service that stores the active `MAC -> VTEP` location for each endpoint. When an endpoint appears, moves, or is reattached, the mapping service is updated first, so traffic can be steered directly to the correct VTEP with minimal extra signaling.

The implementation in this folder assumes the LMEP daemon runs separately from the topotest process. The test script acts as a client and talks to the external server over TCP. By default, the client connects to `127.0.0.1` (`LMEP_SERVER_HOST`) on control port `6000` and data port `6001`.

Important: requests are sent from the host test process (not from individual Topotest namespaces). This keeps the daemon external/independent and avoids namespace-to-host routing issues.

## How To Launch The Server

Start the LMEP daemon before you run the topotest. A `tmux` session works well because the daemon is long-running and independent from pytest.

```bash
cd /Users/nathantam/Projects/capstone/tests/topotests/bgp_evpn_capstone_lmep/
python3 lmep_server.py \
	--bind-host 0.0.0.0 \
	--control-port 6000 \
	--data-port 6001
```

If the daemon is not reachable from the topology namespaces with the auto-detected host address, set `LMEP_SERVER_HOST` to an address they can route to before starting the test. The test file uses `LMEP_SERVER_HOST`, `LMEP_CONTROL_PORT`, `LMEP_DATA_PORT`, and `LMEP_VXLAN_PORT` environment variables.

Example test launch:

```bash
LMEP_SERVER_HOST=<reachable-server-ip> \
LMEP_CONTROL_PORT=6000 \
LMEP_DATA_PORT=6001 \
pytest -s tests/topotests/bgp_evpn_capstone_lmep/03_host_dummy_attached_to_vtep.py
```

## How To Verify It Works

There are two levels of verification.

1. Functional verification.
The server should log registration and forwarding responses. You can also query the standalone daemon by sending it a JSON `lookup` or `dump` request over the control port and checking that the MAC maps to the expected VTEP.

2. Control-plane traffic verification.
The test now uses the same style as `bgp_evpn_capstone_asym`: it starts BGP captures (`tcpdump` on port 179) on multiple nodes, stops them at the end, and prints per-node packet totals plus MP_REACH/MP_UNREACH counts from `tshark` when available.

The pattern is the same as in `bgp_evpn_capstone_asym`:

- start `tcpdump` on the traffic you want to measure,
- run the mobility or registration scenario,
- stop capture,
- count packets with `tcpdump -nr <pcap> | wc -l`.

The LMEP test performs this workflow inside `test_host_movement` and prints the results at teardown.

For LMEP, the useful captures are:

- the daemon control port, to confirm registrations are arriving,
- the overlay/BGP traffic on the VTEPs, if you want to compare LMEP against a baseline EVPN approach.

If you want a simple yes/no check today, the server is working when registrations return `ok: true` and `dump` shows the expected MAC-to-VTEP mapping. If you want a quantitative control-plane comparison, the current code does not yet compute a single “strain score”; you measure it by packet capture and packet counts.

## What LMEP Does
At a high level, LMEP replaces distributed MAC discovery with deterministic translation:
1. A wireless access point or attachment point detects that a client is registering.
2. The AP sends a MAC registration message to the mapping server.
3. The mapping server stores the client MAC, IP, VNI, and destination VTEP.
4. When controller-facing traffic arrives, the mapping server looks up the destination MAC and encapsulates the frame in VXLAN toward the correct VTEP.
5. The destination VTEP decapsulates the packet and forwards it to the attached host.

This keeps the overlay in VXLAN, but removes the need for the network to learn the endpoint through repeated flooding. Return traffic can still use ordinary VXLAN or routed forwarding, because the controller-side endpoint is not mobile.

## Why This Reduces Control Plane Traffic
The key benefit is that endpoint reachability is learned once, at the point of attachment, instead of being inferred across the fabric.

That reduces control-plane traffic in three ways:
1. Fewer flooded frames, because unknown destinations are translated centrally instead of being broadcast across the network.
2. Less MAC-learning churn, because mobile endpoints update the mapping server directly when they move.
3. Less distributed state replication, because the full endpoint database does not need to be shared with every VTEP.

In practice, that means the fabric spends less effort learning and relearning where a robot or mobile endpoint lives, and more effort forwarding only the packets that actually matter.

## Standard Behavior
The `LMEP Standard.md` document in this folder describes the expected behavior in more detail. The important pieces are:
- A listening port on the mapping server intercepts controller-facing Ethernet frames.
- The mapping server resolves the destination MAC to a VTEP IP address.
- The chosen frame is wrapped in VXLAN and sent across the overlay.
- Pre-emptive updates from the AP reduce black-holing during roaming.
- Stale entries can age out or be validated with keepalive-style checks.

The same document also includes a hierarchical extension, H-LMEP, which uses a root-to-leaf forwarding model with path carving and teardown messages. The tests in this folder focus on the base LMEP behavior and mobility-oriented endpoint updates.

## What The Tests Validate

The scripts in this folder are less about packet forwarding performance and more about proving the control-plane model:

- A VTEP can build the expected `br1000` and `vni1000` VXLAN plumbing.
- Endpoint attachments can be created and moved without rebuilding the whole fabric.
- MAC registration updates can be sent to a central mapping server before traffic begins to flow.
- EVPN MAC state reflects the new endpoint location after movement.

The test file includes packet-capture helpers for both LMEP control-port observation and asym-style BGP control-plane measurement. The current implementation reports counts but does not yet enforce pass/fail thresholds for control-plane reduction.

That is the core LMEP claim: the overlay still forwards traffic, but endpoint location is no longer discovered by the fabric through broad learning behavior. It is pushed deliberately from the edge to a single authoritative mapping point.

## In Short

LMEP keeps the VXLAN data plane, but changes how endpoint location is learned. Instead of relying on distributed MAC learning across the fabric, it uses a centralized mapping service and pre-emptive registration from the edge. For standard data center networks, that means less control-plane chatter, faster mobility convergence, and less transient packet loss when endpoints move.