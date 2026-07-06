#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tgcli — Telegram command-line tool
Usage: python3 telegram_tool.py <command> [args]
"""
import os
import sys
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.tl.types import User, Chat, Channel
    from telethon.errors import FloodWaitError, SessionPasswordNeededError
except ImportError:
    print("[ERROR] Please install telethon: pip install telethon")
    sys.exit(1)

# -- Config -------------------------------------------------------------------

# If TELEGRAM_DATA_DIR is set — store config and session there.
# Otherwise fall back to the script's own directory (original behaviour).
_DATA_DIR   = Path(os.environ.get('TELEGRAM_DATA_DIR', '')).resolve() if os.environ.get('TELEGRAM_DATA_DIR') else Path(__file__).parent.resolve()
CONFIG_FILE = _DATA_DIR / 'telegram_config.json'
SESSION     = str(_DATA_DIR / 'telegram_session')

# Temp file used by the non-interactive auth flow to pass state between steps
_AUTH_STATE_FILE = _DATA_DIR / 'telegram_auth_state.json'


def load_config():
    """Load config from file. Returns None if not found."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return None

def save_config(api_id, api_hash):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {'api_id': int(api_id), 'api_hash': api_hash.strip()}
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    CONFIG_FILE.chmod(0o600)
    return cfg

def setup_config():
    """Interactive first-time setup."""
    print("[SETUP]  First run — API credentials required.")
    print()
    print("   Get api_id and api_hash at https://my.telegram.org")
    print("   Log in → 'API development tools' → create an application")
    print()
    api_id   = input("   api_id   (number): ").strip()
    api_hash = input("   api_hash (string): ").strip()

    if not api_id.isdigit() or not api_hash:
        print("[ERROR] Invalid input")
        sys.exit(1)

    cfg = save_config(api_id, api_hash)
    print(f"\n[OK] Config saved: {CONFIG_FILE}")
    print("  Now run authorization:")
    print("  python3 telegram_tool.py auth\n")
    return cfg

def get_config():
    cfg = load_config()
    if cfg is None:
        cfg = setup_config()
        sys.exit(0)
    return cfg

# -- Auth state (non-interactive flow) ----------------------------------------

def _load_auth_state():
    if _AUTH_STATE_FILE.exists():
        with open(_AUTH_STATE_FILE) as f:
            return json.load(f)
    return {}

def _save_auth_state(state: dict):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_AUTH_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    _AUTH_STATE_FILE.chmod(0o600)

def _clear_auth_state():
    if _AUTH_STATE_FILE.exists():
        _AUTH_STATE_FILE.unlink()

# -- Client -------------------------------------------------------------------

def make_client():
    cfg = get_config()
    return TelegramClient(SESSION, cfg['api_id'], cfg['api_hash'])

# -- Helpers ------------------------------------------------------------------

def fmt_date(dt):
    if dt is None:
        return '??:??'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    now   = datetime.now().astimezone()
    if local.date() == now.date():
        return local.strftime('%H:%M')
    return local.strftime('%d.%m %H:%M')

def entity_icon(entity):
    if isinstance(entity, User):    return '[user]'
    if isinstance(entity, Channel): return '[channel]' if entity.broadcast else '[group]'
    if isinstance(entity, Chat):    return '[group]'
    return '[chat]'

def truncate(text, n=1000):
    if not text:
        return '(media / no text)'
    text = text.replace('\n', ' ')
    return text[:n] + '…' if len(text) > n else text

def entity_ref(e):
    """Return @username or numeric id."""
    if isinstance(e, (User, Channel)) and e.username:
        return f'@{e.username}'
    return str(e.id)


async def resolve_entity(client, target):
    """
    Resolve entity by @username or numeric ID.
    Falls back to searching loaded dialogs for private channels/groups
    that have no public username and can't be resolved by get_entity(id).
    """
    try:
        if isinstance(target, str) and target.startswith('@'):
            return await client.get_entity(target)

        try:
            numeric_id = int(str(target).lstrip('-'))
            return await client.get_entity(numeric_id)
        except (ValueError, TypeError):
            return await client.get_entity(target)

    except (ValueError, TypeError, Exception) as first_error:
        raw = str(target).lstrip('-')
        if raw.isdigit():
            target_id = int(raw)
            alt_id = int(raw[3:]) if raw.startswith('100') and len(raw) > 3 else None

            async for dialog in client.iter_dialogs():
                eid = dialog.entity.id
                if eid == target_id or (alt_id and eid == alt_id):
                    return dialog.entity

        raise first_error

