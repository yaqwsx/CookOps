# Planning Interface

Status: Draft

## Primary planner

The event planner is the primary workspace for composing and rearranging the menu.

- Days are presented as a vertically scrollable sequence of day sections.
- Each day displays its editable note and scheduled recipe cards.
- Recipe cards are grouped and ordered by meal-role tag.
- The ordering of meal-role tags is configurable and affects planner presentation.
- Multiple recipe cards MAY exist under the same role on the same day.
- Members can manually order recipe cards within a role.

## Desktop interaction

- Recipe cards MUST support drag-and-drop between days and meal-role groups.
- A recipe catalog panel is displayed on the right side of the planner when space
  permits.
- Members MUST be able to drag a catalog recipe into a day and role to create a
  scheduled recipe instance.
- The planner SHOULD keep sufficient day context visible while browsing or
  searching the catalog.

## Mobile interaction

- Days and their recipe cards use a single-column layout.
- The recipe catalog opens as a separate drawer, sheet, or full-screen panel.
- Every drag-and-drop operation MUST have a non-drag alternative through an
  explicit "Move to..." or "Add to..." action.
- Core planning actions MUST remain possible without precision dragging.

## Scheduled recipe card

A compact recipe card SHOULD expose:

- recipe name;
- meal-role tag;
- final diner count;
- selected scaling amount and unit, plus a changed suggestion when the selected
  value is manual;
- local-override indicator;
- dietary warning indicator;
- missing-price indicator;
- catalog-update availability when applicable.

The selected recipe version, estimated prepared weight, estimated cost, per-diner
values, description, ingredients, and warning details are secondary information
revealed after expansion or in the scheduled-instance detail surface.

Editing the diner count or selected scaling amount directly changes that value to
manual mode. The expanded card or edit dialog MUST provide an explicit action to
resume following event attendance or the calculated scaling suggestion.

Quick edits made from the planner, including ingredient quantity changes or added
ingredients, create visually marked event-local overrides. Editing the underlying
catalog recipe opens the dedicated recipe editor dialog instead of mutating the
scheduled instance inline.

## Catalog updates

Catalog updates are handled on individual scheduled recipe instances. The MVP does
not require a bulk catalog-update screen.

The event planner is the default event route and uses the event summary strip and
workspace navigation defined in `19-information-architecture.md`. The MVP does not
add a separate event dashboard before the planner.
