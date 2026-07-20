# Task 7 — Restart-Safe Pending Cleanup

## Status

Implemented.

## Changes

- Added `PendingCleanupLoop`, which cleans immediately, logs and survives individual cleanup failures, waits by the configured interval, and exits when stopped.
- Started the loop as the `pending-cleanup` task with `Settings.pending_cleanup_interval_seconds`.
- Shutdown now flushes albums, signals both background loops, cancels only unfinished tasks, awaits them with `return_exceptions=True`, then closes the bot session and database.
- Added restart persistence coverage for confirming a pending draft through a reopened database.

## TDD evidence

- RED: `python -m pytest tests/test_maintenance.py tests/test_pending_drafts.py -q` failed during collection because `bottom_post_bot.maintenance` did not exist.
- RED: the app-lifecycle test failed with a timeout before the application created the pending-cleanup task.
- GREEN: `python -m pytest tests/test_maintenance.py tests/test_pending_drafts.py -q` — `9 passed in 3.00s`.
- Full verification: `python -m pytest -q` — `110 passed in 8.24s`.

## Commit

Not created: the workspace is not a Git repository, and the parent task explicitly requested no commit.

## Concerns

None.
