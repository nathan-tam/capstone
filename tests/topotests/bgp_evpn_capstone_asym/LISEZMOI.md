# EVPN Asymmetric Test: Topology and Routing Guide
This guide explains what the `bgp_evpn_capstone_asym` test builds, how routing is intended to work, and what to check when behavior is unexpected.

We assume you are familiar with basic IP routing, but new to this specific topology.

---

### Topology and Intent
`build_topo()` in `test_evpn_capstone_asym.py` creates 2 spines, 7 VTEPs, and 7 hosts. Each VTEP connects to both spines. Each host connects to exactly one VTEP (1:1 with 7 hosts and 7 VTEPs).
<br>
The visualizer component of the test provides an excellent guide. It is highly recommended you run it if you're looking for a visual representation of the topology.

#### Hub-and-Spoke Intent
This topology models a **Hub-and-Spoke EVPN control plane**, not a full mesh.
- `vtep1` is the hub. It is also the controller-side VTEP where the static test endpoint lives.
- `vtep2..vtep7` are spokes. They host mobile VM endpoints.
- The hub learns routes from all spokes. Spokes learn routes only from the hub.
- **Spokes must not learn each other's EVPN routes.** This is the core invariant the spine policy enforces.

#### Route-Target Scheme
Each VTEP has a unique export RT. The hub imports all of them; spokes import only the hub's.

| Role | VTEP | Export RT | Import RTs |
|------|------|-----------|------------|
| Hub | `vtep1` | `65000:1000` | `65000:1000`, `65000:1002..1007` |
| Spoke | `vtep2` | `65000:1002` | `65000:1000` only |
| Spoke | `vtep3` | `65000:1003` | `65000:1000` only |
| Spoke | `vtep4` | `65000:1004` | `65000:1000` only |
| Spoke | `vtep5` | `65000:1005` | `65000:1000` only |
| Spoke | `vtep6` | `65000:1006` | `65000:1000` only |
| Spoke | `vtep7` | `65000:1007` | `65000:1000` only |

---

### Underlay BGP (IPv4 Unicast)
eBGP is used everywhere. The spines are pure transit and filter nodes, they carry no local VXLAN endpoints.

#### Loopbacks
- `spine1`: `192.168.100.13`
- `spine2`: `192.168.100.14`
- `vtep1..vtep7`: `192.168.100.15` through `192.168.100.21`

#### Spine-facing link IPs (from `zebra.conf`)

- `spine1` side: `192.168.{1,2,3,4,9,11,13}.1` (one per VTEP)
- `spine2` side: `192.168.{5,6,7,8,10,12,14}.1` (one per VTEP)

VTEPs peer to the spine's `.1` address; spines peer back to the VTEP's `.2` address on each link.

---

### EVPN Configuration
Every VTEP uses these settings under `address-family l2vpn evpn`:
- `advertise-all-vni` acts as a master switch. Without this, no EVPN routes are originated at all (see §8)
- `advertise-svi-ip` advertises the bridge SVI IP/MAC as a type 2 route so the anycast gateway is reachable without flooding (see §8)
- `neighbor TRANSIT_OVERLAY activate` activates the EVPN address family toward both spines
- `vni 1000` block with explicit RD and RT import/export
- `frr defaults datacenter` disables `ebgp-requires-policy`, reduces timers to 3 s/9 s (see §8)

#### Spine
The spines enforce the hub-spoke asymmetry using outbound route-maps:
```
extcommunity-list standard EVPN-HUB-RT permit rt 65000:1000

route-map EVPN-TO-HUB permit 10
  (no match — permits all EVPN routes)

route-map EVPN-TO-SPOKE permit 10
  match extcommunity EVPN-HUB-RT
route-map EVPN-TO-SPOKE deny 20
```

Per-neighbor bindings under `address-family l2vpn evpn`:

- vtep1 (hub): `neighbor vtep1 route-map EVPN-TO-HUB out`
- vtep2..vtep7 (spokes): `neighbor vtepN route-map EVPN-TO-SPOKE out`

Additional spine settings:

- `neighbor TRANSIT_OVERLAY next-hop-unchanged` — preserves the originating VTEP's loopback as the BGP next-hop (required; see §8)
- `neighbor TRANSIT_OVERLAY send-community extended` — ensures RT extended communities flow through (redundant by default; kept for clarity)

---

