alter table pull_requests add column if not exists body text not null default '';

create table intent_items (
  id text primary key,
  run_id text not null references analysis_runs(id),
  text text not null,
  category text not null,
  source text not null,
  confidence numeric not null,
  severity text not null,
  out_of_scope boolean not null default false,
  mapped_paths jsonb not null default '[]'::jsonb,
  evidence_status text not null default 'missing',
  suggested_test text
);

create table behavioral_deltas (
  id text primary key,
  run_id text not null references analysis_runs(id),
  path text not null,
  symbol text,
  old_behavior text,
  new_behavior text,
  divergent_input text,
  severity text not null,
  confidence numeric,
  category text,
  summary text
);

create table concept_findings (
  id text primary key,
  run_id text not null references analysis_runs(id),
  rule_id text,
  policy_pack_id text,
  policy_pack_name text,
  concept text not null,
  path text,
  symbol text,
  confidence numeric,
  relation text,
  policy_result text,
  severity text,
  owner text,
  message text,
  suggested_action text,
  evidence jsonb not null default '[]'::jsonb
);

create table contract_findings (
  id text primary key,
  run_id text not null references analysis_runs(id),
  path text not null,
  symbol text,
  old_contract text,
  new_contract text,
  violated_assumption text,
  generated_test_status text,
  severity text not null,
  confidence numeric,
  suggested_test jsonb
);

create table prompt_canary_runs (
  id text primary key,
  run_id text not null references analysis_runs(id),
  suite text not null,
  prompt_path text not null,
  model text,
  correctness numeric,
  format numeric,
  style numeric,
  refusal numeric,
  latency numeric,
  cost numeric,
  status text not null,
  drift_summary text,
  before_output text,
  after_output text,
  assertions jsonb not null default '{}'::jsonb
);

create table blast_radius (
  id text primary key,
  run_id text not null references analysis_runs(id),
  path text not null,
  symbol text,
  direct_callers jsonb not null default '[]'::jsonb,
  downstream_services jsonb not null default '[]'::jsonb,
  owners jsonb not null default '[]'::jsonb,
  impacted_tests jsonb not null default '[]'::jsonb,
  confidence numeric
);

create table reviewer_overrides (
  id text primary key,
  run_id text not null references analysis_runs(id),
  finding_id text not null,
  reviewer text not null,
  reason text not null,
  created_at timestamptz not null,
  later_outcome text
);

create table policy_packs (
  id text primary key,
  repo_id text references repositories(id),
  name text not null,
  yaml text not null,
  version integer not null,
  active boolean not null default false,
  created_by text not null,
  created_at timestamptz not null,
  updated_at timestamptz not null
);

create table post_merge_outcomes (
  id text primary key,
  pr_id text not null references pull_requests(id),
  outcome_type text not null,
  label text,
  notes text,
  created_at timestamptz not null
);
