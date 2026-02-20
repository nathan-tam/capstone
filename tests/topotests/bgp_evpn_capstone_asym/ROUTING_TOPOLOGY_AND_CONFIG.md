# EVPN Asymmetric Capstone: Topology and Routing Guide

This guide explains what the `bgp_evpn_capstone_asym` test builds, how routing is intended to work, and what to check when behavior is unexpected.

Target reader: someone familiar with basic IP routing, but new to this specific topology.

## 1) Quick-start snapshot (read this first)

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
- `advertise-svi-ip`
- explicit `vni 1000` block with manual RD/RTs

### Important nuance: `advertise-all-vni` is not uniform

- `vtep1` and `vtep2` explicitly configure `no advertise-all-vni`.
- `vtep3` to `vtep7` currently have `advertise-all-vni` present.

Because all nodes still define explicit `vni 1000` with manual RTs, the service intent remains hub/spoke. This mixed style is worth remembering when troubleshooting.

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

- `bgp retain-route-target all`
- `neighbor TRANSIT_OVERLAY send-community extended`
- EVPN `neighbor TRANSIT_OVERLAY next-hop-unchanged`

Both spines also define:

- extcommunity list `EVPN-HUB-RT` = `rt 65000:1000`
- extcommunity list `EVPN-SPOKE-RTS` = `rt 65000:1002..65000:1007`

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