### Route Flow examples
A Spoke Advertisement, where `vtep3` originates a route:
1. `vtep3` advertises an EVPN type 2 or type 3 route tagged RT `65000:1003`.
2. Both spines receive it.
3. Spines forward it to the hub (VTEP1) via `EVPN-TO-HUB` (permit all).
4. Spines drop it toward all other spokes (VTEPs). `EVPN-TO-SPOKE` only allows RT `65000:1000`.
5. Hub (`vtep1`) imports it because `65000:1003` is in its import list.

The result is that `vtep3's` route is known only at the hub. No spoke-to-spoke visibility.
<br>
<br>

A Hub Advertisement, where `vtep1` originates a route:
1. `vtep1` advertises an EVPN route tagged RT `65000:1000`.
2. Both spines receive it.
3. Spines forward it to all spokes via `EVPN-TO-SPOKE` (hub RT is the one RT the map permits).
4. All spokes import it, since every spoke imports `65000:1000`.

The result is that hub routes are visible everywhere.

---

### Data-Plane Setup
`config_l2vni()` and `config_vtep()` build the following on each VTEP:

| Object | Details |
|--------|---------|
| Bridge | `br1000` |
| VXLAN device | `vni1000`, UDP 4789, VNI 1000, bound to the VTEP's loopback |
| Anycast gateway | `192.168.0.250/16` on `br1000` (shared across all VTEPs) |
| Host-facing bond | `hostbond1`, attached to `br1000`, VLAN 1000 |

Each host has a `vtepbond` interface. Mobile VM endpoints are MACVLAN interfaces (`vm1`, `vm2`, …) created and deleted on host nodes during migration. The controller endpoint is a permanent MACVLAN on `host1`.

VM IPs follow the scheme `192.168.100.{vm_index}/16`. MACs are deterministic: `00:aa:bb:cc:{hi}:{lo}`.

---

### Test Parameters

Key constants in `test_evpn_capstone_asym.py` (most can be overridden via environment variables):

| Constant | Default | Meaning |
|----------|---------|---------|
| `NUM_MOBILE_VMS` | `30` | Total mobile VM endpoints created |
| `CONTROLLER_VTEPS` | `{"vtep1"}` | Hub VTEPs; controller endpoint lives here |
| `MIGRATION_BATCH_SIZE` | `5` | VMs moved per batch |
| `MIGRATION_REPEAT_COUNT` | `5` | Full migration rounds |
| `MIGRATION_BATCH_SETTLE_SECONDS` | `0.6` | Pause between batches |
| `MOBILITY_OVERLAP_SECONDS` | `0.2` | Duplicate-MAC overlap window per VM |

After migration completes, a non-asserting spot-check runs three ping scenarios. See §9 for full details of what those pings test and why they behave unexpectedly.

---

### Basic Troubleshooting

Run these on the relevant nodes:

```
show bgp l2vpn evpn summary
show bgp l2vpn evpn route
show evpn mac vni 1000
show running-config
bridge fdb show
```

Check in this order:

1. **Sessions up** — EVPN sessions established on both spine links for every VTEP.
2. **Route-map bindings** — hub-facing neighbor uses `EVPN-TO-HUB out`; all spoke-facing neighbors use `EVPN-TO-SPOKE out`.
3. **RT import/export** — hub imports `65000:1000..1007`; each spoke imports only `65000:1000` and exports only its own RT.
4. **Underlay reachability** — VTEP loopbacks `192.168.100.15..21` are reachable via BGP from all nodes.
5. **`advertise-all-vni` present** — if `mp_reach` in pcap is 0, this is the first thing to check.

---

### Configuration Command References
Verified against FRR source code and official documentation on 2026-03-01

#### `advertise-all-vni`
**Required.** This is the master on/off switch for EVPN in FRR.

Source: `bgpd/bgp_evpn.h` — `#define EVPN_ENABLED(bgp) (bgp)->advertise_all_vni`. The macro gates every EVPN route-origination code path (type-2, type-3, type-5). When the flag is clear, `bgp_zebra_advertise_all_vni()` is called with `advertise=0`, telling zebra to stop forwarding VNI notifications to BGP. No EVPN routes are ever originated, regardless of any `vni` block configured underneath.

FRR docs: *"The command to enable EVPN for a BGP instance is `advertise-all-vni`."*

The original vtep1 and vtep2 configs had `no advertise-all-vni`, which caused `mp_reach_nlri` to be 0 in all BGP UPDATEs from those nodes.

---

#### `next-hop-unchanged`

