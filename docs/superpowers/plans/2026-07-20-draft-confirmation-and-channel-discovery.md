# Draft Confirmation and Channel Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent confirmation before saving forwarded posts, atomic batch URL-button input, durable channel-slot names, and automatic channel ID recording/binding from `my_chat_member` updates.

**Architecture:** Extend SQLite with pending-draft and slot-name persistence. A `PendingDraftService` owns temporary content, a `ChatMembershipService` owns channel administrator lifecycle events, and existing immutable revisions, permission checks, scheduler, and publisher remain authoritative.

**Tech Stack:** Python 3.12, aiogram 3.26+, aiosqlite, SQLite WAL, pytest, pytest-asyncio.

## Global Constraints

- Keep the bot token-only; do not add Telethon, `API_ID`, `API_HASH`, user sessions, PostgreSQL, or Redis.
- Pending drafts expire after `PENDING_DRAFT_TTL_SECONDS=600`; cleanup runs every `PENDING_CLEANUP_INTERVAL_SECONDS=60`.
- Buttons allow only `https://`, `http://`, and `tg://`, at most 8 per row and 100 total.
- Publication order remains `N → 1`; slot 1 remains the bottom message.
- Drafts stay personal; channel slot names and revision snapshots are shared only with current bound channel administrators.
- Never auto-bind `STORAGE_CHANNEL_ID` as a target.
- Automatic discovery applies to channels only; this work does not add ordinary supergroup refresh listening.
- Keep callback data below Telegram's 64-byte limit by using short operation codes and integer IDs.
- The workspace currently has an empty `.git` directory. Run commit steps only after the user restores or initializes Git metadata; do not initialize Git without authorization.

## File Map

- `config.py`, `.env.example`: pending TTL and cleanup interval.
- `database.py`, `domain.py`, `repositories.py`: migration 4, pending lifecycle, slot metadata.
- New `pending_drafts.py`: temporary-draft orchestration.
- New `membership.py`: `my_chat_member` lifecycle.
- New `maintenance.py`: stoppable cleanup loop.
- `aiogram_gateway.py`, `handlers.py`, `channels.py`, `notifications.py`, `app.py`: Telegram integration and UI.
- `README.md`: administrator setup and all new workflows.
- Existing tests plus new `test_pending_drafts.py`, `test_membership.py`, and `test_maintenance.py`.

---

### Task 1: Schema, Settings, and Domain Types

**Files:**
- Modify: `src/bottom_post_bot/config.py`
- Modify: `src/bottom_post_bot/database.py`
- Modify: `src/bottom_post_bot/domain.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`
- Test: `tests/test_database.py`
- Test: `tests/test_domain.py`

**Interfaces:**
- Produces `Settings.pending_draft_ttl_seconds: int` and `pending_cleanup_interval_seconds: int`.
- Produces `PendingDraft(id, user_id, items, expires_at, status)`.
- Extends `SlotSnapshot` with `display_name` and `name_customized` defaults.
- Produces schema version 4.

- [ ] **Step 1: Write failing settings and domain tests**

Add these default assertions:

```python
self.assertEqual(settings.pending_draft_ttl_seconds, 600)
self.assertEqual(settings.pending_cleanup_interval_seconds, 60)
```

Add a slot test:

```python
revision = DraftRevision(1, 1, (ContentItem(text="post"),))
slot = SlotSnapshot(-1001, 2, revision, True, "活动入口", True)
self.assertEqual((slot.display_name, slot.name_customized), ("活动入口", True))
```

- [ ] **Step 2: Write a failing migration/backfill test**

Create a version-3 database containing a draft and assigned slot, reopen it, and assert:

```python
self.assertEqual(await db.fetch_value("SELECT MAX(version) FROM schema_migrations"), 4)
slot = await db.fetch_one(
    "SELECT display_name, name_customized FROM channel_slots WHERE channel_id=? AND slot_number=?",
    (-1009, 1),
)
self.assertEqual(dict(slot), {"display_name": "Existing draft", "name_customized": 0})
self.assertIsNotNone(await db.fetch_one("SELECT name FROM sqlite_master WHERE name='pending_drafts'"))
```

