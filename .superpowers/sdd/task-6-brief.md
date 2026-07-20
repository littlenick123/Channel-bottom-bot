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
