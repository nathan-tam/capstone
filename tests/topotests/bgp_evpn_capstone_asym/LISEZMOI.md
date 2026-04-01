# EVPN Asymmetric Capstone: Topology and Routing Guide

This guide explains what the `bgp_evpn_capstone_asym` test builds, how routing is intended to work, and what to check when behavior is unexpected.

Target reader: someone familiar with basic IP routing, but new to this specific topology.

---

## 1) Topology and intent

### Physical layout

`build_topo()` in `test_evpn_capstone_asym.py` creates 2 spines, 7 VTEPs, and 7 hosts. Every VTEP connects to both spines. Each host connects to exactly one VTEP (1:1 with 7 hosts and 7 VTEPs).

```text
                    spine1                           spine2
               (AS 65001)                       (AS 65001)
               | | | | | | |                   | | | | | | |
               v v v v v v v                   v v v v v v v
             vtep1 vtep2 vtep3 vtep4 vtep5 vtep6 vtep7
             (hub)       (spoke VTEPs, AS 65012..65025)
               |     |     |     |     |     |     |
             host1 host2 host3 host4 host5 host6 host7
```

### Hub-and-spoke intent

This topology models a **hub-and-spoke EVPN control plane**, not a full mesh:

- `vtep1` is the hub. It is also the controller-side VTEP where the static test endpoint lives.
- `vtep2..vtep7` are spokes. They host mobile VM endpoints.
- The hub learns routes from all spokes. Spokes learn routes only from the hub.
- **Spokes must not learn each other's EVPN routes.** This is the core invariant the spine policy enforces.

### Route-target scheme

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

## 2) Underlay BGP (IPv4 unicast)

eBGP is used everywhere (`remote-as external`). The spines are pure transit and filter nodes — they carry no local VXLAN endpoints.

### Loopbacks

- `spine1`: `192.168.100.13`
- `spine2`: `192.168.100.14`
- `vtep1..vtep7`: `192.168.100.15` through `192.168.100.21`

### Spine-facing link IPs (from `zebra.conf`)

- `spine1` side: `192.168.{1,2,3,4,9,11,13}.1` (one per VTEP)
- `spine2` side: `192.168.{5,6,7,8,10,12,14}.1` (one per VTEP)

VTEPs peer to the spine's `.1` address; spines peer back to the VTEP's `.2` address on each link.

---

## 3) EVPN configuration

### On all VTEPs

Every VTEP uses these settings under `address-family l2vpn evpn`:

- `advertise-all-vni` — master switch; without this, no EVPN routes are originated at all (see §8)
- `advertise-svi-ip` — advertises the bridge SVI IP/MAC as a type-2 route so the anycast gateway is reachable without flooding (see §8)
- `neighbor TRANSIT_OVERLAY activate` — activates the EVPN address family toward both spines
- `vni 1000` block with explicit RD and RT import/export
- `frr defaults datacenter` — disables `ebgp-requires-policy`, reduces timers to 3 s/9 s (see §8)

### On both spines

The spines enforce the hub-spoke asymmetry using outbound route-maps:

```
extcommunity-list standard EVPN-HUB-RT permit rt 65000:1000

route-map EVPN-TO-HUB permit 10
  (no match — permits all EVPN routes)

route-map EVPN-TO-SPOKE permit 10
  match community EVPN-HUB-RT
route-map EVPN-TO-SPOKE deny 20
```

Per-neighbor bindings under `address-family l2vpn evpn`:

- vtep1 (hub): `neighbor vtep1 route-map EVPN-TO-HUB out`
- vtep2..vtep7 (spokes): `neighbor vtepN route-map EVPN-TO-SPOKE out`

Additional spine settings:

- `neighbor TRANSIT_OVERLAY next-hop-unchanged` — preserves the originating VTEP's loopback as the BGP next-hop (required; see §8)
- `neighbor TRANSIT_OVERLAY send-community extended` — ensures RT extended communities flow through (redundant by default; kept for clarity)

---

## 4) Route flow examples

### Spoke advertisement (`vtep3` originates a route)

1. `vtep3` advertises an EVPN type-2 or type-3 route tagged RT `65000:1003`.
2. Both spines receive it.
3. Spines forward it to the hub via `EVPN-TO-HUB` (permit all).
4. Spines **drop** it toward all other spokes — `EVPN-TO-SPOKE` only allows RT `65000:1000`.
5. Hub (`vtep1`) imports it because `65000:1003` is in its import list.

