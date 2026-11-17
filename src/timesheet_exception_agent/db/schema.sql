CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS employees (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  state         CHAR(2) NOT NULL,
  job_type      TEXT NOT NULL,
  pay_code      TEXT NOT NULL,
  manager_id    TEXT REFERENCES employees(id),
  hire_date     DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS coordinator_states (
  coordinator_id TEXT NOT NULL,
  state          CHAR(2) NOT NULL,
  PRIMARY KEY (coordinator_id, state)
);

CREATE TABLE IF NOT EXISTS pay_periods (
  id          SERIAL PRIMARY KEY,
  start_date  DATE NOT NULL,
  end_date    DATE NOT NULL,
  lock_at     TIMESTAMPTZ NOT NULL,
  process_on  DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS timesheet_history (
  employee_id    TEXT REFERENCES employees(id),
  pay_period_id  INT  REFERENCES pay_periods(id),
  hours          NUMERIC(5,2) NOT NULL,
  pay_code       TEXT NOT NULL,
  PRIMARY KEY (employee_id, pay_period_id)
);

CREATE TABLE IF NOT EXISTS exceptions (
  id             SERIAL PRIMARY KEY,
  pay_period_id  INT  NOT NULL REFERENCES pay_periods(id),
  employee_id    TEXT NOT NULL REFERENCES employees(id),
  type           TEXT NOT NULL CHECK (type IN ('not_submitted','not_approved','unusual_hours')),
  status         TEXT NOT NULL DEFAULT 'open',
  next_check_at  TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Kept outside CREATE TABLE so existing databases pick up new statuses on the next seed.
-- 'duplicate' is set by Intake (Module 5) for a second exception on the same employee and
-- period; it is never worked.
ALTER TABLE exceptions DROP CONSTRAINT IF EXISTS exceptions_status_check;
ALTER TABLE exceptions ADD CONSTRAINT exceptions_status_check
  CHECK (status IN ('open','contacted','escalated','resolved',
                    'correction_requested','needs_fallback','closed','duplicate'));

CREATE TABLE IF NOT EXISTS presence (
  person_id   TEXT PRIMARY KEY REFERENCES employees(id),
  status      TEXT NOT NULL CHECK (status IN ('available','away','out_of_office')),
  updated_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id            SERIAL PRIMARY KEY,
  exception_id  INT  NOT NULL REFERENCES exceptions(id),
  person_id     TEXT NOT NULL REFERENCES employees(id),
  channel       TEXT NOT NULL CHECK (channel IN ('email','teams')),
  direction     TEXT NOT NULL CHECK (direction IN ('sent','received')),
  body          TEXT NOT NULL,
  sim_time      TIMESTAMPTZ NOT NULL
);

-- Merged delivery (Module 5): one message covering several exceptions is stored as one row
-- per exception sharing a batch_id, so each exception's history and the per-person cap stay
-- per exception.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS batch_id INT;

-- Outbox (Module 5): in graph runs, send_message queues a draft here; at the end of the tick
-- drafts to the same person and channel may be merged, then delivered into messages.
CREATE TABLE IF NOT EXISTS outbox (
  id            SERIAL PRIMARY KEY,
  exception_id  INT  NOT NULL REFERENCES exceptions(id),
  person_id     TEXT NOT NULL REFERENCES employees(id),
  role          TEXT NOT NULL,
  channel       TEXT NOT NULL CHECK (channel IN ('email','teams')),
  body          TEXT NOT NULL,
  drafted_at    TIMESTAMPTZ NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered')),
  batch_id      INT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id            BIGSERIAL PRIMARY KEY,
  sim_time      TIMESTAMPTZ NOT NULL,
  actor         TEXT NOT NULL,
  action        TEXT NOT NULL,
  exception_id  INT REFERENCES exceptions(id),
  reason        TEXT,
  details       JSONB
);

CREATE TABLE IF NOT EXISTS manager_stats (
  manager_id        TEXT PRIMARY KEY REFERENCES employees(id),
  avg_reply_minutes NUMERIC,
  times_on_leave    INT NOT NULL DEFAULT 0,
  times_contacted   INT NOT NULL DEFAULT 0,
  updated_at        TIMESTAMPTZ NOT NULL
);

-- Estimator (Module 4): one fallback proposal per exception, written by code. The Estimator
-- only proposes; approved_by/approved_at stay empty until the coordinator approves through the
-- Supervisor's approval gate (Week 5). path: copy history; try a teammate first with
-- standard hours as the backup; or standard hours alone when there is no teammate on file.
-- A proposal is withdrawn if a manager decides after all.
CREATE TABLE IF NOT EXISTS estimates (
  id            SERIAL PRIMARY KEY,
  exception_id  INT  NOT NULL UNIQUE REFERENCES exceptions(id),
  path          TEXT NOT NULL CHECK (path IN ('history','teammate','standard_hours')),
  method        TEXT NOT NULL CHECK (method IN ('history','standard_hours')),
  hours         NUMERIC(5,2) NOT NULL CHECK (hours >= 0),
  pay_code      TEXT NOT NULL,
  confidence    NUMERIC(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  reason        TEXT NOT NULL,
  basis         JSONB NOT NULL,
  status        TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','approved','edited','rejected','withdrawn')),
  proposed_at   TIMESTAMPTZ NOT NULL,
  approved_by   TEXT,
  approved_at   TIMESTAMPTZ
);
-- Approval gate (Module 5): what the coordinator actually entered. The proposal's hours stay
-- as proposed, so the feedback loop can measure estimate error. approved_by holds the
-- coordinator's initials for approved, edited and rejected decisions alike.
ALTER TABLE estimates ADD COLUMN IF NOT EXISTS final_hours NUMERIC(5,2)
  CHECK (final_hours >= 0);
ALTER TABLE estimates ADD COLUMN IF NOT EXISTS final_pay_code TEXT;
-- Supervisor (Module 5): a concern about a proposal for the coordinator, at most one.
ALTER TABLE estimates ADD COLUMN IF NOT EXISTS flag TEXT;

-- Intake / triage (Module 5, code only): one row per exception on the period being worked.
-- risk is the lock-risk forecast (chance the exception reaches the lock with no manager
-- decision), from facts the agents may see (never sim_personas); factors records each term.
-- Rescored by the router every tick. duplicate_of points at the exception that is kept.
CREATE TABLE IF NOT EXISTS triage (
  exception_id  INT PRIMARY KEY REFERENCES exceptions(id),
  manager_id    TEXT REFERENCES employees(id),
  risk          NUMERIC(3,2) NOT NULL CHECK (risk BETWEEN 0 AND 1),
  factors       JSONB NOT NULL,
  duplicate_of  INT REFERENCES exceptions(id),
  updated_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_personas (
  manager_id  TEXT PRIMARY KEY REFERENCES employees(id),
  persona     TEXT NOT NULL CHECK (persona IN
              ('responsive','on_leave','slow','correction','silent','no_next_level'))
);

-- Retrieval (Module 3). The corpus comes from files (retrieval/policies/*.md and
-- retrieval/cases.py) and is loaded by retrieval/ingest.py, not by the seed, so reseeding the
-- simulation keeps it. `model` records which embedding model wrote each vector; search
-- refuses to compare a query against vectors from a different model.

CREATE TABLE IF NOT EXISTS policy_chunks (
  id         SERIAL PRIMARY KEY,
  doc        TEXT NOT NULL,            -- policy file name without .md
  title      TEXT NOT NULL,            -- the document's # heading
  section    TEXT NOT NULL,            -- the ## heading, with a part number if split
  body       TEXT NOT NULL,
  model      TEXT NOT NULL,
  embedding  vector(384) NOT NULL,
  tsv        tsvector GENERATED ALWAYS AS
             (to_tsvector('english', title || ' ' || section || ' ' || body)) STORED,
  UNIQUE (doc, section)
);
CREATE INDEX IF NOT EXISTS policy_chunks_embedding_idx
  ON policy_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS policy_chunks_tsv_idx ON policy_chunks USING gin (tsv);

-- Past resolved exceptions from earlier pay periods. Employee and manager ids are kept as
-- plain text, not foreign keys: case history outlives the people in it, and the seed's
-- TRUNCATE ... CASCADE on employees would otherwise wipe it.
CREATE TABLE IF NOT EXISTS case_memory (
  id              TEXT PRIMARY KEY,      -- e.g. 'P3-04'
  period_end      DATE NOT NULL,
  employee_id     TEXT NOT NULL,
  manager_id      TEXT,
  exception_type  TEXT NOT NULL CHECK (exception_type IN
                  ('not_submitted','not_approved','unusual_hours')),
  situation       TEXT NOT NULL,
  resolution      TEXT NOT NULL,
  outcome         TEXT NOT NULL CHECK (outcome IN
                  ('manager_approved','next_level_approved','correction',
                   'history_fallback','team_confirmed','standard_hours')),
  hours_entered   NUMERIC(5,2),
  retro           BOOLEAN NOT NULL DEFAULT false,
  model           TEXT NOT NULL,
  embedding       vector(384) NOT NULL,
  tsv             tsvector GENERATED ALWAYS AS
                  (to_tsvector('english', situation || ' ' || resolution)) STORED
);
CREATE INDEX IF NOT EXISTS case_memory_embedding_idx
  ON case_memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS case_memory_tsv_idx ON case_memory USING gin (tsv);
