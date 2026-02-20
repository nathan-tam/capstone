# EVPN Capstone Test Suite

This document reflects the current behavior of `test_evpn_capstone.py` in this directory.

The test validates EVPN MAC mobility behavior by creating endpoints, migrating them between VTEPs, and collecting BGP packet captures.

## Current Scope

- 7 VTEPs
- 2 spines
- 7 hosts (one per VTEP)
- 64 mobile VM endpoints (`vm1..vm64`)

Controller model:

- `vtep1` is the controller/static VTEP (`CONTROLLER_VTEPS = {"vtep1"}`)
- controller VTEPs participate in the topology/control-plane
- controller VTEPs are excluded from mobile endpoint placement and migration
- controller endpoint is created on `host1` as:
  - interface: `controller`
  - IP: `192.168.100.254/16`
  - MAC: `00:aa:bb:dd:00:01`

Guardrails in test logic:

- at least one mobility-eligible host must exist
- at least two mobility-eligible VTEPs must exist

## Topology and Addressing

Topology:

```text
                     spine1 ---------------- spine2
                    /  |  |  |  |  |  \     /  |  |  |  |  |  \
                   /   |  |  |  |  |   \   /   |  |  |  |  |   \
               vtep1 vtep2 vtep3 vtep4 vtep5 vtep6 vtep7
                 |     |     |     |     |     |     |
               host1 host2 host3 host4 host5 host6 host7
```

Addressing:

- VTEP VXLAN source IP for `vtep{i}`: `192.168.100.{14+i}`
  - `vtep1..vtep7` => `192.168.100.15..192.168.100.21`
- SVI IPs:
  - `vtep1..vtep5`: `192.168.0.251..192.168.0.255`
  - `vtep6+`: `192.168.200.{vtep_index-5}`
- Anycast gateway on each VTEP bridge: `192.168.0.250/16`
- Host underlay-facing IPs for `host1..host7`: `192.168.0.1/16 .. 192.168.0.7/16`
- Mobile VM IP pool: `192.168.100.1/16 .. 192.168.100.64/16` (default count)

## Test Phases

### Phase 1: Deploy mobile endpoints

- create `NUM_MOBILE_VMS` MACVLAN endpoints
- place endpoints round-robin across mobility-eligible hosts only
- pause every 5 VMs, then wait 3 seconds for settling

### Phase 2: Initial connectivity check (optional)

- if `ENABLE_CONTROLLER_PING_CHECKS` is `True`, controller endpoint pings every VM IP
- if disabled (default), phase logs that checks are skipped

### Phase 3: Batched live migration (repeatable rounds)

The migration sequence can be repeated multiple full rounds with `MIGRATION_REPEAT_COUNT`.

If `MIGRATION_REPEAT_COUNT=3`, the test migrates the full VM set three times (round 1, round 2, round 3), updating VM location state between rounds.

For each batch:

1. build migration plans
2. create destination endpoints for the whole batch
3. sleep overlap timer once (`MOBILITY_OVERLAP_SECONDS`)
4. delete source endpoints for the whole batch
5. update in-memory VM location map
6. optional inter-batch settle sleep (`MIGRATION_BATCH_SETTLE_SECONDS`)

Safety rollback behavior:

- if destination creation fails mid-batch and rollback is enabled, already-created destination endpoints in that batch are deleted before re-raising
- if source deletion appears incomplete, idempotent forced cleanup is attempted

### Phase 4: Post-migration connectivity check (optional)

- same controller-to-VM sweep behavior as phase 2

## Packet Captures and Teardown Summary

Captures started during the test:

- spine capture
  - node: `spine1`
  - file: `{logdir}/spine1/spine1_evpn_mobility.pcap`
  - filter: `tcp port 179`
- vtep capture
  - node: `vtep2`
  - file: `{logdir}/vtep2/vtep2_evpn_mobility.pcap`
  - filter: `tcp port 179`
