# Final Fix Report

Date: 2026-07-20  
Status: COMPLETE  
Commit: none; the workspace `.git` directory is intentionally empty, so Git was neither initialized nor used.

## Scope and baseline

All eight findings in `final-fix-brief.md` were reproduced or confirmed in the current codebase. No finding required `NEEDS_CONTEXT`.

Baseline before this fix wave:

```text
.\.venv\Scripts\python.exe -m pytest -q
110 passed in 9.61s
```

For aiogram lifecycle behavior, the installed package was inspected directly. It is aiogram `3.30.0`; `Dispatcher._polling` adds every concurrently handled update task to `_handle_update_tasks` and registers a discard callback, while `Dispatcher.start_polling` stops polling and emits shutdown without awaiting that set. Context7's aiogram lifecycle documentation corroborated that `start_polling` emits shutdown and optionally closes sessions, but does not show an in-flight update-task drain.

## Per-finding changes

### 1. Central storage-channel exclusion

- Added required `storage_channel_id` injection to `ChannelService` and wired it from `Settings` in `app.py`.
- Numeric/string numeric storage IDs are rejected before channel resolution or permission checks.
- A resolved-identity guard also prevents an alias from reaching channel persistence or manager binding.
- The existing membership fast path remains in place.
- Regression verifies the manual service path performs no permission lookup and creates no channel, manager, or audit row.

### 2. Preserve managers when membership cannot be inspected

- Added `PermissionUnavailable`, a `PermissionDenied` subtype representing an indeterminate live lookup.
- `BotApiPermissionGateway.user_is_admin` now returns `False` only for a successfully fetched, definite non-admin member status.
- Every `TelegramAPIError` from the lookup, including forbidden, bad request, retry, network, and server failures, becomes `PermissionUnavailable` instead of `False`.
- Existing `PermissionService` behavior now unbinds only on definite `False`; an unavailable exception surfaces to the caller without deleting the association.
- Notifier regression uses the real permission adapter behavior and proves bot removal preserves the manager row and skips notification delivery.

### 3. Preserve concurrent access-loss pause during publish finalization

- Made the channel-status update in `Repository.finalize_batch` conditional on `status!='paused'`.
- Batch bookkeeping still completes, but a pause committed before finalization retains `paused`, `enabled=0`, and its recovery reason.
- Normal non-paused successful finalization still changes an error state to `active` and clears `last_error`.
- The paused status continues to drive the existing `检查权限并恢复` channel UI while the manager association remains intact.

### 4. Contain and audit membership Telegram failures

- The complete administrator-binding call now catches `TelegramAPIError` alongside permission and quota failures.
- Each failed event writes exactly one `channel.auto_bind_failed` audit after user/channel discovery and before returning.
- Direct Telegram network/retry/API failures leave the discovered channel, create no manager, and do not pause it.
- The secondary bot-capability recheck is wrapped independently; retry/network failures are logged and contained without a second audit or ambiguous pause.
- `PermissionUnavailable` and direct Telegram API failures skip the pause-producing capability recheck.

### 5. Drain in-flight aiogram updates before closing resources

- Added `drain_dispatcher_update_tasks` as the single, bounded adapter around aiogram's private tracked-task set.
- The adapter snapshots and awaits tracked tasks with `return_exceptions=True`, logs failures/cancellation, removes the completed snapshot, and repeats until the live set is empty.
- It never cancels healthy update handlers.
- `app.run` invokes the drain immediately after polling intake stops and before album flush, cleanup/scheduler shutdown, bot-session close, and database close.
- The lifecycle regression blocks a tracked handler, adds a second tracked handler while draining is already underway, and proves neither album flush nor session/database closure happens until both handlers finish.

### 6. Strict URL prefixes

- `ButtonSpec` trims and stores the URL, then validates the normalized value case-insensitively.
- Only exact `https://`, `http://`, and `tg://` prefixes are accepted.
- Opaque forms (`https:foo`, `http:foo`, `tg:foo`), unsupported schemes, whitespace-wrapped opaque forms, and HTTP(S) URLs without an authority are rejected.
- Domain and batch-parser regressions cover normalization, physical-line error reporting, and all rejected forms.

