-- 023_decision_fingerprint.sql — decision-specific approval fingerprint (Phase 6).
--
-- The existing state_fingerprint (migration 018) is the project-wide
-- reconstruction fingerprint: it changes when ANY project state changes,
-- including unrelated invoices, which produces false stale positives.
--
-- Phase 5/6 wants the approval fingerprint to bind the EXACT state used for the
-- decision:
--
--     F = H(InvoiceSnapshot ∥ VerificationPacket ∥ EvidenceSet
--           ∥ PolicyVersion ∥ ApprovalRequirements)
--
-- This migration adds a decision_fingerprint column that stores that
-- decision-specific hash. The executor checks it before ERP execution: if the
-- current decision fingerprint differs from the approved one, the approval is
-- APPROVAL_STALE. The project-wide state_fingerprint is retained as a
-- complementary broad check.

ALTER TABLE approvals ADD COLUMN IF NOT EXISTS decision_fingerprint text;

CREATE INDEX IF NOT EXISTS approvals_decision_fingerprint_idx
  ON approvals(organization_id, decision_fingerprint)
  WHERE decision_fingerprint IS NOT NULL;
