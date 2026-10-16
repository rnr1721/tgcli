#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tgcli — Telegram command-line tool.

Usage: python3 telegram_tool.py [--json] <command> [args]

Text output is meant for humans and LLM agents; --json output is meant for
programs. Both carry the same data. Exit codes are stable (see EXIT_* below).
"""
import os
import re
import sys
import json
import time
import asyncio
import inspect
import logging
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import fcntl  # POSIX only; session locking is skipped where unavailable
except ImportError:  # pragma: no cover
    fcntl = None

__version__ = '1.1.2'

try:
    from telethon import TelegramClient, utils
    from telethon.tl.types import User, Chat, Channel
    from telethon.errors import FloodWaitError, SessionPasswordNeededError
except ImportError:
    print("[ERROR] Please install telethon: pip install telethon")
    sys.exit(1)

# Telethon logs warnings to stderr by default. Keep stderr quiet so callers
# that merge streams (2>&1) still get clean output.
logging.getLogger('telethon').setLevel(logging.ERROR)

# Locale-independent UTF-8 output (php-fpm / cron often run with LANG unset).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# -- Paths --------------------------------------------------------------------

# If TELEGRAM_DATA_DIR is set — store config and session there.
# Otherwise fall back to the script's own directory (original behaviour).
_DATA_DIR = (Path(os.environ['TELEGRAM_DATA_DIR']).resolve()
             if os.environ.get('TELEGRAM_DATA_DIR')
             else Path(__file__).parent.resolve())
CONFIG_FILE      = _DATA_DIR / 'telegram_config.json'
SESSION          = str(_DATA_DIR / 'telegram_session')
_AUTH_STATE_FILE = _DATA_DIR / 'telegram_auth_state.json'
_UPDATES_STATE   = _DATA_DIR / 'updates_state.json'
_LOCK_FILE       = _DATA_DIR / 'telegram_session.lock'
_DOWNLOADS_DIR   = _DATA_DIR / 'downloads'

# -- Exit codes ---------------------------------------------------------------

EXIT_OK    = 0
EXIT_ERROR = 1   # generic failure (network, not found, Telegram error)
EXIT_USAGE = 2   # bad arguments / bad config values
EXIT_AUTH  = 3   # no credentials, no session, or session not authorized
EXIT_FLOOD = 4   # Telegram FloodWait — see retry_after
EXIT_BUSY  = 5   # another tgcli process holds the session lock


class CliError(Exception):
    """An expected failure with a stable machine-readable code."""

    def __init__(self, message, code='error', exit_code=EXIT_ERROR, tag='ERROR', **extra):
        super().__init__(message)
        self.message   = message
        self.code      = code
        self.exit_code = exit_code
        self.tag       = tag      # text-mode prefix: ERROR / WARN / WAIT
        self.extra     = extra


def usage_error(message):
    return CliError(message, code='usage', exit_code=EXIT_USAGE)

# -- Output -------------------------------------------------------------------

OUTPUT_JSON = False


def _json_default(o):
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.astimezone(timezone.utc).isoformat()
    if isinstance(o, (bytes, bytearray)):
        return None
    if isinstance(o, Path):
        return str(o)
    return str(o)


def emit(data, render):
    """Print a successful result: JSON object, or human text via render()."""
    if OUTPUT_JSON:
        print(json.dumps({'ok': True, **data}, ensure_ascii=False, default=_json_default))
    else:
        render(data)


def emit_error(err):
    if OUTPUT_JSON:
        print(json.dumps({'ok': False, 'error': err.message, 'code': err.code, **err.extra},
                         ensure_ascii=False, default=_json_default))
    else:
        print(f"[{err.tag}] {err.message}")

# -- Credentials --------------------------------------------------------------

def load_config():
    """Load config file. Returns None if not found."""
    if not CONFIG_FILE.exists():
        return None
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        return {'api_id': int(cfg['api_id']), 'api_hash': str(cfg['api_hash']).strip()}
    except (OSError, ValueError, KeyError, TypeError) as ex:
        raise CliError(f"Cannot read {CONFIG_FILE}: {ex}", code='bad_config', exit_code=EXIT_USAGE)


def save_config(api_id, api_hash):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {'api_id': int(api_id), 'api_hash': api_hash.strip()}
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    CONFIG_FILE.chmod(0o600)
    return cfg


def env_credentials():
    """
    TELEGRAM_API_ID + TELEGRAM_API_HASH take precedence over the config file.
    Lets callers keep credentials in their own secret store instead of on disk.
    """
    api_id   = os.environ.get('TELEGRAM_API_ID', '').strip()
    api_hash = os.environ.get('TELEGRAM_API_HASH', '').strip()
    if not api_id and not api_hash:
        return None
    if not api_id or not api_hash:
        raise CliError('Set both TELEGRAM_API_ID and TELEGRAM_API_HASH, or neither.',
                       code='bad_config', exit_code=EXIT_USAGE)
    if not api_id.isdigit():
        raise CliError('TELEGRAM_API_ID must be a number.', code='bad_config', exit_code=EXIT_USAGE)
    return {'api_id': int(api_id), 'api_hash': api_hash}


def get_credentials():
    """Return (credentials, source) where source is 'env', 'file' or None."""
    creds = env_credentials()
    if creds:
        return creds, 'env'
    cfg = load_config()
    if cfg:
        return cfg, 'file'
    return None, None


def setup_config():
    """Interactive first-time setup (terminal only)."""
    print("[SETUP]  First run — API credentials required.")
    print()
    print("   Get api_id and api_hash at https://my.telegram.org")
    print("   Log in → 'API development tools' → create an application")
    print()
    api_id   = input("   api_id   (number): ").strip()
    api_hash = input("   api_hash (string): ").strip()
    if not api_id.isdigit() or not api_hash:
        raise usage_error('Invalid input: api_id must be a number, api_hash non-empty')
    cfg = save_config(api_id, api_hash)
    print(f"\n[OK] Config saved: {CONFIG_FILE}\n")
    return cfg


def require_credentials(interactive=False):
    creds, _ = get_credentials()
    if creds:
        return creds
    if interactive and sys.stdin.isatty():
        return setup_config()
    raise CliError(
        'API credentials not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH, '
        'or run: telegram auth:init <api_id> <api_hash>',
        code='no_config', exit_code=EXIT_AUTH)

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

# -- Session lock -------------------------------------------------------------

@contextmanager
def session_lock():
    """
    Serialize tgcli processes sharing one data dir.

    Telethon keeps its session in SQLite; two concurrent processes on the same
    session fail with "database is locked". Callers that poll in the background
    while also sending on demand would hit this constantly, so every
    session-touching command takes an exclusive lock and waits up to
    TELEGRAM_LOCK_TIMEOUT seconds (default 30).
    """
    if fcntl is None:
        yield
        return
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        timeout = float(os.environ.get('TELEGRAM_LOCK_TIMEOUT', '30'))
    except ValueError:
        timeout = 30.0
    with open(_LOCK_FILE, 'a') as fh:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CliError(
                        f'Session is busy (another tgcli process is using it); '
                        f'gave up after {timeout:g}s.',
                        code='session_busy', exit_code=EXIT_BUSY)
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

# -- Client -------------------------------------------------------------------

@asynccontextmanager
async def connected(require_auth=True, creds=None):
    """
    Connected client. Unlike `async with TelegramClient(...)`, this never falls
    into Telethon's interactive start() prompt — a non-authorized session is a
    clean error, not a hang waiting on stdin.
    """
    creds  = creds or require_credentials()
    client = TelegramClient(SESSION, creds['api_id'], creds['api_hash'])
    await client.connect()
    try:
        if require_auth and not await client.is_user_authorized():
            raise CliError(
                'Session is not authorized. Run: telegram auth:phone <phone>',
                code='not_authorized', exit_code=EXIT_AUTH)
        yield client
    finally:
        await client.disconnect()

# -- Entity helpers -----------------------------------------------------------

KINDS = ('users', 'groups', 'channels')

ICONS = {
    'user':    '[user]',
    'bot':     '[bot]',
    'group':   '[group]',
    'channel': '[channel]',
    'chat':    '[chat]',
}


def peer_id_of(e, known=None):
    """
    Marked peer id: users positive, basic groups negative, channels and
    supergroups -100… . Unambiguous across entity types, unlike entity.id.
    """
    if known is not None:
        return known
    try:
        return utils.get_peer_id(e)
    except Exception:
        return getattr(e, 'id', None)


def entity_kind(e):
    if isinstance(e, User):
        return 'bot' if e.bot else 'user'
    if isinstance(e, Channel):
        return 'channel' if e.broadcast else 'group'
    if isinstance(e, Chat):
        return 'group'
    return 'chat'


def kind_matches(e, kind):
    if kind is None:
        return True
    k = entity_kind(e)
    if kind == 'users':
        return k in ('user', 'bot')
    if kind == 'groups':
        return k == 'group'
    if kind == 'channels':
        return k == 'channel'
    return False


def display_name(e):
    """Human-readable name for any entity type."""
    if isinstance(e, User):
        name = f"{e.first_name or ''} {e.last_name or ''}".strip()
        return name or (f'@{e.username}' if e.username else str(e.id))
    return getattr(e, 'title', None) or str(getattr(e, 'id', '???'))


def entity_ref(e, known_peer_id=None):
    """Canonical address for other commands: @username or marked peer id."""
    username = getattr(e, 'username', None)
    return f'@{username}' if username else str(peer_id_of(e, known_peer_id))


def entity_dict(e, known_peer_id=None):
    d = {
        'peer_id':  peer_id_of(e, known_peer_id),
        'id':       getattr(e, 'id', None),
        'kind':     entity_kind(e),
        'name':     display_name(e),
        'username': getattr(e, 'username', None),
    }
    if isinstance(e, User) and getattr(e, 'is_self', False):
        d['self'] = True
    return d


async def resolve_entity(client, target):
    """
    Resolve @username, marked or bare numeric id, or any string Telethon
    accepts (phone, t.me link). Falls back to scanning dialogs for private
    groups/channels that get_entity() can't resolve from a bare id.
    """
    t = str(target).strip()
    if not t:
        raise usage_error('Target (@username or id) is required.')

    if re.fullmatch(r'-?\d+', t):
        n = int(t)
        try:
            return await client.get_entity(n)
        except Exception:
            pass
        bare = abs(n)
        s = str(bare)
        channel_bare = int(s[3:]) if n < 0 and s.startswith('100') and len(s) > 3 else None
        async for dialog in client.iter_dialogs():
            e = dialog.entity
            if dialog.id == n or e.id == bare or (channel_bare and e.id == channel_bare):
                return e
        raise CliError(f'Target not found: {t}', code='not_found')

    try:
        return await client.get_entity(t)
    except (ValueError, TypeError) as ex:
        raise CliError(f'Target not found: {t} ({ex})', code='not_found')

# -- Message helpers ----------------------------------------------------------

# Order matters: voice before audio, video_note/gif before video.
_MEDIA_CHECKS = (
    ('photo', 'photo'), ('voice', 'voice'), ('video_note', 'video_note'),
    ('gif', 'gif'), ('sticker', 'sticker'), ('audio', 'audio'),
    ('video', 'video'), ('document', 'document'), ('geo', 'location'),
    ('contact', 'contact'), ('poll', 'poll'), ('web_preview', 'webpage'),
)
_FILELESS_MEDIA = ('location', 'contact', 'poll', 'webpage')


def media_dict(msg):
    """Media descriptor, or None for plain text messages."""
    if not getattr(msg, 'media', None):
        return None
    mtype = 'other'
    for attr, name in _MEDIA_CHECKS:
        try:
            if getattr(msg, attr, None):
                mtype = name
                break
        except Exception:
            continue
    d = {'type': mtype}
    if mtype not in _FILELESS_MEDIA:
        f = getattr(msg, 'file', None)
        if f is not None:
            for key, out in (('mime_type', 'mime'), ('size', 'size'),
                             ('name', 'name'), ('duration', 'duration')):
                try:
                    v = getattr(f, key)
                except Exception:
                    v = None
                if v not in (None, ''):
                    d[out] = v
    return d


def media_label(msg):
    md = media_dict(msg)
    if not md:
        return ''
    label = md['type'].replace('_', ' ')
    dur = md.get('duration')
    if dur:
        dur = int(dur)
        label += f' {dur // 60}:{dur % 60:02d}'
    return f'({label})'


def message_dict(m):
    sender = getattr(m, 'sender', None)
    return {
        'id':        m.id,
        'peer_id':   getattr(m, 'chat_id', None),
        'date':      m.date,
        'out':       bool(m.out),
        'text':      m.message or '',
        'sender_id': getattr(m, 'sender_id', None),
        'sender':    entity_dict(sender) if sender is not None else None,
        'reply_to':  getattr(m, 'reply_to_msg_id', None),
        'mentioned': bool(getattr(m, 'mentioned', False)),
        'media':     media_dict(m),
    }


def sender_label(m):
    if m.out:
        return 'Me'
    s = getattr(m, 'sender', None)
    if s is None:
        return '???'
    return (getattr(s, 'username', None) or getattr(s, 'first_name', None)
            or getattr(s, 'title', None) or '???')


def truncate(text, n=1000):
    if not text:
        return '(no text)'
    text = text.replace('\n', ' ')
    return text[:n] + '…' if len(text) > n else text


def message_text(m, n=1000):
    """Text-mode rendering: '(voice 0:03)', '(photo) caption', or plain text."""
    label = media_label(m)
    text  = m.message or ''
    if label and text:
        return truncate(f'{label} {text}', n)
    return truncate(text or label, n)


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


def to_int(value, name, minimum=None):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise usage_error(f'{name} must be an integer, got: {value!r}')
    if minimum is not None and n < minimum:
        raise usage_error(f'{name} must be >= {minimum}')
    return n


RULE = '-' * 62

# -- Contacts / resolve -------------------------------------------------------

async def _iter_contacts(client):
    """Yield User entities from the account's address book."""
    from telethon.tl.functions.contacts import GetContactsRequest
    result = await client(GetContactsRequest(hash=0))
    for user in result.users:
        yield user


