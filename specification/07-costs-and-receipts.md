# Costs and Receipts

Status: Draft

## Estimated costs

- Ingredient catalog prices provide approximate recipe and shopping costs.
- Active events calculate recipe costs from event-owned ingredient-price snapshots,
  not directly from subsequently changing catalog prices.
- A scheduled recipe instance SHOULD display total estimated cost and estimated
  cost per diner.
- Events SHOULD display aggregated estimated food cost.
- Estimates remain advisory; shoppers may choose different products and package
  sizes.

When a nonzero resolved ingredient has no event price, the application retains the
partial numeric estimate and displays a warning identifying the missing prices.

## Updating event price estimates

- When an ingredient first becomes relevant to an active event, the event captures
  the ingredient's selected current catalog price or an explicit unavailable
  marker. Offline creation may use the cached pointer to an immutable estimate.
- A member MAY run "Update price estimates" for one active event.
- The operation captures current catalog estimates for every ingredient already
  known to that event and every ingredient currently used by its resolved recipes.
- The refresh is atomic from the event's perspective: readers see either all old or
  all new price snapshots.
- Catalog price changes never update an event automatically and never appear as
  recipe-version updates.
- Shopping lists remain separately materialized; an existing list receives the new
  event prices only through its own explicit refresh.
- Archived events cannot refresh prices and retain the exact price snapshots stored
  at archival time.

## Budget

- An event has one overall budget.
- The event uses one currency inherited initially from the organization's default
  currency. The initial default is CZK.
- The application MUST compare recorded costs with that budget.
- Category-specific budgets are not required for the MVP.

The event cost summary MUST show:

- overall budget;
- estimated cost of scheduled recipe instances;
- expected cost of materialized shopping lists;
- actual total of non-deleted receipts;
- amount remaining in or exceeding the budget.

## Receipts

Members can record receipts against an event. An MVP receipt contains:

- required merchant or short title;
- required total amount in the event currency;
- optional date;
- optional free-form note;
- zero or more receipt photo attachments.

The receipt total is the only structured monetary breakdown. The MVP does not
require receipt categories, payer tracking, submission state, reimbursement state,
or line-item entry.

## Receipt photos

- Members MUST be able to capture a photo with a mobile camera or choose an existing
  image from the device.
- A receipt MAY contain multiple images so long or double-sided receipts can be
  represented.
- A thumbnail or preview MUST be available from the receipt detail.
- Image uploads MUST expose pending, uploading, synchronized, and failed states.
- A receipt and its local photo references can be created offline.
- Pending image bytes MUST survive application reload until upload succeeds or the
  user explicitly removes them.
- The browser MUST normalize orientation and re-encode each selected image before
  placing it in the offline queue.
- The processed image MUST NOT exceed 2000 pixels on its longer edge and MUST NOT
  be enlarged when the source is smaller.
- The client targets a maximum payload of approximately 2 MB per processed image,
  using JPEG or WebP. Compression MUST prioritize readable receipt text.
- Re-encoding MUST remove EXIF and other source metadata, including location data.
- CookOps stores and uploads only the processed image. It does not retain the
  original full-resolution input; an original already present in the device photo
  library is not modified.
- If an image cannot meet the target while remaining readable, the UI MUST ask the
  user to retake it or attach the receipt as multiple images rather than silently
  producing an unreadable attachment.
- Automated OCR and line-item extraction are outside the MVP.

## Deletion

Deleting a receipt is a reversible soft-delete. Deleted receipts do not contribute
to actual cost totals, but authorized members can restore them with their metadata
and synchronized photo attachments.