**Result:** vtep3's route is known only at the hub. No spoke-to-spoke visibility.

### Hub advertisement (`vtep1` originates a route)

1. `vtep1` advertises an EVPN route tagged RT `65000:1000`.
2. Both spines receive it.
3. Spines forward it to all spokes via `EVPN-TO-SPOKE` (hub RT is the one RT the map permits).
4. All spokes import it — every spoke imports `65000:1000`.

**Result:** hub routes are visible everywhere.

---

## 5) Data-plane setup in the test harness

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

## 6) Test parameters

Key constants in `test_evpn_capstone_asym.py` (most can be overridden via environment variables):

| Constant | Default | Meaning |
|----------|---------|---------|
| `NUM_MOBILE_VMS` | `30` | Total mobile VM endpoints created |
| `CONTROLLER_VTEPS` | `{"vtep1"}` | Hub VTEPs; controller endpoint lives here |
| `MIGRATION_BATCH_SIZE` | `5` | VMs moved per batch |
| `MIGRATION_REPEAT_COUNT` | `3` | Full migration rounds |
| `MIGRATION_BATCH_SETTLE_SECONDS` | `0.5` | Pause between batches |
| `MOBILITY_OVERLAP_SECONDS` | `0.2` | Duplicate-MAC overlap window per VM |

After every migration round a non-asserting spot-check runs three ping scenarios. See §9 for full details of what those pings test and why they behave unexpectedly.

---

## 7) Troubleshooting checklist

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

## 8) Configuration command reference

Verified against FRR source code and official documentation on 2026-03-01.

---

### `advertise-all-vni`

**Required.** This is the master on/off switch for EVPN in FRR.

Source: `bgpd/bgp_evpn.h` — `#define EVPN_ENABLED(bgp) (bgp)->advertise_all_vni`. The macro gates every EVPN route-origination code path (type-2, type-3, type-5). When the flag is clear, `bgp_zebra_advertise_all_vni()` is called with `advertise=0`, telling zebra to stop forwarding VNI notifications to BGP. No EVPN routes are ever originated, regardless of any `vni` block configured underneath.

FRR docs: *"The command to enable EVPN for a BGP instance is `advertise-all-vni`."*

The original vtep1 and vtep2 configs had `no advertise-all-vni`, which caused `mp_reach_nlri` to be 0 in all BGP UPDATEs from those nodes.

---

### `next-hop-unchanged`

**Required on spines.** Preserves the originating VTEP's loopback as the BGP next-hop when a spine re-advertises EVPN routes over eBGP.

Source: `bgpd/bgp_updgrp_packet.c`. eBGP normally rewrites the next-hop to the advertising router's own address (RFC 4271 §5.1.3). `PEER_FLAG_NEXTHOP_UNCHANGED` bypasses this in both `bgp_route.c` and `bgp_updgrp_packet.c`. Without it, all EVPN routes would point at the spine as their VXLAN tunnel endpoint — but the spine has no VXLAN device, so all tunnels would be broken.

Must be configured per-neighbor under `address-family l2vpn evpn`.

---

### `send-community extended`

**Redundant but kept for documentation.** Extended communities (which carry route-targets) are sent by default.

Source: `bgpd/bgpd.c` — at peer creation, `PEER_FLAG_SEND_COMMUNITY`, `PEER_FLAG_SEND_EXT_COMMUNITY`, and `PEER_FLAG_SEND_LARGE_COMMUNITY` are set for every AFI/SAFI regardless of the active profile. The CLI only shows `no neighbor X send-community extended` when the default has been explicitly disabled.

The line is kept in the spine configs as an explicit reminder that RTs must flow through the transit node for the route-map RT matching to function.

---

### `bgp retain route-target all` — NOT used, not applicable

**This command has no effect on EVPN and must not appear at the `router bgp` level.**

Source: `bgpd/bgp_vty.c` installs this command only under `BGP_VPNV4_NODE` and `BGP_VPNV6_NODE`. The filter it controls in `bgpd/bgp_route.c` is guarded by `safi == SAFI_MPLS_VPN` and has no effect on `SAFI_EVPN`. Furthermore, the flag it sets (`BGP_VPNVX_RETAIN_ROUTE_TARGET_ALL`) defaults to ON at BGP instance creation, so it would be a no-op even in an L3VPN context unless previously disabled.

An earlier version of the spine configs incorrectly included this at the `router bgp` level; it has been removed.

---

### `frr defaults datacenter`

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

### `advertise-svi-ip`