**Required on spines.** Preserves the originating VTEP's loopback as the BGP next-hop when a spine re-advertises EVPN routes over eBGP.

Source: `bgpd/bgp_updgrp_packet.c`. eBGP normally rewrites the next-hop to the advertising router's own address (RFC 4271 §5.1.3). `PEER_FLAG_NEXTHOP_UNCHANGED` bypasses this in both `bgp_route.c` and `bgp_updgrp_packet.c`. Without it, all EVPN routes would point at the spine as their VXLAN tunnel endpoint — but the spine has no VXLAN device, so all tunnels would be broken.

Must be configured per-neighbor under `address-family l2vpn evpn`.

---

#### `send-community extended`

**Redundant but kept for documentation.** Extended communities (which carry route-targets) are sent by default.

Source: `bgpd/bgpd.c` — at peer creation, `PEER_FLAG_SEND_COMMUNITY`, `PEER_FLAG_SEND_EXT_COMMUNITY`, and `PEER_FLAG_SEND_LARGE_COMMUNITY` are set for every AFI/SAFI regardless of the active profile. The CLI only shows `no neighbor X send-community extended` when the default has been explicitly disabled.

The line is kept in the spine configs as an explicit reminder that RTs must flow through the transit node for the route-map RT matching to function.

---

#### `bgp retain route-target all` — NOT used, not applicable

**This command has no effect on EVPN and must not appear at the `router bgp` level.**

Source: `bgpd/bgp_vty.c` installs this command only under `BGP_VPNV4_NODE` and `BGP_VPNV6_NODE`. The filter it controls in `bgpd/bgp_route.c` is guarded by `safi == SAFI_MPLS_VPN` and has no effect on `SAFI_EVPN`. Furthermore, the flag it sets (`BGP_VPNVX_RETAIN_ROUTE_TARGET_ALL`) defaults to ON at BGP instance creation, so it would be a no-op even in an L3VPN context unless previously disabled.

An earlier version of the spine configs incorrectly included this at the `router bgp` level; it has been removed.

---

#### `frr defaults datacenter`

**Recommended.** Applies BGP defaults suited for datacenter leaf-spine deployments.

Source: `bgpd/bgp_vty.c` and `bgpd/bgp_vty.h`. Key differences from the `traditional` profile:

| Setting | Datacenter | Traditional |
|---------|------------|-------------|
| `bgp ebgp-requires-policy` | **disabled** | enabled (≥ FRR 7.4) |
| BGP keepalive / hold timers | **3 s / 9 s** | 60 s / 180 s |
| `bgp log-neighbor-changes` | enabled | disabled |
| `bgp deterministic-med` | enabled | disabled |
| Dynamic capability | enabled | disabled |

The most significant effect is disabling `ebgp-requires-policy`. Without it (or an explicit `no bgp ebgp-requires-policy`), any eBGP neighbor that lacks inbound and outbound route-maps will silently discard all routes per RFC 8212. The `no bgp ebgp-requires-policy` line in each spine config is redundant with the datacenter profile but is kept for explicitness.

---

#### `advertise-svi-ip`

**Required on all VTEPs with a routed SVI.** Advertises the bridge SVI's IP and MAC as a type-2 EVPN route, making the anycast gateway address (`192.168.0.250`) reachable from remote VTEPs without requiring a BUM flood.

FRR docs: *"This option advertises the SVI IP/MAC address as a type-2 route and eliminates the need for any flooding over VXLAN to reach the IP from a remote VTEP."*

Do not combine with `advertise-default-gw`.

---

### Post-Test Ping Check: Hub-Relay MAC Learning Bypass
`run_post_mobility_controller_spotcheck` runs once after all migration rounds complete. The pings are informational, meaning they print SUCCESS / FAILED but do not assert and cannot fail the test.

| # | Source | Destination | Expected result under hub-spoke policy |
|---|--------|-------------|----------------------------------------|
| 1 | Controller endpoint on vtep1 | 3 sampled VM IPs (`192.168.100.{1-N}`) | **PASS** — hub imports all spoke RTs |
| 2 | A mobile VM on vtep2 | Controller IP on vtep1 | **PASS** — all spokes import hub RT `65000:1000` |
| 3 | A mobile VM on vtep2 | Two mobile VMs on vtep3+ | **Should FAIL** — spoke-to-spoke routes are filtered at spines |

Checks 1 and 2 behave as expected. Check 3 always passes when it should fail.

