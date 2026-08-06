# Shopping

Status: Draft

## Creating a shopping list

- A member creates a shopping list ad hoc.
- A shopping list requires only a name; it has no formal draft, active, or completed
  lifecycle state.
- The member selects arbitrary scheduled recipe instances, including selections
  spanning multiple days and meal roles.
- The application does not prevent the same recipe instance from being included
  in multiple shopping lists.
- The initial list is a snapshot of the selected instances and their resolved
  ingredient requirements.
- The list is materialized when creation is confirmed, before the member enters
  supplies that are already available.
- Subsequent recipe or event changes do not automatically change the list.

## Grouping and information hierarchy

- Top-level rows aggregate requirements by ingredient.
- Rows MUST be grouped or sortable by store section. Section-based shopping is a
  core requirement, not an optional presentation mode.
- An ingredient row MUST show the effective, manually editable amount remaining to
  buy and its unit.
- A completed ingredient row MAY be hidden or shown through a user-controlled
  filter.
- Expanding an ingredient row MUST show every source contribution, including:
  - required amount;
  - recipe and scheduled meal context;
  - day;
  - the optional note from the recipe ingredient line;
  - relevant ingredient or recipe notes.
- Expanded details MUST also expose estimated unit price and expected cost.
- Expanded details MUST show the generated requirement separately from the
  effective manually editable purchase amount.

The application does not attempt to distinguish product varieties in the recipe
catalog solely for shopping purposes. For example, one generic tomato ingredient
may be fulfilled differently for burgers and breakfast after inspecting its source
contributions.

## Available supplies

- Available-supply amounts belong to one shopping list and are not shared as event
  inventory.
- Members can enter an amount already available for each aggregated ingredient.
- Available supplies reduce the amount remaining to buy.
- If available supplies meet or exceed the requirement, the remaining amount is
  zero; a negative purchase requirement is never displayed.
- Available supplies are applied only to the aggregate. The application does not
  allocate them to individual recipe contributions, and those contributions remain
  unfulfilled until explicitly checked.

## Planned purchase amount

- The generated requirement remains immutable snapshot information.
- Every aggregate ingredient row has an effective planned purchase amount initially
  derived from the generated requirement and available supplies.
- A member MAY manually replace that amount, for example changing a calculated
  `3.2 kg` requirement to a practical `4 kg` purchase target.
- The compact list displays the effective manual value.
- Expanding the row displays both the generated calculation and the effective
  purchase target.
- The application MUST retain whether the effective amount is automatically
  derived or manually overridden.

## Fulfilment

- Members can fulfil an entire ingredient row.
- Members can expand a row and fulfil only the contribution belonging to a
  particular scheduled recipe instance.
- Fulfilling a contribution reduces the aggregate amount remaining by that
  contribution's quantity.
- A contribution checkbox represents the whole calculated contribution; the MVP
  does not record an actual purchased quantity for that contribution.
- Fulfilling the aggregate row fulfils every generated contribution and reduces the
  aggregate remaining amount to zero.
- Reopening an individual contribution restores its amount to the aggregate amount
  remaining.
- The aggregate row reflects partial completion when some contributions are
  fulfilled and others are not.
- Fulfilment is operational rather than strict inventory accounting: members may
  select different product varieties or adjust purchasing according to store
  availability.

A conceptual calculation is:

`automatic target = max(0, generated requirement - available supplies)`

`effective target = manual target when present, otherwise automatic target`

`remaining = max(0, effective target - fulfilled generated contributions)`

Completing the aggregate row sets the operational remaining amount to zero.

Manual quantity edits may require an adjusted calculation and are covered by the
refresh conflict rules below.

## Real-time collaboration

- Multiple authenticated organization members MUST be able to open the same list
  on their phones.
- Fulfilment and edits MUST propagate to other open clients in real time.
- The UI MUST remain usable in a narrow mobile viewport.
- Reordering, filtering, expansion, and completion controls MUST not require a
  desktop table layout.

The list MUST implement the application-wide offline and synchronization behavior
defined in `10-offline-and-synchronization.md`.

The latest checkbox change SHOULD display the responsible member as a compact
attribution note. A formal shopping-list status is not required.

The shopping-list screen MUST contain a persistent synchronization status bar. It
MUST show at least:

- whether the device is online or offline;
- the number of local changes not yet acknowledged by the server;
- whether synchronization is currently running or has failed.

Synchronization starts automatically when connectivity is available. Normal
shopping interaction MUST remain available while changes are pending.

## Ad-hoc items

Members can add items directly to an existing shopping list. An ad-hoc item MUST
have:

- name;
- amount;
- unit;
- store section.

Ad-hoc items MAY also carry notes and price estimates. They belong only to the
shopping list and cannot be attached to a recipe instance in the MVP.

## Manual refresh

A member MAY explicitly refresh generated parts of a shopping list from its source
recipe instances.

Refresh MUST preserve:

- ad-hoc items;
- entered available-supply amounts;
- completed aggregate items;
- fulfilled recipe contributions;
- manually adjusted shopping quantities.

Generated contributions MUST have stable identities so preserved state can be
matched after refresh.

If source requirements changed while a generated quantity was manually adjusted,
the application MUST show a conflict instead of silently overwriting either value.
The final conflict-resolution interaction remains to be designed.

## Historical retention

Shopping lists and their full state, including fulfilment checkboxes, MUST remain
available after event archival.