Add a second migration test that temporarily replaces the last migration statement with invalid SQL, calls `_migrate`, and asserts schema version remains 3 and all earlier migration-4 statements were rolled back. Restore the statement tuple in a `finally` block.

- [ ] **Step 3: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_domain.py tests\test_database.py -q
```

Expected: failures for the missing settings, fields, and schema version.

- [ ] **Step 4: Add settings and domain types**

Add settings with positive-integer environment parsing. Add:

```python
@dataclass(frozen=True, slots=True)
class PendingDraft:
    id: int
    user_id: int
    items: tuple[ContentItem, ...]
    expires_at: float
    status: str = "pending"
```

Append these backward-compatible fields after `SlotSnapshot.enabled`:

```python
display_name: str = ""
name_customized: bool = False
```

- [ ] **Step 5: Add transactional migration 4**

Execute individual statements inside `BEGIN IMMEDIATE`: create `pending_drafts`; create `pending_draft_items` with ordered content fields and cascade deletion; add `display_name TEXT NOT NULL DEFAULT ''` and `name_customized INTEGER NOT NULL DEFAULT 0`; backfill names from `draft_revisions JOIN drafts` with fallback `置底帖子 N`; create an index on `(status, expires_at)`; insert schema version 4; commit. Roll back without inserting version 4 on any failure.

- [ ] **Step 6: Update `.env.example`**

```dotenv
PENDING_DRAFT_TTL_SECONDS=600
PENDING_CLEANUP_INTERVAL_SECONDS=60
```

- [ ] **Step 7: Run tests and verify GREEN**

Run Step 3 again. Expected: all selected tests pass.

- [ ] **Step 8: Commit when Git is available**

```powershell
git add src/bottom_post_bot/config.py src/bottom_post_bot/database.py src/bottom_post_bot/domain.py .env.example tests/test_config.py tests/test_database.py tests/test_domain.py
git commit -m "feat: add pending draft and named slot schema"
```

---

### Task 2: Atomic Pending-Draft Repository and Service

**Files:**
- Modify: `src/bottom_post_bot/repositories.py`
- Modify: `src/bottom_post_bot/drafts.py`
- Create: `src/bottom_post_bot/pending_drafts.py`
- Test: `tests/test_database.py`
- Test: `tests/test_drafts.py`
- Create: `tests/test_pending_drafts.py`

**Interfaces:**
- Produces `default_draft_name(messages: Sequence[IncomingContent | ContentItem]) -> str`.
- Produces Repository methods `create_pending_draft`, `get_pending_draft`, `confirm_pending_draft`, `mark_pending_discarded`, `list_pending_cleanup`, and `complete_pending_cleanup`.
- Produces `PendingDraftService.prepare`, `confirm`, `discard`, and `cleanup_expired`.

- [ ] **Step 1: Write failing repository tests**

Test ordered album items and one-time confirmation:

```python
pending = await repo.create_pending_draft(
    1,
    (ContentItem(text="caption", storage_message_id=700, grouped_id="g"),
     ContentItem(storage_message_id=701, media_kind="photo", telegram_file_id="f", grouped_id="g")),
    expires_at=200.0,
)
draft = await repo.confirm_pending_draft(1, pending.id, "Album", 50, now=100.0)
self.assertEqual([x.storage_message_id for x in draft.current_revision.items], [700, 701])
with self.assertRaisesRegex(AuthorizationError, "processed or expired"):
    await repo.confirm_pending_draft(1, pending.id, "Again", 50, now=100.0)
```

Also prove a quota error leaves the pending row unchanged and creates no draft, and a foreign user cannot inspect, confirm, or discard it.

- [ ] **Step 2: Write failing service tests**

With a fake storage gateway, verify prepare copies once, default confirmation uses the content summary, discard deletes storage IDs, expired rows are retried, and a deletion exception leaves a terminal database row for the next cleanup call.

- [ ] **Step 3: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_pending_drafts.py tests\test_database.py tests\test_drafts.py -q
```

Expected: missing pending repository/service APIs.

- [ ] **Step 4: Extract deterministic naming**

Expose this function and make `DraftService.capture` use it:

```python
def default_draft_name(messages: Sequence[IncomingContent | ContentItem]) -> str:
    for message in messages:
        if message.text and message.text.strip():
            return " ".join(message.text.split())[:40]
    return "媒体草稿"
```

