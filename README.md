# Telegram Tool (tgcli)

A command-line utility for Telegram built on top of Telethon.
Designed for shell usage without any interactive interface — by people,
scripts, and AI agents alike.

- Plain-text output by default, one JSON object with `--json`
- Stable exit codes
- Credentials from environment or config file
- Safe to call concurrently on one session (file lock)
- `updates` — "what's new since last time", driven by a cursor, not the unread flag

---

## Installation

**Dependencies:**
```bash
pip install telethon
```

**Files** (all in the same directory by default, see [Data directory](#data-directory)):
```
telegram_tool.py            # the script itself
telegram_config.json        # api_id and api_hash (optional if you use env vars)
telegram_session.session    # Telegram session (created after auth)
telegram_auth_state.json    # temporary state during non-interactive auth (auto-deleted)
updates_state.json          # cursor for `updates` (created on first run)
telegram_session.lock       # lock file for concurrent calls
downloads/                  # default target for `download`
```

---

## First Run

### 1. Get API credentials

Go to https://my.telegram.org → log in → **API development tools** → create an application.
You need: `api_id` (a number) and `api_hash` (a string).

### 2. Provide them — either way

**Environment** (nothing written to disk; takes precedence over the file):
```bash
export TELEGRAM_API_ID=12345678
export TELEGRAM_API_HASH=abc123...
```

**Config file:**
```bash
python3 telegram_tool.py auth:init <api_id> <api_hash>
```

### 3. Authorize — choose one of the flows below.

---

## Authorization

### Interactive (terminal, one time)

```bash
python3 telegram_tool.py auth
```

Asks for credentials (if none are set), phone number, the Telegram code, and
the 2FA password if enabled. Requires a terminal — from scripts use the
non-interactive flow.

### Non-interactive (scripts, web UIs, automation)

Each step is a separate shell call with no prompts.

```bash
python3 telegram_tool.py auth:init <api_id> <api_hash>   # step 0 — only without env vars
python3 telegram_tool.py auth:phone +19001234567         # step 1 — sends the code
python3 telegram_tool.py auth:code 12345                 # step 2
python3 telegram_tool.py auth:password mysecretpassword  # step 3 — only with 2FA
```

With `--json`, every step reports what comes next:

```json
{"ok": true, "authorized": false, "next_step": "code", "phone": "+19001234567"}
{"ok": true, "authorized": false, "next_step": "password"}
{"ok": true, "authorized": true, "next_step": null, "user": {"id": 1, "first_name": "…", "username": "…"}}
```

The temporary state between steps lives in `telegram_auth_state.json` and is
deleted after successful authorization.

`auth:phone` also says **where the code went** — `code.delivery` is `app`
(a message from "Telegram", id 777000, in a session where the account is
logged in), `sms`, `call`, `email` (with `email_pattern`), `fragment`,
`setup_email_required`… If it doesn't arrive, `auth:resend` asks for the next
route when Telegram offers one (`code.next_delivery`, `code.resend_after`).

### QR login (when codes don't arrive)

Telegram sometimes answers "code sent to the app" for third-party clients and
never delivers it. QR login needs no code: a phone where the account is
already logged in scans a token.

```bash
python3 telegram_tool.py auth:qr          # starts a background waiter, prints tg://login?token=…
# scan it: Telegram → Settings → Devices → Link Desktop Device
python3 telegram_tool.py auth:qr:status   # waiting (fresh link) / authorized / next_step=password
python3 telegram_tool.py auth:password …  # only with 2FA
python3 telegram_tool.py auth:qr:cancel   # give up
```

The token is valid ~30 s; the background process (`auth:qr:serve`) refreshes it
and keeps the connection open while you scan, for up to `TELEGRAM_QR_TTL`
seconds. Render the link as a QR code yourself (e.g. `qrencode -t ansiutf8 'tg://…'`).
It holds the session lock while waiting; `auth:phone` cancels it.

---

## Data directory

By default all files are stored next to the script. Set `TELEGRAM_DATA_DIR` to
use another location — one directory per account:

```bash
export TELEGRAM_DATA_DIR=/var/data/telegram
python3 telegram_tool.py auth:phone +19001234567
```

All subsequent calls must use the same `TELEGRAM_DATA_DIR`.

---

## Commands

### `dialogs` — list dialogs

```bash
python3 telegram_tool.py dialogs
python3 telegram_tool.py dialogs 50              # up to 50 dialogs
python3 telegram_tool.py dialogs 20 channels     # channels only
python3 telegram_tool.py dialogs 20 groups       # groups only
python3 telegram_tool.py dialogs 20 users        # direct chats only
```

Shows type, name, address (`@username` or peer id), date, unread count and a
preview of the last message.

### `contacts` — list saved contacts

```bash
python3 telegram_tool.py contacts
```

### `resolve` — find a real target by name, username or id

```bash
python3 telegram_tool.py resolve Alena
python3 telegram_tool.py resolve @durov
python3 telegram_tool.py resolve 123456789
```

Searches **both** contacts and all dialogs and prints candidates with an
explicit match count:

```
[resolve] query: "alena" — matches: 2

  1. [user] Alena Petrova                id: 12345         @alena_p  [contact]
  2. [user] Alena                        id: 67890         (no username)  [dialog]
```

`matches: 0` means the target does not exist in this account.

### `read` — read messages

```bash
python3 telegram_tool.py read @username
python3 telegram_tool.py read @channelname 30    # last 30 messages
python3 telegram_tool.py read -1001820894363 5   # by peer id
```

Media is shown inline: `(voice 0:12)`, `(photo) caption text`, `(document)`.

### `send` — send a message

```bash
python3 telegram_tool.py send @username Hey, how are you?
python3 telegram_tool.py send 123456789 Message text here
```

Everything after the address is the message text.

### `unread` — dialogs with unread messages

```bash
python3 telegram_tool.py unread
python3 telegram_tool.py unread 10
```

### `search` — search in a chat

```bash
python3 telegram_tool.py search @groupname some keyword
```

Returns up to 20 results.

### `info` — info about a user, group or channel

```bash
python3 telegram_tool.py info @username
```

### `mark_read` — mark as read

```bash
python3 telegram_tool.py mark_read @username
```

### `updates` — new messages since the last call

```bash
python3 telegram_tool.py updates
python3 telegram_tool.py updates --kind users
python3 telegram_tool.py --json updates --cursor '{"v":1,"since":"…","peers":{…}}'
```

The first run takes a **baseline** and reports nothing — otherwise it would
replay the whole account. Every later run reports messages that arrived since.

Progress is tracked per dialog by message id, **not** by the unread flag, so
reading the account on your phone doesn't hide messages from `updates`, and
`updates` never marks anything as read.

| Option | Meaning |
|---|---|
| *(default)* | Cursor in `<data dir>/updates_state.json`, advanced automatically |
| `--state FILE` | Cursor in a custom file, advanced automatically |
| `--cursor JSON` | Caller-managed cursor: nothing is written; the new cursor is in the output |
| `--no-commit` | Report, but don't advance the state file (peek) |
| `--reset` | Drop the stored cursor and take a new baseline |
| `--limit N` | Soft cap on reported messages (default 100) |
| `--per-dialog N` | Max messages taken from one dialog (default 20) |
| `--kind K` | `users`, `groups` or `channels` only |
| `--include-out` | Also report messages sent from this account |

Details:
- `--limit` is checked between dialogs. When it's hit, the output says
  `truncated: true` and the next run continues with the remaining dialogs.
- A dialog with more than `--per-dialog` new messages gets its newest N; the
  older ones are skipped and the dialog is listed in `overflow`.
- Dialogs excluded by `--kind`, and your own outgoing messages, advance the
  cursor silently.

**At-least-once delivery** for programs: use `--cursor`, process the messages,
and only then persist the returned cursor. A crash between the two replays the
batch instead of losing it.

Each message in JSON output:

```json
{
  "id": 4215,
  "peer_id": 123456789,
  "date": "2026-10-15T12:34:56+00:00",
  "out": false,
  "text": "hi",
  "sender_id": 123456789,
  "sender": {"peer_id": 123456789, "id": 123456789, "kind": "user", "name": "Alena", "username": "alena_p"},
  "reply_to": null,
  "mentioned": false,
  "media": {"type": "voice", "mime": "audio/ogg", "size": 12345, "duration": 3},
  "chat": {"peer_id": 123456789, "id": 123456789, "kind": "user", "name": "Alena", "username": "alena_p"},
  "private": true
}
```

`kind` is one of `user`, `bot`, `group`, `channel`. `private` is true for
direct chats with a user or bot.

### `download` — download a message's media

```bash
python3 telegram_tool.py download @username 4215               # → <data dir>/downloads/
python3 telegram_tool.py download @username 4215 ./voice.ogg   # → exact file
python3 telegram_tool.py download @username 4215 ./media/      # → into a directory
```

Prints the saved path. Files over `TELEGRAM_DOWNLOAD_MAX_MB` (default 50) are
refused with code `too_large`; set it to 0 to disable the cap.

### `me` — current account

```bash
python3 telegram_tool.py me
```

### `config` — check config

```bash
python3 telegram_tool.py config
```

Shows version, data directory, where the credentials come from (`env` /
`file` / `missing`), masked api_hash, and whether a session file exists.

---

## JSON output

Put `--json` **before** the command (or set `TGCLI_JSON=1`):

```bash
python3 telegram_tool.py --json dialogs 10
```

Every call prints exactly one JSON object to stdout:

```json
{"ok": true, ...command data...}
{"ok": false, "error": "human-readable message", "code": "not_found"}
```

Dates are ISO 8601 in UTC. Peer ids are *marked* ids (users positive, groups
negative, channels and supergroups `-100…`), which every command accepts as an
address.

Error codes: `usage`, `bad_config`, `no_config`, `no_session`,
`not_authorized`, `no_auth_state`, `not_found`, `no_media`, `too_large`,
`download_failed`, `bad_state`, `flood_wait` (with `retry_after` seconds),
`session_busy`, `error`.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Error (network, Telegram error, target not found, …) |
| 2 | Usage error (bad arguments, bad config values) |
| 3 | No credentials, no session, or session not authorized |
| 4 | Telegram FloodWait — retry after `retry_after` seconds |
| 5 | Session busy — another tgcli process held the lock too long |

---

## Concurrency

Telethon stores the session in SQLite, and two processes on one session fail
with `database is locked`. tgcli takes an exclusive lock on
`telegram_session.lock` for every command that touches the session and waits up
to `TELEGRAM_LOCK_TIMEOUT` seconds (default 30) before giving up with exit
code 5. A background `updates` poller and on-demand `send` calls can therefore
share one account safely.

---

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_DATA_DIR` | script directory | Config, session and state files |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | Credentials; override the config file. Set both or neither |
| `TELEGRAM_LOCK_TIMEOUT` | 30 | Seconds to wait for a busy session |
| `TELEGRAM_DOWNLOAD_MAX_MB` | 50 | Download cap, 0 = unlimited |
| `TELEGRAM_QR_TTL` | 180 | How long a QR login waits for a scan, seconds |
| `TGCLI_JSON` | — | `1` = same as `--json` |

---

## Addressing chats

Wherever `<@chat|id>` is expected, you can use:

| Format | Example | When to use |
|---|---|---|
| `@username` | `@durov` | if the chat has a username |
| peer id | `-1001820894363` | always works; shown by `dialogs`, `resolve`, `updates` |
| bare id | `1820894363` | older form, still accepted (resolved via dialogs) |

---

## File structure

```
telegram_config.json        — { "api_id": 12345, "api_hash": "abc..." }, mode 600
telegram_session.session    — binary Telethon session; contains the auth token
telegram_auth_state.json    — temporary, only during non-interactive auth
updates_state.json          — cursor for `updates`, mode 600
telegram_session.lock       — empty lock file
```

> ⚠️ Do not share `telegram_config.json` or `telegram_session.session` with anyone —
> they provide full access to the account.

---

## Common errors

| Message | Cause | Fix |
|---|---|---|
| `Session not found` | Never authorized | `auth`, or `auth:phone` flow |
| `Session is not authorized` | Session file exists but sign-in never finished | Repeat `auth:phone` → `auth:code` |
| `API credentials not found` | No env vars and no config file | Set `TELEGRAM_API_ID`/`HASH` or `auth:init` |
| `No pending auth state` | `auth:code` called before `auth:phone` | Run `auth:phone` first |
| `Too many requests` | Telegram FloodWait | Wait the indicated number of seconds |
| `Session is busy` | Another tgcli call held the lock | Retry, or raise `TELEGRAM_LOCK_TIMEOUT` |
| `Target not found` | Wrong username/id or no access | Use `resolve` to find the right address |
| `matches: 0` | Target not in contacts or dialogs | Not an error — the target doesn't exist here |

---

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests cover the `updates` cursor logic against a fake client and the CLI
paths that need no network. See [CHANGELOG.md](CHANGELOG.md).
