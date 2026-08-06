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

- Creation and every explicit refresh produce an immutable generated revision. The
  current generated requirement changes only by selecting a newer revision through
  an explicit refresh; recipe and event edits never mutate it in place.
- Every aggregate ingredient row has an effective planned purchase amount initially
  derived from the generated requirement and available supplies.
- A member MAY manually replace that amount, for example changing a calculated
  `3.2 kg` requirement to a practical `4 kg` purchase target.
- The compact list displays the effective manual value.
- Expanding the row displays both the generated calculation and the effective
  purchase target.
- The application MUST retain whether the effective amount is automatically
  derived or manually overridden.

All persisted amounts use the ingredient's canonical unit. A compatible display or
input unit MAY be selected without changing the calculation semantics.

For an aggregate ingredient row, define:

- `G`: the sum of current generated quantities of all active contributions;
- `A`: the available-supply quantity;
- `T_auto = max(0, G - A)`: the automatically planned purchase target;
- `T = manual_target` when an override is present, otherwise `T_auto`;
- `C`: the total fulfilment credit described below;
- `R = max(0, T - C)`: the amount remaining to buy.

`manual_target` is the final planned amount to purchase after considering supplies,
not a replacement for `G`. Changing `A` while a manual target exists updates
`T_auto` for comparison but does not silently modify or clear the manual target.

Expected shopping cost is based on `T`, not `R`, so completing a purchase does not
make its expected cost disappear.

## Fulfilment

- Members can fulfil an entire ingredient row.
- Members can expand a row and fulfil only the contribution belonging to a
  particular scheduled recipe instance.
- Fulfilling a contribution reduces the aggregate amount remaining by that
  contribution's quantity.
- A contribution checkbox represents the whole calculated contribution; the MVP
  does not ask the user to enter an actual purchased quantity for that contribution.
- Fulfilling the aggregate row fulfils every generated contribution and reduces the
  aggregate remaining amount to zero.
- Reopening an individual contribution clears its stored credit and recalculates
  the aggregate amount remaining.
- The aggregate row reflects partial completion when some contributions are
  fulfilled and others are not.
- Fulfilment is operational rather than strict inventory accounting: members may
  select different product varieties or adjust purchasing according to store
  availability.

### Fulfilment credit

Each contribution stores a non-editable `fulfilment_credit` in the canonical unit.
This value is operational checkbox state, not a claim about a measured quantity
placed in the cart.

- Checking an unfulfilled or partial contribution sets its credit to that
  contribution's current generated quantity.
- Unchecking a contribution sets its credit to zero.
- Refreshing a contribution does not automatically change its existing credit.
- A contribution is unchecked when its credit is zero, partial when its positive
  credit is less than the refreshed generated quantity, and checked when its credit
  meets or exceeds the refreshed generated quantity.
- Credit remains associated with its aggregate ingredient even when a refresh
  retires the source contribution. This reflects that an item already obtained does
  not disappear when the menu changes.

The total aggregate credit `C` is the sum of all active and retired contribution
credits plus an optional aggregate-level credit. The aggregate-level credit covers
the part of a manual target that does not correspond to generated contributions.

Checking the aggregate row is one atomic user action that:

1. sets every active contribution's credit to its current generated quantity;
2. retains credit belonging to retired contributions;
3. sets aggregate-level credit to the additional amount, if any, required to make
   `R` zero.

The aggregate action has one operation identity and user-action timestamp. Its
per-contribution and aggregate-credit writes are persisted in one local transaction
and submitted as one logical synchronization command. A newer concurrent write to
an individual contribution may still win for that field under the global LWW rule.

Unchecking the aggregate row sets every active and retired contribution credit and
the aggregate-level credit to zero. It intentionally does not restore an older
partially fulfilled state.

An individual contribution change after an aggregate action updates that
contribution normally. For example, unchecking one contribution reopens the
corresponding amount while leaving the other contributions fulfilled.

The remaining calculation is normative:

`T_auto = max(0, G - A)`

`T = manual_target if present, otherwise T_auto`

`C = contribution credits + aggregate-level credit`

`R = max(0, T - C)`

For example, suppose burgers contribute `6 kg` of tomatoes, breakfast contributes
`4 kg`, and `2 kg` are already available. Then `G = 10 kg` and `T_auto = 8 kg`.
Checking the burger contribution creates `6 kg` of credit and leaves `R = 2 kg`.
If a refresh raises the burger contribution to `8 kg`, its credit remains `6 kg`,
`G` becomes `12 kg`, and `R` becomes `4 kg`. The burger contribution is shown as
partial until it is checked again against the refreshed quantity.

### Derived row presentation

The aggregate row does not persist a separate open/partial/complete enum. Its
presentation is derived:

- `not_required`: `T` is zero, whether because supplies cover the requirement or a
  member explicitly set the manual target to zero;
- `open`: `T` is positive, `R` is positive, and `C` is zero;
- `partial`: `R` and `C` are both positive;
- `complete`: `T` is positive and `R` is zero.

The main checkbox is empty for `open`, indeterminate for `partial`, and checked for
`complete`. `not_required` uses a resolved "nothing to buy" presentation that is
visually distinguishable from a purchase checkbox. Both `complete` and
`not_required` rows are hidden by the "hide completed" filter.

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

An ad-hoc item uses its entered amount as `T` and has a non-editable fulfilment
credit controlled by its checkbox. Checking sets the credit to the current target;
unchecking sets it to zero. If the amount is subsequently increased, the item
becomes partial; if it is decreased below existing credit, it remains complete.

## Manual refresh

A member MAY explicitly refresh generated parts of a shopping list. The refresh
dialog begins with the list's current source scheduled-recipe instances selected
and allows members to add or remove source instances before confirming.

Refresh MUST preserve:

- ad-hoc items;
- entered available-supply amounts;
- completed aggregate items;
- fulfilled recipe contributions;
- manually adjusted shopping quantities.

Generated contributions MUST have stable identities so preserved state can be
matched after refresh. Within a shopping list, the stable source key is the pair of
scheduled-recipe-instance identity and logical ingredient-catalog identity.
Multiple resolved lines for the same ingredient in one scheduled instance are
combined into one contribution while retaining their individual notes for detail
display.

Refresh applies these transitions atomically:

- a matched contribution receives its new generated quantity, source details, and
  notes while retaining its stable identity and fulfilment credit;
- a newly generated contribution starts with zero credit;
- a contribution no longer produced by the selected sources becomes retired, is
  excluded from `G`, and retains its last generated details and fulfilment credit;
- a contribution produced again later reactivates with the same stable identity and
  retained credit;
- newly added source instances create contributions normally;
- removed source instances retire their contributions;
- ad-hoc rows, available supplies, manual targets, section overrides, and all
  existing credits remain unchanged.

If refresh changes `T_auto` while `manual_target` exists, the manual target remains
authoritative. The row displays a non-blocking notice with the previous and new
automatic values and offers "Use automatic calculation", which clears the manual
override. Refresh does not require a blocking conflict-resolution dialog.

Because credit does not grow automatically, increasing a fulfilled contribution's
generated quantity makes it partial and increases `R`. Decreasing or retiring a
contribution may leave excess credit, which can cover other requirements for the
same aggregate ingredient.

## Historical retention

Shopping lists and their full state, including fulfilment checkboxes, MUST remain
available after event archival.
