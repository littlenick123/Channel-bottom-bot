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
