# EVPN Capstone Test Suite

This document describes the current implementation of the EVPN mobility topotest in this directory.

> The test validates BGP EVPN (L2VNI) behavior during repeated MAC mobility events. It creates a small Clos fabric, deploys many MACVLAN-based endpoints, migrates them between VTEPs, and captures BGP control-plane traffic for analysis.

## Current Scope

- 7 VTEPs (EVPN leaf nodes)
- 2 spine routers
- 7 hosts (one host connected to each VTEP)
- 128 mobile VM endpoints (simulated MACVLAN interfaces)

### Controller VTEP Role

- `vtep1` is configured as a controller/static VTEP in test logic (`CONTROLLER_VTEPS = {"vtep1"}`)
- Controller VTEPs still participate fully in topology and BGP/EVPN control plane
- Controller VTEPs are intentionally excluded from VM endpoint placement and migration
- Mobility operations run only on mobility-eligible VTEPs/hosts
- With current settings, this means 6 mobility-eligible VTEPs and 6 mobility-eligible hosts
- A static controller endpoint (`controller`) is created on `host1` to probe mobility endpoints

#### Configuration Knob

You can change controller participation by editing `CONTROLLER_VTEPS` in `test_evpn_capstone.py`.

Examples:

- Single controller VTEP: `CONTROLLER_VTEPS = {"vtep1"}`
- Two controller VTEPs: `CONTROLLER_VTEPS = {"vtep1", "vtep2"}`
- No controller VTEP mode: `CONTROLLER_VTEPS = set()`

Constraints enforced by test guard assertions:

- At least one mobility-eligible host must exist
- At least two mobility-eligible VTEPs must exist

If these constraints are violated, the test fails early with an explicit assertion message.

## Network Topology

Each VTEP connects to:

- spine1
- spine2
- one host-facing bond (`hostbond1`)

### Topology Structure

```text
                     spine1 ---------------- spine2
                    /  |  |  |  |  |  \     /  |  |  |  |  |  \
                   /   |  |  |  |  |   \   /   |  |  |  |  |   \
               vtep1 vtep2 vtep3 vtep4 vtep5 vtep6 vtep7
                 |     |     |     |     |     |     |
               host1 host2 host3 host4 host5 host6 host7
```

### Addressing Scheme

- VTEP BGP router-id values (from per-VTEP BGP config): `192.168.100.15` to `192.168.100.21` (`vtep1`..`vtep7`)
- VTEP VXLAN local source IPs (set by test setup code): `{10*i}.{10*i}.{10*i}.{10*i}` for VTEP `i`
- SVI IPs:
  - `vtep1`..`vtep5`: `192.168.0.251` through `192.168.0.255`
  - `vtep6+`: `192.168.200.x` (introduced to avoid invalid `.256` and avoid overlap with underlay subnets)
- Anycast gateway on all VTEPs: `192.168.0.250/16`
- Host IPs (`host1`..`host7` in current scale): `192.168.0.1/16` through `192.168.0.7/16`
- Mobile VM IPs: `192.168.100.1/16` through `192.168.100.128/16`

---

## How It Works

### Bridge + VXLAN Setup

- Linux bridge `br1000` is created per VTEP
- Per-VTEP SVI IP and anycast gateway IP are added to `br1000`
- VXLAN interface `vni1000` (UDP 4789, VNI 1000) is attached to the bridge
- ARP accept is enabled on `br1000`
- Bridge VLAN 1000 membership is configured for host bond and VNI interfaces
- Current runtime behavior sets bridge learning **on** for `vni1000` (`bridge link set dev vni1000 learning on`)

### Host and VM Modeling

- Each host uses bond `vtepbond`
- Mobile endpoints are MACVLAN interfaces (`mode bridge`) created on `vtepbond`
- A static controller endpoint is also modeled as a MACVLAN on `host1`:
  - Interface: `controller`
  - IP: `192.168.100.254/16`
  - MAC: `00:aa:bb:dd:00:01`
- VM naming: `vm1`..`vm128`
- VM MAC formula:

```python
vm_mac = "00:aa:bb:cc:{:02x}:{:02x}".format((vm_idx >> 8) & 0xFF, vm_idx & 0xFF)
```

## Test Phases

The test executes four phases in order:

### Phase 1: VM Deployment

- Deploy 128 VM MACVLAN interfaces
- Distribute round-robin across mobility-eligible hosts only (currently 6 hosts)
- Pause every 5 VMs for control-plane settling
- Final stabilization sleep

### Phase 2: Initial Connectivity Verification

- Test code prints the phase banner
- The static `controller` endpoint pings all VM IPs and asserts zero failures
- The connectivity helper exists, but the call is currently commented out

### Phase 3: Live Migration

For each VM:

1. Determine current host/VTEP
2. Select host on next mobility-eligible VTEP in the mobility VTEP ring
3. Live move sequence:
   - Create VM MACVLAN on destination host
   - Sleep 500ms (intentional duplicate MAC window)
   - Delete VM MACVLAN from source host

Note: the test includes guard assertions requiring at least two mobility-eligible VTEPs and at least one mobility-eligible host.

### Phase 4: Post-Migration Verification

- Test code prints the phase banner
- The static `controller` endpoint again pings all VM IPs and asserts zero failures
- The post-migration connectivity helper exists, but the call is currently commented out

## Packet Capture

- Capture runs on `spine1`.
- Captures TCP/179 traffic to `{logdir}/spine1/evpn_mobility.pcap`
- Intended for offline EVPN/BGP update analysis.

Note: capture output depends on `tcpdump` starting successfully in the runtime environment.

## Configuration Files

Per-node files expected by test loader:

- `{routername}/zebra.conf`
- `{routername}/evpn.conf`

Current topology includes:

- `spine1`, `spine2`
- `vtep1`..`vtep7`
- `host1`..`host7`

## Success Criteria (Current Behavior)

- Topology and daemons start successfully
- Mobility loop executes for all 128 VMs without fatal test exceptions
- Static `controller` endpoint reaches all VM IPs before and after migration
- Packet capture file is expected to be produced when `tcpdump` starts successfully

If connectivity checks are uncommented, additional criteria become:

- 0 initial connectivity failures
- 0 post-migration connectivity failures

## Metrics Measured

- Control-plane stress from 128 sequential mobility events
- BGP update behavior during duplicate-MAC and move windows
- Scale validation at current lab topology size (7 VTEPs, 7 hosts, with controller/static VTEP support)

## Debugging

1. `show evpn mac vni 1000`
2. `show bgp l2vpn evpn`
3. `bridge fdb show`
4. Validate VNI/VXLAN interface state and BGP neighbor state
5. Inspect `evpn_mobility.pcap` in Wireshark/tshark