**Required on all VTEPs with a routed SVI.** Advertises the bridge SVI's IP and MAC as a type-2 EVPN route, making the anycast gateway address (`192.168.0.250`) reachable from remote VTEPs without requiring a BUM flood.

FRR docs: *"This option advertises the SVI IP/MAC address as a type-2 route and eliminates the need for any flooding over VXLAN to reach the IP from a remote VTEP."*

Do not combine with `advertise-default-gw`.

---

## 9) Post-mobility ping check: hub-relay MAC learning bypass

### The three ping checks

`run_post_mobility_controller_spotcheck` runs after each migration round. The pings are informational — they print `SUCCESS` / `FAILED` but do not assert and cannot fail the test.

| # | Source | Destination | Expected result under hub-spoke policy |
|---|--------|-------------|----------------------------------------|
| 1 | Controller endpoint on vtep1 | 3 sampled VM IPs (`192.168.100.{1-N}`) | **PASS** — hub imports all spoke RTs |
| 2 | A mobile VM on vtep2 | Controller IP on vtep1 | **PASS** — all spokes import hub RT `65000:1000` |
| 3 | A mobile VM on vtep2 | Two mobile VMs on vtep3+ | **Should FAIL** — spoke-to-spoke routes are filtered at spines |

Checks 1 and 2 behave as expected. Check 3 always passes when it should fail.

### Why check 3 passes despite correct control-plane policy

The spine policy is correct: spoke VTEPs never receive type-2 or type-3 EVPN routes from peer spokes. vtep2's FDB should have no entry for vtep3 VMs. The pings should fail.

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

### The fix

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


### Roles and RTs at a glance

```text
                           +------------------------------+
                           | spine1 + spine2 (AS 65001)  |
                           | Transit EVPN policy nodes    |
                           +---------------+--------------+
                                           |
                      -------------------------------------------------
                      |                    |                          |
                 Hub VTEP             Spoke VTEPs                Hosts
               vtep1 (AS 65011)      vtep2..vtep7               host1..host7
               export RT 1000        export RT 1002..1007       one per VTEP
               import RT 1000,       import RT 1000 only
               1002..1007
```

### Who learns what

| Origin route RT | Learned by hub (`vtep1`) | Learned by spokes (`vtep2..vtep7`) |
| --- | --- | --- |
| `65000:1000` (hub routes) | Yes | Yes |
| `65000:1002..1007` (spoke routes) | Yes | No |

### Spine forwarding policy summary

- To hub neighbor: `EVPN-TO-HUB out` (permit all EVPN routes)
- To spoke neighbors: `EVPN-TO-SPOKE out` (permit only RT `65000:1000`)

## 2) Quick intent (what “asymmetric” means here)

The test models a **hub-and-spoke EVPN control plane**:

- `vtep1` is the hub (controller-side VTEP).
- `vtep2` to `vtep7` are spokes.
- Hub learns spoke routes.
- Spokes learn hub routes.
- Spokes should not learn each other’s spoke-tagged EVPN routes.

In short: route visibility is intentionally one-to-many via the hub policy, not full mesh.

## 3) Topology built by the test

`build_topo()` in `test_evpn_capstone_asym.py` creates:

- 2 spines: `spine1`, `spine2`
- 7 VTEPs: `vtep1..vtep7`
- 7 hosts: `host1..host7`
- Every VTEP connects to both spines.
- Every host connects to exactly one VTEP (round-robin; with 7/7 it is effectively 1:1).

Layout:

```text
                    spine1                           spine2
                      |  \                         /  |
                      |   \                       /   |
          ---------------------------------------------------------
          |      |      |      |      |      |      |
        vtep1  vtep2  vtep3  vtep4  vtep5  vtep6  vtep7
          |      |      |      |      |      |      |
        host1  host2  host3  host4  host5  host6  host7
```

## 4) Underlay BGP (IPv4 unicast)

- eBGP everywhere between VTEPs and spines (`remote-as external`).
- Spines are pure transit/filter nodes in this test.

### Spine identities

- `spine1`: router-id / loopback `192.168.100.13`
- `spine2`: router-id / loopback `192.168.100.14`

### Spine-facing link IPs

From spine `zebra.conf`:

- `spine1` uses: `192.168.1.1`, `2.1`, `3.1`, `4.1`, `9.1`, `11.1`, `13.1`
- `spine2` uses: `192.168.5.1`, `6.1`, `7.1`, `8.1`, `10.1`, `12.1`, `14.1`