### 7. Complete successful discard immediately

- `PendingDraftService.discard` now calls `complete_pending_cleanup` immediately after storage deletion succeeds.
- Successful or gateway-handled already-missing deletion leaves no pending row.
- A deletion exception still propagates while leaving the row terminal (`discarded`) for the maintenance retry path.

### 8. Correct README forwarding text

- Replaced the remaining claim that direct forwarding automatically saves a personal draft.
- README now says forwarding creates a 10-minute pending item with Save, Save and Name, and Discard actions.

## RED/GREEN evidence

### Finding 1

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_permissions.py::PermissionServiceTests::test_manual_binding_rejects_storage_channel_before_lookup_or_persistence -q
1 failed: ChannelService did not accept storage_channel_id
```

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_permissions.py::PermissionServiceTests::test_manual_binding_rejects_storage_channel_before_lookup_or_persistence tests\test_permissions.py tests\test_membership.py -q
26 passed in 5.05s
```

### Finding 2

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_returns_false_for_definite_non_admin_member tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_forbidden_membership_lookup_as_unavailable tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_ambiguous_bad_request_as_unavailable tests\test_membership.py::TelegramAdminNotifierTests::test_bot_removal_during_admin_lookup_preserves_manager_binding -q
3 failed, 1 passed
```

The forbidden and bad-request lookups did not raise an unavailable result, and notifier processing deleted the manager binding.

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_returns_false_for_definite_non_admin_member tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_forbidden_membership_lookup_as_unavailable tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_ambiguous_bad_request_as_unavailable tests\test_membership.py::TelegramAdminNotifierTests::test_bot_removal_during_admin_lookup_preserves_manager_binding tests\test_permissions.py::PermissionServiceTests::test_each_management_action_rechecks_live_permission -q
5 passed in 2.54s
```

### Finding 3

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_database.py::DatabaseTests::test_finalize_batch_preserves_concurrent_access_loss_pause_for_recovery -q
1 failed: expected ('paused', 0, pause reason), got ('active', 0, None)
```

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_database.py::DatabaseTests::test_finalize_batch_preserves_concurrent_access_loss_pause_for_recovery tests\test_database.py::DatabaseTests::test_finalize_batch_activates_channel_after_normal_success -q
2 passed in 2.54s
```

### Finding 4

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_membership.py::ChatMembershipServiceTests::test_binding_gateway_network_failure_is_contained_and_audited_once tests\test_membership.py::ChatMembershipServiceTests::test_capability_recheck_retry_failure_is_contained_without_pause_or_second_audit -q
2 failed: TelegramNetworkError and TelegramRetryAfter escaped
```

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_membership.py::ChatMembershipServiceTests::test_binding_gateway_network_failure_is_contained_and_audited_once tests\test_membership.py::ChatMembershipServiceTests::test_capability_recheck_retry_failure_is_contained_without_pause_or_second_audit -q
2 passed in 2.64s
```

### Finding 5

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_maintenance.py::PendingCleanupLoopTests::test_app_drains_tracked_updates_added_during_shutdown_before_closing_resources tests\test_maintenance.py::PendingCleanupLoopTests::test_update_task_drain_collects_and_logs_handler_exceptions -q
2 failed: albums flushed before the blocked handler; drain adapter was absent
```

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_maintenance.py::PendingCleanupLoopTests::test_app_drains_tracked_updates_added_during_shutdown_before_closing_resources tests\test_maintenance.py::PendingCleanupLoopTests::test_update_task_drain_collects_and_logs_handler_exceptions -q
2 passed in 2.33s
```

### Finding 6

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_domain.py::ButtonSpecTests::test_accepts_case_insensitive_prefix_and_stores_trimmed_url tests\test_domain.py::ButtonSpecTests::test_rejects_opaque_or_authorityless_supported_schemes tests\test_handlers.py::HandlerHelpersTests::test_parse_button_batch_enforces_strict_url_prefixes_and_normalizes_valid_url -q
12 failed, 2 passed, 2 subtests passed
```

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_domain.py::ButtonSpecTests::test_accepts_case_insensitive_prefix_and_stores_trimmed_url tests\test_domain.py::ButtonSpecTests::test_rejects_opaque_or_authorityless_supported_schemes tests\test_handlers.py::HandlerHelpersTests::test_parse_button_batch_enforces_strict_url_prefixes_and_normalizes_valid_url -q
3 passed, 13 subtests passed in 2.30s
```

