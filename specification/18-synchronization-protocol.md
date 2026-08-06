# Synchronization Protocol

Status: Draft protocol contract

## Goals and scope

CookOps uses an organization-partitioned synchronization protocol between the PWA's
IndexedDB replica and the authoritative PostgreSQL application. The protocol MUST:

- make the connected and disconnected web write paths identical;
- preserve pending work across reload, reconnect, and bootstrap replacement;
- converge multiple browsers through the documented field-level LWW rules;
- carry immutable versions and snapshot records without converting them into
  mutable documents;
- expose transaction boundaries so clients never render half of an accepted
  application command;
- remain small enough to implement, inspect, and test without a general replication
  framework.

Synchronization transports JSON domain records and commands. Receipt image bytes,
OAuth state, browser sessions, and system administration are outside this protocol.

## Organization partitions and cursors

Every replicated organization has an independent opaque cursor. A cursor is:

- bound to one organization and sync schema version;
- meaningful only to the server;
- advanced only after the client durably applies every transaction group before
  it;
- never accepted as authorization or as evidence of membership.

Clients retain one cursor per cached organization. They prioritize the active
organization but MAY synchronize other cached organizations in the background.
Every bootstrap, pull, push, and WebSocket subscription re-evaluates current access.

## Canonical record envelopes

Pull and bootstrap return complete canonical entity records, not JSON Patch,
merge-patch, or field-delta documents. "Complete entity" means one relationally
meaningful sync record, not an entire aggregate graph: a recipe publication may
produce separate complete records for its recipe root, version, lines, and tag
associations in one transaction group.

A canonical record envelope contains at least:

- organization, entity type, and stable entity UUID;
- record schema version;
- complete current domain fields for that sync record;
- lifecycle/tombstone state where applicable;
- field-clock metadata for synchronizable mutable fields;
- immutable/version marker where applicable;
- change sequence and transaction identity in pull responses.

Immutable records are sent once when needed and may safely be upserted by identity.
Mutable records replace the client's prior canonical server record as a whole. The
client then reapplies any still-pending optimistic commands as a separate overlay.
Large binary values are never embedded; attachment records contain metadata and
protected retrieval identifiers only.

Unknown entity types or incompatible record schema versions MUST NOT be silently
discarded. The server advertises the sync schema version and the client fails into
an upgrade-required state when it cannot safely interpret authoritative records.

## Bootstrap

Bootstrap creates a consistent organization snapshot and a cursor from which later
pulls continue.

The normal active-organization bootstrap includes:

- organization configuration needed by ordinary members;
- the caller's effective organization capabilities;
- active and retired catalog roots, relevant configuration records, and immutable
  catalog versions required to render their histories and references;
- all active events and their days, roles, scheduled recipes, overrides, dietary
  exceptions, price snapshots, shopping lists, receipt metadata, and attachment
  metadata;
- tombstones required to prevent an optimistic or stale local record from being
  mistaken for active data.

Archived event payloads and receipt image bytes are not part of the ordinary
bootstrap. Archived events are fetched and cached on demand. OAuth, other users'
private identity data, token records, and system-administration state are never
included merely because an organization is bootstrapped.

The server MAY page a bootstrap. All pages share one snapshot identity and final
change cursor. A page never splits the records from one application-command
transaction when doing so would expose an invalid intermediate graph.

The browser builds paged bootstrap data in a staging IndexedDB area. Only after all
pages validate does one local transaction:

1. replace the prior canonical server layer for that organization;
2. retain the existing outbox, rejected-work area, and pending receipt bytes;
3. replay still-pending optimistic commands in their original local order without
   changing their identities or action timestamps; and
4. publish the new local cursor and visible projection.

A failed or interrupted bootstrap leaves the previous usable cache and outbox
intact. It never clears unsynchronized work as a recovery shortcut.

## Pull

`sync/pull` accepts an organization and opaque cursor and returns ordered,
transaction-grouped changes plus a next cursor and current server time.

- Each group contains every sync-visible canonical record changed by one committed
  application command.
- The client applies one group in one IndexedDB transaction.
- A response page never splits a transaction group. Bulk commands are bounded so a
  single group remains transportable.
- The client advances its durable cursor only through the last successfully
  committed group.
- Empty pulls may advance protocol metadata but do not invent domain changes.

Pull changes contain full canonical entity records. Retirement and restoration are
represented by the record's lifecycle state rather than physical deletion or a
bare delete instruction.

## Change-feed retention

The server retains organization change-feed data for at least 30 days. Cleanup is
based on server commit time, never client action time. Responses expose the oldest
available boundary needed for diagnostics without making sequence internals part
of the public cursor contract.

When a cursor predates retained history, pull returns `bootstrap_required` and no
partial tail. The browser runs the safe staging bootstrap procedure and then
replays its unchanged pending outbox. The seven-day offline authorization lease is
shorter than feed retention, leaving operational recovery margin without promising
unbounded incremental history.

## Push

`sync/push` carries:

- organization ID;
- client installation ID;
- current client request-send wall-clock time;
- sync schema version;
- an ordered list of typed application commands.

