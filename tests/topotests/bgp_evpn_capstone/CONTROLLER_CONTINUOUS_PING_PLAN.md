# Controller Continuous Ping Plan

## Goal

Add an optional "ping while moving" mode where the static `controller` endpoint on `host1` probes mobility VM IPs during Phase 3 migrations, then reports summarized disruption metrics.

## Why

Current checks are snapshots (pre- and post-migration). They validate end-state reachability but do not show transient loss during live moves.

## Design Principles

- Keep existing test behavior as default.
- Make richer probing fully optional via toggles.
- Prefer summarized metrics over per-ping spam.
- Fail only on explicit thresholds, not raw transient events by default.

## Proposed Toggles

Add these constants in `test_evpn_capstone.py`:

- `ENABLE_CONTROLLER_CONTINUOUS_PROBES = False`
- `CONTROLLER_PROBE_INTERVAL_SEC = 0.5`
- `CONTROLLER_PROBE_SAMPLE_SIZE = 16`  
  (`16` by default; can be increased up to all VMs)
- `CONTROLLER_PROBE_TIMEOUT_SEC = 1`
- `CONTROLLER_PROBE_FAIL_THRESHOLD = None`  
  (`None` means informational only; integer enables fail criteria)

## Implementation Plan

### Phase 1: Lightweight in-loop checkpoints (low risk)

1. During migration loop, every `N` moves (e.g., 10), run a short probe batch from `controller`.
2. Probe selected VM IPs with existing `verify_ping` helper (`-W` bounded timeout).
3. Record timestamp, checked count, failures.
4. Print compact progress lines and one final summary.

**Pros**

- Minimal complexity and easy debugging.
- No background process lifecycle management.

**Cons**

- Not truly continuous; can miss short transient events between checkpoints.

### Phase 2: True concurrent probing (richer mode)

1. Start a background probe loop on `host1` before migration starts.
2. Probe sample targets repeatedly at configured interval.
3. Write machine-parseable logs (CSV/JSON lines) to test logdir.
4. Stop background loop in `finally` block and parse summary metrics.

**Pros**

- Better visibility into transient convergence behavior.
- Enables rough outage-window analysis.

**Cons**

- Higher complexity and flakiness risk.
- More load and potential timing perturbation.
- Requires robust start/stop/cleanup and parsing logic.

## Recommended Initial Rollout

Start with Phase 1 only, behind toggles. Promote to Phase 2 after proving stability and log usefulness.

## Metrics to Report

- Total probes attempted
- Total probe failures
- Failure rate (%)
- First and last failure timestamps
- Max consecutive failures per target (if tracked)

## Suggested Pass/Fail Policy

- Default: informational (no fail on transient loss)
- Optional strict mode: fail if failures exceed `CONTROLLER_PROBE_FAIL_THRESHOLD`

## Risks and Mitigations

- **Risk:** Test runtime increases significantly.  
  **Mitigation:** sample subset + bounded timeout + interval control.
- **Risk:** Log noise.  
  **Mitigation:** summary-first output; detailed logs to file only.
- **Risk:** False negatives from probe load.  
  **Mitigation:** conservative defaults and easy disable toggle.

## Open Questions

- Should strict thresholds be enabled in CI, or local-only?
- Is sample-based probing sufficient, or do we need all 128 VMs in continuous mode?
- Preferred summary format in logs (plain text vs JSON/CSV)?

## Minimal Next Step

Implement Phase 1 checkpoint probing with toggles and summary metrics, leaving true concurrent background mode for a follow-up change.
