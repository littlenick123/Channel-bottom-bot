# Task 1 Report: Schema, Settings, and Domain Types

## Status

DONE

## Implementation

- Added `Settings.pending_draft_ttl_seconds` and `Settings.pending_cleanup_interval_seconds`, both parsed as positive integers with defaults of 600 and 60 seconds.
- Added `PendingDraft`, an immutable slots dataclass with the requested pending status default.
- Extended `SlotSnapshot` with backward-compatible `display_name` and `name_customized` defaults.
- Added transactional schema migration 4. It creates pending-draft tables, adds and backfills slot-name columns, creates the pending-expiration index, and records version 4 only after all statements complete.
- Added the two pending-draft environment values to `.env.example`.
- Added coverage for new settings, domain values, version-3 backfill migration, and migration-4 rollback behavior.

## TDD Evidence

### RED

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_domain.py tests\test_database.py -q
```

Output summary:

```text
7 failed, 13 passed in 1.04s
```

The failures were the intended missing settings attributes and validation, missing `PendingDraft`, absent `SlotSnapshot` fields, missing migration 4/version 4, and absent `MIGRATION_4` statement tuple.

### GREEN

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_domain.py tests\test_database.py -q
```

Output:

```text
20 passed in 1.14s
```

### Full Regression

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Output:

```text
49 passed in 5.04s
```

## Files Changed

- `.env.example`
- `src/bottom_post_bot/config.py`
- `src/bottom_post_bot/database.py`
- `src/bottom_post_bot/domain.py`
- `tests/test_config.py`
- `tests/test_database.py`
- `tests/test_domain.py`
- `.superpowers/sdd/task-1-report.md`

## Self-Review

- The new `SlotSnapshot` fields are appended after `enabled`, preserving existing positional and keyword callers.
- Migration 4 executes each SQL statement after `BEGIN IMMEDIATE`; the existing exception handler rolls back the transaction, so a failing statement leaves schema version 3 and no preceding migration-4 schema changes.
- The backfill uses the assigned revision's draft name and falls back to `置底帖子 N` where the revision has no associated draft.
- Tests cover both the successful version-3 migration path and a deliberately invalid final migration statement to verify atomic rollback.

## Concerns

The workspace is not a valid Git repository: `git status` and `git diff` report `fatal: not a git repository`. Per task instruction, no repository was initialized and no commit was attempted.

## Commit Note

No commit created because the supplied workspace has an empty/non-functional `.git` directory.
