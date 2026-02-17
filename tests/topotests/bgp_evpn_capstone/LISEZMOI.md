# EVPN Capstone Test Suite
Apologies for further ballooning the administrative burden by creating a second README file. I will attempt to refrain from speedrunning bureaucratic paralysis. The purpose of this document is to be a single-location on the technical aspects of this test. Also be aware that a majority of this document was generated from dialogue with Copilot. Excerise skepticism, especially since Nathan was the one who edited it afterwards. As a funny side note, it did the orignal generation in French because of the file name. Anyway, an overview:
> This test suite validates BGP EVPN (Ethernet VPN) functionality with L2VNI (Layer 2 VNI) in FRRouting (FRR). It simulates a large-scale data center environment with multiple VTEPs (VXLAN Tunnel Endpoints), hosts, and mobile VMs to stress-test the EVPN control plane during VM mobility events. The primary goal is to measure and validate network behavior when virtual machines migrate across different VTEPs in an EVPN fabric. 

### Network Topology
- 4 VTEPs (VXLAN Tunnel Endpoints) - acting as EVPN-PE routers
- 2 Spine Routers - providing full-mesh connectivity
- 128 Hosts - distributed round-robin across VTEPs
- 20 Mobile VMs - simulated using MACVLAN interfaces
Each VTEP connects to:
- Both spine routers
- About 32 hosts via bonded interfaces

##### Topology Structure

```
        spine1 -------- spine2
         /  |  \         /  |  \
       /    |    \     /    |    \
   vtep1  vtep2  vtep3  vtep4  ...
     |      |      |      |
   hosts  hosts  hosts  hosts
  (32ea) (32ea) (32ea) (32ea)
```

##### Addressing Scheme
- VTEP Loopbacks: `{10*i}.{10*i}.{10*i}.{10*i}` (e.g., vtep1 = 10.10.10.10)
- SVI IPs: `192.168.0.{250+i}` (e.g., vtep1 = 192.168.0.251)
- Anycast Gateway: `192.168.0.250` (shared across all VTEPs)
- Host IPs: `192.168.0.1` through `192.168.0.127`, then `192.168.1.1+`
- Mobile VM IPs: `192.168.100.1` through `192.168.100.20`
---
### How It Works
Bridge Setup:
- A Linux bridge `br1000` for VLAN 1000
- SVI (Switch Virtual Interface) with both unique IP and anycast gateway IP
- ARP accept enabled for anycast gateway functionality

VXLAN Configuration:
- VXLAN interface `vni1000` for VNI 1000
- Local VTEP IP as source
- UDP port 4789 (standard VXLAN port)
- Learning disabled (BGP EVPN handles MAC learning)

Bonding:
- 802.3ad LACP bonds for multi-homing
- Layer 3+4 hash policy for traffic distribution
- Connected to hosts via `hostbond1`

#### Host Configuration
Each host is configured with:
- An 802.3ad bonded interface (`vtepbond`)
- Unique IP and MAC addresses computed from host ID
- Connection to a single VTEP (for simplicity in this test)

#### Simulation
Mobile VMs are simulated using **MACVLAN interfaces**:
- Created on top of the host's bonded interface
- Each VM gets its own IP and MAC address
- MACVLAN mode: `bridge` (allows communication with host)

## Test Phases

### Phase 1: VM Deployment
- Deploy 20 VMs across different hosts
- VMs distributed round-robin across all 128 hosts
- Small delays (every 5 VMs) to allow BGP convergence
- Final 3-second stabilization period

### Phase 2: Initial Connectivity Verification
- Ping from each VM to the anycast gateway (192.168.0.250)
- Validates that all VMs have Layer 3 connectivity
- Reports any connectivity failures

### Phase 3: Live Migration
This is the **core test phase**:

For each VM:
1. Identify current location (old host on old VTEP)
2. Select new location (host on different VTEP)
3. **Live migration sequence**:
   - Create MACVLAN on destination host (VM now exists in two places)
   - Wait 500ms (stresses control plane with duplicate MAC)
   - Delete MACVLAN from source host
4. Update VM location tracking

The migration creates a brief period where the same MAC exists on two VTEPs, forcing BGP EVPN to:
- Detect the duplicate MAC
- Update route advertisements
- Withdraw old routes
- Establish new forwarding paths

### Phase 4: Post-Migration Verification
- Ping from each VM at its **new location** to the gateway
- Validates that EVPN successfully updated all forwarding tables
- Asserts zero connectivity failures (test fails if any VM can't reach gateway)

## Packet Capture

The test captures BGP traffic on spine1 during the entire test:
- Captures all TCP port 179 traffic (BGP)
- Stores to: `{logdir}/spine1/evpn_mobility.pcap`
- Allows post-test analysis of EVPN route updates

This PCAP file can be analyzed to:
- Count BGP UPDATE messages
- Measure convergence times
- Identify route flapping
- Study MAC mobility signaling

## BGP EVPN

### MAC Mobility
- When a VM moves, EVPN Type-2 routes (MAC/IP advertisement) are updated
- Old VTEP withdraws the route
- New VTEP advertises the route with updated next-hop
- All other VTEPs update their forwarding tables

### Anycast Gateway
- All VTEPs share the same default gateway IP (192.168.0.250)
- VMs can migrate without changing their gateway configuration
- Distributed anycast routing for optimal traffic flow

## Configuration Files
The test expects per-router configuration in subdirectories:
- `{routername}/zebra.conf` - Interface and routing configuration
- `{routername}/evpn.conf` - BGP EVPN-specific configuration

For example:
- `vtep1/zebra.conf` - VTEP1's Zebra daemon config
- `vtep1/evpn.conf` - VTEP1's BGP EVPN config
- `spine1/zebra.conf` - Spine1's routing config
- etc.

## Success Criteria
The test passes when:
1. TO-DO: FILL THIS SECTION OUT.

## Metrics Measured
While the test primarily validates functional correctness, it provides data for:
- Control plane stress: 20 simultaneous MAC mobility events
- Convergence time: Time from VM creation/migration to connectivity
- Scale validation: 128 hosts + 20 mobile VMs across 4 VTEPs
- BGP update volume: Captured in PCAP for offline analysis

## Debugging
1. Check EVPN status: `show evpn mac vni 1000` on any VTEP
2. BGP routes: `show bgp l2vpn evpn` to see advertised routes
3. Bridge MACs**: `bridge fdb show` to see learned MAC addresses
4. Connectivity issues: Check that VNIs match and VXLAN is up
5. PCAP analysis: Use Wireshark to examine `evpn_mobility.pcap`
