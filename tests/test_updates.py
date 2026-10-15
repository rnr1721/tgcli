"""
Tests for the `updates` cursor logic (collect_updates) against a fake client,
plus CLI smoke tests for paths that need no Telegram connection.

Run: python3 -m unittest discover -s tests -v
"""
import os
import sys
import json
import asyncio
import tempfile
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / 'telegram_tool.py'
sys.path.insert(0, str(HERE.parent))

import telegram_tool as tg  # noqa: E402

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


class FakeEntity:
    def __init__(self, title):
        self.title = title
        self.id = abs(hash(title)) % 10_000
        self.username = None


class FakeMsg:
    def __init__(self, chat_id, mid, date, text='', out=False):
        self.id = mid
        self.chat_id = chat_id
        self.date = date
        self.message = text
        self.out = out
        self.sender = None
        self.sender_id = None
        self.reply_to_msg_id = None
        self.mentioned = False
        self.media = None


class FakeDialog:
    def __init__(self, peer_id, entity, message, pinned=False):
        self.id = peer_id
        self.entity = entity
        self.message = message
        self.pinned = pinned


class FakeClient:
    """Holds chats as {peer_id: [FakeMsg...]}; dialogs ordered like Telegram."""

    def __init__(self):
        self.chats = {}
        self.entities = {}
        self.pinned = set()
        self.fetch_calls = []

    def add(self, peer_id, mid, minutes, text='', out=False):
        self.entities.setdefault(peer_id, FakeEntity(f'chat{peer_id}'))
        self.chats.setdefault(peer_id, []).append(FakeMsg(peer_id, mid, at(minutes), text, out))

    async def iter_dialogs(self):
        tops = {p: max(ms, key=lambda m: m.id) for p, ms in self.chats.items() if ms}
        pinned = [p for p in tops if p in self.pinned]
        rest = sorted((p for p in tops if p not in self.pinned),
                      key=lambda p: tops[p].date, reverse=True)
        for p in pinned + rest:
            yield FakeDialog(p, self.entities[p], tops[p], pinned=p in self.pinned)

    async def get_messages(self, entity, limit=20, min_id=0):
        peer = next(p for p, e in self.entities.items() if e is entity)
        self.fetch_calls.append(peer)
        ms = sorted((m for m in self.chats[peer] if m.id > min_id), key=lambda m: m.id, reverse=True)
        return ms[:limit]


def run(client, cursor=None, **kw):
    return asyncio.run(tg.collect_updates(client, cursor, **kw))


def roundtrip(cursor):
    """Cursor goes through JSON in real use — make the tests do the same."""
    return json.loads(json.dumps(cursor, default=tg._json_default))


