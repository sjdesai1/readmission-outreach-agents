-- Outreach system of record: everything the agents read and write goes through these tables,
-- and only through the MCP servers. Safe to re-run (IF NOT EXISTS everywhere).
create schema if not exists outreach;

-- The simulation clock: "now" for every tool. The conversation loop advances it day by day.
create table if not exists outreach.sim_clock (
    id      int primary key default 1 check (id = 1),
    now     timestamp not null
);

create table if not exists outreach.providers (
    provider_id text primary key,
    name        text not null,
    role        text not null
);

-- Clinic calendar. Most slots are already taken by other patients ('held_other'), so availability is real.
create table if not exists outreach.slots (
    slot_id        text primary key,
    provider_id    text not null references outreach.providers,
    start_at       timestamp not null,
    end_at         timestamp not null,
    mode           text not null check (mode in ('in_person', 'telehealth')),
    status         text not null default 'open' check (status in ('open', 'booked', 'held_other')),
    patient_id     text,
    appointment_id text
);
create index if not exists slots_open_idx on outreach.slots (status, start_at);

-- One outreach case per flagged discharge.
create table if not exists outreach.cases (
    case_id        text primary key,
    encounter_id   text unique not null,
    patient_id     text not null,
    opened_at      timestamp not null,
    tier           text not null,
    risk           double precision not null,
    status         text not null default 'new',
    next_action_at timestamp,
    attempts       int not null default 0,
    closed_at      timestamp,
    close_reason   text,
    updated_at     timestamp
);
create index if not exists cases_due_idx on outreach.cases (status, next_action_at);

-- Audit log: every action on a case, in order.
create table if not exists outreach.events (
    event_id bigserial primary key,
    case_id  text not null,
    at       timestamp not null,
    type     text not null,
    detail   jsonb
);
create index if not exists events_case_idx on outreach.events (case_id, event_id);

-- Every message in or out. idempotency_key makes a retried send a no-op instead of a duplicate.
create table if not exists outreach.messages (
    seq             bigserial,                 -- strict order, even when two messages share a timestamp
    message_id      text primary key,
    idempotency_key text unique,
    case_id         text not null,
    patient_id      text not null,
    direction       text not null check (direction in ('outbound', 'inbound')),
    channel         text not null check (channel in ('portal', 'sms', 'email', 'phone')),
    subject         text,
    body            text not null,
    status          text not null,
    created_at      timestamp not null
);
create index if not exists messages_case_idx on outreach.messages (case_id, seq);

create table if not exists outreach.appointments (
    appointment_id  text primary key,
    idempotency_key text unique,
    case_id         text not null,
    patient_id      text not null,
    slot_id         text not null,
    provider_id     text not null,
    start_at        timestamp not null,
    mode            text not null,
    status          text not null check (status in ('booked', 'cancelled', 'completed', 'no_show')),
    video_link      text,
    created_at      timestamp not null,
    updated_at      timestamp not null
);

create table if not exists outreach.referrals (
    referral_id     text primary key,
    idempotency_key text unique,
    case_id         text not null,
    patient_id      text not null,
    type            text not null check (type in ('pharmacist', 'transport', 'social_work', 'interpreter')),
    reason          text not null,
    status          text not null default 'requested',
    created_at      timestamp not null
);

-- Milestone 3 step 3 additions. ALTER ... IF NOT EXISTS keeps this file safe to re-run on an existing database.
alter table outreach.providers add column if not exists languages text[] not null default '{English}';
alter table outreach.cases add column if not exists pcp_provider_id text;        -- the patient's own doctor
alter table outreach.cases add column if not exists follow_up_by timestamp;      -- target date for the visit
alter table outreach.messages add column if not exists deliver_at timestamp;     -- held for quiet hours
alter table outreach.cases add column if not exists identity_verified_at timestamp;  -- confirmed by year of birth
alter table outreach.cases add column if not exists identity_failures int not null default 0;

-- Milestone 3 step 5: nurse handoff and patient memory.
create table if not exists outreach.nurse_tasks (
    task_id         text primary key,
    idempotency_key text unique,
    case_id         text not null,
    patient_id      text not null,
    kind            text not null check (kind in ('emergency', 'symptom', 'clinical_question', 'callback', 'review')),
    priority        text not null check (priority in ('urgent', 'routine')),
    reason          text not null,
    summary         text,
    status          text not null default 'open' check (status in ('open', 'done')),
    due_at          timestamp not null,
    created_at      timestamp not null,
    resolved_at     timestamp,
    resolution      text
);
create index if not exists nurse_tasks_open_idx on outreach.nurse_tasks (status, priority, due_at);

-- Standing preferences a patient stated ("I work mornings"); patient-level, so they carry over to later stays.
create table if not exists outreach.patient_memory (
    patient_id text not null,
    key        text not null,
    value      text not null,
    source     text,
    updated_at timestamp not null,
    primary key (patient_id, key)
);

-- Milestone 4 step 3: why a booked patient needs a new visit, and since when (the cadence restarts from here).
alter table outreach.cases add column if not exists rebook_reason text;  -- no_show | clinic_cancelled
alter table outreach.cases add column if not exists rebook_at timestamp;

-- Milestone 4 step 4: contact details that turned out to be wrong, and barriers patients told us about.
create table if not exists outreach.bad_contacts (
    patient_id text not null,
    channel    text not null,
    reason     text not null,
    at         timestamp not null,
    primary key (patient_id, channel)
);
create table if not exists outreach.case_barriers (
    case_id text not null,
    barrier text not null,
    detail  text,
    source  text not null,
    at      timestamp not null,
    primary key (case_id, barrier)
);

-- Milestone 4 step 6: who wrote each message (ai, nurse, patient), so nurse and AI can share one thread.
alter table outreach.messages add column if not exists author text not null default 'ai';

-- Milestone 5: the options a patient was last offered, so "the first one" means the slot they actually saw.
create table if not exists outreach.offers (
    case_id    text not null,
    option     int not null,
    slot_id    text not null,
    offered_at timestamp not null,
    message_id text,
    primary key (case_id, option)
);
