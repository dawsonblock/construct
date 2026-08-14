-- 006_matching.sql — what the first candidate producers need.
--
-- 1. SAME_AS. Identity resolution is not the same relation as invoice→PO
--    matching, and collapsing them into MATCHES would make "this invoice is
--    that PO" and "this vendor is that vendor" indistinguishable to every
--    consumer downstream.
--
--    Note what this migration does NOT add: any notion of a merged record.
--    An approved SAME_AS asserts that two entities refer to the same thing. It
--    does not authorize consolidating the rows behind them. Identity resolution
--    and record consolidation stay separate operations.
--
-- 2. Dates. The invoice→PO matcher's date-window signal needs a document date
--    on each side. Without them the signal is permanently unavailable, which is
--    honest but useless.

ALTER TABLE relationships DROP CONSTRAINT relationships_relation_check;
ALTER TABLE relationships ADD CONSTRAINT relationships_relation_check
  CHECK (relation IN ('BELONGS_TO','REFERENCES','SUPERSEDES','SUPPORTS','CONTRADICTS',
                      'BILLED_BY','ORDERED_FROM','MATCHES','SAME_AS','APPROVES','REQUIRES',
                      'DERIVED_FROM','AFFECTS'));

ALTER TABLE invoices ADD COLUMN invoice_date date;
ALTER TABLE purchase_orders ADD COLUMN ordered_on date;

-- A candidate producer records which version of itself made a proposal, so a
-- rule change is visible in the data rather than only in the git history.
ALTER TABLE relationships ADD COLUMN producer text;
CREATE INDEX relationships_producer_idx ON relationships(organization_id, producer, status);
