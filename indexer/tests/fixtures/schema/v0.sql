-- The v0 schema exactly as Database._apply_initial_schema created it on
-- main before #928 (commit 80b3bc6d), with EMBEDDING_DIM = 4096
-- substituted. tests/test_database.py builds a v0 database from it to
-- test the v0 -> v1 migration against the real v0 shape. It is a
-- snapshot: do not edit it.
-- Thread-level coarse retrieval
            CREATE TABLE threads (
                thread_id       TEXT PRIMARY KEY,
                subject         TEXT NOT NULL,           -- normalized matching key (lowercased, prefix-stripped)
                participants    TEXT NOT NULL,           -- JSON array
                senders         TEXT NOT NULL DEFAULT '[]',  -- JSON array (From only)
                folder          TEXT NOT NULL,
                date_first      TEXT NOT NULL,
                date_last       TEXT NOT NULL,
                message_ids     TEXT NOT NULL,           -- JSON array of claimant IDs
                snippet         TEXT,
                has_attachments INTEGER DEFAULT 0,
                body_text       TEXT,
                fts_rowid       INTEGER,
                display_subject TEXT                     -- original-cased subject for retrieval; NULL until set, COALESCE'd back to subject by readers
            );

            -- mcp-server hybrid-search joins ``threads`` back from ``threads_fts``
            -- on ``threads_fts.rowid = threads.fts_rowid``. Without this index
            -- SQLite planned ``SCAN threads`` for every FTS hit, which turned
            -- search_emails O(N_fts × N_threads) and blew the 4-min MCP timeout
            -- on populated mailboxes.
            CREATE INDEX idx_threads_fts_rowid ON threads(fts_rowid);

            -- Threader subject-fallback lookup runs once per incoming
            -- message that fails In-Reply-To and References lookups —
            -- equality on subject + folder, DESC by date_last, projects
            -- thread_id (see find_threads_by_subject). The first three
            -- columns satisfy filter + sort in one B-tree walk;
            -- including thread_id as the fourth key column makes the
            -- index COVERING for the projection, so SQLite returns the
            -- thread_id straight from the index without a per-match
            -- table seek. A 50k-message initial scan does not serialize
            -- 50k full table scans through the ``_synchronized`` writer
            -- lock.
            CREATE INDEX idx_threads_subject_folder
                ON threads(subject, folder, date_last, thread_id);

            CREATE VIRTUAL TABLE threads_fts USING fts5(
                subject,
                participants,
                body,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            );

            CREATE VIRTUAL TABLE threads_vec USING vec0(
                thread_id TEXT PRIMARY KEY,
                embedding FLOAT[4096]
            );

            -- Per-message chunks (precision retrieval)
            CREATE TABLE message_chunks (
                chunk_id        TEXT PRIMARY KEY,
                claimant_id     TEXT NOT NULL,           -- the message's claimant ID (parser.claimant_id)
                thread_id       TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                text            TEXT NOT NULL,
                char_start      INTEGER NOT NULL,
                char_end        INTEGER NOT NULL,
                token_est       INTEGER NOT NULL,
                chunked_at      TEXT NOT NULL,
                fts_rowid       INTEGER,
                attachment_id   TEXT,
                -- What the chunk's text is (chunker.CHUNK_KINDS, #646);
                -- ``attachment`` exactly when attachment_id is set.
                kind            TEXT NOT NULL CHECK (kind IN (
                                    'body', 'quote', 'signature', 'forwarded',
                                    'calendar', 'attachment')),
                FOREIGN KEY (claimant_id) REFERENCES message_thread_map(claimant_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_message_chunks_claimant ON message_chunks(claimant_id);
            CREATE INDEX idx_message_chunks_thread ON message_chunks(thread_id);
            CREATE INDEX idx_message_chunks_attachment ON message_chunks(attachment_id);
            -- Hybrid-search chunk lane joins ``message_chunks`` back from
            -- ``message_chunks_fts`` on ``fts_rowid``. Critical for query
            -- latency on populated mailboxes.
            CREATE INDEX idx_message_chunks_fts_rowid ON message_chunks(fts_rowid);

            CREATE VIRTUAL TABLE message_chunks_fts USING fts5(
                text,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            );

            CREATE VIRTUAL TABLE message_chunks_vec USING vec0(
                chunk_id TEXT PRIMARY KEY,
                embedding FLOAT[4096]
            );

            -- Attachment indexing
            CREATE TABLE attachments (
                attachment_occurrence_id TEXT PRIMARY KEY,
                claimant_id               TEXT NOT NULL,
                attachment_id             TEXT NOT NULL,
                thread_id                 TEXT NOT NULL,
                filename                  TEXT NOT NULL,
                content_type              TEXT NOT NULL,
                size_bytes                INTEGER NOT NULL,
                seen_at                   TEXT NOT NULL,
                fts_rowid                 INTEGER,
                FOREIGN KEY (claimant_id) REFERENCES message_thread_map(claimant_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_attachments_attachment_id ON attachments(attachment_id);
            CREATE INDEX idx_attachments_thread ON attachments(thread_id);
            CREATE INDEX idx_attachments_claimant ON attachments(claimant_id);
            -- Hybrid-search attachment lane joins ``attachments`` back from
            -- ``attachments_fts`` on ``fts_rowid``.
            CREATE INDEX idx_attachments_fts_rowid ON attachments(fts_rowid);

            CREATE TABLE attachment_extractions (
                attachment_id      TEXT PRIMARY KEY,
                extraction_status  TEXT NOT NULL,
                extractor          TEXT,
                extracted_text     TEXT,
                extraction_error   TEXT,
                extracted_at       TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE attachments_fts USING fts5(
                filename,
                content_type,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            );

            -- Cross-cutting tables
            --
            -- Every per-message row is keyed by ``claimant_id``: the
            -- Message-ID plus a short hash of the file's bytes
            -- (``parser.claimant_id``). A Message-ID is sender-controlled,
            -- so two different files can claim one; both are kept, under
            -- distinct claimant IDs, and neither's rows can overwrite or
            -- delete the other's (#217). ``message_id`` stays alongside
            -- as the bare Message-ID: threading resolves In-Reply-To and
            -- References through it, and readers look a message up by it.
            CREATE TABLE message_thread_map (
                claimant_id TEXT PRIMARY KEY,
                message_id  TEXT NOT NULL,
                thread_id   TEXT NOT NULL,
                filepath    TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
            );
            CREATE INDEX idx_message_thread_map_message ON message_thread_map(message_id);
            -- Flag renames match rows by filepath; thread rebuilds and
            -- removals match them by thread_id.
            CREATE INDEX idx_message_thread_map_filepath ON message_thread_map(filepath);
            CREATE INDEX idx_message_thread_map_thread ON message_thread_map(thread_id);

            -- One row per indexed message: the authoritative per-message
            -- record (own headers, send and delivery time, folder, source
            -- locator and content hash) behind exact enumeration and
            -- provenance. Cascades from ``message_thread_map`` so every
            -- existing message / thread removal path cleans it up.
            -- ``sent_at`` is the parsed ``Date:`` header; ``occurred_at``
            -- the date of the topmost ``Received:`` header, NULL when
            -- absent or unparseable. ``effective_at`` is the message's
            -- effective time, which every date filter, thread span and
            -- time ordering uses (docs/architecture.md "Message time").
            CREATE TABLE messages (
                claimant_id     TEXT PRIMARY KEY,
                message_id      TEXT NOT NULL,
                thread_id       TEXT NOT NULL,
                filepath        TEXT NOT NULL,
                folder          TEXT NOT NULL,
                subject         TEXT NOT NULL,
                sent_at         TEXT NOT NULL,
                occurred_at     TEXT,
                effective_at    TEXT GENERATED ALWAYS AS (COALESCE(occurred_at, sent_at))
                                VIRTUAL,
                in_reply_to     TEXT,
                references_json TEXT NOT NULL,
                has_attachments INTEGER NOT NULL,
                size_bytes      INTEGER,
                content_hash    TEXT,
                indexed_at      TEXT NOT NULL,
                -- Maildir S / F / R flags of ``filepath`` (maildir.message_state),
                -- written with it on every insert and rename.
                seen            INTEGER NOT NULL DEFAULT 0,
                flagged         INTEGER NOT NULL DEFAULT 0,
                replied         INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (claimant_id) REFERENCES message_thread_map(claimant_id)
                    ON DELETE CASCADE
            );
            -- ``get_message`` lists the claimants of one Message-ID in
            -- claimant-ID order and oldest first; each index matches one
            -- order so ``LIMIT`` stops the walk instead of every file
            -- claiming the Message-ID being read and sorted (#538).
            CREATE INDEX idx_messages_message ON messages(message_id, claimant_id);
            CREATE INDEX idx_messages_message_effective
                ON messages(message_id, effective_at, claimant_id);
            CREATE INDEX idx_messages_thread_effective ON messages(thread_id, effective_at);
            CREATE INDEX idx_messages_folder_effective ON messages(folder, effective_at);
            CREATE INDEX idx_messages_effective ON messages(effective_at);
            -- Flag renames and cross-folder moves update by filepath.
            CREATE INDEX idx_messages_filepath ON messages(filepath);

            -- Normalized From / To / Cc. ``address`` is the canonical
            -- lowercased bare address, so "every message from/to X" is an
            -- indexed lookup rather than a scan of JSON participant lists.
            CREATE TABLE message_participants (
                claimant_id TEXT NOT NULL,
                role        TEXT NOT NULL CHECK (role IN ('from', 'to', 'cc')),
                address     TEXT NOT NULL,
                name        TEXT,
                PRIMARY KEY (claimant_id, role, address),
                FOREIGN KEY (claimant_id) REFERENCES messages(claimant_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX idx_message_participants_address
                ON message_participants(address, role);
            -- The reap's alias prune asks whether any surviving row
            -- still carries an (address, display name) pair (#464).
            CREATE INDEX idx_message_participants_address_name
                ON message_participants(address, name);

            CREATE TABLE indexed_files (
                filepath     TEXT PRIMARY KEY,
                indexed_at   TEXT NOT NULL,
                size         INTEGER,
                mtime_ns     INTEGER,
                content_hash TEXT
            );

            -- Tombstones for opt-in deletion reconciler. ``marked_at``
            -- is ISO 8601 UTC so the reaper's lexicographic cutoff
            -- comparison is well-defined.
            CREATE TABLE pending_deletions (
                filepath    TEXT PRIMARY KEY,
                claimant_id TEXT NOT NULL,
                thread_id   TEXT NOT NULL,
                marked_at   TEXT NOT NULL
            );
            CREATE INDEX idx_pending_deletions_thread
                ON pending_deletions(thread_id);

            -- One row per message the reconciler reaped, written in the
            -- reap transaction, so mcp-server can answer a lookup of a
            -- cited claimant ID or thread ID with "reaped"
            -- instead of "not found" (PLAN Phase 4 item 4). Identifiers
            -- and the reap time only, never content; pruned after
            -- ``REAPED_RECORD_RETENTION_DAYS`` (``prune_reaped_messages``).
            CREATE TABLE reaped_messages (
                claimant_id TEXT PRIMARY KEY,
                message_id  TEXT NOT NULL,
                thread_id   TEXT NOT NULL,
                reaped_at   TEXT NOT NULL
            );
            CREATE INDEX idx_reaped_messages_message ON reaped_messages(message_id, reaped_at);
            CREATE INDEX idx_reaped_messages_thread
                ON reaped_messages(thread_id, reaped_at, claimant_id);
            CREATE INDEX idx_reaped_messages_reaped_at ON reaped_messages(reaped_at);

            -- Durable retry / dead-letter queue. Every discovered file
            -- is enqueued first; the worker loop claims due rows and
            -- runs parse/thread/embed/upsert. Failures back off
            -- exponentially up to ``INDEXER_MAX_ATTEMPTS`` then
            -- transition to ``status='dead'``.
            CREATE TABLE indexing_jobs (
                filepath        TEXT PRIMARY KEY,
                reason          TEXT NOT NULL,
                status          TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                last_stage      TEXT,
                last_error_class TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL
            );
            CREATE INDEX idx_indexing_jobs_status_next
                ON indexing_jobs(status, next_attempt_at);

            -- One row: mbsync's last successful sync as the indexer last
            -- read it from the Maildir stamp, and when the indexer last
            -- reported. mcp-server's ``get_mailbox_status`` reads it.
            CREATE TABLE ingestion_state (
                id                 INTEGER PRIMARY KEY CHECK (id = 1),
                sync_completed_at  TEXT,
                sync_interval_secs INTEGER,
                indexer_seen_at    TEXT NOT NULL
            );

CREATE TABLE entities (
                entity_id       TEXT PRIMARY KEY,
                kind            TEXT NOT NULL CHECK (kind IN ('person', 'organization')),
                canonical_key   TEXT NOT NULL,
                organization_id TEXT REFERENCES entities(entity_id),
                authority_class TEXT NOT NULL DEFAULT 'unclassified',
                authority_rule  TEXT
            );
CREATE INDEX idx_entities_organization ON entities(organization_id);
CREATE INDEX idx_entities_authority ON entities(authority_class);
CREATE TABLE entity_aliases (
                entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
                alias     TEXT NOT NULL,
                PRIMARY KEY (entity_id, alias)
            );
CREATE TABLE vector_generations (
                generation_id      INTEGER PRIMARY KEY,
                provider           TEXT NOT NULL,
                endpoint           TEXT NOT NULL,
                model              TEXT NOT NULL,
                revision           TEXT,
                dimensions         INTEGER NOT NULL,
                tokenizer          TEXT,
                context_window     INTEGER,
                chunk_config_hash  TEXT,
                label              TEXT,
                calibration_sha256 TEXT NOT NULL,
                calibration_vector BLOB NOT NULL,
                created_at         TEXT NOT NULL,
                status             TEXT NOT NULL CHECK (status IN
                    ('building', 'caught-up', 'active', 'retained', 'retired'))
            );
CREATE UNIQUE INDEX idx_vector_generations_active ON vector_generations(status) WHERE status = 'active';