- [ ] **Step 5: Implement pending persistence**

Insert the pending parent and all `ContentItem` fields in one transaction. Load items by `position`. `get_pending_draft` must filter by owner and return no information for another user.

- [ ] **Step 6: Implement atomic confirmation**

Inside one `Database.transaction()`, select an owned unexpired `pending` row, check the non-deleted draft count, insert draft and revision through `_insert_revision`, update `current_revision_id`, and delete the pending parent. Raise `AuthorizationError("pending draft already processed or expired")` for missing, foreign, terminal, or expired rows. Preserve the pending row on `ResourceLimitError`.

- [ ] **Step 7: Implement terminal cleanup states**

`mark_pending_discarded` changes only an owned pending row to `discarded`. `list_pending_cleanup` first changes due pending rows to `expired`, then returns `discarded` and `expired` rows ordered by ID. `complete_pending_cleanup` deletes only terminal rows.

- [ ] **Step 8: Implement `PendingDraftService`**

Use constructor:

```python
def __init__(self, repository, storage, max_drafts: int, ttl_seconds: int, clock=time.time): ...
```

`prepare` copies first and compensates by deleting copied IDs if database insertion fails. `confirm` delegates to the atomic repository method. `discard` marks terminal before Telegram deletion. `cleanup_expired` deletes storage IDs and completes only successful or already-missing deletions; other failures are logged for retry.

- [ ] **Step 9: Run tests and verify GREEN**

Run Step 3 again. Expected: all selected tests pass.

- [ ] **Step 10: Commit when Git is available**

```powershell
git add src/bottom_post_bot/repositories.py src/bottom_post_bot/drafts.py src/bottom_post_bot/pending_drafts.py tests/test_database.py tests/test_drafts.py tests/test_pending_drafts.py
git commit -m "feat: persist pending draft confirmation"
```

---

### Task 3: Storage Preview and Confirmation UI

**Files:**
- Modify: `src/bottom_post_bot/aiogram_gateway.py`
- Modify: `src/bottom_post_bot/handlers.py`
- Modify: `src/bottom_post_bot/app.py`
- Test: `tests/test_aiogram_gateway.py`
- Test: `tests/test_aiogram_handlers.py`

**Interfaces:**
- Produces `BotApiGateway.preview_storage_messages(user_id, message_ids) -> list[int]`.
- Extends `BotHandlers` with `PendingDraftService`.
- Adds callbacks `p:s:<id>`, `p:n:<id>`, `p:x:<id>` and state `await_pending_name`.

- [ ] **Step 1: Write failing gateway and handler tests**

Assert preview uses:

```python
await bot.copy_messages(
    chat_id=42,
    from_chat_id=-10050,
    message_ids=[700, 701],
    disable_notification=True,
)
```