# -- Contact / entity resolution ----------------------------------------------

async def _iter_contacts(client):
    """Yield User entities from the account's address book."""
    from telethon.tl.functions.contacts import GetContactsRequest
    result = await client(GetContactsRequest(hash=0))
    for user in result.users:
        yield user


def _display_name(e):
    """Human-readable name for any entity type."""
    if isinstance(e, User):
        name = f"{e.first_name or ''} {e.last_name or ''}".strip()
        return name or (f'@{e.username}' if e.username else str(e.id))
    return getattr(e, 'title', None) or str(e.id)


def _match_score(query, e):
    """
    Cheap fuzzy match. Returns True if query plausibly refers to this entity.
    Matches against name, username, and id — case-insensitive, substring-based.
    Intentionally permissive: the tool surfaces candidates, the agent decides.
    """
    q = query.lower().lstrip('@').strip()
    if not q:
        return False

    haystack = []
    if isinstance(e, User):
        haystack += [e.first_name or '', e.last_name or '',
                     f"{e.first_name or ''} {e.last_name or ''}".strip()]
    else:
        haystack.append(getattr(e, 'title', '') or '')
    if getattr(e, 'username', None):
        haystack.append(e.username)
    haystack.append(str(e.id))

    return any(q in h.lower() for h in haystack if h)


async def cmd_contacts(*words):
    """List the account's address book (saved contacts)."""
    async with make_client() as client:
        print(f"\n{'-'*62}")
        print(f"  CONTACTS")
        print(f"{'-'*62}")

        count = 0
        async for user in _iter_contacts(client):
            ref  = entity_ref(user)
            name = _display_name(user)
            print(f"[user] {name:<30} id: {user.id:<14} {ref if ref.startswith('@') else '(no username)'}")
            count += 1

        print(f"{'-'*62}")
        print(f"  Total: {count}")


async def cmd_resolve(*words):
    """
    Resolve a free-form query to real, addressable targets in this account.
    Searches contacts AND all dialogs (users, groups, channels).
    Prints a compact, machine-readable candidate list with an explicit count.

    This is the verification point: the agent may know WHO to write from any
    context (memory, documents, the conversation) — but before sending it
    confirms the target actually exists here and gets its canonical address.
    """
    query = ' '.join(words).strip()
    if not query:
        print("[resolve] query: \"\" — matches: 0 (empty query)")
        return

    async with make_client() as client:
        seen    = set()
        matches = []

        # 1. Address book
        async for user in _iter_contacts(client):
            if _match_score(query, user) and user.id not in seen:
                seen.add(user.id)
                matches.append(('contact', user))

        # 2. All dialogs (users, groups, channels)
        async for dialog in client.iter_dialogs():
            e = dialog.entity
            if e.id in seen:
                continue
            if _match_score(query, e):
                seen.add(e.id)
                kind = 'dialog'
                matches.append((kind, e))

        n = len(matches)
        print(f"[resolve] query: \"{query}\" — matches: {n}"
              + ("" if n else " (not found in contacts or dialogs)"))

        if n == 0:
            print("  This target does not exist in the account. "
                  "The request may rest on a wrong assumption — report it rather than guess.")
            return

        print()
        for i, (source, e) in enumerate(matches, 1):
            ref  = entity_ref(e)
            addr = ref if ref.startswith('@') else f"id: {e.id}"
            name = _display_name(e)
            icon = entity_icon(e)
            uname = f"  {ref}" if ref.startswith('@') else "  (no username)"
            print(f"  {i}. {icon} {name:<28} id: {e.id:<14}{uname}  [{source}]")

# -- Commands -----------------------------------------------------------------

async def cmd_dialogs(limit=30, kind=None):
    """List dialogs. kind: users | groups | channels"""
    async with make_client() as client:
        print(f"\n{'-'*62}")
        print(f"  DIALOGS{' (' + kind + ')' if kind else ''}")
        print(f"{'-'*62}")

        count = 0
        async for dialog in client.iter_dialogs():
            e = dialog.entity
            if kind == 'users'    and not isinstance(e, User): continue
            if kind == 'groups'   and not (isinstance(e, (Chat, Channel)) and not getattr(e, 'broadcast', False)): continue
            if kind == 'channels' and not (isinstance(e, Channel) and e.broadcast): continue

            icon   = entity_icon(e)
            name   = dialog.name or '???'
            unread = f'  [{dialog.unread_count} new]' if dialog.unread_count else ''
            last   = truncate(dialog.message.message if dialog.message else None, 50)
            date   = fmt_date(dialog.message.date if dialog.message else None)
            ref    = entity_ref(e)

            print(f"{icon} {name:<28} {ref:<22} {date}{unread}")
            if last:
                print(f"      {last}")

            count += 1
            if count >= int(limit):
                break

        print(f"{'-'*62}")
        print(f"  Shown: {count}")


