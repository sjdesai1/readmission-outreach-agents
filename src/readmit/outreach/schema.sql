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
