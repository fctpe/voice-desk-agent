# ADR-0001: Fake in-memory providers over real calendar/CRM integrations

## Status
Accepted.

## Context
The agent needs a calendar, a contacts book, and a way to send confirmations. A
"real" portfolio might wire Google Calendar OAuth, a CRM, and an email/SMS
sender. But real integrations are non-deterministic (live data changes),
require secrets and network access, and make the evals and unit tests flaky and
un-runnable in CI or on a reviewer's laptop.

## Decision
Ship narrow `Protocol` ports (`CalendarProvider`, `ContactsProvider`,
`ConfirmationSink`) with **deterministic, seeded, in-memory implementations**.
The `.ics` confirmation is written to a local outbox — no network. Latency and
failure injection are built in so resilience can be demonstrated on demand.

## Consequences
- The whole test suite and the text-mode evals run offline and reproducibly; a
  given seed always yields the same slots and contacts.
- Swapping in a production adapter is a class that satisfies the same Protocol —
  the tools and prompt don't change.
- The limitation is honest and explicit: the calendar is fake **by design**, not
  by omission. This is called out in the README's Limitations section.