Assert a forwarded post prepares but does not immediately create a formal draft; an album prepares once in message-ID order; the three callbacks save, enter naming, or discard; custom name text is trimmed; `/cancel` exits naming and shows the confirmation choices again.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py tests\test_aiogram_handlers.py -q
```

Expected: missing preview method, dependency, callbacks, and state behavior.

- [ ] **Step 3: Implement storage preview copy**

Call `copy_messages` from the storage channel, keep order and grouping, set silent delivery, translate `TelegramRetryAfter` to `FloodWaitSignal`, and translate forbidden/bad-request errors to a private-preview-specific `PermanentPublishError`.

- [ ] **Step 4: Replace immediate forwarding capture**

`_capture` converts messages, calls `pending_drafts.prepare`, previews all storage IDs, and sends a keyboard with `保存为草稿`, `保存并命名`, and `放弃`. If preview delivery fails, send a warning but still send the confirmation keyboard. Do not call `DraftService.capture` from this path.

- [ ] **Step 5: Implement callback and naming state behavior**

Default save confirms and opens the created draft. Named save stores `{"pending_id": id}` and waits for 1–100 non-whitespace characters. Discard marks and cleans the pending item. Repeated/expired operations use one non-leaking error message. A quota alert leaves the confirmation usable.

- [ ] **Step 6: Wire the service in `app.py`**

Construct `PendingDraftService(repository, telegram, settings.max_drafts_per_user, settings.pending_draft_ttl_seconds)` and pass it to `BotHandlers`.

- [ ] **Step 7: Run tests and verify GREEN**

Run Step 2 again. Expected: all selected tests pass.

- [ ] **Step 8: Commit when Git is available**

```powershell
git add src/bottom_post_bot/aiogram_gateway.py src/bottom_post_bot/handlers.py src/bottom_post_bot/app.py tests/test_aiogram_gateway.py tests/test_aiogram_handlers.py
git commit -m "feat: confirm forwarded posts before saving"
```

---

### Task 4: Atomic Batch URL Buttons

**Files:**
- Modify: `src/bottom_post_bot/handlers.py`
- Test: `tests/test_handlers.py`
- Test: `tests/test_aiogram_handlers.py`

**Interfaces:**
- Produces `parse_button_batch(value, existing=()) -> tuple[ButtonSpec, ...]` returning the complete combined layout.
- Retains `parse_button_input` as a one-button compatibility wrapper.

- [ ] **Step 1: Write failing parser tests**

```python
existing = (ButtonSpec("旧按钮", "https://old.example", 0, 0),)
buttons = parse_button_batch(
    "官网 | https://example.com | 1\n客服 | tg://resolve?domain=example | 1\n下载 | https://example.com/d | 2",
    existing,
)
self.assertEqual([(b.row, b.column) for b in buttons], [(0, 0), (0, 1), (0, 2), (1, 0)])
```

Add independent tests for blank lines, malformed line 2, invalid URL, ninth button in a row, and 101st total button. Error messages must identify the physical input line when applicable.

- [ ] **Step 2: Write a failing atomic handler test**

Submit one valid and one invalid line and assert `update_buttons` is not called. Submit a valid batch and assert it is called once with the complete combined tuple.

- [ ] **Step 3: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_handlers.py tests\test_aiogram_handlers.py -q
```

Expected: missing batch parser and old one-button behavior.

- [ ] **Step 4: Implement parsing and validation**

Parse all non-empty lines, convert one-based row numbers to zero-based values, allocate columns after existing buttons in each row, and construct every `ButtonSpec`. Validate the complete result once through `DraftRevision(0, 1, (ContentItem(text="validate"),), combined)`. Return the combined tuple. Make `parse_button_input` call this function and require exactly one result.

- [ ] **Step 5: Update the conversation UI**

Show the approved three-line syntax. In `await_button`, load the owned draft, parse against existing buttons, and call `update_buttons` once. Report the number of newly added buttons.

- [ ] **Step 6: Run tests and verify GREEN**

Run Step 3 again. Expected: all selected tests pass.

- [ ] **Step 7: Commit when Git is available**

```powershell
git add src/bottom_post_bot/handlers.py tests/test_handlers.py tests/test_aiogram_handlers.py
git commit -m "feat: add URL buttons in atomic batches"
```

---

### Task 5: Stable Channel Slot Names

**Files:**
- Modify: `src/bottom_post_bot/repositories.py`
- Modify: `src/bottom_post_bot/channels.py`
- Modify: `src/bottom_post_bot/handlers.py`
- Test: `tests/test_database.py`
- Test: `tests/test_permissions.py`
- Test: `tests/test_aiogram_handlers.py`

**Interfaces:**
- Produces `owned_draft_name_for_revision(revision_id, user_id) -> str | None`.
- Extends `assign_slot` with the source draft name while preserving custom names.
- Produces `ChannelService.rename_slot(channel_id, slot_number, display_name, actor_id)`.
- Adds state `await_slot_name`.

- [ ] **Step 1: Write failing repository and service tests**

Prove first assignment copies `First`, uncustomized replacement changes it to `Second`, manual rename to `首页入口` survives later replacement, move/swap carries all name metadata with the row, and clear removes it.

- [ ] **Step 2: Write failing UI tests**

Assert channel text includes `1. 首页入口｜已启用｜版本 2`, occupied slot buttons contain truncated names, the rename callback sets `await_slot_name`, whitespace is rejected, and a valid name invokes the service.