### Finding 7

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_pending_drafts.py::PendingDraftServiceTests::test_successful_discard_deletes_storage_items_and_completes_cleanup_row tests\test_pending_drafts.py::PendingDraftServiceTests::test_failed_discard_deletion_keeps_terminal_row_retryable -q
1 failed, 1 passed: successful discard still returned a terminal row
```

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_pending_drafts.py::PendingDraftServiceTests::test_successful_discard_deletes_storage_items_and_completes_cleanup_row tests\test_pending_drafts.py::PendingDraftServiceTests::test_failed_discard_deletion_keeps_terminal_row_retryable -q
2 passed in 0.25s
```

### Finding 8

Documentation-only change. Before editing, `rg` identified README line 87 as the remaining stale statement. After editing, the stale phrase `自动保存为个人草稿` has no README match.

## Verification

Changed-subsystem regression:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_permissions.py tests\test_aiogram_gateway.py tests\test_membership.py tests\test_database.py tests\test_maintenance.py tests\test_domain.py tests\test_handlers.py tests\test_pending_drafts.py -q
91 passed, 13 subtests passed in 8.83s
```

Fresh full suite:

```text
.\.venv\Scripts\python.exe -m pytest -q
125 passed, 13 subtests passed in 10.16s
```

Compilation:

```text
.\.venv\Scripts\python.exe -m compileall -q src tests
exit code 0; no output
```

Task 8 registration check:

```text
.\.venv\Scripts\python.exe -c "from aiogram import Dispatcher, Router; d=Dispatcher(); r=Router(); r.my_chat_member.register(lambda event: None); d.include_router(r); assert 'my_chat_member' in d.resolve_used_update_types(); print('my_chat_member OK')"
my_chat_member OK
```

## Files changed

Production/documentation:

- `README.md`
- `src/bottom_post_bot/aiogram_gateway.py`
- `src/bottom_post_bot/app.py`
- `src/bottom_post_bot/channels.py`
- `src/bottom_post_bot/database.py`
- `src/bottom_post_bot/domain.py`
- `src/bottom_post_bot/membership.py`
- `src/bottom_post_bot/pending_drafts.py`
- `src/bottom_post_bot/permissions.py`
- `src/bottom_post_bot/repositories.py`

Tests:

- `tests/test_aiogram_gateway.py`
- `tests/test_aiogram_handlers.py`
- `tests/test_database.py`
- `tests/test_domain.py`
- `tests/test_handlers.py`
- `tests/test_maintenance.py`
- `tests/test_membership.py`
- `tests/test_pending_drafts.py`
- `tests/test_permissions.py`

## Self-review

- Storage exclusion is enforced centrally with no persistence side effects and remains duplicated as a cheap membership fast path.
- Permission unavailability is distinguishable from definite non-admin; the only unbind branch is still the explicit `False` branch.
- Publish finalization and membership pause transactions are race-safe under SQLite serialization: either the finalizer sees `paused`, or a later pause wins.
- Membership failures retain discovery data, do not create manager associations, produce one failure audit per event, and do not pause on transient ambiguity.
- Concurrent update handling remains enabled (`handle_as_tasks=True` is unchanged); only shutdown intake-to-resource ordering changed.
- The update drain handles tasks added during the await and does not cancel healthy handlers.
- URL validation remains centralized in `ButtonSpec`, so repository loading and batch input use the same invariant.
- Successful discard removes the terminal row; failed deletion remains compatible with the existing periodic retry loop.
- Existing high-slot-to-low-slot publishing, immutable revisions, channel sharing, and `my_chat_member` registration remain covered by the full suite.

## Concerns

- The shutdown adapter intentionally relies on aiogram 3.30's private `_handle_update_tasks` set because aiogram exposes no public drain hook. The dependency is isolated to one helper and covered by a lifecycle regression, but an aiogram upgrade should revalidate that internal attribute and callback behavior.
- No Git diff/status/commit evidence is available because `.git` is empty by design; no repository metadata was initialized.

---

## Final re-review follow-up

Date: 2026-07-20  
Status: COMPLETE

The final re-review identified one legacy-schema compatibility gap and one permission-failure observability gap. Both were handled with new focused RED/GREEN cycles.

### 9. Schema migration 5 quarantines legacy-invalid button URLs

- Extracted pure `normalize_button_url` in `domain.py`; `ButtonSpec` now delegates to it, so runtime validation and migration classification cannot drift.
- Added transactional schema migration 5 and advanced the current schema version expectation from 4 to 5.
- Migration 5 creates `quarantined_draft_buttons` with the original button ID, revision ID, row, column, text, URL, reason, and quarantine timestamp.
- It reads legacy `draft_buttons` in ID order, validates each URL with `normalize_button_url`, copies invalid rows into quarantine, deletes them from the active table, and inserts schema version 5 in the same explicit `BEGIN IMMEDIATE` transaction.
- The quarantine table deliberately has no foreign key back to a live revision, preserving recoverability even if the source draft is later deleted.
- Integration coverage starts from a version-4 database containing valid `HTTPS://...`, invalid `https:foo`, and invalid `tg:foo` rows. After reopening, schema 5 is present, both invalid rows and all original metadata are quarantined, the valid row remains, and `get_draft`, `list_channel_slots`, and scheduler-facing `load_publish_state` all load without raising.
- Rollback coverage injects a failure after the first invalid row has been quarantined/deleted. The transaction restores both original rows, leaves schema version 4, and removes the newly created quarantine table.