VTEPs peer to these `.1` addresses; spines peer to VTEP `.2` addresses.

## 5) EVPN configuration model on VTEPs

All VTEPs use:

- `address-family l2vpn evpn`
- `neighbor TRANSIT_OVERLAY activate`
- `advertise-all-vni` (required for BGP to discover local VNIs and originate EVPN routes)
- `advertise-svi-ip`
- explicit `vni 1000` block with manual RD/RTs
- `frr defaults datacenter` (disables `ebgp-requires-policy`, reduces BGP keepalive/hold timers to 3 s/9 s, and enables other datacenter BGP defaults; see §11 for full details)

### RT policy by role

- Hub (`vtep1`):
  - export `65000:1000`
  - import `65000:1000` and `65000:1002..65000:1007`
- Spokes:
  - `vtep2` export `65000:1002`, import `65000:1000`
  - `vtep3` export `65000:1003`, import `65000:1000`
  - `vtep4` export `65000:1004`, import `65000:1000`
  - `vtep5` export `65000:1005`, import `65000:1000`
  - `vtep6` export `65000:1006`, import `65000:1000`
  - `vtep7` export `65000:1007`, import `65000:1000`

## 6) Spine policy that enforces asymmetry

Both spines currently include:

- `neighbor TRANSIT_OVERLAY send-community extended`
- EVPN `neighbor TRANSIT_OVERLAY next-hop-unchanged`

Both spines also define:

- extcommunity list `EVPN-HUB-RT` = `rt 65000:1000`

Route-maps:

- `EVPN-TO-HUB permit 10` (match-all; hub receives all EVPN routes)
- `EVPN-TO-SPOKE permit 10 match extcommunity EVPN-HUB-RT`
- `EVPN-TO-SPOKE deny 20`

Neighbor bindings:

- hub-facing neighbor (`vtep1`) uses `EVPN-TO-HUB out`
- all spoke-facing neighbors use `EVPN-TO-SPOKE out`

## 7) Route flow examples

### A) Spoke endpoint (`vtep3`) advertisement

1. `vtep3` advertises EVPN route with RT `65000:1003`.
2. Both spines receive it.
3. Spines advertise it to hub (hub neighbor uses `EVPN-TO-HUB`).
4. Spines do not advertise it to other spokes (`EVPN-TO-SPOKE` only allows `65000:1000`).
5. Hub imports it (`vtep1` imports `65000:1003`).

### B) Hub endpoint (`vtep1`) advertisement

1. `vtep1` advertises EVPN route with RT `65000:1000`.
2. Both spines receive it.
3. Spines advertise it to spokes (`EVPN-TO-SPOKE` permits hub RT).
4. Spokes import it (all spokes import `65000:1000`).

## 8) Data-plane setup in the test harness

`config_l2vni()` / `config_vtep()` build on each VTEP:

- bridge `br1000`
- VXLAN interface `vni1000` (UDP 4789, VNI 1000)
- anycast gateway `192.168.0.250/16` on `br1000`
- host-facing bond `hostbond1` attached to `br1000` (VLAN 1000)

Each host uses `vtepbond`; mobile endpoints are MACVLAN interfaces created/deleted during migration.

## 9) Test behavior details that matter

From `test_evpn_capstone_asym.py`:

- `CONTROLLER_VTEPS = {"vtep1"}` (hub/controller side is fixed)
- `NUM_MOBILE_VMS = 30`
- migration behavior is controlled by:
  - `MIGRATION_BATCH_SIZE` (default `5`)
  - `MIGRATION_REPEAT_COUNT` (default `3`)
  - `MIGRATION_BATCH_SETTLE_SECONDS` (default `0.5`)
  - `MOBILITY_OVERLAP_SECONDS` (default `0.2`)
- an always-on post-mobility informational spot-check runs after migration:
  - controller -> 3 deterministic-random VM IPs
  - one VM on VTEP2 -> controller endpoint
  - same VTEP2 VM -> 2 VM IPs on one VTEP3+

These spot-check pings print `SUCCESS` / `FAILED` and do not assert/fail the test.

## 10) Fast troubleshooting checklist

Run these on relevant nodes:

- `show bgp l2vpn evpn summary`
- `show bgp l2vpn evpn route`
- `show evpn mac vni 1000`
- `show running-config` (verify route-map neighbor attachments)
- `bridge fdb show`

If results do not match intent, check in this order:

1. EVPN sessions up on both spine links for every VTEP.
2. Hub neighbor vs spoke neighbors have the correct outbound route-map.
3. RT import/export lines are correct for each VTEP role.
4. Underlay reachability to EVPN next-hop loopbacks (`192.168.100.15..21`).

## 11) Configuration command reference and verification notes

This section records what was verified against the FRR source code and official documentation (verified 2026-03-01). Each command is explained so that future readers can understand why it is present and whether it is strictly required.

---

### `advertise-all-vni`

**Required.** This is the master on/off switch for EVPN in FRR.

Source: `bgpd/bgp_evpn.h` — `#define EVPN_ENABLED(bgp) (bgp)->advertise_all_vni`. The macro gates every EVPN route-origination code path (type-2, type-3, type-5). When the flag is clear, `bgp_zebra_advertise_all_vni()` is called with `advertise=0`, telling zebra to stop sending VNI add/delete notifications to BGP. As a result, no EVPN routes are ever originated, regardless of any `vni` block configuration underneath.

FRR docs: *"The command to enable EVPN for a BGP instance is `advertise-all-vni`."*

Without it — as was the case in the original vtep1/vtep2 configs — `mp_reach_nlri` in BGP UPDATEs will be 0.

---

### `next-hop-unchanged`

**Required on spines.** Preserves the originating VTEP's loopback as the BGP next-hop when an eBGP spine re-advertises EVPN routes to other VTEPs.

Source: `bgpd/bgp_updgrp_packet.c`. For eBGP peers, BGP normally rewrites the next-hop to its own address before sending an UPDATE (RFC 4271 §5.1.3). The flag `PEER_FLAG_NEXTHOP_UNCHANGED` bypasses this rewrite in both the route-announcement stage (`bgp_route.c`) and the packet-formation stage (`bgp_updgrp_packet.c`). Without it, all EVPN routes received by a VTEP would point at the spine as their next-hop; since the spine has no VXLAN termination, all tunnels would break.

Must be configured per-neighbor under `address-family l2vpn evpn`.

---

### `send-community extended`

**Technically redundant but kept for documentation.** Extended communities (which carry route-targets) are sent by default.

Source: `bgpd/bgpd.c` — at peer creation time, `PEER_FLAG_SEND_COMMUNITY`, `PEER_FLAG_SEND_EXT_COMMUNITY`, and `PEER_FLAG_SEND_LARGE_COMMUNITY` are all set for every AFI/SAFI regardless of the active profile. There is no need to configure this explicitly. The CLI shows `no neighbor X send-community extended` only when the default is explicitly disabled.

The explicit line is kept in the spine configs as a reminder that extended communities (carrying RTs) must flow through the transit node for the RT-based filtering to work at all.

---

### `bgp retain route-target all` — NOT used in this topology

**This command does not apply to EVPN and must not be placed at the `router bgp` level.**

Source: `bgpd/bgp_vty.c` — `install_element(BGP_VPNV4_NODE, ...)` / `install_element(BGP_VPNV6_NODE, ...)`. The command is only valid inside `address-family ipv4 vpn` or `address-family ipv6 vpn`. It controls a filter in `bgpd/bgp_route.c` that is guarded by `safi == SAFI_MPLS_VPN`; it has no effect on `SAFI_EVPN`.

Additionally, the flag it sets (`BGP_VPNVX_RETAIN_ROUTE_TARGET_ALL`) defaults to **ON** at BGP instance creation, so even in an L3VPN context this command is only needed when `no bgp retain route-target all` has previously been applied.

An earlier version of the spine configs incorrectly included this line at the `router bgp` level; this was removed.

---

### `frr defaults datacenter`

**Recommended.** Applies a set of BGP defaults suited for datacenter/leaf-spine deployments.

Source: `bgpd/bgp_vty.c` and `bgpd/bgp_vty.h`. Key changes relative to the `traditional` profile:

| Setting | Datacenter | Traditional |
|---|---|---|
| `bgp ebgp-requires-policy` | **disabled** | enabled (≥ 7.4) |
| BGP keepalive / hold | **3 s / 9 s** | 60 s / 180 s |
| `bgp log-neighbor-changes` | enabled | disabled |
| `bgp deterministic-med` | enabled | disabled |
| Dynamic capability | enabled | disabled |

The most operationally significant effect is disabling `ebgp-requires-policy`. Without the datacenter profile (or an explicit `no bgp ebgp-requires-policy`), eBGP neighbors without route-maps configured in both directions will silently drop all routes per RFC 8212.

