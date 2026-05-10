create table organizations (
  id text primary key,
  github_org_id text not null unique,
  name text not null,
  plan text not null default 'local',
  created_at timestamptz not null,
  updated_at timestamptz not null
);

create table repositories (
  id text primary key,
  org_id text not null references organizations(id),
  github_repo_id text not null unique,
  owner text not null,
  name text not null,
  default_branch text not null,
  installation_id text,
  enabled boolean not null default true,
  created_at timestamptz not null,
  updated_at timestamptz not null
);

create table pull_requests (
  id text primary key,
  repo_id text not null references repositories(id),
  number integer not null,
  title text not null,
  author text not null,
  base_sha text,
  head_sha text not null,
  state text not null,
  draft boolean not null default false,
  labels jsonb not null default '[]'::jsonb,
  github_url text,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  unique (repo_id, number)
);

create table analysis_runs (
  id text primary key,
  pr_id text not null references pull_requests(id),
  head_sha text not null,
  status text not null,
  started_at timestamptz not null,
  completed_at timestamptz,
  trigger text not null,
  duration_ms integer,
  summary jsonb,
  external_sync jsonb
);

create table changed_files (
  id text primary key,
  run_id text not null references analysis_runs(id),
  path text not null,
  status text not null,
  additions integer not null default 0,
  deletions integer not null default 0,
  changes integer not null default 0,
  language text,
  generated boolean not null default false,
  classification text not null,
  risk_score integer not null,
  risk_reasons jsonb not null default '[]'::jsonb,
  safe_to_skim boolean not null default false,
  must_inspect boolean not null default false
);

create table hotspots (
  id text primary key,
  run_id text not null references analysis_runs(id),
  path text not null,
  symbol text,
  risk_score integer not null,
  reason text not null,
  owner text,
  required_action text
);

create table evidence_links (
  id text primary key,
  run_id text not null references analysis_runs(id),
  finding_id text,
  type text not null,
  path text,
  test_name text,
  url text,
  confidence numeric,
  status text not null,
  severity text,
  message text,
  suggested_action text
);

create table check_results (
  id text primary key,
  run_id text not null references analysis_runs(id),
  check_name text not null,
  conclusion text not null,
  summary text,
  blocking boolean not null default false,
  details_url text
);

create table webhook_events (
  id text primary key,
  idempotency_key text not null unique,
  github_delivery_id text,
  event_name text not null,
  action text,
  repository_id text,
  pull_number integer,
  head_sha text,
  status text not null,
  run_id text references analysis_runs(id),
  error text,
  created_at timestamptz not null,
  updated_at timestamptz not null
);