Migration RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_database.py::DatabaseTests::test_migration_enables_foreign_keys_and_wal tests\test_database.py::DatabaseTests::test_migration_four_backfills_slot_names_from_existing_drafts tests\test_database.py::DatabaseTests::test_migration_five_quarantines_legacy_invalid_urls_without_breaking_active_loads tests\test_database.py::DatabaseTests::test_migration_five_rolls_back_quarantine_and_deletion_when_validation_fails -q
4 failed in 3.01s
```

The database remained at schema 4, migration 5 was absent, and no shared canonical URL helper existed.

Migration/domain GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_database.py::DatabaseTests::test_migration_enables_foreign_keys_and_wal tests\test_database.py::DatabaseTests::test_migration_four_backfills_slot_names_from_existing_drafts tests\test_database.py::DatabaseTests::test_migration_five_quarantines_legacy_invalid_urls_without_breaking_active_loads tests\test_database.py::DatabaseTests::test_migration_five_rolls_back_quarantine_and_deletion_when_validation_fails tests\test_domain.py::ButtonSpecTests -q
9 passed, 7 subtests passed in 3.05s
```

### 10. Preserve Telegram lookup failure provenance without exposing it to users

- `BotApiPermissionGateway.user_is_admin` now wraps failures as `PermissionUnavailable(f"{ExceptionClass}: {message}")` and retains `raise ... from exc` chaining.
- `channel.auto_bind_failed` therefore records both the wrapper type (`PermissionUnavailable`) and the exact underlying Telegram exception class/message.
- Added `PermissionUnavailable.public_message` and centralized handler formatting so manual messages and callback alerts continue to show only the existing safe retry guidance.
- Regression coverage verifies forbidden and ambiguous bad-request provenance plus exception causes, exact audit detail content, and non-disclosure in the private-chat binding flow.

Observability RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_forbidden_membership_lookup_as_unavailable tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_ambiguous_bad_request_as_unavailable tests\test_membership.py::ChatMembershipServiceTests::test_permission_unavailable_audit_preserves_underlying_lookup_type_and_message tests\test_aiogram_handlers.py::PendingDraftConfirmationHandlerTests::test_permission_unavailable_diagnostics_are_not_exposed_to_user -q
4 failed in 2.58s
```

The wrapper/audit contained only the safe generic text, while a diagnostic exception supplied to the handler would have been displayed verbatim.

Observability GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_forbidden_membership_lookup_as_unavailable tests\test_aiogram_gateway.py::BotApiGatewayTests::test_permission_gateway_surfaces_ambiguous_bad_request_as_unavailable tests\test_membership.py::ChatMembershipServiceTests::test_permission_unavailable_audit_preserves_underlying_lookup_type_and_message tests\test_aiogram_handlers.py::PendingDraftConfirmationHandlerTests::test_permission_unavailable_diagnostics_are_not_exposed_to_user -q
4 passed in 2.46s
```