The `no bgp ebgp-requires-policy` line in each spine config is technically redundant with `frr defaults datacenter` but is kept for explicitness.

---

### `advertise-svi-ip`

**Required on all VTEPs that have a routed SVI.** Advertises the SVI IP/MAC as a type-2 EVPN route, making the gateway IP reachable from remote VTEPs without flooding.

FRR docs: *"This option advertises the SVI IP/MAC address as a type-2 route and eliminates the need for any flooding over VXLAN to reach the IP from a remote VTEP."*

Do not combine with `advertise-default-gw`.

---

## 12) Post-mobility ping check: hub-relay MAC learning bypass

### The three ping checks

`run_post_mobility_controller_spotcheck` performs three classes of ping after every migration round:

| # | Source | Destination | Expected result (hub-spoke policy) |
|---|--------|-------------|-------------------------------------|
| 1 | Controller endpoint on vtep1 | Sampled VM IPs (`192.168.100.{1-N}`) | **PASS** — hub imports all spoke RTs |
| 2 | A mobile VM on vtep2 | Controller IP on vtep1 | **PASS** — all spokes import hub RT `65000:1000` |
| 3 | A mobile VM on vtep2 | Two mobile VMs on vtep3+ | **Should FAIL** — spoke-to-spoke routes are filtered at spines |

Check 3 is the interesting one: spoke VTEPs never receive type-2/type-3 EVPN routes from peer spokes (the spine route-maps block them), so vtep2 should have no FDB entry for vtep3 VMs and the pings should fail. In practice they always pass.

### Why check 3 passes despite correct control-plane policy

The root cause is `learning on` (no `neigh_suppress`) on the VXLAN bridge port, set in `config_l2vni`:

```python
node.run("/sbin/bridge link set dev vni1000 learning on")
# neigh_suppress is NOT set
```

With kernel MAC learning enabled, the data plane silently works around the route policy via hub-relayed frames:

1. A VM on vtep2 ARPs for a vtep3 VM IP → vtep2 BUM-floods over VXLAN to **vtep1 only** (vtep2's FDB at this point only has IMET routes for vtep1, as intended by the policy).
2. vtep1 (the hub) has IMET routes from **all** VTEPs and so re-floods the BUM frame onward to vtep3, vtep4, … in a second VXLAN encapsulation (hub-relay).
3. The ARP reply from the vtep3 VM travels: vtep3 → vtep1 (only path the spoke FDB knows) → vtep2. vtep1 is a regular VXLAN tunnel endpoint and forwards the unicast reply.
4. vtep2's bridge sees the ARP reply arrive from outer source IP **vtep1**, not vtep3. It learns the vtep3 VM MAC as reachable via vtep1 in its kernel FDB.
5. All subsequent vtep2→vtep3 unicast traffic is hairpinned through vtep1, and the pings succeed.

The EVPN control plane is correct — no spoke-to-spoke type-2 routes are exchanged. The kernel data plane circumvents this by learning a stale hairpin path through the hub.

### The correct fix

Set both `learning off` and `neigh_suppress on` on the VXLAN bridge port:

```python
node.run("/sbin/bridge link set dev vni1000 learning off")
node.run("/sbin/bridge link set dev vni1000 neigh_suppress on")
```

`neigh_suppress on` instructs the kernel to suppress ARP/ND broadcasts on that bridge port and instead rely on the BGP EVPN control plane (FRR injects type-2 MAC+IP bindings as kernel ARP/neighbour entries). `learning off` prevents the kernel from populating the FDB from data-plane source MACs.

With both flags set:
- vtep2 can reach hub VMs (type-2 routes imported via `65000:1000`) ✓
- vtep2 cannot reach vtep3 VMs (no type-2 routes imported, `neigh_suppress` suppresses the ARP, kernel FDB never learns the hairpin) ✓ → check 3 correctly fails
- ARP for the anycast gateway (`192.168.0.250`) continues to work because FRR advertises the SVI IP/MAC as a type-2 route via `advertise-svi-ip` ✓

### Current test status

The test currently uses `learning on` / no `neigh_suppress`, so check 3 always passes and does **not** validate spoke-to-spoke isolation. This means:

- The spoke-to-spoke isolation enforced by the spine route-maps is not exercised by the test.
- The base `bgp_evpn_capstone` topology (full-mesh, all-to-all RTs) is unaffected — spoke-to-spoke traffic is expected to reach there, and FDB learning works correctly in that context.