One request contains at most 100 commands and at most 1 MiB of decoded JSON request
data, whichever limit is reached first. Reverse-proxy and HTTP limits allow only the
small protocol overhead required around that application limit. Binary attachment
data uses the media endpoints.

The client automatically divides a larger outbox into ordered batches and never
splits one command. Per-command domain limits still apply, so a single oversized
command is rejected rather than bypassing the batch bound.

Commands are evaluated in request order, each in its own PostgreSQL transaction as
defined in `17-application-services-and-api.md`. The response preserves input order
and includes one structured outcome per command, current server time, an optional
clock warning, and a change-availability cursor hint.

An HTTP or infrastructure failure before a command outcome is known leaves that
command pending for an idempotent retry. Accepted, superseded, and deterministic
rejected outcomes reconcile or move the corresponding local command out of the
pending outbox. A rejected command's optimistic data remains in a recoverable-work
area when user action can repair or export it.

## Clock handling and LWW

LWW compares the original client wall-clock time of the user action and uses the
mutation UUID as the deterministic equal-time tie-breaker. Server receive time is
recorded for diagnostics and retention but does not silently replace an accepted
action time.

Clock-skew detection uses the push request's current send time, not the age of each
queued mutation. This prevents a legitimate day-old offline action from being
misidentified as a device clock that is one day slow.

- If request send time differs from server receive time by more than five minutes,
  the response contains a visible `clock_skew_warning` with the signed approximate
  difference and server time.
- The client retains and displays the warning in synchronization status until a
  later successful comparison falls within the threshold.
- Old action timestamps remain valid and may naturally lose against later field
  writes.
- A command whose action time is more than 24 hours ahead of server receive time is
  rejected as `client_time_too_far_ahead` before applying any domain fields.
- A rejected future-dated command remains recoverable. After correcting the device
  clock, the user may explicitly resubmit the intent as a new command with a new
  identity and current action time; CookOps never rewrites the original timestamp
  invisibly.

The server MUST use a reliable UTC clock. The warning threshold is operational
protection, not a claim that client clocks are authoritative or cryptographically
trusted.

## Push reconciliation

For each accepted or partially superseded command, the push outcome returns either
the complete affected canonical records or a compact reference to transaction
groups immediately available through pull. The client MUST reconcile before
removing the optimistic overlay permanently.

Field clocks returned by the server determine which local optimistic fields won.
A partially superseded command can therefore preserve winning fields while
replacing losing fields with canonical values. Guarded commands never return a
partially applied domain result.

Push responses do not replace pull as the recovery path. After processing outcomes,
the client pulls from its durable cursor until it reaches the latest advertised
change, including concurrent work by other browsers and MCP clients.

## WebSocket hints

The authenticated WebSocket carries only change-availability hints:

- organization ID;
- latest known opaque cursor or comparable pull hint;
- optional reason such as domain change or access change.

It carries no authoritative entity data and no mutation acknowledgement. Clients
coalesce hints and call pull. Missing, duplicating, or reordering hints cannot lose
data because the durable cursor remains authoritative. Reconnect always triggers a
pull for every subscribed cached organization still authorized.

## Local outbox ordering and dependencies

The outbox retains local creation order independently for each organization. The
sync worker sends commands in that order. A command may reference an entity created
by an earlier pending command because entity IDs already exist locally.

Since batch commands commit independently, rejection of the creator can make later
commands fail with a missing dependency. Those commands remain individually
explainable in rejected work; the client MUST NOT pretend the entire transport
batch was atomic or repeatedly retry a deterministic failure.

Commands for different organizations are never mixed into one push. Switching the
visible organization does not discard or rewrite pending work for another one.

## Sync status presentation

The application exposes at least:

- online/offline connectivity;
- number of pending command changes;
- number and state of pending binary uploads;
- whether synchronization is active, caught up, retrying, or blocked;
- last successful server contact;
- persistent clock-skew warning where applicable;
- recoverable rejected-work count.

The shopping-list screen additionally keeps its compact pending-change status bar
visible during collaborative shopping.

## Protocol tests

Automated tests MUST cover:

- canonical full-record upsert and tombstone handling;
- multi-page bootstrap consistency and atomic local publication;
- interrupted bootstrap retaining the prior cache, outbox, and pending media;
- outbox replay over a replacement bootstrap without changing mutation identities
  or action timestamps;
- transaction groups never split across pull pages and cursors never advance past
  an unapplied group;
- a cursor inside and outside the 30-day retained window;
- push boundaries at 100 commands and 1 MiB, automatic ordered splitting, and
  oversized single-command rejection;
- ordered per-command transactions with accepted, superseded, rejected, and
  transient outcomes in one transport batch;
- idempotent retry after an unknown HTTP outcome;
- five-minute warning boundaries using request send time;
- acceptance of old offline actions and rejection/recovery of actions more than 24
  hours in the future;
- missed and duplicate WebSocket hints converging through pull;
- concurrent browser and MCP changes converging through canonical records and field
  clocks;
- independent cursors and outboxes for a user switching between organizations.
