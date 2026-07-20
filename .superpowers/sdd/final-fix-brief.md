# Final Review Fix Brief

Address every final-review finding below in one cohesive fix wave. Follow strict TDD: add a focused failing regression for each behavior, record RED, implement the smallest safe fix, record focused GREEN, then run the complete suite and compilation once.

## 1. Central storage-channel exclusion

Manual binding currently allows `STORAGE_CHANNEL_ID`. Enforce exclusion centrally so both manual and automatic paths cannot bind the private storage channel as a target. Prefer injecting the storage channel ID into `ChannelService` and rejecting it before permission resolution/upsert/bind. Keep the membership fast-path exclusion. Add manual binding/service tests and verify no channel/manager/audit record is created by the rejected manual path.

## 2. Preserve managers when the bot cannot inspect membership

`BotApiPermissionGateway.user_is_admin` currently maps forbidden and all bad requests to `False`, causing `PermissionService` and notifications to unbind valid managers when the bot itself lost access. Introduce a three-way outcome or a domain exception that distinguishes definite non-admin from unavailable/ambiguous lookup. Only definite non-admin results may remove a manager association. Bot-inaccessible/network/API errors must preserve bindings and surface a permission/unavailable result. Add tests for definite non-admin, bot forbidden, ambiguous bad request, and notifier behavior after bot removal.

## 3. Prevent stale publish finalization from clearing pause

`finalize_batch` must not unconditionally set a channel back to active after a concurrent access-loss pause. Make finalization preserve `paused` status (a conditional SQL update is acceptable) and add an integration regression that pauses between batch start and finalization, then asserts status remains paused and recovery UI remains available. Preserve normal successful finalization behavior.

## 4. Contain and audit membership Telegram API failures

Wrap the complete administrator binding and secondary capability-check flow so `TelegramAPIError` subclasses, including retry/network/server errors, do not escape. Keep the discovered channel record, write exactly one `channel.auto_bind_failed` audit, do not create a manager association, and do not pause on ambiguous transient errors. Add focused tests for a binding gateway network/retry failure and a capability recheck failure.

## 5. Drain in-flight aiogram update tasks before resource closure

With `handle_as_tasks=True`, aiogram does not await all outstanding update tasks before `start_polling` returns. Add a small dispatcher/lifecycle adapter that drains the dispatcher's tracked update-task set after intake stops and before album flush, background-loop shutdown, bot-session close, and database close. Avoid changing to globally sequential update handling. Do not close/cancel healthy in-flight handlers prematurely; await completion with exception collection/logging. Add a lifecycle test using a blocked update task that proves database/session closure occurs only after the handler is released and finished. Account for tasks that are added while the drain begins by looping until the tracked set is empty.

## 6. Strict URL prefixes

`ButtonSpec` must accept only URLs beginning case-insensitively with exactly `https://`, `http://`, or `tg://`. Reject `https:foo`, `http:foo`, `tg:foo`, empty authority for HTTP(S), surrounding whitespace tricks, and unsupported schemes. Store the trimmed valid URL or require callers to pass the normalized value consistently. Add domain and batch-parser regressions.

## 7. Complete successful discard immediately

After `PendingDraftService.discard` successfully deletes storage messages (including already-missing behavior handled by the gateway), call `complete_pending_cleanup` immediately. Retain terminal rows only when deletion fails. Update tests so successful discard leaves no pending row and failure remains retryable.

## 8. Correct README forwarding text

Replace the remaining statement that forwarding automatically saves a personal draft. It must say forwarding creates a 10-minute pending confirmation with Save, Save and Name, and Discard actions.

## Verification

- Run focused tests for every changed subsystem and record RED/GREEN evidence.
- Run `\.venv\Scripts\python.exe -m pytest -q`.
- Run `\.venv\Scripts\python.exe -m compileall -q src tests`.
- Run the aiogram `my_chat_member` registration check from Task 8.
- Do not initialize Git or commit; `.git` is empty.
- Write a complete report to `.superpowers/sdd/final-fix-report.md` with files changed, RED/GREEN evidence, full verification, and self-review.