class CollectUpdatesTest(unittest.TestCase):

    def setUp(self):
        self.c = FakeClient()
        self.c.add(1, 10, 0, 'old in 1')
        self.c.add(2, 20, 1, 'old in 2')

    def baseline(self):
        r = run(self.c)
        self.assertTrue(r['baseline'])
        self.assertEqual(r['count'], 0)
        return roundtrip(r['cursor'])

    def test_baseline_reports_nothing_and_records_positions(self):
        cur = self.baseline()
        self.assertEqual(cur['peers'], {'1': 10, '2': 20})
        self.assertEqual(self.c.fetch_calls, [])

    def test_new_message_reported_once(self):
        cur = self.baseline()
        self.c.add(1, 11, 5, 'hello')
        r = run(self.c, cur)
        self.assertEqual([m['text'] for m in r['messages']], ['hello'])
        self.assertTrue(r['messages'][0]['private'] is False)  # fake entity kind = chat
        r2 = run(self.c, roundtrip(r['cursor']))
        self.assertEqual(r2['count'], 0)

    def test_messages_reported_in_chronological_order(self):
        cur = self.baseline()
        self.c.add(1, 11, 5, 'a')
        self.c.add(1, 12, 6, 'b')
        self.c.add(1, 13, 7, 'c')
        r = run(self.c, cur)
        self.assertEqual([m['text'] for m in r['messages']], ['a', 'b', 'c'])

    def test_outgoing_filtered_but_cursor_advances(self):
        cur = self.baseline()
        self.c.add(2, 21, 5, 'mine', out=True)
        r = run(self.c, cur)
        self.assertEqual(r['count'], 0)
        self.assertEqual(r['cursor']['peers']['2'], 21)
        r_out = run(self.c, cur, include_out=True)
        self.assertEqual([m['text'] for m in r_out['messages']], ['mine'])

    def test_new_dialog_after_baseline_is_included(self):
        cur = self.baseline()
        self.c.add(3, 30, 5, 'first contact')
        r = run(self.c, cur)
        self.assertEqual([m['text'] for m in r['messages']], ['first contact'])
        self.assertEqual(r['cursor']['peers']['3'], 30)

    def test_scan_stops_at_old_dialogs(self):
        for p in range(10, 20):                      # long tail of stale dialogs
            self.c.add(p, 100 + p, -1000 - p, 'ancient')
        cur = self.baseline()
        self.c.fetch_calls.clear()
        self.c.add(1, 11, 5, 'new')
        r = run(self.c, cur)
        self.assertEqual(r['count'], 1)
        self.assertEqual(self.c.fetch_calls, [1])    # no fetch for the tail

    def test_pinned_old_dialog_does_not_stop_scan(self):
        self.c.add(9, 90, -2000, 'pinned ancient')
        self.c.pinned.add(9)
        cur = self.baseline()
        self.c.add(2, 21, 5, 'behind a pinned dialog')
        r = run(self.c, cur)
        self.assertEqual([m['text'] for m in r['messages']], ['behind a pinned dialog'])

    def test_limit_keeps_since_so_next_run_continues(self):
        cur = self.baseline()
        self.c.add(1, 11, 5, 'one')
        self.c.add(2, 21, 6, 'two')
        r = run(self.c, cur, limit=1)
        self.assertTrue(r['truncated'])
        self.assertEqual(r['count'], 1)
        self.assertEqual(roundtrip(r['cursor'])['since'], cur_since(cur))
        r2 = run(self.c, roundtrip(r['cursor']), limit=1)
        self.assertEqual(r2['count'], 1)
        got = {r['messages'][0]['text'], r2['messages'][0]['text']}
        self.assertEqual(got, {'one', 'two'})
        r3 = run(self.c, roundtrip(r2['cursor']))
        self.assertEqual(r3['count'], 0)

    def test_per_dialog_overflow_skips_older_and_flags(self):
        cur = self.baseline()
        for i in range(30):
            self.c.add(1, 11 + i, 5 + i, f'm{i}')
        r = run(self.c, cur, per_dialog=5)
        self.assertEqual([m['text'] for m in r['messages']], ['m25', 'm26', 'm27', 'm28', 'm29'])
        self.assertEqual(r['overflow'], [1])
        self.assertEqual(r['cursor']['peers']['1'], 40)

    def test_kind_filter_advances_silently(self):
        cur = self.baseline()
        self.c.add(1, 11, 5, 'filtered')
        r = run(self.c, cur, kind='users')          # fake entities are not users
        self.assertEqual(r['count'], 0)
        self.assertEqual(r['cursor']['peers']['1'], 11)

    def test_since_uses_server_dates(self):
        cur = self.baseline()
        self.assertEqual(cur_since(cur), at(1).isoformat())


def cur_since(cur):
    s = cur['since']
    return s if isinstance(s, str) else s.isoformat()


