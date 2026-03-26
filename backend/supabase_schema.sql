-- ==================== OZGER SUPABASE SCHEMA (Node backend compatible) ====================
-- Tables used by the backend:
-- - profiles
-- - user_stats
-- - materials
-- - favorites (favorites of materials)
create extension if not exists "pgcrypto";

-- ==================== PROFILES ====================
create table if not exists public.profiles (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade unique not null,
    username varchar(50) unique not null,
    email varchar(255),
    country varchar(10) default 'kz',
    city varchar(50),
    school varchar(200),
    class varchar(10),
    class_number smallint,
    class_letter varchar(5),
    subject_combination varchar(100),
    subject1 varchar(100),
    subject2 varchar(100),
    avatar_url text,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_profiles_user_id on public.profiles(user_id);
create index if not exists idx_profiles_username on public.profiles(username);
create index if not exists idx_profiles_classmates on public.profiles(city, school, class);
create index if not exists idx_profiles_classmates_parts on public.profiles(city, school, class_number);

alter table public.profiles enable row level security;

drop policy if exists "Public profiles are viewable by everyone" on public.profiles;
create policy "Public profiles are viewable by everyone"
  on public.profiles for select
  using (true);

drop policy if exists "Users can insert their own profile" on public.profiles;
create policy "Users can insert their own profile"
  on public.profiles for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own profile" on public.profiles;
create policy "Users can update their own profile"
  on public.profiles for update
  using (auth.uid() = user_id);

-- ==================== USER_STATS ====================
create table if not exists public.user_stats (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references public.profiles(user_id) on delete cascade unique not null,
    total_tests_completed integer default 0,
    average_score numeric(6,2) default 0,
    last_test_date timestamptz,
    -- Frontend legacy fields (used in `frontend/script.js`)
    total_tests integer default 0,
    guess_streak integer default 0,
    guess_best_streak integer default 0,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_user_stats_user_id on public.user_stats(user_id);
-- Cleanup legacy ENT score fields from older deployments.
drop index if exists idx_user_stats_best_ent_score;
alter table public.user_stats drop column if exists best_ent_score;
alter table public.user_stats drop column if exists ent_best_score;
alter table public.user_stats drop column if exists ent_tests_completed;

alter table public.user_stats enable row level security;

drop policy if exists "Users can view their own stats" on public.user_stats;
create policy "Users can view their own stats"
  on public.user_stats for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own stats" on public.user_stats;
create policy "Users can insert their own stats"
  on public.user_stats for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own stats" on public.user_stats;
create policy "Users can update their own stats"
  on public.user_stats for update
  using (auth.uid() = user_id);

-- ==================== MATERIALS ====================
create table if not exists public.materials (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade not null,
    title varchar(200) not null,
    content text not null,
    subject varchar(100),
    type varchar(50),
    is_public boolean default false,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_materials_user_id on public.materials(user_id);
create index if not exists idx_materials_created_at on public.materials(created_at desc);
create index if not exists idx_materials_is_public on public.materials(is_public);

alter table public.materials enable row level security;

drop policy if exists "Users can view their own materials" on public.materials;
create policy "Users can view their own materials"
  on public.materials for select
  using (auth.uid() = user_id or is_public = true);

drop policy if exists "Users can insert their own materials" on public.materials;
create policy "Users can insert their own materials"
  on public.materials for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own materials" on public.materials;
create policy "Users can update their own materials"
  on public.materials for update
  using (auth.uid() = user_id);

drop policy if exists "Users can delete their own materials" on public.materials;
create policy "Users can delete their own materials"
  on public.materials for delete
  using (auth.uid() = user_id);

-- ==================== FAVORITES (materials) ====================
create table if not exists public.favorites (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade not null,
    material_id uuid references public.materials(id) on delete cascade not null,
    created_at timestamptz default now(),
    unique(user_id, material_id)
);

create index if not exists idx_favorites_user_id on public.favorites(user_id);
create index if not exists idx_favorites_material_id on public.favorites(material_id);

alter table public.favorites enable row level security;

drop policy if exists "Users can view their own favorites" on public.favorites;
create policy "Users can view their own favorites"
  on public.favorites for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own favorites" on public.favorites;
create policy "Users can insert their own favorites"
  on public.favorites for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own favorites" on public.favorites;
create policy "Users can delete their own favorites"
  on public.favorites for delete
  using (auth.uid() = user_id);

-- ==================== TESTS (community / library) ====================
-- Used directly by `frontend/script.js`
create table if not exists public.tests (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade,
    title varchar(500) not null,
    subject varchar(100),
    content text,
    questions jsonb not null default '[]'::jsonb,
    is_public boolean default true,
    author varchar(200),
    favorite_count integer default 0,
    count integer default 0,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_tests_user_id on public.tests(user_id);
create index if not exists idx_tests_subject on public.tests(subject);
create index if not exists idx_tests_is_public on public.tests(is_public);
create index if not exists idx_tests_created_at on public.tests(created_at desc);
create index if not exists idx_tests_favorite_count on public.tests(favorite_count desc);

alter table public.tests enable row level security;

drop policy if exists "Public tests are viewable by everyone" on public.tests;
create policy "Public tests are viewable by everyone"
  on public.tests for select
  using (is_public = true or auth.uid() = user_id);

drop policy if exists "Authenticated users can insert tests" on public.tests;
create policy "Authenticated users can insert tests"
  on public.tests for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own tests" on public.tests;
create policy "Users can update their own tests"
  on public.tests for update
  using (auth.uid() = user_id);

drop policy if exists "Users can delete their own tests" on public.tests;
create policy "Users can delete their own tests"
  on public.tests for delete
  using (auth.uid() = user_id);

-- ==================== USER_FAVORITES (tests) ====================
create table if not exists public.user_favorites (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade,
    test_id uuid references public.tests(id) on delete cascade,
    created_at timestamptz default now(),
    unique(user_id, test_id)
);

create index if not exists idx_user_favorites_user_id on public.user_favorites(user_id);
create index if not exists idx_user_favorites_test_id on public.user_favorites(test_id);

alter table public.user_favorites enable row level security;

drop policy if exists "Users can view their own favorites (tests)" on public.user_favorites;
create policy "Users can view their own favorites (tests)"
  on public.user_favorites for select
  using (auth.uid() = user_id);

drop policy if exists "Users can add to favorites (tests)" on public.user_favorites;
create policy "Users can add to favorites (tests)"
  on public.user_favorites for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can remove from favorites (tests)" on public.user_favorites;
create policy "Users can remove from favorites (tests)"
  on public.user_favorites for delete
  using (auth.uid() = user_id);


create or replace function public.update_test_favorite_count()
returns trigger
language plpgsql
as $$
begin
  if tg_op = 'INSERT' then
    update public.tests set favorite_count = favorite_count + 1 where id = new.test_id;
    return new;
  elsif tg_op = 'DELETE' then
    update public.tests set favorite_count = greatest(favorite_count - 1, 0) where id = old.test_id;
    return old;
  end if;
  return null;
end;
$$;

drop trigger if exists on_test_favorite_change on public.user_favorites;
create trigger on_test_favorite_change
  after insert or delete on public.user_favorites
  for each row execute function public.update_test_favorite_count();

-- ==================== ASSISTANT SESSIONS ====================
create table if not exists public.assistant_sessions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade not null,
    title varchar(160) not null default 'New chat',
    last_message_preview text default '',
    last_intent varchar(60),
    last_route varchar(60),
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    last_message_at timestamptz default now()
);

create index if not exists idx_assistant_sessions_user_id on public.assistant_sessions(user_id);
create index if not exists idx_assistant_sessions_last_message_at on public.assistant_sessions(last_message_at desc);

alter table public.assistant_sessions enable row level security;

drop policy if exists "Users can view their own assistant sessions" on public.assistant_sessions;
create policy "Users can view their own assistant sessions"
  on public.assistant_sessions for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own assistant sessions" on public.assistant_sessions;
create policy "Users can insert their own assistant sessions"
  on public.assistant_sessions for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own assistant sessions" on public.assistant_sessions;
create policy "Users can update their own assistant sessions"
  on public.assistant_sessions for update
  using (auth.uid() = user_id);

drop policy if exists "Users can delete their own assistant sessions" on public.assistant_sessions;
create policy "Users can delete their own assistant sessions"
  on public.assistant_sessions for delete
  using (auth.uid() = user_id);

-- ==================== ASSISTANT MESSAGES ====================
create table if not exists public.assistant_messages (
    id uuid primary key default gen_random_uuid(),
    session_id uuid references public.assistant_sessions(id) on delete cascade not null,
    user_id uuid references auth.users(id) on delete cascade not null,
    role varchar(20) not null,
    title varchar(200),
    content text not null,
    intent varchar(60),
    actions jsonb not null default '[]'::jsonb,
    citations jsonb not null default '[]'::jsonb,
    created_at timestamptz default now()
);

create index if not exists idx_assistant_messages_session_id on public.assistant_messages(session_id);
create index if not exists idx_assistant_messages_user_id on public.assistant_messages(user_id);
create index if not exists idx_assistant_messages_created_at on public.assistant_messages(created_at asc);

alter table public.assistant_messages enable row level security;

drop policy if exists "Users can view their own assistant messages" on public.assistant_messages;
create policy "Users can view their own assistant messages"
  on public.assistant_messages for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own assistant messages" on public.assistant_messages;
create policy "Users can insert their own assistant messages"
  on public.assistant_messages for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own assistant messages" on public.assistant_messages;
create policy "Users can delete their own assistant messages"
  on public.assistant_messages for delete
  using (auth.uid() = user_id);

-- ==================== ASSISTANT EVENTS ====================
create table if not exists public.assistant_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade not null,
    session_id uuid references public.assistant_sessions(id) on delete set null,
    event_type varchar(60) not null,
    action varchar(80),
    route varchar(80),
    topic varchar(200),
    source_type varchar(80),
    source_id varchar(200),
    correct integer,
    total integer,
    percent integer,
    message text,
    created_at timestamptz default now()
);

create index if not exists idx_assistant_events_user_id on public.assistant_events(user_id);
create index if not exists idx_assistant_events_session_id on public.assistant_events(session_id);
create index if not exists idx_assistant_events_created_at on public.assistant_events(created_at desc);

-- Extend assistant events to capture richer telemetry and context.
alter table public.assistant_events add column if not exists event_name varchar(120);
alter table public.assistant_events add column if not exists category varchar(80);
alter table public.assistant_events add column if not exists page_context jsonb not null default '{}'::jsonb;
alter table public.assistant_events add column if not exists details jsonb not null default '{}'::jsonb;
alter table public.assistant_events add column if not exists client_ts timestamptz;
alter table public.assistant_events add column if not exists confidence numeric(4,3);
alter table public.assistant_events add column if not exists severity varchar(20);
alter table public.assistant_events add column if not exists metadata jsonb not null default '{}'::jsonb;
alter table public.assistant_events add column if not exists duration_ms integer;

create index if not exists idx_assistant_events_event_type on public.assistant_events(event_type);
create index if not exists idx_assistant_events_event_name on public.assistant_events(event_name);
create index if not exists idx_assistant_events_route on public.assistant_events(route);

alter table public.assistant_events enable row level security;

drop policy if exists "Users can view their own assistant events" on public.assistant_events;
create policy "Users can view their own assistant events"
  on public.assistant_events for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own assistant events" on public.assistant_events;
create policy "Users can insert their own assistant events"
  on public.assistant_events for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own assistant events" on public.assistant_events;
create policy "Users can delete their own assistant events"
  on public.assistant_events for delete
  using (auth.uid() = user_id);

-- ==================== ASSISTANT QUIZ ATTEMPTS ====================
create table if not exists public.assistant_quiz_attempts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade not null,
    session_id uuid references public.assistant_sessions(id) on delete set null,
    assistant_event_id uuid references public.assistant_events(id) on delete set null,
    mode varchar(20) not null default 'practice',
    route varchar(80),
    topic varchar(200),
    source_type varchar(80),
    source_id varchar(200),
    source_title varchar(200),
    correct integer not null default 0,
    total integer not null default 0,
    percent integer,
    language varchar(12),
    page_context jsonb not null default '{}'::jsonb,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz default now()
);

create index if not exists idx_assistant_quiz_attempts_user_id on public.assistant_quiz_attempts(user_id);
create index if not exists idx_assistant_quiz_attempts_session_id on public.assistant_quiz_attempts(session_id);
create index if not exists idx_assistant_quiz_attempts_created_at on public.assistant_quiz_attempts(created_at desc);
create index if not exists idx_assistant_quiz_attempts_mode on public.assistant_quiz_attempts(mode);

alter table public.assistant_quiz_attempts enable row level security;

drop policy if exists "Users can view their own assistant quiz attempts" on public.assistant_quiz_attempts;
create policy "Users can view their own assistant quiz attempts"
  on public.assistant_quiz_attempts for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own assistant quiz attempts" on public.assistant_quiz_attempts;
create policy "Users can insert their own assistant quiz attempts"
  on public.assistant_quiz_attempts for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own assistant quiz attempts" on public.assistant_quiz_attempts;
create policy "Users can delete their own assistant quiz attempts"
  on public.assistant_quiz_attempts for delete
  using (auth.uid() = user_id);

create table if not exists public.assistant_quiz_attempt_items (
    id uuid primary key default gen_random_uuid(),
    attempt_id uuid references public.assistant_quiz_attempts(id) on delete cascade not null,
    user_id uuid references auth.users(id) on delete cascade not null,
    question_index integer not null,
    question_id varchar(120),
    question_text text not null,
    selected_answer text,
    correct_answer text,
    is_correct boolean not null default false,
    explanation text,
    topic_hint varchar(200),
    source_question jsonb not null default '{}'::jsonb,
    created_at timestamptz default now(),
    unique(attempt_id, question_index)
);

create index if not exists idx_assistant_quiz_attempt_items_attempt_id on public.assistant_quiz_attempt_items(attempt_id);
create index if not exists idx_assistant_quiz_attempt_items_user_id on public.assistant_quiz_attempt_items(user_id);
create index if not exists idx_assistant_quiz_attempt_items_created_at on public.assistant_quiz_attempt_items(created_at desc);
create index if not exists idx_assistant_quiz_attempt_items_is_correct on public.assistant_quiz_attempt_items(is_correct);

alter table public.assistant_quiz_attempt_items enable row level security;

drop policy if exists "Users can view their own assistant quiz attempt items" on public.assistant_quiz_attempt_items;
create policy "Users can view their own assistant quiz attempt items"
  on public.assistant_quiz_attempt_items for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own assistant quiz attempt items" on public.assistant_quiz_attempt_items;
create policy "Users can insert their own assistant quiz attempt items"
  on public.assistant_quiz_attempt_items for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own assistant quiz attempt items" on public.assistant_quiz_attempt_items;
create policy "Users can delete their own assistant quiz attempt items"
  on public.assistant_quiz_attempt_items for delete
  using (auth.uid() = user_id);

-- ==================== ASSISTANT MEMORY EXTENSIONS ====================
-- Session lifecycle and quality fields.
alter table public.assistant_sessions add column if not exists status varchar(20) not null default 'active';
alter table public.assistant_sessions add column if not exists quality_score integer not null default 0;
alter table public.assistant_sessions add column if not exists summary text not null default '';
alter table public.assistant_sessions add column if not exists closed_at timestamptz;
alter table public.assistant_sessions add column if not exists abandoned_at timestamptz;
alter table public.assistant_sessions add column if not exists conversation_turns integer not null default 0;
alter table public.assistant_sessions add column if not exists fallback_count integer not null default 0;
alter table public.assistant_sessions add column if not exists last_error_code varchar(80);
alter table public.assistant_sessions add column if not exists last_model varchar(80);

create index if not exists idx_assistant_sessions_status on public.assistant_sessions(status);
create index if not exists idx_assistant_sessions_quality on public.assistant_sessions(quality_score desc);

-- Message-level observability fields.
alter table public.assistant_messages add column if not exists turn_id uuid default gen_random_uuid();
alter table public.assistant_messages add column if not exists latency_ms integer;
alter table public.assistant_messages add column if not exists model_used varchar(80);
alter table public.assistant_messages add column if not exists fallback_used boolean not null default false;
alter table public.assistant_messages add column if not exists error_code varchar(80);
alter table public.assistant_messages add column if not exists metadata jsonb not null default '{}'::jsonb;

create index if not exists idx_assistant_messages_turn_id on public.assistant_messages(turn_id);
create index if not exists idx_assistant_messages_model on public.assistant_messages(model_used);

-- Aggregated per-user assistant state.
create table if not exists public.assistant_user_state (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade unique not null,
    preferred_language varchar(12) default 'kk',
    preferred_difficulty varchar(20) default 'medium',
    response_style varchar(40) default 'concise',
    learning_goals jsonb not null default '[]'::jsonb,
    weak_topics jsonb not null default '[]'::jsonb,
    strong_topics jsonb not null default '[]'::jsonb,
    recent_routes jsonb not null default '[]'::jsonb,
    last_active_route varchar(80),
    total_events integer not null default 0,
    total_quizzes integer not null default 0,
    successful_quizzes integer not null default 0,
    average_quiz_percent numeric(6,2) not null default 0,
    last_seen_at timestamptz default now(),
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_assistant_user_state_user_id on public.assistant_user_state(user_id);
create index if not exists idx_assistant_user_state_last_seen on public.assistant_user_state(last_seen_at desc);

alter table public.assistant_user_state enable row level security;

drop policy if exists "Users can view their own assistant user state" on public.assistant_user_state;
create policy "Users can view their own assistant user state"
  on public.assistant_user_state for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own assistant user state" on public.assistant_user_state;
create policy "Users can insert their own assistant user state"
  on public.assistant_user_state for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own assistant user state" on public.assistant_user_state;
create policy "Users can update their own assistant user state"
  on public.assistant_user_state for update
  using (auth.uid() = user_id);

drop policy if exists "Users can delete their own assistant user state" on public.assistant_user_state;
create policy "Users can delete their own assistant user state"
  on public.assistant_user_state for delete
  using (auth.uid() = user_id);

-- Long-lived facts extracted from user behavior/conversation.
create table if not exists public.assistant_user_facts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade not null,
    fact_key varchar(160) not null,
    fact_value text not null,
    confidence numeric(4,3) not null default 0.5,
    source_event_id uuid references public.assistant_events(id) on delete set null,
    source_session_id uuid references public.assistant_sessions(id) on delete set null,
    active boolean not null default true,
    expires_at timestamptz,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    unique(user_id, fact_key)
);

create index if not exists idx_assistant_user_facts_user_id on public.assistant_user_facts(user_id);
create index if not exists idx_assistant_user_facts_active on public.assistant_user_facts(active);
create index if not exists idx_assistant_user_facts_confidence on public.assistant_user_facts(confidence desc);

alter table public.assistant_user_facts enable row level security;

drop policy if exists "Users can view their own assistant user facts" on public.assistant_user_facts;
create policy "Users can view their own assistant user facts"
  on public.assistant_user_facts for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their own assistant user facts" on public.assistant_user_facts;
create policy "Users can insert their own assistant user facts"
  on public.assistant_user_facts for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own assistant user facts" on public.assistant_user_facts;
create policy "Users can update their own assistant user facts"
  on public.assistant_user_facts for update
  using (auth.uid() = user_id);

drop policy if exists "Users can delete their own assistant user facts" on public.assistant_user_facts;
create policy "Users can delete their own assistant user facts"
  on public.assistant_user_facts for delete
  using (auth.uid() = user_id);

-- ==================== updated_at helper ====================
create or replace function public.update_updated_at_column()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists update_profiles_updated_at on public.profiles;
create trigger update_profiles_updated_at
  before update on public.profiles
  for each row execute function public.update_updated_at_column();

drop trigger if exists update_user_stats_updated_at on public.user_stats;
create trigger update_user_stats_updated_at
  before update on public.user_stats
  for each row execute function public.update_updated_at_column();

drop trigger if exists update_materials_updated_at on public.materials;
create trigger update_materials_updated_at
  before update on public.materials
  for each row execute function public.update_updated_at_column();

drop trigger if exists update_tests_updated_at on public.tests;
create trigger update_tests_updated_at
  before update on public.tests
  for each row execute function public.update_updated_at_column();

drop trigger if exists update_assistant_sessions_updated_at on public.assistant_sessions;
create trigger update_assistant_sessions_updated_at
  before update on public.assistant_sessions
  for each row execute function public.update_updated_at_column();

drop trigger if exists update_assistant_user_state_updated_at on public.assistant_user_state;
create trigger update_assistant_user_state_updated_at
  before update on public.assistant_user_state
  for each row execute function public.update_updated_at_column();

drop trigger if exists update_assistant_user_facts_updated_at on public.assistant_user_facts;
create trigger update_assistant_user_facts_updated_at
  before update on public.assistant_user_facts
  for each row execute function public.update_updated_at_column();

-- ==================== Auto-create defaults on signup ====================
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (
    user_id,
    username,
    email,
    country,
    city,
    school,
    class,
    class_number,
    class_letter,
    subject_combination,
    subject1,
    subject2
  )
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'username', split_part(new.email, '@', 1)),
    new.email,
    coalesce(new.raw_user_meta_data->>'country', 'kz'),
    coalesce(new.raw_user_meta_data->>'city', 'almaty'),
    nullif(new.raw_user_meta_data->>'school', ''),
    nullif(new.raw_user_meta_data->>'class', ''),
    nullif(substring(new.raw_user_meta_data->>'class' from '^(\d{1,2})'), '')::int,
    nullif(substring(new.raw_user_meta_data->>'class' from '^\d{1,2}(.+)$'), ''),
    nullif(coalesce(new.raw_user_meta_data->>'subject_combination', new.raw_user_meta_data->>'subjectCombination'), ''),
    nullif(new.raw_user_meta_data->>'subject1', ''),
    nullif(new.raw_user_meta_data->>'subject2', '')
  )
  on conflict (user_id) do nothing;

  insert into public.user_stats (user_id)
  values (new.id)
  on conflict (user_id) do nothing;

  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