- [ ] **Step 3: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_database.py tests\test_permissions.py tests\test_aiogram_handlers.py -q
```

Expected: missing slot metadata mapping, rename API, and UI.

- [ ] **Step 4: Map metadata in repository reads**

Select and construct `display_name` and `name_customized` in both `list_channel_slots` and `load_publish_state`. Publisher behavior remains unchanged because it uses only revision and enabled state.

- [ ] **Step 5: Preserve custom names during assignment**

`ChannelService.assign_slot` resolves the owned active draft name. Repository inserts it with `name_customized=0`; conflict update uses `CASE WHEN channel_slots.name_customized=1 THEN channel_slots.display_name ELSE excluded.display_name END` while updating revision, enabled state, actor, and timestamp.

- [ ] **Step 6: Implement rename and audit**

Require a trimmed 1–100 character name, verify current live channel management in the service and bound-manager status in Repository, set `name_customized=1`, and audit `slot.rename` with slot number and name.

- [ ] **Step 7: Update channel UI**

Render empty and occupied slots using the approved format, add `改名 N号`, accept the new state, and return to the channel page. Do not schedule a publish refresh because the name is management-only metadata.

- [ ] **Step 8: Run tests and publisher regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_database.py tests\test_permissions.py tests\test_aiogram_handlers.py tests\test_publisher.py tests\test_scheduler.py -q
```

Expected: all selected tests pass and publication remains high-to-low.

- [ ] **Step 9: Commit when Git is available**

```powershell
git add src/bottom_post_bot/repositories.py src/bottom_post_bot/channels.py src/bottom_post_bot/handlers.py tests/test_database.py tests/test_permissions.py tests/test_aiogram_handlers.py
git commit -m "feat: name shared channel slots"
```

---

### Task 6: Automatic Channel ID Recording and Binding

**Files:**
- Create: `src/bottom_post_bot/membership.py`
- Modify: `src/bottom_post_bot/notifications.py`
- Modify: `src/bottom_post_bot/repositories.py`
- Modify: `src/bottom_post_bot/app.py`
- Create: `tests/test_membership.py`
- Test: `tests/test_permissions.py`

**Interfaces:**
- Produces `TelegramAdminNotifier.notify_user(user_id, text) -> bool`.
- Produces `ChatMembershipService.handle(event: ChatMemberUpdated) -> None`.
- Uses `ChannelService.bind(user_id, channel_id)` for live permission checks.

- [ ] **Step 1: Write failing lifecycle tests**

Use simple channel-event factories and cover administrator promotion, duplicate promotion, storage-channel exclusion, supergroup exclusion, non-admin actor, missing bot capabilities, channel quota, downgrade, removal, unknown-channel removal, and private-notification failure. Successful promotion must record the numeric ID, bind once, audit `channel.auto_bind`, and notify with channel name and ID. Failed binding must keep the channel record and audit `channel.auto_bind_failed`. Loss of access must pause without deleting slots or manager rows.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_membership.py tests\test_permissions.py -q
```

Expected: missing membership module and notifier API.

- [ ] **Step 3: Add safe direct notification**

`notify_user` calls `send_message`, returns `True` on success, catches/logs Telegram errors, and returns `False`. Notification outcome must never roll back a binding.

- [ ] **Step 4: Implement administrator transition handling**

Normalize enum/string status values. Ignore non-channel chats and the storage channel. For administrator/creator state, upsert the actor and channel first, check whether already bound, then call `ChannelService.bind`. Audit success only on a new binding. On permission or quota failure, retain the channel record, audit the exact failure class/message, and do not create a manager association. If the channel already has configuration and a fresh `bot_capabilities` check shows missing post/delete rights, also pause that configuration; an inviter who is not an administrator must not cause a pause.

- [ ] **Step 5: Implement access-loss handling**

For left, kicked, member, or restricted state, return if the channel is unknown. Otherwise call `pause_channel`, then audit `channel.bot_access_lost` with actor, old status, and new status. Preserve channel, slots, and manager associations.

- [ ] **Step 6: Register the observer**

Construct the membership service in `app.py` and register:

```python
router.my_chat_member.register(membership.handle)
```

Add a Dispatcher test asserting `"my_chat_member" in dispatcher.resolve_used_update_types()`.

- [ ] **Step 7: Run tests and verify GREEN**

Run Step 2 again. Expected: all selected tests pass.

- [ ] **Step 8: Commit when Git is available**

```powershell
git add src/bottom_post_bot/membership.py src/bottom_post_bot/notifications.py src/bottom_post_bot/repositories.py src/bottom_post_bot/app.py tests/test_membership.py tests/test_permissions.py
git commit -m "feat: auto-bind channels from membership updates"
```

---

### Task 7: Restart-Safe Pending Cleanup

**Files:**
- Create: `src/bottom_post_bot/maintenance.py`
- Modify: `src/bottom_post_bot/app.py`
- Create: `tests/test_maintenance.py`
- Test: `tests/test_pending_drafts.py`

**Interfaces:**
- Produces `PendingCleanupLoop(service, interval_seconds)`, `run_forever()`, and `stop()`.

- [ ] **Step 1: Write failing loop and restart tests**

Assert cleanup runs immediately, continues after a logged cleanup exception, and exits after `stop`. Create a pending row with one service/database instance, close it, reopen the database, and prove a new instance can confirm it before expiry or clean it after expiry.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_maintenance.py tests\test_pending_drafts.py -q
```

