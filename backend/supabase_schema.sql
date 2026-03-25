-- ==================== OZGER SUPABASE SCHEMA (Node backend compatible) ====================
-- Run this in Supabase SQL Editor to create required tables for `backend/server.js`
--
-- Tables used by the backend:
-- - profiles
-- - user_stats
-- - materials
-- - favorites (favorites of materials)

-- UUID helpers (Supabase usually already has this, but safe to include)
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
    best_ent_score integer default 0,
    total_tests_completed integer default 0,
    average_score numeric(6,2) default 0,
    last_test_date timestamptz,
    -- Frontend legacy fields (used in `frontend/script.js`)
    total_tests integer default 0,
    guess_streak integer default 0,
    guess_best_streak integer default 0,
    ent_best_score integer default 0,
    ent_tests_completed integer default 0,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_user_stats_user_id on public.user_stats(user_id);
create index if not exists idx_user_stats_best_ent_score on public.user_stats(best_ent_score desc);

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
    -- "stars" system: how many users saved the test (favorites)
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

-- Maintain tests.favorite_count automatically (stars system)
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
