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
  status         TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','contacted','escalated','resolved',
                                   'correction_requested','needs_fallback','closed')),
  next_check_at  TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS sim_personas (
  manager_id  TEXT PRIMARY KEY REFERENCES employees(id),
  persona     TEXT NOT NULL CHECK (persona IN
              ('responsive','on_leave','slow','correction','silent','no_next_level'))
);