-- 005_evidence_subject.sql — say what a piece of evidence is *about*.
--
-- 003 gave evidence a field, a value and a source, but no subject. That is
-- under-specified: "total = 4760" is not a fact about a project, it is a fact
-- about one invoice. Without a subject, any check that compares evidence for the
-- same field has to compare across every record on the project, and two invoices
-- each carrying a `total` read as a contradiction.
--
-- The subject is deliberately a loose (type, id) pair rather than a foreign key:
-- evidence is written during extraction, before the subject row necessarily
-- exists, and a hard FK would force the pipeline to create records it has not
-- verified yet.

ALTER TABLE evidence
  ADD COLUMN subject_type text,
  ADD COLUMN subject_id uuid;

CREATE INDEX evidence_subject_idx
  ON evidence(organization_id, subject_type, subject_id, field);
