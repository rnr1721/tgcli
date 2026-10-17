"""QR login: the background serve loop, the state file and its reporting."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import telegram_tool as tt  # noqa: E402
from telethon.errors import SessionPasswordNeededError  # noqa: E402


class FakeQR:
    def __init__(self, script):
        self.script = list(script)      # what each wait() does
        self.n = 0
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)

    @property
    def url(self):
        return f'tg://login?token=T{self.n}'

    async def wait(self, timeout=None):
        step = self.script.pop(0)
        if step == 'timeout':
            raise asyncio.TimeoutError()
        if step == 'password':
            raise SessionPasswordNeededError(request=None)
        return SimpleNamespace(id=7, first_name='Ada', username='ada')

    async def recreate(self):
        self.n += 1


class FakeClient:
    def __init__(self, qr, authorized=False):
        self.qr, self.authorized = qr, authorized

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=7, first_name='Ada', username='ada')

    async def qr_login(self):
        return self.qr


class QrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._saved = (tt._DATA_DIR, tt._QR_FILE, tt._QR_LOG, tt._AUTH_STATE_FILE, tt.connected, tt.OUTPUT_JSON)
        tt._DATA_DIR, tt._QR_FILE, tt._QR_LOG = d, d / 'telegram_qr.json', d / 'telegram_qr.log'
        tt._AUTH_STATE_FILE = d / 'telegram_auth_state.json'
        tt.OUTPUT_JSON = True
        self.writes = []
        real_write = tt._qr_write

        def spy(state):
            self.writes.append(dict(state))
            real_write(state)
        self._real_write = real_write
        tt._qr_write = spy

    def tearDown(self):
        (tt._DATA_DIR, tt._QR_FILE, tt._QR_LOG, tt._AUTH_STATE_FILE, tt.connected, tt.OUTPUT_JSON) = self._saved
        tt._qr_write = self._real_write
        self.tmp.cleanup()

    def serve(self, client):
        @asynccontextmanager
        async def fake_connected(require_auth=True, creds=None):
            yield client
        tt.connected = fake_connected
        asyncio.run(tt.cmd_auth_qr_serve())
        return tt._qr_read()

    def report(self, state):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            tt._qr_report(state)
        return json.loads(buf.getvalue())

    # ── serve loop ──────────────────────────────────────────────────────────

    def test_scan_authorizes_and_clears_phone_state(self):
        tt._AUTH_STATE_FILE.write_text('{"phone": "+1"}')
        state = self.serve(FakeClient(FakeQR(['user'])))
        self.assertEqual('authorized', state['status'])
        self.assertEqual('ada', state['user']['username'])
        self.assertEqual('waiting', self.writes[0]['status'])
        self.assertEqual('tg://login?token=T0', self.writes[0]['url'])
        self.assertFalse(tt._AUTH_STATE_FILE.exists())

    def test_expired_token_is_recreated_with_a_new_url(self):
        self.serve(FakeClient(FakeQR(['timeout', 'timeout', 'user'])))
        urls = [w['url'] for w in self.writes if w['status'] == 'waiting']
        self.assertEqual(['tg://login?token=T0', 'tg://login?token=T1', 'tg://login?token=T2'], urls)
        self.assertEqual('authorized', self.writes[-1]['status'])

    def test_two_factor_account_ends_in_password_step(self):
        state = self.serve(FakeClient(FakeQR(['password'])))
        self.assertEqual('password', state['status'])
        self.assertEqual({'authorized': False, 'next_step': 'password', 'ok': True}, self.report(state))

    def test_already_authorized_session_needs_no_qr(self):
        state = self.serve(FakeClient(FakeQR([]), authorized=True))
        self.assertEqual('authorized', state['status'])

    def test_ttl_ends_the_wait(self):
        os.environ['TELEGRAM_QR_TTL'] = '30'
        try:
            qr = FakeQR(['timeout'] * 1000)
            t0 = time.monotonic()
            real = time.monotonic
            tt.time.monotonic = lambda: real() + (0 if qr.n == 0 else 31)   # jump past the TTL after one refresh
            try:
                state = self.serve(FakeClient(qr))
            finally:
                tt.time.monotonic = real
        finally:
            del os.environ['TELEGRAM_QR_TTL']
        self.assertEqual('expired', state['status'])
        self.assertLess(time.monotonic() - t0, 5)

    def test_unexpected_error_is_recorded_not_lost(self):
        class Boom(FakeClient):
            async def qr_login(self):
                raise RuntimeError('network down')
        state = self.serve(Boom(None))
        self.assertEqual('error', state['status'])
        self.assertIn('network down', state['error'])

    # ── reporting ───────────────────────────────────────────────────────────

    def test_waiting_with_live_pid_reports_the_qr(self):
        r = self.report({'status': 'waiting', 'pid': os.getpid(), 'url': 'tg://login?token=X', 'expires': 123})
        self.assertEqual('qr', r['next_step'])
        self.assertEqual('tg://login?token=X', r['qr']['url'])

    def test_waiting_with_dead_pid_is_expired(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        with self.assertRaises(tt.CliError) as e:
            tt._qr_report({'status': 'waiting', 'pid': p.pid, 'url': 'x'})
        self.assertEqual('qr_expired', e.exception.code)

    def test_no_state_is_expired_and_flood_carries_retry_after(self):
        with self.assertRaises(tt.CliError) as e:
            tt._qr_report({})
        self.assertEqual('qr_expired', e.exception.code)
        with self.assertRaises(tt.CliError) as e:
            tt._qr_report({'status': 'error', 'code': 'flood_wait', 'retry_after': 40, 'error': 'wait'})
        self.assertEqual('flood_wait', e.exception.code)
        self.assertEqual(40, e.exception.extra['retry_after'])

    def test_cancel_stops_a_running_serve(self):
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self._real_write({'status': 'waiting', 'pid': p.pid})
        tt._qr_cancel()
        self.assertIsNotNone(p.wait(timeout=5))
        self.assertFalse(tt._QR_FILE.exists())


class QrCliTest(unittest.TestCase):
    """auth:qr really spawns serve; without network it must fail cleanly, not hang."""

    def test_start_reports_serve_failure(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, 'TELEGRAM_DATA_DIR': d, 'TELEGRAM_API_ID': '1', 'TELEGRAM_API_HASH': 'x',
                   'TELEGRAM_LOCK_TIMEOUT': '5'}
            script = str(Path(tt.__file__).resolve())
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, script, '--json', 'auth:qr'], env=env,
                               capture_output=True, text=True, timeout=60)
            out = json.loads(r.stdout)
            self.assertFalse(out['ok'])
            self.assertNotEqual('qr_expired', out['code'])
            self.assertLess(time.monotonic() - t0, 40)

            s = subprocess.run([sys.executable, script, '--json', 'auth:qr:status'], env=env,
                               capture_output=True, text=True, timeout=10)
            self.assertFalse(json.loads(s.stdout)['ok'])


if __name__ == '__main__':
    unittest.main()


class SendArgsTest(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual((None, 'hello world'), tt._split_send_args(['hello', 'world']))

    def test_reply_to(self):
        self.assertEqual((4215, 'да, согласна'), tt._split_send_args(['--reply-to', '4215', 'да,', 'согласна']))

    def test_double_dash_protects_text(self):
        self.assertEqual((None, '--reply-to is a flag'), tt._split_send_args(['--', '--reply-to is a flag']))
        self.assertEqual((7, '-- hi'), tt._split_send_args(['--reply-to', '7', '--', '-- hi']))

    def test_options_only_right_after_target(self):
        self.assertEqual((None, 'see --reply-to 5'), tt._split_send_args(['see', '--reply-to', '5']))

    def test_bad_reply_id(self):
        with self.assertRaises(tt.CliError):
            tt._split_send_args(['--reply-to', 'abc', 'hi'])
        with self.assertRaises(tt.CliError):
            tt._split_send_args(['--reply-to'])


class SendReplyFallbackTest(unittest.TestCase):
    """A reply to a vanished message still delivers the words."""

    def setUp(self):
        self._saved = (tt.connected, tt.resolve_entity, tt.OUTPUT_JSON)
        tt.OUTPUT_JSON = True

    def tearDown(self):
        tt.connected, tt.resolve_entity, tt.OUTPUT_JSON = self._saved

    def run_send(self, client, *args):
        import io
        from contextlib import redirect_stdout

        @asynccontextmanager
        async def fake_connected(require_auth=True, creds=None):
            yield client

        async def fake_resolve(c, target):
            return SimpleNamespace(id=42, first_name='Eugeny', last_name=None, username='e', title=None)

        tt.connected, tt.resolve_entity = fake_connected, fake_resolve
        buf = io.StringIO()
        with redirect_stdout(buf):
            asyncio.run(tt.cmd_send('@e', *args))
        return json.loads(buf.getvalue()), client.calls

    class Client:
        def __init__(self, fail_reply=False):
            self.calls, self.fail_reply = [], fail_reply

        async def send_message(self, entity, text, reply_to=None):
            self.calls.append((text, reply_to))
            if reply_to is not None and self.fail_reply:
                raise Exception('The specified reply_to_msg_id is invalid (REPLY_MESSAGE_ID_INVALID)')
            return SimpleNamespace(id=99, date=datetime(2026, 10, 17, tzinfo=timezone.utc), chat_id=42)

    def test_reply_is_sent_as_reply(self):
        out, calls = self.run_send(self.Client(), '--reply-to', '71', '--', 'привет')
        self.assertEqual([('привет', 71)], calls)
        self.assertEqual(71, out['sent']['reply_to'])

    def test_missing_target_message_falls_back_to_plain(self):
        out, calls = self.run_send(self.Client(fail_reply=True), '--reply-to', '71', '--', 'привет')
        self.assertEqual([('привет', 71), ('привет', None)], calls)
        self.assertIsNone(out['sent']['reply_to'])
        self.assertTrue(out['ok'])
