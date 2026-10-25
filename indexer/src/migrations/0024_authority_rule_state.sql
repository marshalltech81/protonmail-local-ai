-- Applied rules metadata is unknown until the indexer reclassifies entities.
CREATE TABLE authority_rule_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    configured INTEGER NOT NULL CHECK (configured IN (0, 1)),
    rule_count INTEGER NOT NULL CHECK (rule_count >= 0),
    classes TEXT NOT NULL CHECK (json_valid(classes))
);
