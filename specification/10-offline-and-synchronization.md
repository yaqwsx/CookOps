# Offline Operation and Synchronization

Status: Draft

## Offline-first requirement

CookOps MUST remain usable without network connectivity after the relevant
application shell and organization data have been cached on the device.

- A first-time Google sign-in requires connectivity.
- Data that the device has never loaded is not expected to be available offline.
- Previously loaded events, recipes, ingredients, and shopping lists MUST remain
  readable offline.
- Members MUST be able to perform ordinary edits against cached data while offline.
- Offline edits MUST be persisted locally across page reloads and application
  restarts.
- Receipt photos captured offline MUST be retained locally and queued for upload.
- The application MUST clearly indicate connectivity and synchronization state.

This requirement applies to the whole application, not only shopping lists.
Shopping remains the highest-priority offline workflow.

For the active organization, the client SHOULD automatically cache the recipe and
ingredient catalogs and all active events. Archived events are cached on demand
after they are opened. An organization that has never been opened on the device is
not available offline.

## Synchronization

- Local mutations are queued while connectivity is unavailable.
- Queued mutations are synchronized automatically and without confirmation after
  connectivity returns.
- The UI MUST distinguish at least synchronized, pending, and failed changes.
- Binary upload state MUST be included in synchronization status and pending-change
  counts.
- A transient synchronization failure MUST NOT discard a local change.
- Mutations MUST carry stable client-generated identities so independently created
  records can be merged without relying on a server round trip.
- Deletions and retirements MUST synchronize through durable tombstone or lifecycle
  operations rather than by erasing local records immediately.

Google authentication, invitations, membership changes, and other identity or
access-control administration require connectivity. Ordinary work with previously
cached application data remains available offline.

A previously verified organization member MAY work offline for at most seven days
after the last successful online authorization check. After that interval, the
application requires connectivity before organization data can be accessed or
edited. Membership removal takes effect immediately on connected clients and no
later than the next authorization check on an offline client.

## Collaboration metadata

Shopping fulfilment changes SHOULD retain the member who performed the latest
checkbox action and its effective timestamp. This metadata may be displayed as a
compact note such as "checked by Alex".

## Conflict handling

The system uses last-write-wins conflict resolution based on the wall-clock time of
the user action:

- every mutation records its client-side wall-clock timestamp, actor, client
  identity, and stable mutation identity;
- for concurrent writes to the same mutable field, the write with the later
  wall-clock timestamp wins;
- the same rule applies to fulfilment credit, quantities, units, notes, manual
  shopping targets, recipe-instance placement, and other scalar fields;
- equal timestamps are resolved through a stable deterministic tie-breaker, such as
  mutation identity;
- clients SHOULD detect and visibly warn about substantial device-clock skew when
  it can be compared with server time.

Independently created entities are merged because they have distinct stable
client-generated identities. Two independently created recipes, receipts, or
shopping-list items therefore survive synchronization.

Retirement or deletion is a special case: a synchronized tombstone remains in
effect when another client concurrently edits the retired record. The edited data
MUST remain recoverable so a member can restore the record without losing the
offline work.

Recipe and ingredient versions are immutable entities. Concurrently published
versions are both retained; last-write-wins applies only to a mutable pointer such
as the currently recommended version. A later version can use either retained
version as its parent or basis.

The same rules apply when synchronizing shopping-list edits made concurrently with
a manual list refresh. Refresh changes generated contribution fields and source
membership; checkbox actions change fulfilment-credit fields. These independent
fields merge without one erasing the other. Refresh MUST NOT erase ad-hoc records
because they have independent stable identities. An aggregate checkbox action
contains one timestamped credit mutation for each affected contribution so a later
individual checkbox action wins for that contribution.
