-- ==========================================================================
-- VISTA-MCP job history schema  (docs/db_setup.md explains every column)
--
--   psql -h <host> -U <admin-user> -d usage_logs -f docs/db_setup.sql
--
-- Every statement is IF NOT EXISTS, so re-running is safe. This file is the
-- same DDL the server would create itself when MCP_DB_AUTO_CREATE=1; in
-- production run it by hand and set MCP_DB_AUTO_CREATE=0.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS mcp_jobs (
        job_id            text PRIMARY KEY,
        tool_name         text        NOT NULL,
        question          text        NOT NULL DEFAULT '',
        filename          text        NOT NULL DEFAULT '',
        file_bytes        bigint      NOT NULL DEFAULT 0,
        status            text        NOT NULL DEFAULT 'running',
        started_at        timestamptz NOT NULL DEFAULT now(),
        finished_at       timestamptz,
        duration_ms       integer,
        mandatory_ids     jsonb       NOT NULL DEFAULT '[]'::jsonb,
        optional_ids      jsonb       NOT NULL DEFAULT '[]'::jsonb,
        discovered_ids    jsonb       NOT NULL DEFAULT '[]'::jsonb,
        dropped_ids       jsonb       NOT NULL DEFAULT '[]'::jsonb,
        selected_ids      jsonb       NOT NULL DEFAULT '[]'::jsonb,
        used_ai           boolean     NOT NULL DEFAULT false,
        ai_mode           text        NOT NULL DEFAULT '',
        ai_reason         text        NOT NULL DEFAULT '',
        ai_system_prompt  text        NOT NULL DEFAULT '',
        ai_raw            text        NOT NULL DEFAULT '',
        ai_plan_source    text        NOT NULL DEFAULT '',
        ai_elapsed_ms     integer,
        orb_enabled       boolean     NOT NULL DEFAULT false,
        orb_status        text        NOT NULL DEFAULT '',
        orb_chars         integer     NOT NULL DEFAULT 0,
        orb_elapsed_ms    integer,
        report_chars      integer     NOT NULL DEFAULT 0,
        report_markdown   text        NOT NULL DEFAULT '',
        error             text,
        server_host       text        NOT NULL DEFAULT '',
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now()
    );

CREATE INDEX IF NOT EXISTS mcp_jobs_started_idx   ON mcp_jobs (started_at DESC);

CREATE INDEX IF NOT EXISTS mcp_jobs_tool_idx      ON mcp_jobs (tool_name, started_at DESC);

CREATE INDEX IF NOT EXISTS mcp_jobs_status_idx    ON mcp_jobs (status, started_at DESC);

CREATE INDEX IF NOT EXISTS mcp_jobs_selected_idx  ON mcp_jobs USING gin (selected_ids);

CREATE TABLE IF NOT EXISTS mcp_job_events (
        id          bigserial PRIMARY KEY,
        job_id      text        NOT NULL REFERENCES mcp_jobs(job_id) ON DELETE CASCADE,
        seq         integer     NOT NULL,
        step        text        NOT NULL,
        status      text        NOT NULL DEFAULT '',
        detail      text        NOT NULL DEFAULT '',
        at          timestamptz NOT NULL DEFAULT now(),
        elapsed_ms  integer,
        data        jsonb       NOT NULL DEFAULT '{}'::jsonb,
        UNIQUE (job_id, seq)
    );

CREATE INDEX IF NOT EXISTS mcp_job_events_job_idx ON mcp_job_events (job_id, seq);

CREATE TABLE IF NOT EXISTS mcp_job_analyzers (
        id                   bigserial PRIMARY KEY,
        job_id               text        NOT NULL REFERENCES mcp_jobs(job_id) ON DELETE CASCADE,
        analyzer_id          text        NOT NULL,
        title                text        NOT NULL DEFAULT '',
        api_url              text        NOT NULL DEFAULT '',
        mandatory            boolean     NOT NULL DEFAULT false,
        discovered           boolean     NOT NULL DEFAULT false,
        selected             boolean     NOT NULL DEFAULT false,
        called               boolean     NOT NULL DEFAULT false,
        ok                   boolean,
        http_status          integer,
        when_to_use          text        NOT NULL DEFAULT '',
        discovery            jsonb       NOT NULL DEFAULT '{}'::jsonb,
        request_method       text        NOT NULL DEFAULT '',
        request_url          text        NOT NULL DEFAULT '',
        request_content_type text        NOT NULL DEFAULT '',
        request_file_param   text        NOT NULL DEFAULT '',
        request_fields       jsonb       NOT NULL DEFAULT '{}'::jsonb,
        plan_source          text        NOT NULL DEFAULT '',
        plan_notes           jsonb       NOT NULL DEFAULT '[]'::jsonb,
        elapsed_ms           integer,
        report_chars         integer     NOT NULL DEFAULT 0,
        report_markdown      text        NOT NULL DEFAULT '',
        meta                 jsonb       NOT NULL DEFAULT '{}'::jsonb,
        error                text        NOT NULL DEFAULT '',
        created_at           timestamptz NOT NULL DEFAULT now(),
        UNIQUE (job_id, analyzer_id)
    );

CREATE INDEX IF NOT EXISTS mcp_job_analyzers_aid_idx ON mcp_job_analyzers (analyzer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mcp_config_audit (
        id          bigserial PRIMARY KEY,
        at          timestamptz NOT NULL DEFAULT now(),
        action      text        NOT NULL DEFAULT 'save',
        actor       text        NOT NULL DEFAULT '',
        tools       jsonb       NOT NULL DEFAULT '[]'::jsonb,
        config      jsonb       NOT NULL DEFAULT '{}'::jsonb,
        note        text        NOT NULL DEFAULT ''
    );
