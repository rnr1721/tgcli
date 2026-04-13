# Telegram Tool

A command-line utility for Telegram built on top of Telethon.  
Designed for shell usage without any interactive interface.

---

## Installation

**Dependencies:**
```bash
pip install telethon
```

**Files** (all in the same directory by default):
```
telegram_tool.py            # the script itself
telegram_config.json        # api_id and api_hash (created on first run or via auth:init)
telegram_session.session    # Telegram session (created after auth)
telegram_auth_state.json    # temporary state during non-interactive auth (auto-deleted)
```

---

## First Run

### 1. Get API credentials

Go to https://my.telegram.org → log in → **API development tools** → create an application.  
You need: `api_id` (a number) and `api_hash` (a string).

### 2. Authorization

Choose one of the two flows below.

---

## Authorization

### Interactive (terminal, one time)

Run the full wizard — it will prompt for everything step by step:

```bash
python3 telegram_tool.py auth
```

The wizard asks for:
- `api_id` and `api_hash` — if config doesn't exist yet
- Phone number
- Telegram confirmation code
- Two-factor authentication password (if enabled)

After that the session is saved — no need to authorize again.

---

### Non-interactive (scripts, web UIs, automation)

Use the step-by-step commands. Each step is a separate shell call with no prompts.

**Step 0 — save API credentials** (only if `telegram_config.json` doesn't exist yet):
```bash
python3 telegram_tool.py auth:init <api_id> <api_hash>
```

**Step 1 — send confirmation code to the phone:**
```bash
python3 telegram_tool.py auth:phone +19001234567
```

**Step 2 — submit the code received in Telegram:**
```bash
python3 telegram_tool.py auth:code 12345
```

**Step 3 — submit 2FA password** (only if your account has two-factor authentication):
```bash
python3 telegram_tool.py auth:password mysecretpassword
```

The temporary state between steps is stored in `telegram_auth_state.json` and is
deleted automatically after successful authorization.

---

## Data directory

By default, `telegram_config.json` and `telegram_session.session` are stored next to
the script. Set `TELEGRAM_DATA_DIR` to use a different location:

```bash
export TELEGRAM_DATA_DIR=/var/data/telegram
python3 telegram_tool.py auth:phone +19001234567
```

All subsequent calls must use the same `TELEGRAM_DATA_DIR` to find the session.

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

Output shows type icon, name, `@username` or id (for use in other commands), date, and a preview of the last message.

---

### `read` — read messages

```bash
python3 telegram_tool.py read @username
python3 telegram_tool.py read @channelname 30    # last 30 messages
python3 telegram_tool.py read 123456789          # by numeric id
```

Works with direct chats, groups, and channels.

---

### `send` — send a message

```bash
python3 telegram_tool.py send @username Hey, how are you?
python3 telegram_tool.py send 123456789 Message text here
```

Everything after the username/id is the message text — no quotes needed.

---

### `unread` — unread dialogs

```bash
python3 telegram_tool.py unread
python3 telegram_tool.py unread 10    # show up to 10
```

---

### `search` — search in a chat

```bash
python3 telegram_tool.py search @groupname some keyword
python3 telegram_tool.py search @channel python asyncio
```

Searches message text, returns up to 20 results.

---

### `info` — info about a user or channel

```bash
python3 telegram_tool.py info @username
python3 telegram_tool.py info @channelname
python3 telegram_tool.py info 123456789
```

Shows name, username, id, member count (for channels/groups), and description.

---

### `mark_read` — mark as read

```bash
python3 telegram_tool.py mark_read @username
```

---

### `me` — current account

```bash
python3 telegram_tool.py me
```

---

### `config` — check config

```bash
python3 telegram_tool.py config
```

Shows data directory, file paths, api_id, masked api_hash, and session status.

---

## Addressing chats

Wherever `<@chat|id>` is expected, you can use:

| Format       | Example           | When to use                    |
|--------------|-------------------|-------------------------------|
| `@username`  | `@durov`          | if the chat has a username     |
| numeric id   | `123456789`       | if there's no username         |

To find a username or id, use `dialogs` — they appear in the third column.

---

## File structure

```
telegram_config.json        — { "api_id": 12345, "api_hash": "abc..." }
                              permissions 600 (owner only)

telegram_session.session    — binary Telethon session file
                              contains the auth token, keep it safe

telegram_auth_state.json    — temporary, only present during non-interactive auth
                              deleted automatically after successful sign-in
```

> ⚠️ Do not share `telegram_config.json` or `telegram_session.session` with anyone —  
> they provide full access to the account.

---

## Common errors

| Message | Cause | Fix |
|---|---|---|
| `Session not found` | Never authorized | `python3 telegram_tool.py auth` |
| `No pending auth state` | `auth:code` called before `auth:phone` | Run `auth:phone` first |
| `Too many requests` | Telegram FloodWait | Wait the indicated number of seconds |
| `Could not find the input entity` | Wrong username/id or no access | Check the address, make sure you're in the chat |
| `Please install telethon` | Library not installed | `pip install telethon` |
