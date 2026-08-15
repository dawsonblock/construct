-- rc8: Ensure one confirmation has at most one direct successor.
-- This prevents concurrent supersession races where two workers both
-- create confirmations that supersede the same active confirmation.
-- The invariant is: OneConfirmation <= OneDirectSuccessor
-- unless a dedicated correction workflow explicitly allows branching.

-- The unique partial index ensures that no two confirmations can share
-- the same supersedes_confirmation_id. If a second worker tries to
-- insert a confirmation superseding an already-superseded confirmation,
-- the database rejects it with a unique violation.
CREATE UNIQUE INDEX IF NOT EXISTS work_confirmations_one_successor_uq
    ON work_confirmations (organization_id, supersedes_confirmation_id)
    WHERE supersedes_confirmation_id IS NOT NULL;
