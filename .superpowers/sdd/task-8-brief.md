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
