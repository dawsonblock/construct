-- 004_graph_currency.sql — make the "current" edge set enforceable.
--
-- 003 declared UNIQUE(organization_id, source_entity_id, relation, target_entity_id,
-- valid_from). NULLs are distinct in a unique constraint, so an edge with no
-- valid_from could be inserted any number of times — and a structural edge
-- projected from a row has no valid_from, because it is true for as long as the
-- row says it is. Re-projecting a project would have multiplied its edges.
--
-- A partial unique index over the currently-valid edges fixes that and gives the
-- projection a real ON CONFLICT target.

CREATE UNIQUE INDEX relationships_current_unique
  ON relationships(organization_id, source_entity_id, relation, target_entity_id)
  WHERE valid_from IS NULL;

-- Reconstruction reads a project's whole edge set on every call; without this it
-- is a sequential scan filtered by project.
CREATE INDEX relationships_project_relation_idx
  ON relationships(organization_id, project_id, relation);

-- Entities are looked up by the row they stand for during projection.
CREATE INDEX entities_record_idx
  ON entities(organization_id, record_table, record_id);