- controller-VTEP capture
  - node: current controller VTEP (default `vtep1`)
  - file: `{logdir}/{controller_vtep}/{controller_vtep}_evpn_controller_mobility.pcap`
  - filter: `tcp port 179`

Teardown summary prints, per capture:

- capture file path
- packet count status from `get_pcap_packet_count()`:
  - numeric count
  - `missing` (if file was not created)

Then the test sleeps 5 seconds before continuing cleanup output.

## Configuration and Environment Knobs

### In-code constants

- `NUM_VTEPS = 7`
- `NUM_HOSTS = 7`
- `NUM_MOBILE_VMS = 64`
- `CONTROLLER_VTEPS = {"vtep1"}`
- `ENABLE_CONTROLLER_PING_CHECKS = False`

### Environment variables consumed by the test

- `MOBILITY_OVERLAP_SECONDS`
  - default: `0.2`
- `MIGRATION_BATCH_SIZE`
  - default: `1`
- `MIGRATION_BATCH_SETTLE_SECONDS`
  - default: `0.2`
- `MIGRATION_REPEAT_COUNT`
  - default: `1`
- `ENABLE_MIGRATION_BATCH_SAFETY_ROLLBACK`
  - default: `true`
- `MUNET_CLI`
  - set to `1` to drop into `munet>` CLI at test end

Note: `ENABLE_CONTROLLER_PING_CHECKS` is currently a code constant, not an environment variable.

## Run Example

```bash
PYTEST_XDIST_MODE=no \
python3 -m pytest -s test_evpn_capstone.py::test_mobility
```

## Operational Notes

- run in the intended FRR topotest/container environment
- capture files exist only if `tcpdump` starts successfully in each node namespace

## Debugging Checklist

1. `show evpn mac vni 1000`
2. `show bgp l2vpn evpn`
3. `bridge fdb show`
4. validate VNI/VXLAN state and BGP sessions
5. inspect capture files with Wireshark/tshark

## CLI Cookbook: Example Test Invocations

Run these from the FRR repo root.

- Baseline normal mobility test:

```bash
PYTEST_XDIST_MODE=no \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```

- Repeat the full migration sequence 3 times:

```bash
PYTEST_XDIST_MODE=no MIGRATION_REPEAT_COUNT=3 \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```

- Migrate endpoints in batches of 8:

```bash
PYTEST_XDIST_MODE=no MIGRATION_BATCH_SIZE=8 \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```

- Increase duplicate-MAC overlap window to 1.0s:

```bash
PYTEST_XDIST_MODE=no MOBILITY_OVERLAP_SECONDS=1.0 \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```

- Remove inter-batch settle delay:

```bash
PYTEST_XDIST_MODE=no MIGRATION_BATCH_SETTLE_SECONDS=0 \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```

- Disable batch safety rollback (debug behavior differences):

```bash
PYTEST_XDIST_MODE=no ENABLE_MIGRATION_BATCH_SAFETY_ROLLBACK=false \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```

- Stress-style run: 5 rounds, batch size 16, short settle timer:

```bash
PYTEST_XDIST_MODE=no MIGRATION_REPEAT_COUNT=5 MIGRATION_BATCH_SIZE=16 MIGRATION_BATCH_SETTLE_SECONDS=0.05 \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```

- Baseline asym mobility test:

```bash
PYTEST_XDIST_MODE=no \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone_asym/test_evpn_capstone_asym.py::test_mobility
```

- Asym test with repeated migrations (3 rounds):

```bash
PYTEST_XDIST_MODE=no MIGRATION_REPEAT_COUNT=3 \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone_asym/test_evpn_capstone_asym.py::test_mobility
```

- Enter `munet>` CLI after test teardown for live inspection:

```bash
PYTEST_XDIST_MODE=no MUNET_CLI=1 MIGRATION_REPEAT_COUNT=2 \
python3 -m pytest -s tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py::test_mobility
```
