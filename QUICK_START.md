# Telegram Tool — Quick Start for AI

This tool gives you access to a Telegram account via shell commands.
No interactive input is required during normal operation.

---

## Setup (done by a human, one time)

Credentials can come from the environment instead of a file:

```bash
export TELEGRAM_API_ID=12345678
export TELEGRAM_API_HASH=abc123...
```

### Option A — interactive (simplest)

```bash
python3 telegram_tool.py auth
```

### Option B — non-interactive (for scripts and web UIs)

```bash
# Step 0 — save API credentials (skip if the env vars above are set)
python3 telegram_tool.py auth:init <api_id> <api_hash>

# Step 1 — send code to the phone
python3 telegram_tool.py auth:phone +19001234567

# Step 2 — submit the code from Telegram
python3 telegram_tool.py auth:code 12345

# Step 3 — only if the account has 2FA enabled
python3 telegram_tool.py auth:password mysecretpassword
```

After either flow, all commands below work without further interaction.

---

## Data directory

```bash
export TELEGRAM_DATA_DIR=/shared/telegram
```

Use the same value for every call.

---

## How to use

### What's new since I last checked
```bash
python3 telegram_tool.py updates
python3 telegram_tool.py updates --kind users     # direct messages only
```
The very first call only takes a baseline and reports nothing.
Each later call shows messages that arrived since the previous one.

### Check unread messages
```bash
python3 telegram_tool.py unread
```

### List dialogs
```bash
python3 telegram_tool.py dialogs
python3 telegram_tool.py dialogs 50 channels
python3 telegram_tool.py dialogs 50 groups
python3 telegram_tool.py dialogs 50 users
```

### List saved contacts
```bash
python3 telegram_tool.py contacts
```

### Find who to write to (by name, username, or id)
```bash
python3 telegram_tool.py resolve Alena
python3 telegram_tool.py resolve @username
```
Searches contacts and all dialogs. Prints candidates with a match count.

### Read a conversation
```bash
python3 telegram_tool.py read @username
python3 telegram_tool.py read @username 30
python3 telegram_tool.py read -1001820894363 5
```
Media shows as `(voice 0:12)`, `(photo) caption`, `(document)`.

### Get a voice message, photo or file
```bash
python3 telegram_tool.py download @username <message_id>
```
Message ids are shown by `updates` as `[msg 4215]`. Prints the saved path.

### Send a message
```bash
python3 telegram_tool.py send @username Hello, how are you?
```
Everything after the address is the message.

### Search in a chat
```bash
python3 telegram_tool.py search @username keyword
```

### Get info about a user or channel
```bash
python3 telegram_tool.py info @username
```

### Mark a dialog as read
```bash
python3 telegram_tool.py mark_read @username
```

### Check current account
```bash
python3 telegram_tool.py me
```

---

## Important notes

- Before sending to someone, use `resolve` to confirm they exist and get the exact address
- If `resolve` returns `matches: 0`, that target isn't in the account — don't guess an address
- If a chat has no @username, use its id exactly as shown (e.g. `-1001820894363`)
- `@username` and ids work for read, send, search, info, mark_read and download
- `updates` does not mark anything as read; `mark_read` does
- Do not run `auth` — it requires a terminal and is for the human doing setup
- `[WAIT] Too many requests, wait N sec.` — Telegram rate limit; wait that long before retrying
- `[ERROR] Session is busy` — another call is using the account right now; retry shortly
- Add `--json` before the command for machine-readable output
