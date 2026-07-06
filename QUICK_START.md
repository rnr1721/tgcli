# Telegram Tool — Quick Start for AI

This tool gives you access to Telegram via shell commands.
No interactive input is required during normal operation.

---

## Setup (done by human, one time)

### Option A — interactive (simplest)

Run in a terminal:

```bash
python3 telegram_tool.py auth
```

This saves credentials to `telegram_config.json` and creates `telegram_session.session`.

### Option B — non-interactive (for scripts and web UIs)

```bash
# Step 0 — save API credentials (skip if telegram_config.json already exists)
python3 telegram_tool.py auth:init <api_id> <api_hash>

# Step 1 — send code to the phone
python3 telegram_tool.py auth:phone +19001234567

# Step 2 — submit the code from Telegram
python3 telegram_tool.py auth:code 12345

# Step 3 — only if the account has 2FA enabled
python3 telegram_tool.py auth:password mysecretpassword
```

After any of the two flows, all commands below work without further interaction.

---

## Data directory

To store session files in a custom location, set `TELEGRAM_DATA_DIR`:

```bash
export TELEGRAM_DATA_DIR=/shared/telegram
python3 telegram_tool.py auth:phone +19001234567
```

Use the same variable for all subsequent calls.

---

## How to use

### Check unread messages
```bash
python3 telegram_tool.py unread
```

### List all dialogs
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
Use it to confirm a target exists before sending.

### Read a conversation
```bash
python3 telegram_tool.py read @username
python3 telegram_tool.py read @username 30
python3 telegram_tool.py read 1820894363 5
```

### Send a message
```bash
python3 telegram_tool.py send @username Hello, how are you?
```
Everything after the @username is the message. No quotes needed.

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

- Use `dialogs` to see existing chats, or `resolve <name>` to find a specific target
- Before sending to someone, use `resolve` to confirm they exist and get the exact address
- `resolve` searches both your contacts and all dialogs — people, groups, and channels
- If `resolve` returns `matches: 0`, that target isn't in the account — don't guess an address
- If a chat has no @username, use its numeric id instead (shown in `dialogs` output)
- Both @username and numeric id work for read, send, search, info, and mark_read
- Do not run `auth` — it requires interactive input and is for the start only
- All commands print plain text output, easy to parse
- The session persists between runs — no login needed each time
