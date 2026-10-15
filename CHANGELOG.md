# Changelog

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
