"""rc4 Phase 1/17 — Tests for execution leases, reaper, and recovery daemon.

Tests the invariants:

    EXECUTING ∧ LeaseExpired ⇒ AutomaticRecovery (UNKNOWN)
    Worker A acquires → Worker B cannot acquire
    Worker A dies → lease expires → reaper runs → UNKNOWN
    CONFIRMED ∧ NoFinalAudit ⇒ RepairAudit
    Recovery daemon is idempotent

These tests use the real database (not mocks) to verify the lease SQL is correct.
"""
from __future__ import annotations

from uuid import uuid4




# -- Lease acquisition and concurrency --------------------------------------

def test_acquire_lease_succeeds_for_pending_action(repos, org_a):
    """A worker can acquire the lease on a PENDING action."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"lease-test-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    acquired = repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        lease_duration_seconds=60,
    )
    assert acquired is not None
    assert acquired.status == "executing"
    assert acquired.execution_owner == "worker-A"
    assert acquired.lease_acquired_at is not None
    assert acquired.lease_expires_at is not None
    assert acquired.heartbeat_at is not None


def test_worker_b_cannot_acquire_lease_held_by_worker_a(repos, org_a):
    """Worker B cannot acquire a lease already held by Worker A."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"concurrent-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    # Worker A acquires.
    acquired_a = repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
    )
    assert acquired_a is not None

    # Worker B tries to acquire from PENDING — should fail (status is now EXECUTING).
    acquired_b = repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-B",
        from_status="pending",
    )
    assert acquired_b is None  # Worker B cannot execute


def test_heartbeat_extends_lease(repos, org_a):
    """A heartbeat extends the lease expiry."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"heartbeat-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    acquired = repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        lease_duration_seconds=10,
    )
    original_expiry = acquired.lease_expires_at

    # Heartbeat with a longer duration.
    heartbeated = repos.external_actions.heartbeat(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        lease_duration_seconds=120,
    )
    assert heartbeated is not None
    assert heartbeated.lease_expires_at > original_expiry


def test_wrong_owner_cannot_heartbeat(repos, org_a):
    """Only the lease owner can heartbeat."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"wrong-owner-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
    )

    # Worker B tries to heartbeat — should fail.
    result = repos.external_actions.heartbeat(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-B",
    )
    assert result is None


def test_release_clears_lease(repos, org_a):
    """Releasing a lease clears the ownership and transitions the action."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"release-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
    )

    released = repos.external_actions.release(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        to_status="failed_retryable",
        last_error="worker released",
    )
    assert released is not None
    assert released.status == "failed_retryable"
    assert released.execution_owner is None
    assert released.lease_expires_at is None


# -- Reaper -----------------------------------------------------------------

def test_reaper_transitions_expired_executing_to_unknown(repos, org_a):
    """EXECUTING ∧ lease_expired ⇒ UNKNOWN (automatic, not manual)."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"reaper-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    # Acquire with a 1-second lease.
    acquired = repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        lease_duration_seconds=1,
    )
    assert acquired is not None

    # Wait for the lease to expire.
    import time
    time.sleep(2)

    # Run the reaper.
    reaped = repos.external_actions.reap_expired(scope=scope.organization_only)
    assert len(reaped) == 1
    assert str(reaped[0].action_id) == str(action.action_id)
    assert reaped[0].status == "unknown"
    assert reaped[0].remote_state == "remote_unknown"
    assert reaped[0].recovery_attempts >= 1


def test_reaper_does_not_touch_active_leases(repos, org_a):
    """EXECUTING ∧ lease_not_expired ⇒ no reaping."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"active-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        lease_duration_seconds=300,  # 5 minutes — won't expire
    )

    reaped = repos.external_actions.reap_expired(scope=scope.organization_only)
    assert len(reaped) == 0


def test_reaper_is_idempotent(repos, org_a):
    """Running the reaper twice on the same expired lease only reaps once."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"idempotent-reaper-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        lease_duration_seconds=1,
    )

    import time
    time.sleep(2)

    # First reap.
    reaped1 = repos.external_actions.reap_expired(scope=scope.organization_only)
    assert len(reaped1) == 1

    # Second reap — nothing to reap (already UNKNOWN).
    reaped2 = repos.external_actions.reap_expired(scope=scope.organization_only)
    assert len(reaped2) == 0


# -- Recovery daemon --------------------------------------------------------

def test_recovery_daemon_reaps_expired_leases(repos, org_a):
    """The recovery daemon reaps expired EXECUTING actions."""
    from construction_ai.executive.recovery_daemon import run_recovery_cycle

    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"daemon-reap-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    repos.external_actions.acquire(
        scope=scope.organization_only,
        action_id=action.action_id,
        owner="worker-A",
        lease_duration_seconds=1,
    )

    import time
    time.sleep(2)

    result = run_recovery_cycle(repos, scope=scope)
    assert len(result.reaped_leases) == 1
    assert str(action.action_id) in result.reaped_leases


def test_recovery_daemon_repairs_missing_final_audit(repos, org_a):
    """CONFIRMED ∧ NoFinalAudit ⇒ RepairAudit."""
    from construction_ai.executive.recovery_daemon import run_recovery_cycle

    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"audit-repair-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    # Manually transition to CONFIRMED without a final audit event.
    repos.external_actions.transition(
        scope=scope.organization_only,
        action_id=action.action_id,
        from_status="pending",
        to_status="confirmed",
        remote_document_id="PINV-TEST",
        remote_state="remote_submitted",
        result={"docname": "PINV-TEST", "docstatus": 1},
    )

    # Run the recovery daemon.
    result = run_recovery_cycle(repos, scope=scope)
    assert len(result.repaired_audits) == 1
    assert str(action.action_id) in result.repaired_audits

    # Verify the audit event was linked.
    updated = repos.external_actions.get(scope=scope.organization_only, action_id=action.action_id)
    assert updated.final_audit_event_id is not None


def test_recovery_daemon_is_idempotent(repos, org_a):
    """Running the recovery daemon twice doesn't double-repair."""
    from construction_ai.executive.recovery_daemon import run_recovery_cycle

    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"idempotent-daemon-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    repos.external_actions.transition(
        scope=scope.organization_only,
        action_id=action.action_id,
        from_status="pending",
        to_status="confirmed",
        remote_document_id="PINV-TEST",
        remote_state="remote_submitted",
        result={"docname": "PINV-TEST", "docstatus": 1},
    )

    # First run — should repair.
    result1 = run_recovery_cycle(repos, scope=scope)
    assert len(result1.repaired_audits) == 1

    # Second run — nothing to repair.
    result2 = run_recovery_cycle(repos, scope=scope)
    assert len(result2.repaired_audits) == 0