def _match(query, e):
    """
    Cheap fuzzy match against name, username and id — case-insensitive,
    substring-based. Intentionally permissive: the tool surfaces candidates,
    the caller decides.
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
    haystack.append(str(peer_id_of(e)))
    return any(q in h.lower() for h in haystack if h)


async def cmd_contacts():
    """List the account's address book (saved contacts)."""
    async with connected() as client:
        contacts = [entity_dict(u) async for u in _iter_contacts(client)]

    def render(d):
        print(f"\n{RULE}\n  CONTACTS\n{RULE}")
        for c in d['contacts']:
            uname = f"@{c['username']}" if c['username'] else '(no username)'
            print(f"[user] {c['name']:<30} id: {c['peer_id']:<14} {uname}")
        print(f"{RULE}\n  Total: {d['total']}")

    emit({'contacts': contacts, 'total': len(contacts)}, render)


async def cmd_resolve(*words):
    """
    Resolve a free-form query to real, addressable targets. Searches contacts
    AND all dialogs (users, groups, channels) and reports an explicit match
    count — zero matches means the target does not exist in this account.
    """
    query   = ' '.join(words).strip()
    matches = []

    if query:
        async with connected() as client:
            seen = set()
            async for user in _iter_contacts(client):
                pid = peer_id_of(user)
                if pid not in seen and _match(query, user):
                    seen.add(pid)
                    matches.append({'source': 'contact', **entity_dict(user, pid)})
            async for dialog in client.iter_dialogs():
                if dialog.id in seen:
                    continue
                if _match(query, dialog.entity):
                    seen.add(dialog.id)
                    matches.append({'source': 'dialog', **entity_dict(dialog.entity, dialog.id)})

    def render(d):
        n = d['count']
        if not d['query']:
            print('[resolve] query: "" — matches: 0 (empty query)')
            return
        print(f"[resolve] query: \"{d['query']}\" — matches: {n}"
              + ("" if n else " (not found in contacts or dialogs)"))
        if n == 0:
            print("  This target does not exist in the account. "
                  "The request may rest on a wrong assumption — report it rather than guess.")
            return
        print()
        for i, m in enumerate(d['matches'], 1):
            uname = f"  @{m['username']}" if m['username'] else "  (no username)"
            print(f"  {i}. {ICONS[m['kind']]} {m['name']:<28} id: {m['peer_id']:<14}{uname}  [{m['source']}]")

    emit({'query': query, 'count': len(matches), 'matches': matches}, render)