async def cmd_read(target, limit=15):
    """Read messages from a dialog / group / channel."""
    async with make_client() as client:
        try:
            entity   = await resolve_entity(client, target)
            messages = await client.get_messages(entity, limit=int(limit))
            name     = getattr(entity, 'title', None) or getattr(entity, 'first_name', target)

            print(f"\n{'-'*62}")
            print(f"  {entity_icon(entity)} {name}  (last {len(messages)} messages)")
            print(f"{'-'*62}")

            for msg in reversed(messages):
                if msg.out:
                    sender = 'Me'
                elif msg.sender:
                    s      = msg.sender
                    sender = getattr(s, 'username', None) or getattr(s, 'first_name', '???')
                else:
                    sender = '???'

                print(f"{fmt_date(msg.date)} | {sender:<18} {truncate(msg.message, 1000)}")

            print(f"{'-'*62}")

        except Exception as ex:
            print(f"[ERROR] Error: {ex}")


async def cmd_send(target, *words):
    """Send a message."""
    text = ' '.join(words)
    if not text:
        print("[ERROR] Message text is required")
        return
    async with make_client() as client:
        try:
            entity = await resolve_entity(client, target)
            await client.send_message(entity.id, text)
            name = getattr(entity, 'title', None) or getattr(entity, 'first_name', target)
            print(f"[OK] Sent → {name}: {text[:60]}")
        except FloodWaitError as e:
            print(f"[WAIT] Too many requests, wait {e.seconds} sec.")
        except Exception as ex:
            print(f"[ERROR] Error: {ex}")


async def cmd_unread(limit=20):
    """Show dialogs with unread messages."""
    async with make_client() as client:
        print(f"\n{'-'*62}")
        print(f"  UNREAD")
        print(f"{'-'*62}")

        count = 0
        async for dialog in client.iter_dialogs():
            if not dialog.unread_count:
                continue
            e    = dialog.entity
            name = dialog.name or '???'
            last = truncate(dialog.message.message if dialog.message else None, 1000)
            date = fmt_date(dialog.message.date if dialog.message else None)

            print(f"{entity_icon(e)} [{dialog.unread_count:>3} new] {name:<28} {date}")
            if last:
                print(f"            {last}")

            count += 1
            if count >= int(limit):
                break

        if count == 0:
            print("  [OK] All caught up!")
        print(f"{'-'*62}")


async def cmd_search(target, *words):
    """Search messages in a chat."""
    query = ' '.join(words)
    if not query:
        print("[ERROR] Please provide a search query")
        return
    async with make_client() as client:
        try:
            entity   = await resolve_entity(client, target)
            messages = await client.get_messages(entity, search=query, limit=20)
            name     = getattr(entity, 'title', None) or getattr(entity, 'first_name', target)

            print(f"\n{'-'*62}")
            print(f"  [search] Search '{query}' in {name} — found {len(messages)}")
            print(f"{'-'*62}")

            for msg in reversed(messages):
                sender = 'Me' if msg.out else (
                    getattr(msg.sender, 'username', None) or
                    getattr(msg.sender, 'first_name', '???') if msg.sender else '???'
                )
                print(f"{fmt_date(msg.date)} | {sender:<18} {truncate(msg.message, 1000)}")

            print(f"{'-'*62}")

        except Exception as ex:
            print(f"[ERROR] Error: {ex}")


