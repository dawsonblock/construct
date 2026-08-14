-- v0.5.0-rc3 Phase 7: Real multi-approver quorum.
--
-- The previous design treated dual approval as a second decide_approval() call
-- that overwrote the first. This is not a real quorum — it's a second decision
-- that replaces the first, with no record of the first vote.
--
-- This migration adds:
-- 1. approval_votes — one row per approver vote, append-only.
-- 2. approval_quorum_config — per-approval quorum requirements.
--
-- The approval only transitions to 'approved' when the quorum is met.
-- Until then, each vote is recorded but the approval stays 'pending'.

CREATE TABLE IF NOT EXISTS approval_votes (
    organization_id UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    vote_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_id UUID NOT NULL,
    actor_id UUID NOT NULL,
    FOREIGN KEY (organization_id, actor_id) REFERENCES users(organization_id, user_id) ON DELETE CASCADE,
    vote VARCHAR(20) NOT NULL CHECK (vote IN ('approve', 'reject', 'hold')),
    reason TEXT NOT NULL DEFAULT '',
    policy_version VARCHAR(100),
    actor_role_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(organization_id, approval_id, actor_id),  -- one vote per approver
    FOREIGN KEY (organization_id, approval_id) REFERENCES approvals(organization_id, approval_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_approval_votes_approval
    ON approval_votes(organization_id, approval_id);

-- Append-only: the app role can INSERT and SELECT but not UPDATE or DELETE.
GRANT SELECT, INSERT ON approval_votes TO construction_app;
REVOKE UPDATE, DELETE ON approval_votes FROM construction_app;

-- RLS: votes are tenant-isolated.
ALTER TABLE approval_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_votes FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS approval_votes_tenant_isolation ON approval_votes;
CREATE POLICY approval_votes_tenant_isolation ON approval_votes
    USING (organization_id = current_setting('app.organization_id')::uuid);

-- Add quorum_threshold to approvals (how many approve votes are needed).
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS quorum_threshold INT NOT NULL DEFAULT 1;

-- Add votes_count to approvals (denormalized for quick checks).
-- This is maintained by the approval service, not by triggers.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approve_votes_count INT NOT NULL DEFAULT 0;