# -- Commands -----------------------------------------------------------------

def _dialog_row(dialog):
    m = dialog.message
    return {
        **entity_dict(dialog.entity, dialog.id),
        'unread':       dialog.unread_count,
        'pinned':       bool(getattr(dialog, 'pinned', False)),
        'date':         m.date if m else None,
        'last_message': (m.message or media_label(m)) if m else None,
    }


async def cmd_dialogs(limit='30', kind=None):
    """List dialogs. kind: users | groups | channels"""
    limit = to_int(limit, 'limit', 1)
    if kind is not None and kind not in KINDS:
        raise usage_error(f"kind must be one of: {', '.join(KINDS)}")

    rows = []
    async with connected() as client:
        async for dialog in client.iter_dialogs():
            if not kind_matches(dialog.entity, kind):
                continue
            rows.append(_dialog_row(dialog))
            if len(rows) >= limit:
                break

    def render(d):
        print(f"\n{RULE}\n  DIALOGS{' (' + kind + ')' if kind else ''}\n{RULE}")
        for r in d['dialogs']:
            ref    = f"@{r['username']}" if r['username'] else str(r['peer_id'])
            unread = f"  [{r['unread']} new]" if r['unread'] else ''
            print(f"{ICONS[r['kind']]} {r['name']:<28} {ref:<22} {fmt_date(r['date'])}{unread}")
            if r['last_message'] is not None:
                print(f"      {truncate(r['last_message'], 50)}")
        print(f"{RULE}\n  Shown: {d['shown']}")

    emit({'dialogs': rows, 'shown': len(rows)}, render)


