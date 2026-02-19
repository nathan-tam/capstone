# EVPN Asymmetric Capstone: Topology, Routing Intent, and Config Summary

This document explains how the topology is intended to work and what the current configs implement in `tests/topotests/bgp_evpn_capstone_asym`.

It is written for readers comfortable with networking fundamentals, but not necessarily deep BGP/EVPN internals.

## 1) What this topology is trying to achieve

The asym test models a **hub-and-spoke EVPN control plane**:

- `vtep1` is the **hub** VTEP.
- `vtep2` through `vtep7` are **spoke** VTEPs.
- Spokes should learn hub endpoints.
- Hub should learn spoke endpoints.
- Spokes should not directly learn each other’s endpoint routes.

In practical terms: endpoint reachability is intended to be mediated by the hub route-target policy, instead of full spoke-to-spoke route exchange.

## 2) Physical/logical topology built by the test

From `test_evpn_capstone_asym.py` (`build_topo()`):

- 2 spines: `spine1`, `spine2`
- 7 VTEPs: `vtep1..vtep7`
- 7 hosts: `host1..host7`
- Each VTEP has one link to each spine
- Each host is attached to exactly one VTEP

High-level layout:

```text
                     spine1 ---------------- spine2
                    /  |  |  |  |  |  \     /  |  |  |  |  |  \
               vtep1 vtep2 vtep3 vtep4 vtep5 vtep6 vtep7
                 |     |     |     |     |     |     |
               host1 host2 host3 host4 host5 host6 host7
```

## 3) Underlay routing model (IPv4 unicast eBGP)

All BGP adjacencies are eBGP (`remote-as external`) between each VTEP and both spines.

### Spine loopbacks / router IDs

- `spine1`: loopback `192.168.100.13` (BGP router-id `192.168.100.13`)
- `spine2`: loopback `192.168.100.14` (BGP router-id `192.168.100.14`)

### Point-to-point subnets used for spine↔VTEP links

From `spine1/zebra.conf` and `spine2/zebra.conf`:

- Spine1 side: `192.168.1.1`, `2.1`, `3.1`, `4.1`, `9.1`, `11.1`, `13.1`
- Spine2 side: `192.168.5.1`, `6.1`, `7.1`, `8.1`, `10.1`, `12.1`, `14.1`

VTEP neighbors use the `.1` addresses above, and spines peer to corresponding VTEP `.2` addresses.

## 4) EVPN control-plane model

Each VTEP enables:

- `address-family l2vpn evpn`
- `advertise-all-vni`
- `advertise-svi-ip`
- VNI `1000` with explicit `rd` and RT import/export policy

### Hub/spoke RT policy on VTEPs

Route-target behavior from VTEP `evpn.conf` files:

- **Hub (`vtep1`)**
  - exports: `RT 65000:1000`
  - imports: `RT 65000:1000`, `65000:1002`, `65000:1003`, `65000:1004`, `65000:1005`, `65000:1006`, `65000:1007`

- **Spokes (`vtep2..vtep7`)**
  - each spoke exports its own RT:
    - `vtep2` -> `65000:1002`
    - `vtep3` -> `65000:1003`
    - `vtep4` -> `65000:1004`
    - `vtep5` -> `65000:1005`
    - `vtep6` -> `65000:1006`
    - `vtep7` -> `65000:1007`
  - each spoke imports only `RT 65000:1000` (hub RT)

This means spokes are configured to learn hub-labeled EVPN routes, not each other’s spoke-labeled routes.

## 5) Spine EVPN export filtering policy

Both spines apply outbound EVPN route-maps by neighbor in `address-family l2vpn evpn`.

Shared policy objects on both spines:

- `bgp extcommunity-list standard EVPN-HUB-RT permit rt 65000:1000`
- `bgp extcommunity-list standard EVPN-SPOKE-RTS permit rt 65000:1002 ... rt 65000:1007`

Route-maps:

- `EVPN-TO-HUB`
  - `permit 10 match extcommunity EVPN-SPOKE-RTS any`
  - `deny 20`
- `EVPN-TO-SPOKE`
  - `permit 10 match extcommunity EVPN-HUB-RT any`
  - `deny 20`

Neighbor attachment:

- On each spine, hub-facing neighbor gets `EVPN-TO-HUB`
- All spoke-facing neighbors get `EVPN-TO-SPOKE`

