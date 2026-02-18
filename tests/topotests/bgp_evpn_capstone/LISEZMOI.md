# EVPN Capstone Test Suite

This document describes the current implementation of the EVPN mobility topotest in this directory.

The test validates BGP EVPN (L2VNI) behavior during repeated MAC mobility events. It creates a small Clos fabric, deploys MACVLAN endpoints, migrates them between VTEPs, and captures control/data-plane traffic for offline analysis.

## Current Scope

- 7 VTEPs (EVPN leaf nodes)
- 2 spine routers
- 7 hosts (one host connected to each VTEP)
- 128 mobile VM endpoints (MACVLAN interfaces)

Controller role:

- vtep1 is configured as controller/static in test logic
- Controller VTEPs still participate in topology and EVPN control-plane
- Controller VTEPs are excluded from VM placement and migration
- With current defaults, mobility runs on vtep2..vtep7 (6 mobility-eligible hosts)
- A static controller endpoint named controller is created on host1

Guardrails enforced by assertions:

- At least one mobility-eligible host must exist
- At least two mobility-eligible VTEPs must exist

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

Addressing summary:

- VTEP VXLAN local source IPs: {10*i}.{10*i}.{10*i}.{10*i} for VTEP i
- SVI IPs:
  - vtep1..vtep5: 192.168.0.251..192.168.0.255
  - vtep6+: 192.168.200.x
- Anycast gateway on all VTEPs: 192.168.0.250/16
- Host IPs (host1..host7): 192.168.0.1/16 .. 192.168.0.7/16
- Mobile VM IPs: 192.168.100.1/16 .. 192.168.100.128/16
- Controller endpoint:
  - Host: host1
  - Iface: controller
  - IP: 192.168.100.254/16
  - MAC: 00:aa:bb:dd:00:01

## Test Phases (Current Behavior)

### Phase 1: Deployment

- Deploy 128 VM endpoints (vm1..vm128)
- Round-robin placement across mobility-eligible hosts only
- Pause every 5 endpoints, then stabilization sleep

### Phase 2: Initial Reachability

- If controller ping checks are enabled, controller endpoint pings every VM IP
- Progress and failures are printed; phase asserts on any failure
- If disabled, phase logs that checks are skipped

### Phase 3: Batched Live Migration

Migration is now batched (default batch size 5):

1. Build migration plan for batch members
2. Create destination endpoints for the whole batch
3. Sleep overlap timer once per batch (duplicate-MAC window)
4. Delete source endpoints for the whole batch
5. Update in-memory VM location mapping
6. Optional settle sleep between batches

Safety mode (enabled by default):

- If destination creation fails partway through a batch, already-created destinations are rolled back before the error is re-raised
- Warning lines are printed when rollback or forced cleanup is triggered
- If source delete appears incomplete, forced idempotent cleanup is attempted and logged

### Phase 4: Post-Migration Reachability

- Same controller-to-VM sweep behavior as phase 2 (gated by the same toggle)

## Packet Captures and Reporting

The test starts two captures:

- spine1 capture:
  - File: {logdir}/spine1/evpn_mobility.pcap
  - Filter: tcp port 179
- controller VTEP capture (vtep1 by default):
  - File: {logdir}/vtep1/evpn_controller_mobility.pcap
  - Filter: tcp port 179 or udp port 4789 or arp

At teardown, the test prints:

- pcap file path
- pcap size (human-readable binary units)
- packet count

Packet-count auto-threshold:

- packet counting is skipped for files larger than configured threshold
- output format: skipped(>X MiB/GiB)

## Configurable Settings and Environment Variables

### In-code settings (edit test file)

- NUM_VTEPS = 7
- NUM_HOSTS = 7
- NUM_MOBILE_VMS = 128
- CONTROLLER_VTEPS = {"vtep1"}
- ENABLE_CONTROLLER_PING_CHECKS = False

### Environment variables

- MOBILITY_OVERLAP_SECONDS
  - Default: 0.3
  - Purpose: duplicate-MAC overlap duration per migration batch
- MIGRATION_BATCH_SIZE
  - Default: 5
  - Purpose: number of VMs moved together in phase 3
- MIGRATION_BATCH_SETTLE_SECONDS
  - Default: 0.0
  - Purpose: optional delay between migration batches
- ENABLE_MIGRATION_BATCH_SAFETY_ROLLBACK
  - Default: true
  - Purpose: rollback partial destination creates on batch failure
- PCAP_PACKET_COUNT_MAX_BYTES
  - Default: 1073741824 (1 GiB)
  - Purpose: max file size for packet counting; above this, count is skipped
- MUNET_CLI
  - Default: unset
  - Use 1 to drop into munet CLI at end of test

## Optional Quick-Run Examples (Hypothetical)

These are optional examples to illustrate how knobs can be combined. They are not required for normal runs.

- Baseline-like run (current defaults):

```bash
PYTEST_XDIST_MODE=no \
python3 -m pytest -s test_evpn_capstone.py::test_mobility
```

- Faster migration stress (larger batches, no settle delay):

```bash
PYTEST_XDIST_MODE=no \
MIGRATION_BATCH_SIZE=10 \
MIGRATION_BATCH_SETTLE_SECONDS=0 \
python3 -m pytest -s test_evpn_capstone.py::test_mobility
```

- Conservative timing (smaller batches, larger overlap):

```bash
PYTEST_XDIST_MODE=no \
MIGRATION_BATCH_SIZE=3 \
MOBILITY_OVERLAP_SECONDS=0.5 \
python3 -m pytest -s test_evpn_capstone.py::test_mobility
```

- Enable controller reachability sweeps during phase 2/4:

```bash
PYTEST_XDIST_MODE=no \
ENABLE_CONTROLLER_PING_CHECKS=true \
python3 -m pytest -s test_evpn_capstone.py::test_mobility
```

- Keep packet counting only for small captures (e.g., <= 512 MiB):

```bash
PYTEST_XDIST_MODE=no \
PCAP_PACKET_COUNT_MAX_BYTES=$((512*1024*1024)) \
python3 -m pytest -s test_evpn_capstone.py::test_mobility
```

- Disable batch safety rollback (not recommended except targeted debugging):

```bash
PYTEST_XDIST_MODE=no \
ENABLE_MIGRATION_BATCH_SAFETY_ROLLBACK=false \
python3 -m pytest -s test_evpn_capstone.py::test_mobility
```

## Change Log (Recent)

- Added controller/static VTEP role and static controller endpoint model
- Added optional controller-to-VM ping sweeps with progress output
- Added migration overlap timer configuration
- Added batched migration flow for faster execution
- Added batch safety rollback and warning logging for partial failures
- Added dual packet capture (spine and controller VTEP)
- Added teardown summary with pcap size and packet count
- Added packet-count auto-threshold with human-readable skip reason
- Added shell path quoting for capture/stat commands

## Operational Notes

- Full runtime execution should be done in the intended FRR topotest environment/container
- Capture output depends on tcpdump starting successfully inside each relevant node namespace

## Debugging Checklist

1. show evpn mac vni 1000
2. show bgp l2vpn evpn
3. bridge fdb show
4. Validate VNI/VXLAN state and BGP neighbor state
5. Inspect both pcap files with Wireshark/tshark
