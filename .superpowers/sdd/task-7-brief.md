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