Resulting intent:

- Hub receives spoke RT routes
- Spokes receive only hub RT routes
- Spoke RT routes are not propagated to other spokes by the spine policy

## 6) BGP advertisement flow (step-by-step)

This section focuses on how EVPN advertisements are expected to flow in this design.

### Flow A: a spoke endpoint is advertised

Example: endpoint is on `vtep3`.

1. `vtep3` originates EVPN routes for that endpoint under VNI 1000.
1. `vtep3` exports those routes with RT `65000:1003`.
1. Both spines receive the route from `vtep3`.
1. Toward the hub neighbor on each spine, `EVPN-TO-HUB` is applied and permits RTs in `EVPN-SPOKE-RTS` (`65000:1002..1007`), so the route is sent to `vtep1`.
1. Toward spoke neighbors on each spine, `EVPN-TO-SPOKE` is applied and permits only RT `65000:1000`, so the spoke-origin RT `65000:1003` route is not sent to other spokes.
1. `vtep1` imports RT `65000:1003` and installs the route; other spokes do not receive it from spines in this policy.

### Flow B: a hub endpoint is advertised

Example: endpoint is on `vtep1`.

1. `vtep1` originates EVPN routes and exports RT `65000:1000`.
2. Both spines receive the route from `vtep1`.
3. Toward spoke neighbors, spines apply `EVPN-TO-SPOKE`.
4. `EVPN-TO-SPOKE` permits RT `65000:1000`, so hub-origin routes are advertised to spokes.
5. Each spoke imports RT `65000:1000`, so spoke VTEPs learn hub-origin routes.

### Flow C: why spoke-to-spoke route distribution is blocked

- Spoke-origin routes carry RTs `65000:1002..1007`.
- Spines only advertise those RTs to the hub (`EVPN-TO-HUB`).
- Spoke-facing advertisements are restricted to RT `65000:1000` (`EVPN-TO-SPOKE`).
- Therefore, a spoke does not learn another spoke’s endpoint routes through the spines.

## 7) Node identity summary (from current `evpn.conf`)

- `spine1`: ASN `65001`, router-id `192.168.100.13`, transit/filter node
- `spine2`: ASN `65001`, router-id `192.168.100.14`, transit/filter node
- `vtep1`: ASN `65011`, router-id `192.168.100.15`, hub
- `vtep2`: ASN `65012`, router-id `192.168.100.16`, spoke
- `vtep3`: ASN `65021`, router-id `192.168.100.17`, spoke
- `vtep4`: ASN `65022`, router-id `192.168.100.18`, spoke
- `vtep5`: ASN `65023`, router-id `192.168.100.19`, spoke
- `vtep6`: ASN `65024`, router-id `192.168.100.20`, spoke
- `vtep7`: ASN `65025`, router-id `192.168.100.21`, spoke

## 8) Data-plane construction done by the test code

From `config_l2vni()` and `config_vtep()` in `test_evpn_capstone_asym.py`:

- each VTEP builds bridge `br1000`
- VTEP SVI IP is assigned to `br1000`
- anycast gateway `192.168.0.250/16` is assigned to `br1000`
- VXLAN device `vni1000` (UDP 4789, ID 1000) is attached to `br1000`
- host-facing bond `hostbond1` is attached to `br1000` and placed in VLAN 1000

Hosts use `vtepbond`, and mobile endpoints are created as MACVLAN interfaces on top of that bond.

## 9) Why this is “asymmetric” behavior

In a full-mesh EVPN import/export design, every VTEP would typically import the same service RT(s), allowing direct spoke-to-spoke endpoint route learning.

Here, the config intentionally does not do that:

- spokes only import hub RT `65000:1000`
- spines only send spoke RT routes toward hub, not toward other spokes

So control-plane route visibility is intentionally asymmetric (hub sees all spokes; each spoke mainly sees hub scope).

## 10) Quick validation commands

Useful checks while troubleshooting:

- `show bgp l2vpn evpn summary`
- `show bgp l2vpn evpn route`
- `show evpn mac vni 1000`
- `show running-config` (confirm route-map attachment per spine neighbor)
- `bridge fdb show`

If behavior differs from intent, first verify:

1. EVPN neighbor sessions are established on both spines.
2. Spine route-map bindings are on the correct hub vs spoke neighbors.
3. VTEP import/export RT lines match the intended role.