### Follow-up verification

Combined migration/domain/database/membership/adapter regressions:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_database.py tests\test_domain.py tests\test_membership.py tests\test_aiogram_gateway.py tests\test_aiogram_handlers.py -q
79 passed, 7 subtests passed in 7.79s
```

Fresh final full suite (supersedes the earlier 125-test result above):

```text
.\.venv\Scripts\python.exe -m pytest -q
129 passed, 13 subtests passed in 11.08s
```

Compilation:

```text
.\.venv\Scripts\python.exe -m compileall -q src tests
exit code 0; no output
```

Registration check:

```text
.\.venv\Scripts\python.exe -c "from aiogram import Dispatcher, Router; d=Dispatcher(); r=Router(); r.my_chat_member.register(lambda event: None); d.include_router(r); assert 'my_chat_member' in d.resolve_used_update_types(); print('my_chat_member OK')"
my_chat_member OK
```

### Follow-up concerns

- No new concern from migration 5: invalid legacy rows remain fully recoverable in quarantine, and the rollback test covers a failure after mutation has begun.
- The previously documented aiogram private-task-set upgrade concern remains unchanged.
- No Git operations were performed; `.git` remains intentionally empty.

---

## Parser-level URL validation follow-up

Date: 2026-07-20  
Status: COMPLETE

### 11. Convert URL parser failures into canonical validation failures

- Added a domain regression for malformed authority input `https://[`.
- Extended the version-4 migration integration fixture with the same malformed URL alongside `https:foo` and `tg:foo`.
- `normalize_button_url` now catches `ValueError` raised internally by `urllib.parse.urlsplit` and re-raises the standard `ValidationError("button URL must use https://, http:// or tg://")` with exception chaining.
- Because migration 5 already classifies `ValidationError` using this shared helper, the malformed row is quarantined with its original metadata and reason instead of aborting database startup.
- The integration regression still proves that the valid button remains loadable through the draft, slot, and scheduler-facing publish-state paths. The injected migration rollback regression also remains green.

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_domain.py::ButtonSpecTests::test_parser_level_url_error_uses_standard_validation_error tests\test_database.py::DatabaseTests::test_migration_five_quarantines_legacy_invalid_urls_without_breaking_active_loads -q
2 failed, 1 error in 2.70s
```

The raw `ValueError: Invalid IPv6 URL` escaped both `ButtonSpec` and migration 5, blocking startup before quarantine completed.

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_domain.py::ButtonSpecTests::test_parser_level_url_error_uses_standard_validation_error tests\test_database.py::DatabaseTests::test_migration_five_quarantines_legacy_invalid_urls_without_breaking_active_loads tests\test_database.py::DatabaseTests::test_migration_five_rolls_back_quarantine_and_deletion_when_validation_fails -q
3 passed in 2.76s
```

### Final verification refresh

Fresh full suite (supersedes prior full-suite counts):

```text
.\.venv\Scripts\python.exe -m pytest -q
130 passed, 13 subtests passed in 11.42s
```

Compilation:

```text
.\.venv\Scripts\python.exe -m compileall -q src tests
exit code 0; no output
```

Registration check:

```text
.\.venv\Scripts\python.exe -c "from aiogram import Dispatcher, Router; d=Dispatcher(); r=Router(); r.my_chat_member.register(lambda event: None); d.include_router(r); assert 'my_chat_member' in d.resolve_used_update_types(); print('my_chat_member OK')"
my_chat_member OK
```

Concerns remain unchanged: only the previously documented aiogram private `_handle_update_tasks` upgrade compatibility risk remains. No Git operations were performed.
