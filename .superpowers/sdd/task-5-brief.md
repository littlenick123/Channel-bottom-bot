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
