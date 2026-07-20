# Task 3 Report: Storage Preview and Confirmation UI

## Status

Completed and verified.

## Implemented scope

- Added `BotApiGateway.preview_storage_messages(user_id, message_ids)`. It copies the stored messages to the user's private chat in the original order, silently, with the storage channel as the source. It maps rate limits to `FloodWaitSignal` and forbidden/bad-request preview failures to a preview-specific `PermanentPublishError`.
- Injected `PendingDraftService` into `BotHandlers` and constructed it from the repository, Telegram gateway, draft quota, and pending-draft TTL in `app.py`.
- Replaced forwarding capture with pending-draft preparation. The handler stores copied content first, sends a private preview, and then presents the persistent confirmation actions. It does not invoke `DraftService.capture` on this route.
- Preserved album aggregation: the existing 0.8-second aggregation path now prepares one pending item after sorting by Telegram message ID.
- Added confirmation callbacks:
  - `p:s:<id>` confirms using the default name and opens the saved draft.
  - `p:n:<id>` records `await_pending_name` and asks for a 1–100-character name.
  - `p:x:<id>` discards the pending item and returns to the draft list.
- Name input trims surrounding whitespace. `/cancel` while naming clears only the naming state and restores the same three pending confirmation actions.
- A failed private preview displays a warning but still sends the confirmation keyboard. Duplicate/expired pending operations use a single non-leaking message; quota errors only show an alert, leaving the original confirmation UI in place.

## TDD evidence

### RED

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py tests\test_aiogram_handlers.py -q
```

Result: `7 failed, 6 passed in 2.55s` (exit 1), with the expected missing behavior:

- `BotApiGateway.preview_storage_messages` did not exist.
- `BotHandlers` did not accept the `pending_drafts` dependency.
- The confirmation callbacks and pending-name conversation behavior were consequently unavailable.

### GREEN

The same focused command after the minimal implementation:

```text
13 passed in 2.50s
```

The added tests cover the exact storage-to-private `copy_messages` arguments; single forwarded-post preparation without a formal draft; one ordered album preparation; save/name/discard callbacks; trimmed and bounded custom names; `/cancel` returning to the three choices; and a quota error that leaves the callback flow usable.

## Full regression verification

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Result:

```text
65 passed in 5.52s
```

## Files changed

- `src/bottom_post_bot/aiogram_gateway.py`
- `src/bottom_post_bot/handlers.py`
- `src/bottom_post_bot/app.py`
- `tests/test_aiogram_gateway.py`
- `tests/test_aiogram_handlers.py`
- `.superpowers/sdd/task-3-report.md`

## Requirements review

- Preview sends a source-free storage copy to the requesting user and preserves the passed message-ID order.
- Pending persistence, atomic confirmation, discard cleanup, and quota retention remain delegated to the Task 2 service/repository interfaces.
- Forwarded captures now prepare rather than create formal drafts; all confirmation paths route through `PendingDraftService`.
- Confirmation callback data consists only of the operation code and pending integer ID.
- A preview-delivery exception cannot leave the user without a confirmation keyboard.
- Existing draft management, channel handling, and album aggregation remain otherwise unchanged.

## Commit note

No commit was created. The task instruction and workspace context specify an empty/non-functional `.git` directory; no repository was initialized.

## Concerns

None for Task 3. Expired/discarded storage cleanup scheduling is intentionally deferred to Task 7; Task 3 uses the already implemented immediate discard behavior and persistent pending rows.

## Review-fix evidence

### Findings addressed

- `p:n:<id>` now calls `PendingDraftService.assert_confirmable` before it records `await_pending_name`. The service checks owner-scoped existence, `pending` status, and expiry against its clock, returning the existing safe `AuthorizationError` for foreign, terminal, or expired IDs. Atomic confirmation remains the final race-safe check in `Repository.confirm_pending_draft`.
- A naming confirmation that fails due to expiry/processing or draft quota now clears the conversation state and replies with the same three confirmation actions. Quota handling does not mutate the pending row, so the user can remove an old formal draft and retry from that menu. Unexpected confirmation exceptions also clear the state before propagating to the existing error handler.
- `Repository.mark_pending_discarded` now accepts `now` and atomically requires `status='pending' AND expires_at>now`. `PendingDraftService.discard` passes its clock value, so an expired row cannot be marked discarded or have storage messages deleted through the discard path; the scheduled cleanup owns it.
- Added private-preview gateway regressions proving `TelegramRetryAfter` maps to `FloodWaitSignal` and bad-request errors map to the private-preview `PermanentPublishError`.
- Added UI regressions for preview-copy failure retaining the confirmation keyboard, `p:n` rejection without conversation state, and cleared/reusable state after named-confirm failures. Added service coverage for expired discard avoiding storage deletion.

### Review-fix RED

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py tests\test_aiogram_handlers.py tests\test_pending_drafts.py -q
```

Result: `4 failed, 19 passed in 3.10s` (exit 1). The failures exactly exposed absent pre-state confirmability validation, retained naming state after failed confirmation, and expired discard deleting storage content. The new gateway mappings already passed because that part of Task 3 was present.

One further interaction RED test required a failed named confirmation to restore the confirmation keyboard:

```text
1 failed, 10 passed in 2.63s
```

It failed with the expected unhandled `AuthorizationError` before a keyboard could be sent.

### Review-fix GREEN

The covering focused command passed after the minimal service, repository, and handler changes:

```text
23 passed in 2.81s
```

### Review-fix full regression

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

```text
71 passed in 5.73s
```

## Final review-follow-up: forbidden private preview

Added a focused gateway regression for a storage-to-private preview that receives `TelegramForbiddenError` (for example, when the user blocks the bot). The gateway now proves this is translated to the same private-preview-specific `PermanentPublishError` as `TelegramBadRequest`, rather than leaking an aiogram exception.

### RED

The existing forbidden-error conversion was temporarily removed before writing/running the focused regression, so the test exercised the intended failure mode:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py -q
```

```text
1 failed, 7 passed in 2.51s
```

The failure was the raw `TelegramForbiddenError` escaping `preview_storage_messages`.

### GREEN

Restoring the minimal symmetric exception tuple produced:

```text
8 passed in 2.36s
```

### Full regression

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

```text
72 passed in 5.78s
```