class CliSmokeTest(unittest.TestCase):
    """Paths that need no network: argument handling, config, error shapes."""

    def run_cli(self, *args, env_extra=None):
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items()
                   if k not in ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TGCLI_JSON')}
            env['TELEGRAM_DATA_DIR'] = d
            env.update(env_extra or {})
            p = subprocess.run([sys.executable, str(TOOL), *args],
                               capture_output=True, text=True, env=env, timeout=60)
            return p.returncode, p.stdout, p.stderr

    def test_version_json(self):
        code, out, _ = self.run_cli('--json', '--version')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['version'], tg.__version__)

    def test_no_session_is_auth_error(self):
        code, out, _ = self.run_cli('--json', 'read', '@x')
        self.assertEqual(code, tg.EXIT_AUTH)
        self.assertEqual(json.loads(out)['code'], 'no_session')

    def test_no_session_text_keeps_warn_tag(self):
        code, out, _ = self.run_cli('read', '@x')
        self.assertEqual(code, tg.EXIT_AUTH)
        self.assertIn('[WARN]', out)          # TelegramService::getStatus relies on it

    def test_unknown_command(self):
        code, out, _ = self.run_cli('--json', 'nope')
        self.assertEqual(code, tg.EXIT_USAGE)
        self.assertEqual(json.loads(out)['code'], 'usage')

    def test_wrong_arg_count(self):
        code, out, _ = self.run_cli('--json', 'download', '@x')
        self.assertEqual(code, tg.EXIT_USAGE)
        self.assertIn('Usage: download', json.loads(out)['error'])

    def test_config_reports_env_credentials(self):
        code, out, _ = self.run_cli('--json', 'config',
                                    env_extra={'TELEGRAM_API_ID': '123',
                                               'TELEGRAM_API_HASH': 'abcdef123456'})
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data['credentials'], 'env')
        self.assertEqual(data['api_id'], 123)
        self.assertEqual(data['api_hash'], 'abcd****3456')

    def test_partial_env_credentials_rejected(self):
        code, out, _ = self.run_cli('--json', 'config', env_extra={'TELEGRAM_API_ID': '123'})
        self.assertEqual(code, tg.EXIT_USAGE)
        self.assertEqual(json.loads(out)['code'], 'bad_config')

    def test_auth_phone_without_credentials(self):
        code, out, _ = self.run_cli('--json', 'auth:phone', '+10000000000')
        self.assertEqual(code, tg.EXIT_AUTH)
        self.assertEqual(json.loads(out)['code'], 'no_config')

    def test_interactive_auth_refuses_without_tty(self):
        code, out, _ = self.run_cli('auth')
        self.assertEqual(code, tg.EXIT_USAGE)
        self.assertIn('needs a terminal', out)

    def test_auth_init_then_config(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, TELEGRAM_DATA_DIR=d)
            p1 = subprocess.run([sys.executable, str(TOOL), '--json', 'auth:init', '42', 'hashhashhash'],
                                capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(p1.stdout)['next_step'], 'phone')
            self.assertEqual(oct(os.stat(Path(d) / 'telegram_config.json').st_mode & 0o777), '0o600')
            p2 = subprocess.run([sys.executable, str(TOOL), '--json', 'config'],
                                capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(p2.stdout)['credentials'], 'file')

    def test_session_lock_times_out(self):
        if tg.fcntl is None:
            self.skipTest('no fcntl')
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'telegram_session.session').write_bytes(b'')
            with open(Path(d, 'telegram_session.lock'), 'a') as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                env = dict(os.environ, TELEGRAM_DATA_DIR=d, TELEGRAM_LOCK_TIMEOUT='0.5',
                           TELEGRAM_API_ID='1', TELEGRAM_API_HASH='x')
                p = subprocess.run([sys.executable, str(TOOL), '--json', 'me'],
                                   capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(p.returncode, tg.EXIT_BUSY)
            self.assertEqual(json.loads(p.stdout)['code'], 'session_busy')

    def test_stdout_is_single_json_object(self):
        code, out, _ = self.run_cli('--json', 'config')
        self.assertEqual(len(out.strip().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