async def cmd_unread(limit='20'):
    """Show dialogs with unread messages."""
    limit = to_int(limit, 'limit', 1)
    rows  = []
    async with connected() as client:
        async for dialog in client.iter_dialogs():
            if not dialog.unread_count:
                continue
            rows.append(_dialog_row(dialog))
            if len(rows) >= limit:
                break

    def render(d):
        print(f"\n{RULE}\n  UNREAD\n{RULE}")
        for r in d['dialogs']:
            ref = f"@{r['username']}" if r['username'] else str(r['peer_id'])
            print(f"{ICONS[r['kind']]} [{r['unread']:>3} new] {r['name']:<28} {ref:<22} {fmt_date(r['date'])}")
            if r['last_message'] is not None:
                print(f"            {truncate(r['last_message'], 1000)}")
        if not d['dialogs']:
            print("  [OK] All caught up!")
        print(RULE)

    emit({'dialogs': rows, 'count': len(rows)}, render)


async def cmd_read(target, limit='15'):
    """Read the last N messages from a dialog / group / channel."""
    limit = to_int(limit, 'limit', 1)
    async with connected() as client:
        entity   = await resolve_entity(client, target)
        messages = list(reversed(await client.get_messages(entity, limit=limit)))

    def render(_):
        print(f"\n{RULE}\n  {ICONS[entity_kind(entity)]} {display_name(entity)}  (last {len(messages)} messages)\n{RULE}")
        for m in messages:
            print(f"{fmt_date(m.date)} | {sender_label(m):<18} {message_text(m)}")
        print(RULE)

    emit({'chat': entity_dict(entity), 'messages': [message_dict(m) for m in messages]}, render)


async def cmd_send(target, *words):
    """Send a text message."""
    text = ' '.join(words)
    if not text.strip():
        raise usage_error('Message text is required')
    async with connected() as client:
        entity = await resolve_entity(client, target)
        sent   = await client.send_message(entity, text)

    def render(_):
        print(f"[OK] Sent → {display_name(entity)}: {text[:60]}")

    emit({'chat': entity_dict(entity),
          'sent': {'id': sent.id, 'date': sent.date, 'peer_id': getattr(sent, 'chat_id', None)}},
         render)


async def cmd_search(target, *words):
    """Search messages in a chat (up to 20 results)."""
    query = ' '.join(words).strip()
    if not query:
        raise usage_error('Please provide a search query')
    async with connected() as client:
        entity   = await resolve_entity(client, target)
        messages = list(reversed(await client.get_messages(entity, search=query, limit=20)))

    def render(_):
        print(f"\n{RULE}\n  [search] Search '{query}' in {display_name(entity)} — found {len(messages)}\n{RULE}")
        for m in messages:
            print(f"{fmt_date(m.date)} | {sender_label(m):<18} {message_text(m)}")
        print(RULE)

    emit({'chat': entity_dict(entity), 'query': query,
          'messages': [message_dict(m) for m in messages]}, render)


async def cmd_info(target):
    """Info about a user, group or channel."""
    async with connected() as client:
        entity = await resolve_entity(client, target)
        data   = {'entity': entity_dict(entity)}
        if isinstance(entity, User):
            data['bot'] = bool(entity.bot)
        elif isinstance(entity, Channel):
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await client(GetFullChannelRequest(entity))
                data['members'] = full.full_chat.participants_count
                data['about']   = full.full_chat.about or None
            except Exception:
                pass

    def render(d):
        e = d['entity']
        uname = f"@{e['username']}" if e['username'] else '—'
        print(f"\n{RULE}")
        if isinstance(entity, User):
            print("  [user] User")
            print(f"  Name:     {(entity.first_name or '')} {(entity.last_name or '')}".rstrip())
            print(f"  Username: {uname}")
            print(f"  ID:       {e['peer_id']}")
            print(f"  Bot:      {'yes' if d.get('bot') else 'no'}")
        elif isinstance(entity, Channel):
            print(f"  {'[channel] Channel' if entity.broadcast else '[group] Group/Supergroup'}")
            print(f"  Title:      {entity.title}")
            print(f"  Username:   {uname}")
            print(f"  ID:         {e['peer_id']}")
            if d.get('members') is not None:
                print(f"  Members:    {d['members']}")
            if d.get('about'):
                print(f"  About:      {truncate(d['about'], 1000)}")
        else:
            print(f"  [chat] Chat: {e['name']}")
            print(f"  ID: {e['peer_id']}")
        print(RULE)

    emit(data, render)


async def cmd_mark_read(target):
    """Mark a dialog as read."""
    async with connected() as client:
        entity = await resolve_entity(client, target)
        await client.send_read_acknowledge(entity)
    emit({'chat': entity_dict(entity)},
         lambda _: print(f"[OK] Marked as read: {display_name(entity)}"))


async def cmd_me():
    """Show current account info."""
    async with connected() as client:
        me = await client.get_me()

    def render(_):
        print(f"\n  [user] {me.first_name} {me.last_name or ''}".rstrip())
        if me.username:
            print(f"  Username: @{me.username}")
        print(f"  ID:       {me.id}")
        print(f"  Phone:    {me.phone}")

    emit({'user': {**entity_dict(me), 'first_name': me.first_name,
                   'last_name': me.last_name, 'phone': me.phone}}, render)