async def cmd_info(target):
    """Info about a user or channel."""
    async with make_client() as client:
        try:
            entity = await resolve_entity(client, target)
            print(f"\n{'-'*62}")

            if isinstance(entity, User):
                print(f"  [user] User")
                print(f"  Name:     {(entity.first_name or '')} {(entity.last_name or '')}".rstrip())
                print(f"  Username: @{entity.username}" if entity.username else "  Username: —")
                print(f"  ID:       {entity.id}")
                print(f"  Bot:      {'yes' if entity.bot else 'no'}")
            elif isinstance(entity, Channel):
                print(f"  {'[channel] Channel' if entity.broadcast else '[group] Group/Supergroup'}")
                print(f"  Title:      {entity.title}")
                print(f"  Username:   @{entity.username}" if entity.username else "  Username:   —")
                print(f"  ID:         {entity.id}")
                try:
                    from telethon.tl.functions.channels import GetFullChannelRequest
                    full = await client(GetFullChannelRequest(entity))
                    print(f"  Members:    {full.full_chat.participants_count}")
                    if full.full_chat.about:
                        print(f"  About:      {truncate(full.full_chat.about, 1000)}")
                except Exception:
                    pass
            else:
                print(f"  [chat] Chat: {getattr(entity, 'title', str(entity))}")
                print(f"  ID: {entity.id}")

            print(f"{'-'*62}")

        except Exception as ex:
            print(f"[ERROR] Error: {ex}")


async def cmd_mark_read(target):
    """Mark a dialog as read."""
    async with make_client() as client:
        try:
            entity = await resolve_entity(client, target)
            await client.send_read_acknowledge(entity)
            name = getattr(entity, 'title', None) or getattr(entity, 'first_name', target)
            print(f"[OK] Marked as read: {name}")
        except Exception as ex:
            print(f"[ERROR] Error: {ex}")


