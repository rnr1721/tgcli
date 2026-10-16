# Changelog

## 1.1.3

### Added
- QR login, for when Telegram won't deliver login codes to third-party
  clients: `auth:qr` starts a detached `auth:qr:serve` process that holds the
  connection, refreshes the token as it expires and records progress in
  `telegram_qr.json`; `auth:qr:status` reads it (no network, no lock) —
  `waiting` with a fresh `tg://login` link, `authorized`, or
  `next_step: password` for 2FA accounts. `auth:qr:cancel` stops it;
  `auth:phone` cancels a pending one. `TELEGRAM_QR_TTL` (default 180 s).

## 1.1.2

### Added
- `auth:phone` reports **where the code was sent**: `code: {delivery, hint,
  length, next_delivery, resend_after, email_pattern}`. Delivery is one of
  `app` (in-app message from "Telegram", id 777000), `sms`, `call`,
  `flash_call`, `missed_call`, `email`, `setup_email_required`, `fragment`.
  The interactive `auth` wizard prints it too.
- `auth:resend`: ask Telegram to deliver the pending code the next way
  (usually SMS or a call) — for "the code never came".

## 1.1.0

### Added
- `--json` global flag (and `TGCLI_JSON=1`): every command prints exactly one
  JSON object, `{"ok": true, ...}` or `{"ok": false, "error", "code"}`.
- Stable exit codes: 0 ok, 1 error, 2 usage, 3 auth, 4 flood wait, 5 session busy.
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` environment variables, taking
  precedence over `telegram_config.json`. With them, `auth:init` is not needed.
- `updates` command: new messages since the last call, tracked by a per-dialog
  message-id cursor instead of the unread flag. Baseline on first run,
  state file or caller-managed cursor, `--limit`, `--per-dialog`, `--kind`,
  `--include-out`, `--no-commit`, `--reset`.
- `download` command: save a message's media (voice, photo, document, …),
  size-capped by `TELEGRAM_DOWNLOAD_MAX_MB`.
- Media is described in output: `(voice 0:12)`, `(photo) caption`; in JSON as
  `media: {type, mime, size, name, duration}`.
- Session lock: concurrent tgcli calls on one data dir wait for each other
  (`TELEGRAM_LOCK_TIMEOUT`) instead of failing with "database is locked".
- `auth:*` steps report `next_step` in JSON (`code`, `password`, or null).
- `--version`; `config` shows where credentials come from.
- Test suite (`tests/`).

### Changed
- Ids printed for groups and channels are now *marked* peer ids
  (`-1001820894363`), which resolve directly. Bare ids are still accepted.
- Errors inside commands now exit non-zero (previously many printed
  `[ERROR]` and exited 0). Text-mode messages keep the `[ERROR]` / `[WARN]` /
  `[WAIT]` prefixes.
- Bots are shown as `[bot]` instead of `[user]`.

### Fixed
- A session file that exists but was never authorized no longer drops into
  Telethon's interactive prompt; it fails with `not_authorized`.
- `auth` and first-run setup no longer crash with EOFError when there is no
  terminal; they fail with a clear message.
- `send` passes the resolved entity instead of a bare id, fixing sends to
  chats not in Telethon's entity cache.
- `resolve_entity` keeps the sign of numeric ids, so marked channel ids resolve
  without a full dialog scan.
- Output is forced to UTF-8 regardless of locale; Telethon warnings no longer
  leak into stdout when callers merge stderr.

## 1.0.1 and earlier
- See git history.