async def cmd_download(target, msg_id, dest=None):
    """
    Download the media of one message. Without dest, saves into
    <data dir>/downloads/ with a generated name. Prints the saved path.
    Size cap: TELEGRAM_DOWNLOAD_MAX_MB (default 50, 0 = unlimited).
    """
    mid = to_int(msg_id, 'msg_id', 1)
    try:
        max_mb = float(os.environ.get('TELEGRAM_DOWNLOAD_MAX_MB', '50'))
    except ValueError:
        max_mb = 50.0

    async with connected() as client:
        entity = await resolve_entity(client, target)
        msg    = await client.get_messages(entity, ids=mid)
        if msg is None:
            raise CliError(f'Message {mid} not found in {display_name(entity)}', code='not_found')
        media = media_dict(msg)
        if not media or getattr(msg, 'file', None) is None:
            raise CliError(f'Message {mid} has no downloadable media', code='no_media')

        size = media.get('size') or 0
        if max_mb > 0 and size > max_mb * 1024 * 1024:
            raise CliError(
                f'Media is {size / 1048576:.1f} MB, over the {max_mb:g} MB limit '
                f'(TELEGRAM_DOWNLOAD_MAX_MB)', code='too_large', size=size)

        if dest:
            p = Path(dest)
            if dest.endswith(('/', os.sep)) or p.is_dir():
                p.mkdir(parents=True, exist_ok=True)
                target_path = str(p) + os.sep
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                target_path = str(p)
        else:
            _DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            target_path = str(_DOWNLOADS_DIR) + os.sep

        path = await client.download_media(msg, file=target_path)

    if not path:
        raise CliError('Download failed (no file written)', code='download_failed')

    emit({'path': str(Path(path).resolve()), 'size': size, 'media': media,
          'message_id': mid, 'chat': entity_dict(entity)},
         lambda d: print(f"[OK] Downloaded → {d['path']} ({d['size']} bytes)"))

# -- Updates ------------------------------------------------------------------

CURSOR_VERSION = 1

# Dialogs are ordered by their last message date. Once an unpinned dialog's
# last message is older than the previous high-water mark (minus this slack)
# nothing newer can follow, so the scan stops. Slack covers ordering ties.
_SCAN_SLACK = timedelta(minutes=5)