async def cmd_auth():
    """Authorization (run once manually, interactive)."""
    get_config()  # ensure config exists, will prompt if missing
    print("[AUTH] Telegram Authorization")
    print(f"   Session: {SESSION}.session\n")

    client = make_client()
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[OK] Already authorized as {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    phone = input("[PHONE] Phone number (e.g. +19001234567): ").strip()
    await client.send_code_request(phone)
    code = input("[CODE] Telegram code: ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("[2FA] Two-factor authentication password: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"\n[OK] Success! Authorized as {me.first_name} (@{me.username})")
    print("  All commands are now available without re-authorization.")
    await client.disconnect()


# -- Non-interactive auth flow ------------------------------------------------

async def cmd_auth_init(api_id, api_hash):
    """
    Non-interactive step 0: save API credentials.
    Must be called before auth:phone if config doesn't exist yet.

    Usage: telegram auth:init <api_id> <api_hash>
    """
    if not str(api_id).isdigit() or not api_hash:
        print("[ERROR] api_id must be a number, api_hash must be a non-empty string")
        sys.exit(1)
    save_config(api_id, api_hash)
    print(f"[OK] Credentials saved to {CONFIG_FILE}")
    print("  Next step: telegram auth:phone <phone_number>")


async def cmd_auth_phone(phone):
    """
    Non-interactive step 1: send confirmation code to the phone number.

    Usage: telegram auth:phone +19001234567
    """
    get_config()
    client = make_client()
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[OK] Already authorized as {me.first_name} (@{me.username})")
        await client.disconnect()
        _clear_auth_state()
        return

    try:
        result = await client.send_code_request(phone)
        _save_auth_state({'phone': phone, 'phone_code_hash': result.phone_code_hash})
        print(f"[OK] Code sent to {phone}")
        print("  Next step: telegram auth:code <code>")
    except Exception as ex:
        print(f"[ERROR] {ex}")
        sys.exit(1)
    finally:
        await client.disconnect()


async def cmd_auth_code(code):
    """
    Non-interactive step 2: submit the confirmation code received via Telegram.
    If 2FA is enabled the response will tell you to call auth:password next.

    Usage: telegram auth:code 12345
    """
    state = _load_auth_state()
    if not state.get('phone') or not state.get('phone_code_hash'):
        print("[ERROR] No pending auth state. Run auth:phone first.")
        sys.exit(1)

    client = make_client()
    await client.connect()

    try:
        await client.sign_in(
            phone=state['phone'],
            code=code,
            phone_code_hash=state['phone_code_hash'],
        )
        me = await client.get_me()
        _clear_auth_state()
        print(f"[OK] Authorized as {me.first_name} (@{me.username})")
        print("  All commands are now available.")
    except SessionPasswordNeededError:
        print("[2FA] Two-factor authentication required.")
        print("  Next step: telegram auth:password <your_password>")
    except Exception as ex:
        print(f"[ERROR] {ex}")
        sys.exit(1)
    finally:
        await client.disconnect()


async def cmd_auth_password(password):
    """
    Non-interactive step 3 (only if 2FA is enabled): submit the cloud password.

    Usage: telegram auth:password mypassword
    """
    client = make_client()
    await client.connect()

    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        _clear_auth_state()
        print(f"[OK] Authorized as {me.first_name} (@{me.username})")
        print("  All commands are now available.")
    except Exception as ex:
        print(f"[ERROR] {ex}")
        sys.exit(1)
    finally:
        await client.disconnect()


async def cmd_me():
    """Show current account info."""
    async with make_client() as client:
        me = await client.get_me()
        print(f"\n  [user] {me.first_name} {me.last_name or ''}".rstrip())
        if me.username:
            print(f"  Username: @{me.username}")
        print(f"  ID:       {me.id}")
        print(f"  Phone:    {me.phone}")


async def cmd_config():
    """Show current config."""
    cfg = load_config()
    if cfg is None:
        print("[SETUP]  Config not found. Run the script without arguments to set up.")
        return
    masked = cfg['api_hash'][:4] + '****' + cfg['api_hash'][-4:]
    print(f"\n  Data dir: {_DATA_DIR}")
    print(f"  Config:   {CONFIG_FILE}")
    print(f"  Session:  {SESSION}.session")
    print(f"  api_id:   {cfg['api_id']}")
    print(f"  api_hash: {masked}")
    authorized = Path(SESSION + '.session').exists()
    print(f"  Status:   {'[OK] session active' if authorized else '✗ not authorized, run auth'}")


# -- Help ---------------------------------------------------------------------

HELP = """
Telegram Tool — commands:

  dialogs   [N] [users|groups|channels]  List dialogs (up to N, default 30)
  contacts                               List saved contacts (address book)
  resolve   <query...>                   Find real targets by name/@username/id
  read      <@chat|id> [N]               Last N messages (default 15)
  send      <@user|id> <text...>         Send a message
  unread    [N]                          Dialogs with unread messages
  search    <@chat|id> <query...>        Search messages in a chat
  info      <@user|channel|id>           Info about a user or channel
  mark_read <@chat|id>                   Mark dialog as read
  me                                     My account info
  config                                 Show current config and data dir

Interactive authorization (run once in a terminal):
  auth                                   Full interactive auth wizard

Non-interactive authorization (for scripts and web UIs):
  auth:init <api_id> <api_hash>          Save API credentials (step 0, if needed)
  auth:phone <phone>                     Send confirmation code  (step 1)
  auth:code  <code>                      Submit the code         (step 2)
  auth:password <password>               Submit 2FA password     (step 3, if needed)

Environment variables:
  TELEGRAM_DATA_DIR   Directory for config and session files.
                      Defaults to the script's own directory.

Examples:
  python3 telegram_tool.py dialogs
  python3 telegram_tool.py dialogs 50 channels
  python3 telegram_tool.py read @durov 20
  python3 telegram_tool.py read 1820894363 5
  python3 telegram_tool.py send @username Hello how are you
  python3 telegram_tool.py unread
  python3 telegram_tool.py search @groupname important word
  python3 telegram_tool.py info @telegram
  python3 telegram_tool.py mark_read @username
  python3 telegram_tool.py me

  TELEGRAM_DATA_DIR=/data/tg python3 telegram_tool.py auth:phone +19001234567
"""

# -- Main ---------------------------------------------------------------------

COMMANDS = {
    'dialogs':       cmd_dialogs,
    'contacts':      cmd_contacts,
    'resolve':       cmd_resolve,
    'read':          cmd_read,
    'send':          cmd_send,
    'unread':        cmd_unread,
    'search':        cmd_search,
    'info':          cmd_info,
    'mark_read':     cmd_mark_read,
    'me':            cmd_me,
    'auth':          cmd_auth,
    'auth:init':     cmd_auth_init,
    'auth:phone':    cmd_auth_phone,
    'auth:code':     cmd_auth_code,
    'auth:password': cmd_auth_password,
    'config':        cmd_config,
}

NON_SESSION_COMMANDS = {'auth', 'auth:init', 'auth:phone', 'auth:code', 'auth:password', 'config'}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        print(HELP)
        sys.exit(0)

    cmd  = sys.argv[1]
    args = sys.argv[2:]

    if cmd not in COMMANDS:
        print(f"[ERROR] Unknown command: {cmd}")
        print(HELP)
        sys.exit(1)

    if cmd not in NON_SESSION_COMMANDS and not Path(SESSION + '.session').exists():
        print("[WARN]  Session not found. Run first:")
        print("   python3 telegram_tool.py auth")
        print("   or: python3 telegram_tool.py auth:phone <phone>")
        sys.exit(1)

    try:
        asyncio.run(COMMANDS[cmd](*args))
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
