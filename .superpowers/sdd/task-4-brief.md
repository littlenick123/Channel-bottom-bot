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
