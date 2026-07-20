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