def _parse_since(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        raise usage_error(f'Invalid cursor: bad "since" value {value!r}')
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def collect_updates(client, cursor=None, limit=100, per_dialog=20,
                          kind=None, include_out=False):
    """
    Return messages that arrived since `cursor`, plus the advanced cursor.

    The cursor is {"v": 1, "since": <ISO date>, "peers": {<peer_id>: <last id>}}.
    It tracks the last seen message id per dialog — NOT the unread flag, which
    the account owner changes by reading on another device. All dates compared
    are Telegram server dates, so local clock skew does not matter.

    - Empty cursor → baseline: record current positions, report nothing.
      (Otherwise the first run would replay the account's entire history.)
    - `limit` is checked between dialogs. When hit, "since" is NOT advanced,
      so the next run picks up the dialogs not yet visited.
    - At most `per_dialog` newest messages are taken per dialog; older ones in
      a burst are skipped and the dialog is listed in "overflow".
    - Outgoing messages advance the cursor but are only reported with
      include_out. Dialogs filtered out by `kind` advance silently too.
    """
    cursor   = cursor or {}
    since    = _parse_since(cursor.get('since'))
    peers    = {str(k): int(v) for k, v in (cursor.get('peers') or {}).items()}
    baseline = since is None

    collected  = []
    overflow   = []
    truncated  = False
    high_water = since

    async for dialog in client.iter_dialogs():
        top = dialog.message
        if top is None:
            continue
        key       = str(dialog.id)
        last_seen = peers.get(key)

        if baseline:
            peers[key] = max(top.id, last_seen or 0)
            high_water = top.date if high_water is None else max(high_water, top.date)
            continue

        if not getattr(dialog, 'pinned', False) and top.date < since - _SCAN_SLACK:
            break

        if last_seen is not None and top.id <= last_seen:
            continue

        if not kind_matches(dialog.entity, kind):
            peers[key] = top.id
            high_water = max(high_water, top.date)
            continue

        if len(collected) >= limit:
            truncated = True
            break

        if last_seen is None:
            # Not in the cursor: either a brand-new dialog, or one that was
            # never reached because earlier scans stopped before it.
            if top.date <= since:
                peers[key] = top.id
                continue
            fetched = await client.get_messages(dialog.entity, limit=per_dialog)
            fresh   = [m for m in fetched if m.date > since]
            more    = len(fetched) >= per_dialog and all(m.date > since for m in fetched)
        else:
            fetched = await client.get_messages(dialog.entity, limit=per_dialog, min_id=last_seen)
            fresh   = list(fetched)
            more    = len(fetched) >= per_dialog

        if more:
            overflow.append(dialog.id)

        chat = entity_dict(dialog.entity, dialog.id)
        for m in sorted(fresh, key=lambda m: m.id):
            if m.out and not include_out:
                continue
            collected.append({**message_dict(m), 'chat': chat,
                              'private': chat['kind'] in ('user', 'bot')})

        peers[key] = max([top.id, last_seen or 0] + [m.id for m in fetched])
        high_water = max(high_water, top.date)

    next_since = high_water if not truncated else since
    return {
        'messages':  collected,
        'count':     len(collected),
        'baseline':  baseline,
        'truncated': truncated,
        'overflow':  overflow,
        'cursor':    {'v': CURSOR_VERSION, 'since': next_since, 'peers': peers},
    }


def _parse_opts(args, spec):
    """Tiny option parser: spec maps '--name' to a type (bool for flags)."""
    opts = {}
    it   = iter(args)
    for a in it:
        if a not in spec:
            raise usage_error(f'Unknown option: {a}')
        if spec[a] is bool:
            opts[a] = True
            continue
        try:
            raw = next(it)
        except StopIteration:
            raise usage_error(f'{a} requires a value')
        opts[a] = spec[a](raw) if spec[a] is not str else raw
    return opts


def _load_state(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError) as ex:
        raise CliError(f'Cannot read state file {p}: {ex}', code='bad_state', exit_code=EXIT_USAGE)


def _save_state(path, cursor):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(cursor, f, default=_json_default)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


async def cmd_updates(*args):
    """
    New messages since the last call.

    Cursor storage, pick one:
      (default)          <data dir>/updates_state.json, advanced automatically
      --state FILE       custom state file, advanced automatically
      --cursor JSON      caller-managed cursor; nothing is written, the new
                         cursor is in the output (use with --json)
    Options:
      --no-commit        with a state file: report, but do not advance it
      --reset            discard the stored cursor and take a new baseline
      --limit N          soft cap on reported messages (default 100)
      --per-dialog N     max messages taken from one dialog (default 20)
      --kind K           users | groups | channels
      --include-out      also report messages sent from this account
    """
    opts = _parse_opts(args, {
        '--state': str, '--cursor': str, '--no-commit': bool, '--reset': bool,
        '--limit': str, '--per-dialog': str, '--kind': str, '--include-out': bool,
    })
    if '--state' in opts and '--cursor' in opts:
        raise usage_error('Use either --state or --cursor, not both')
    kind = opts.get('--kind')
    if kind is not None and kind not in KINDS:
        raise usage_error(f"--kind must be one of: {', '.join(KINDS)}")
    limit      = to_int(opts.get('--limit', 100), '--limit', 1)
    per_dialog = to_int(opts.get('--per-dialog', 20), '--per-dialog', 1)

    state_path = None
    if '--cursor' in opts:
        try:
            cursor = json.loads(opts['--cursor']) if opts['--cursor'].strip() else {}
        except ValueError as ex:
            raise usage_error(f'--cursor is not valid JSON: {ex}')
        if not isinstance(cursor, dict):
            raise usage_error('--cursor must be a JSON object')
    else:
        state_path = opts.get('--state', str(_UPDATES_STATE))
        cursor     = {} if opts.get('--reset') else _load_state(state_path)

    if opts.get('--reset'):
        cursor = {}

    async with connected() as client:
        result = await collect_updates(client, cursor, limit=limit, per_dialog=per_dialog,
                                       kind=kind, include_out=bool(opts.get('--include-out')))

    committed = False
    if state_path and not opts.get('--no-commit'):
        _save_state(state_path, result['cursor'])
        committed = True
    result['committed'] = committed
    if state_path:
        result['state_file'] = state_path

    def render(d):
        print(f"\n{RULE}")
        if d['baseline']:
            print("  UPDATES — baseline taken; new messages will be reported from now on")
        else:
            print(f"  UPDATES — {d['count']} new")
        print(RULE)
        for m in d['messages']:
            c   = m['chat']
            ref = f"@{c['username']}" if c['username'] else str(c['peer_id'])
            who = 'Me' if m['out'] else (
                (m['sender'] or {}).get('username') or (m['sender'] or {}).get('name') or '???')
            text = m['text'] or ''
            if m['media']:
                label = media_label_from_dict(m['media'])
                text  = f'{label} {text}'.strip()
            print(f"{fmt_date(m['date'])} | {ICONS[c['kind']]} {c['name']} ({ref}) | {who}: "
                  f"{truncate(text, 1000)}  [msg {m['id']}]")
        if d['truncated']:
            print("  … limit reached — run again for the rest")
        if d['overflow']:
            print(f"  … busy dialogs, older messages skipped: {', '.join(map(str, d['overflow']))}")
        print(RULE)
        if not d['committed'] and d.get('state_file'):
            print("  (cursor not saved: --no-commit)")

    emit(result, render)


def media_label_from_dict(md):
    label = md['type'].replace('_', ' ')
    dur = md.get('duration')
    if dur:
        dur = int(dur)
        label += f' {dur // 60}:{dur % 60:02d}'
    return f'({label})'

# -- Config -------------------------------------------------------------------

async def cmd_config():
    """Show current config and data dir."""
    creds, source = get_credentials()
    session_exists = Path(SESSION + '.session').exists()
    data = {
        'version':        __version__,
        'data_dir':       str(_DATA_DIR),
        'config_file':    str(CONFIG_FILE),
        'session_file':   SESSION + '.session',
        'credentials':    source or 'missing',
        'api_id':         creds['api_id'] if creds else None,
        'api_hash':       (creds['api_hash'][:4] + '****' + creds['api_hash'][-4:]) if creds else None,
        'session_exists': session_exists,
    }

    def render(d):
        print(f"\n  tgcli:    {d['version']}")
        print(f"  Data dir: {d['data_dir']}")
        print(f"  Config:   {d['config_file']}")
        print(f"  Session:  {d['session_file']}")
        if creds:
            print(f"  Creds:    from {d['credentials']}")
            print(f"  api_id:   {d['api_id']}")
            print(f"  api_hash: {d['api_hash']}")
        else:
            print("  Creds:    [SETUP] not found — set TELEGRAM_API_ID/TELEGRAM_API_HASH or run auth:init")
        print(f"  Status:   {'[OK] session file present' if session_exists else '✗ not authorized, run auth'}")

    emit(data, render)

# -- Authorization ------------------------------------------------------------

def _user_brief(me):
    return {'id': me.id, 'first_name': me.first_name, 'username': me.username}


async def cmd_auth():
    """Interactive authorization wizard (terminal only)."""
    if not sys.stdin.isatty():
        raise usage_error('auth is interactive and needs a terminal. '
                          'Use auth:phone / auth:code / auth:password from scripts.')
    creds = require_credentials(interactive=True)
    print("[AUTH] Telegram Authorization")
    print(f"   Session: {SESSION}.session\n")

    async with connected(require_auth=False, creds=creds) as client:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"[OK] Already authorized as {me.first_name} (@{me.username})")
            return
        phone = input("[PHONE] Phone number (e.g. +19001234567): ").strip()
        sent = await client.send_code_request(phone)
        info = _describe_sent_code(sent)
        print(f"   Code sent {info['hint']}"
              + (f" <{info['email_pattern']}>" if 'email_pattern' in info else ''))
        code = input("[CODE] Telegram code: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = input("[2FA] Two-factor authentication password: ").strip()
            await client.sign_in(password=password)
        me = await client.get_me()

    print(f"\n[OK] Success! Authorized as {me.first_name} (@{me.username})")
    print("  All commands are now available without re-authorization.")



_CODE_DELIVERY = {
    'SentCodeTypeApp':               ('app',   'in the Telegram app — a message from "Telegram" (id 777000) in an already logged-in session'),
    'SentCodeTypeSms':               ('sms',   'by SMS'),
    'SentCodeTypeSmsWord':           ('sms',   'by SMS (a word instead of digits)'),
    'SentCodeTypeSmsPhrase':         ('sms',   'by SMS (a phrase instead of digits)'),
    'SentCodeTypeFirebaseSms':       ('sms',   'by SMS via Firebase — may never arrive for third-party clients; try auth:resend'),
    'SentCodeTypeCall':              ('call',  'by a phone call (the code is dictated)'),
    'SentCodeTypeFlashCall':         ('flash_call',  'by a flash call (the code is in the caller number)'),
    'SentCodeTypeMissedCall':        ('missed_call', 'by a missed call (the code is the last digits of the caller number)'),
    'SentCodeTypeEmailCode':         ('email', 'to the login email of the account'),
    'SentCodeTypeSetUpEmailRequired':('setup_email_required', 'nowhere yet: Telegram requires a login email to be set up first (do it in an official app)'),
    'SentCodeTypeFragmentSms':       ('fragment', 'via Fragment (anonymous number) — open fragment.com'),
}

_NEXT_TYPE = {
    'CodeTypeSms': 'sms', 'CodeTypeCall': 'call', 'CodeTypeFlashCall': 'flash_call',
    'CodeTypeMissedCall': 'missed_call', 'CodeTypeFragmentSms': 'fragment',
}


def _describe_sent_code(sent):
    """Where Telegram sent the login code — the answer to "the code never came"."""
    kind = type(getattr(sent, 'type', None)).__name__
    delivery, hint = _CODE_DELIVERY.get(kind, ('unknown', f'unknown delivery ({kind})'))
    info = {'delivery': delivery, 'hint': hint}
    length = getattr(sent.type, 'length', None)
    if length:
        info['length'] = length
    pattern = getattr(sent.type, 'email_pattern', None)
    if pattern:
        info['email_pattern'] = pattern
    nxt = getattr(sent, 'next_type', None)
    if nxt is not None:
        info['next_delivery'] = _NEXT_TYPE.get(type(nxt).__name__, type(nxt).__name__)
    if getattr(sent, 'timeout', None):
        info['resend_after'] = sent.timeout
    return info


def _print_code_info(phone, info):
    print(f"[OK] Code sent to {phone} {info['hint']}"
          + (f" <{info['email_pattern']}>" if 'email_pattern' in info else ''))
    if 'next_delivery' in info:
        wait = f" after {info['resend_after']}s" if 'resend_after' in info else ''
        print(f"  Not arriving? telegram auth:resend{wait} → {info['next_delivery']}")
    print("  Next step: telegram auth:code <code>")


async def cmd_auth_init(api_id, api_hash):
    """Step 0: save API credentials to the config file."""
    if not str(api_id).isdigit() or not api_hash:
        raise usage_error('api_id must be a number, api_hash must be a non-empty string')
    save_config(api_id, api_hash)
    emit({'saved': str(CONFIG_FILE), 'next_step': 'phone'},
         lambda d: print(f"[OK] Credentials saved to {d['saved']}\n"
                         f"  Next step: telegram auth:phone <phone_number>"))


async def cmd_auth_phone(phone):
    """Step 1: send the confirmation code to the phone number."""
    async with connected(require_auth=False) as client:
        if await client.is_user_authorized():
            me = await client.get_me()
            _clear_auth_state()
            emit({'authorized': True, 'next_step': None, 'user': _user_brief(me)},
                 lambda _: print(f"[OK] Already authorized as {me.first_name} (@{me.username})"))
            return
        result = await client.send_code_request(phone)
        _save_auth_state({'phone': phone, 'phone_code_hash': result.phone_code_hash})
        info = _describe_sent_code(result)

    emit({'authorized': False, 'next_step': 'code', 'phone': phone, 'code': info},
         lambda _: _print_code_info(phone, info))


async def cmd_auth_resend():
    """Step 1b: ask Telegram to deliver the code another way (SMS, call...)."""
    from telethon.tl.functions.auth import ResendCodeRequest
    state = _load_auth_state()
    if not state.get('phone') or not state.get('phone_code_hash'):
        raise CliError('No pending auth state. Run auth:phone first.', code='no_auth_state')

    async with connected(require_auth=False) as client:
        result = await client(ResendCodeRequest(state['phone'], state['phone_code_hash']))
        _save_auth_state({'phone': state['phone'], 'phone_code_hash': result.phone_code_hash})
        info = _describe_sent_code(result)

    emit({'authorized': False, 'next_step': 'code', 'phone': state['phone'], 'code': info},
         lambda _: _print_code_info(state['phone'], info))


async def cmd_auth_code(code):
    """Step 2: submit the code. Reports next_step=password when 2FA is on."""
    state = _load_auth_state()
    if not state.get('phone') or not state.get('phone_code_hash'):
        raise CliError('No pending auth state. Run auth:phone first.', code='no_auth_state')

    async with connected(require_auth=False) as client:
        try:
            await client.sign_in(phone=state['phone'], code=code,
                                 phone_code_hash=state['phone_code_hash'])
        except SessionPasswordNeededError:
            emit({'authorized': False, 'next_step': 'password'},
                 lambda _: print("[2FA] Two-factor authentication required.\n"
                                 "  Next step: telegram auth:password <your_password>"))
            return
        me = await client.get_me()

    _clear_auth_state()
    emit({'authorized': True, 'next_step': None, 'user': _user_brief(me)},
         lambda _: print(f"[OK] Authorized as {me.first_name} (@{me.username})\n"
                         f"  All commands are now available."))


async def cmd_auth_password(password):
    """Step 3 (2FA only): submit the cloud password."""
    async with connected(require_auth=False) as client:
        await client.sign_in(password=password)
        me = await client.get_me()

    _clear_auth_state()
    emit({'authorized': True, 'next_step': None, 'user': _user_brief(me)},
         lambda _: print(f"[OK] Authorized as {me.first_name} (@{me.username})\n"
                         f"  All commands are now available."))

# -- Help ---------------------------------------------------------------------

HELP = f"""
Telegram Tool (tgcli {__version__}) — commands:

  dialogs   [N] [users|groups|channels]  List dialogs (up to N, default 30)
  contacts                               List saved contacts (address book)
  resolve   <query...>                   Find real targets by name/@username/id
  read      <@chat|id> [N]               Last N messages (default 15)
  send      <@user|id> <text...>         Send a message
  unread    [N]                          Dialogs with unread messages
  search    <@chat|id> <query...>        Search messages in a chat
  info      <@user|channel|id>           Info about a user, group or channel
  mark_read <@chat|id>                   Mark dialog as read
  updates   [options]                    New messages since the last call
  download  <@chat|id> <msg_id> [dest]   Download a message's media
  me                                     My account info
  config                                 Show current config and data dir

updates options:
  --state FILE | --cursor JSON           Where the cursor lives (default: data dir)
  --no-commit  --reset                   Don't advance / start a new baseline
  --limit N  --per-dialog N              Caps (default 100 / 20)
  --kind users|groups|channels           Only these dialogs
  --include-out                          Also report your own messages

Interactive authorization (run once in a terminal):
  auth                                   Full interactive auth wizard

Non-interactive authorization (for scripts and web UIs):
  auth:init <api_id> <api_hash>          Save API credentials (step 0, if needed)
  auth:phone <phone>                     Send confirmation code  (step 1)
  auth:resend                            Deliver the code another way (SMS, call)
  auth:code  <code>                      Submit the code         (step 2)
  auth:password <password>               Submit 2FA password     (step 3, if needed)

Global options (before the command):
  --json                                 Machine-readable output (one JSON object)
  --version                              Print version

Environment variables:
  TELEGRAM_DATA_DIR          Directory for config, session and state files.
                             Defaults to the script's own directory.
  TELEGRAM_API_ID            API credentials; override telegram_config.json.
  TELEGRAM_API_HASH          Set both or neither.
  TELEGRAM_LOCK_TIMEOUT      Seconds to wait for a busy session (default 30).
  TELEGRAM_DOWNLOAD_MAX_MB   Download size cap, 0 = unlimited (default 50).
  TGCLI_JSON=1               Same as --json.

Exit codes:
  0 ok, 1 error, 2 usage, 3 not authorized / no credentials,
  4 flood wait (see retry_after), 5 session busy

Examples:
  python3 telegram_tool.py dialogs 50 channels
  python3 telegram_tool.py read @durov 20
  python3 telegram_tool.py send @username Hello how are you
  python3 telegram_tool.py --json updates --kind users
  python3 telegram_tool.py download @username 4215

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
    'updates':       cmd_updates,
    'download':      cmd_download,
    'me':            cmd_me,
    'auth':          cmd_auth,
    'auth:init':     cmd_auth_init,
    'auth:phone':    cmd_auth_phone,
    'auth:resend':   cmd_auth_resend,
    'auth:code':     cmd_auth_code,
    'auth:password': cmd_auth_password,
    'config':        cmd_config,
}

USAGE = {
    'dialogs':       'dialogs [N] [users|groups|channels]',
    'contacts':      'contacts',
    'resolve':       'resolve <query...>',
    'read':          'read <@chat|id> [N]',
    'send':          'send <@user|id> <text...>',
    'unread':        'unread [N]',
    'search':        'search <@chat|id> <query...>',
    'info':          'info <@user|channel|id>',
    'mark_read':     'mark_read <@chat|id>',
    'updates':       'updates [--state FILE | --cursor JSON] [--limit N] ...',
    'download':      'download <@chat|id> <msg_id> [dest]',
    'me':            'me',
    'auth':          'auth',
    'auth:init':     'auth:init <api_id> <api_hash>',
    'auth:phone':    'auth:phone <phone>',
    'auth:resend':   'auth:resend',
    'auth:code':     'auth:code <code>',
    'auth:password': 'auth:password <password>',
    'config':        'config',
}

# Commands that may run without an existing session file.
NON_SESSION_COMMANDS = {'auth', 'auth:init', 'auth:phone', 'auth:resend', 'auth:code', 'auth:password', 'config'}

# Commands that never touch the session and so skip the lock.
UNLOCKED_COMMANDS = {'config', 'auth:init'}


def main(argv):
    global OUTPUT_JSON
    OUTPUT_JSON = os.environ.get('TGCLI_JSON', '').strip().lower() in ('1', 'true', 'yes')

    args = list(argv)
    while args and args[0].startswith('-'):
        flag = args.pop(0)
        if flag == '--json':
            OUTPUT_JSON = True
        elif flag in ('-h', '--help'):
            print(HELP)
            return EXIT_OK
        elif flag == '--version':
            emit({'version': __version__}, lambda d: print(f"tgcli {d['version']}"))
            return EXIT_OK
        else:
            emit_error(usage_error(f'Unknown option: {flag}'))
            return EXIT_USAGE

    if not args or args[0] == 'help':
        print(HELP)
        return EXIT_OK

    cmd, cargs = args[0], args[1:]
    fn = COMMANDS.get(cmd)
    if fn is None:
        emit_error(usage_error(f'Unknown command: {cmd}'))
        if not OUTPUT_JSON:
            print(HELP)
        return EXIT_USAGE

    try:
        inspect.signature(fn).bind(*cargs)
    except TypeError:
        emit_error(usage_error(f'Usage: {USAGE[cmd]}'))
        return EXIT_USAGE

    try:
        if cmd not in NON_SESSION_COMMANDS and not Path(SESSION + '.session').exists():
            raise CliError(
                "Session not found. Run first:\n"
                "   python3 telegram_tool.py auth\n"
                "   or: python3 telegram_tool.py auth:phone <phone>",
                code='no_session', exit_code=EXIT_AUTH, tag='WARN')

        if cmd in UNLOCKED_COMMANDS:
            asyncio.run(fn(*cargs))
        else:
            with session_lock():
                asyncio.run(fn(*cargs))
        return EXIT_OK

    except CliError as e:
        emit_error(e)
        return e.exit_code
    except FloodWaitError as e:
        emit_error(CliError(f'Too many requests, wait {e.seconds} sec.', code='flood_wait',
                            exit_code=EXIT_FLOOD, tag='WAIT', retry_after=e.seconds))
        return EXIT_FLOOD
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted")
        return 130
    except Exception as e:
        emit_error(CliError(str(e) or e.__class__.__name__, code='error'))
        return EXIT_ERROR


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
