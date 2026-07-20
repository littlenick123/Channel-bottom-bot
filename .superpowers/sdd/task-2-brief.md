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