#### Check 3
Why does check 3 pass despite correct control-plane policy?

The spine policy is correct: spoke VTEPs never receive type 2 or type 3 EVPN routes from peer spokes. vtep2's FDB should have no entry for vtep3 VMs. The pings should fail.

They pass because the VXLAN bridge port is configured with `learning on` and *no* `neigh_suppress`, in `config_l2vni`:

```python
node.run("/sbin/bridge link set dev vni1000 learning on")
# neigh_suppress is NOT set
```

With kernel MAC learning enabled, the data plane works around the route policy through hub-relayed ARP frames:

1. A VM on vtep2 ARPs for a vtep3 VM IP. vtep2 BUM-floods the ARP over VXLAN to **vtep1 only** — the only IMET entry in vtep2's FDB, exactly as the control plane intends.
2. vtep1 (the hub) holds IMET routes from all VTEPs and re-floods the BUM frame onward to vtep3, vtep4, … in a second VXLAN encapsulation.
3. The vtep3 VM replies. The reply travels vtep3 → vtep1 (the only path vtep3's FDB knows) → vtep2.
4. vtep2's bridge sees the ARP reply arrive with outer source IP **vtep1**, not vtep3. It learns the vtep3 VM MAC as reachable via vtep1 in its kernel FDB.
5. All subsequent vtep2 → vtep3 unicast traffic is silently hairpinned through vtep1, and the pings succeed.

The control plane is correct. The kernel data plane bypasses it by populating the FDB from data-plane frames rather than control-plane routes.

#### The Fix

Disable kernel learning and enable neighbour suppression on the VXLAN bridge port:

```python
node.run("/sbin/bridge link set dev vni1000 learning off")
node.run("/sbin/bridge link set dev vni1000 neigh_suppress on")
```

`neigh_suppress on` suppresses ARP/ND broadcasts on the bridge port. The kernel instead relies on FRR to supply MAC+IP bindings via type-2 EVPN routes as kernel ARP/neighbour table entries. `learning off` prevents the FDB from being populated by data-plane source MACs.

With both flags set:
- vtep2 → hub VMs: **PASS** (type-2 routes imported via `65000:1000`; FRR populates kernel ARP table)
- vtep2 → vtep3 VMs: **FAIL** (no type-2 routes imported; `neigh_suppress` suppresses the ARP; FDB never learns the hairpin) ✓
- vtep2 → anycast gateway `192.168.0.250`: **PASS** (FRR advertises SVI IP/MAC via `advertise-svi-ip`)

### Current test status

The test currently uses `learning on` with no `neigh_suppress`, so check 3 always passes. The spoke-to-spoke isolation enforced by the spine route-maps is **not exercised** by the test in its current form.

The base `bgp_evpn_capstone` topology (full-mesh, all-to-all RTs) is unaffected — spoke-to-spoke traffic is the expected and correct behaviour there, and `learning on` works correctly in that context.

---

## Multi-Threaded Inter-Batch Reachability Pings

To simulate realistic data-plane usage, this test contains a traffic verification step where a static controller endpoint (located on `vtep1` / `host1`) attempts to ping recently migrated mobile VMs. 

Because EVPN route propagation requires a convergence window, a 5-second hold (`REACHABILITY_HOLD_SECONDS`) is observed before pinging. To ensure this delay does not artificially blockade the continuous mobility simulation, the reachability checks are **multi-threaded**.

### How it works
1. A batch of 5 VMs is migrated.
2. The main Python thread instantly spawns a background `threading.Thread` to handle the pings for that batch.
3. The main thread immediately proceeds to migrate the next batch of 5 VMs.
4. Concurrently, the background thread sleeps for 5 seconds, and then issues the ICMP echo requests. 
5. All interaction with the underlying virtual nodes is guarded by a global `mininet_lock` (`threading.Lock()`) to prevent concurrent `pexpect` shell corruption.

### How to verify it is working
- **Terminal Output**: When running the test (`sudo -E pytest -s tests/topotests/bgp_evpn_capstone_asym/test_evpn_capstone_asym.py`), you will see the `[Background] Ping vmX (192.168.100.X) ... OK` messages interleave asynchronously with the "Migrating batch Y" messages.
- **Visualizer**: Open the topology visualizer in your browser. As the test runs, you will see cyan data-plane packet dots shooting from the static `controller` endpoint to the roaming `vm` endpoints, representing the concurrent data-plane ping reachability checks seamlessly happening alongside mobility events.