Expected: missing maintenance loop and restart behavior.

- [ ] **Step 3: Implement the loop**

Use an `asyncio.Event`. Each iteration calls `cleanup_expired`, catches/logs exceptions without terminating, then waits for the stop event with `asyncio.wait_for(..., timeout=interval_seconds)`. Timeout begins the next iteration; a set event exits.

- [ ] **Step 4: Wire startup and shutdown**

Create `pending-cleanup` beside `refresh-scheduler`. In `finally`, signal stop, cancel only if still running, gather with `return_exceptions=True`, and preserve existing album flush, scheduler shutdown, bot-session close, and database close order.

- [ ] **Step 5: Run tests and verify GREEN**

Run Step 2 again. Expected: all selected tests pass.

- [ ] **Step 6: Commit when Git is available**

```powershell
git add src/bottom_post_bot/maintenance.py src/bottom_post_bot/app.py tests/test_maintenance.py tests/test_pending_drafts.py
git commit -m "feat: clean expired pending drafts after restart"
```

---

### Task 8: Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Verify: all files changed in Tasks 1–7.

**Interfaces:** No new runtime interface.

- [ ] **Step 1: Expand setup documentation**

Document storage-channel and target-channel administrator permissions, automatic binding and displayed numeric ID, storage-channel exclusion, and the fact that ordinary supergroup messages remain outside automatic refresh scope.

- [ ] **Step 2: Document management workflows**

Document the three confirmation buttons, 10-minute expiry, batch-button example and limits, automatic slot naming, custom rename persistence, and removal/downgrade pause behavior.

- [ ] **Step 3: Run the complete test suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: exit code 0 and zero failed tests.

- [ ] **Step 4: Run bytecode compilation**

```powershell
.\.venv\Scripts\python.exe -m compileall -q src tests
```

Expected: exit code 0 and no syntax errors.

- [ ] **Step 5: Verify aiogram update registration**

```powershell
.\.venv\Scripts\python.exe -c "from aiogram import Dispatcher, Router; d=Dispatcher(); r=Router(); r.my_chat_member.register(lambda event: None); d.include_router(r); assert 'my_chat_member' in d.resolve_used_update_types(); print('my_chat_member OK')"
```

Expected output: `my_chat_member OK`.

- [ ] **Step 6: Check every approved acceptance criterion**

Confirm from code and tests: confirmation is persistent and one-time; discard/expiry cleanup retries; batches append atomically; occupied slots show stable editable names; administrator promotion records and binds the channel ID; storage/supergroups are excluded; access loss pauses without deletion; existing `N → 1` publishing is unchanged.

- [ ] **Step 7: Inspect final changes when Git is available**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors and only planned files modified.

- [ ] **Step 8: Commit documentation when Git is available**

```powershell
git add README.md .env.example
git commit -m "docs: explain draft confirmation and channel discovery"
```

## Final Verification Gate

Before reporting completion, rerun the full pytest and compile commands from Task 8 in the same turn, read their complete output, and compare the code line-by-line with the approved design. Focused or previous test runs are not sufficient evidence for completion.
