-- Voice AGI Session 1 — public.tasks / public.agents / public.transcript_lines
-- Matches server/models.py Task, Agent, TranscriptLine (plus Task.location / Task.result).
-- Nested objects stay jsonb with camelCase keys (model_dump): location, result, business, facts, call.
-- Does NOT drop or alter public.guiders or public.requests.
-- RLS on, no anon/authenticated policies: PostgREST clients are denied; service_role bypasses RLS.

create table if not exists public.tasks (
  id text primary key default gen_random_uuid()::text,
  title text not null,
  request text not null,
  created_at timestamptz not null default now(),
  status text not null default 'planning'
    check (status in ('planning', 'running', 'complete', 'failed')),
  user_quote double precision,
  location jsonb,
  result jsonb,
  constraint tasks_location_shape check (
    location is null
    or (
      jsonb_typeof(location) = 'object'
      and (location ? 'lat')
      and (location ? 'lng')
    )
  ),
  constraint tasks_result_shape check (
    result is null
    or (
      jsonb_typeof(result) = 'object'
      and (result ? 'options')
      and (result ? 'recommendedOptionIndex')
      and (result ? 'recommendedAgentId')
      and (result ? 'why')
    )
  )
);

comment on table public.tasks is 'Voice AGI Task. result jsonb is Result when status=complete.';
comment on column public.tasks.user_quote is 'Task.userQuote';
comment on column public.tasks.location is 'Task.location { lat, lng, label? }';
comment on column public.tasks.result is 'Task.result { options, recommendedOptionIndex, recommendedAgentId, why, savingsVsQuote? }';

create table if not exists public.agents (
  id text primary key default gen_random_uuid()::text,
  task_id text not null references public.tasks (id) on delete cascade,
  kind text not null check (kind in ('call', 'web')),
  status text not null default 'queued'
    check (status in ('queued', 'active', 'done', 'failed')),
  business jsonb not null,
  summary text,
  facts jsonb,
  call jsonb,
  constraint agents_business_shape check (
    jsonb_typeof(business) = 'object'
    and (business ? 'name')
    and (business ? 'type')
    and (business ->> 'type') in ('mechanic', 'dealer', 'parts')
  ),
  constraint agents_call_shape check (
    call is null
    or (
      jsonb_typeof(call) = 'object'
      and (call ? 'durationS')
      and (call ? 'outcome')
      and (call ->> 'outcome') in ('quote', 'voicemail', 'refused', 'error')
    )
  )
);

comment on table public.agents is 'Voice AGI Agent. transcript lives in transcript_lines.';
comment on column public.agents.task_id is 'Agent.taskId → tasks.id';
comment on column public.agents.business is 'Agent.business { name, type, phone?, url? }';
comment on column public.agents.facts is 'Agent.facts Facts jsonb';
comment on column public.agents.call is 'Agent.call { durationS, answeredBy?, outcome }';

create table if not exists public.transcript_lines (
  id uuid primary key default gen_random_uuid(),
  task_id text not null references public.tasks (id) on delete cascade,
  agent_id text not null references public.agents (id) on delete cascade,
  role text not null check (role in ('agent', 'business')),
  text text not null,
  t double precision not null
);

comment on table public.transcript_lines is 'Agent.transcript[] rows: role, text, t (seconds).';
comment on column public.transcript_lines.t is 'TranscriptLine.t — seconds from call start';

create index if not exists tasks_status_idx on public.tasks (status);
create index if not exists tasks_created_at_idx on public.tasks (created_at desc);

create index if not exists agents_task_id_idx on public.agents (task_id);
create index if not exists agents_task_id_status_idx on public.agents (task_id, status);

create index if not exists transcript_lines_agent_id_t_idx on public.transcript_lines (agent_id, t);
create index if not exists transcript_lines_task_id_idx on public.transcript_lines (task_id);

alter table public.tasks enable row level security;
alter table public.agents enable row level security;
alter table public.transcript_lines enable row level security;

revoke all on table public.tasks from anon, authenticated;
revoke all on table public.agents from anon, authenticated;
revoke all on table public.transcript_lines from anon, authenticated;

grant all on table public.tasks to service_role;
grant all on table public.agents to service_role;
grant all on table public.transcript_lines to service_role;
