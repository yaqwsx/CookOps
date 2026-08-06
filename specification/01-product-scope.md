# Product Scope

Status: Draft

## Purpose

CookOps is a responsive web application for planning and operating cooking for
large groups. It replaces a Google Sheets workflow while retaining its strongest
parts: recipe breakdowns, automatic quantity estimates, weight and cost estimates,
and collaborative shopping lists.

The application MUST be usable on desktop and mobile devices. Shopping is a
mobile-first, real-time collaborative workflow.

CookOps is open-source software distributed under the MIT License.

## Primary workflow

1. A member creates an event in an organization.
2. Members establish the event schedule and an initial menu overview.
3. Members add catalog recipes or create new recipes while planning the event.
4. Recipe instances are scaled for the expected attendance and adjusted locally
   when necessary.
5. Members inspect estimated ingredient weight and cost per recipe and per diner.
6. A member selects arbitrary scheduled recipes and generates a shopping list.
7. Members enter supplies that are already available.
8. Multiple members use the shopping list concurrently on their phones and mark
   ingredient requirements as fulfilled.
9. Members record the event budget, receipts, and receipt notes.
10. The event may be archived into a self-contained historical record.

## MVP capabilities

- Google-authenticated access restricted to organization members and system
  administrators.
- System administrator, organization administrator, and member roles.
- Multiple organizations per user and an organization switcher.
- Organization-owned, versioned ingredient and recipe catalogs.
- Versioned catalog recipes and event-local recipe overrides.
- Events with days, an arbitrary number of scheduled recipes, and configurable
  meal-role tags.
- Base attendance with an explicit, editable diner count on each scheduled recipe
  instance.
- Single-variable recipe scaling with manual control.
- Canonical ingredient units, compatible SI conversion, and portion-weight
  estimation.
- Weight and estimated cost calculations.
- Named dietary exceptions with structured requirement tags and free-form notes.
- Organization-managed dietary labels and ingredient-based warnings.
- Snapshot-based, manually refreshable shopping lists.
- Real-time collaborative shopping on multiple mobile devices.
- Offline use of previously cached application data with automatic synchronization
  after connectivity returns.
- Event-level budget and receipt tracking.
- Receipt photo capture and attachment, including deferred offline upload.
- Archiving, reactivation, and duplication of events.
- Czech and English user interfaces.

## Explicitly deferred

- Equipment planning and tracking.
- Automated receipt OCR and line-item extraction.
- General multi-variable recipe formulas.
- Automatic purchase-package rounding.
- Automatic detection of ingredients already included in another shopping list.
- Full participant registration or attendance management.
- Import of the existing Google Sheets ingredient catalog. This will be delivered
  as a separate one-time migration after the catalog schema is stable.
- Import of historical events or day-sheet recipes; the CookOps recipe catalog
  starts as a new catalog apart from the separate ingredient migration.
