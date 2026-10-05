"""Both Telegram bots against a fake Bot API: they survive the network, fail fast
on a bad token, and never write the token to their output.

Background (2026-10-05): the old production bot restarted 86 times in 4 days —
uncaught ReadTimeout / "Network is unreachable" / 502 from getUpdates — and
every traceback carried the token in the request URL.

Each test runs the real python-telegram-bot application in a subprocess — built
by the same ``app.build`` the management commands use and started through the
same ``myapp.telegram.polling.run`` — with ``base_url`` pointed at a local
HTTP server that plays Telegram. Everything the process writes (stdout +
stderr, i.e. what journald would get) is captured and checked. Nothing talks
to the real Telegram.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from myapp.telegram import polling

# Shaped like a real token (numeric id, colon, 35 url-safe characters), so the
# log redaction regex would match it too — the tests check that nothing even
# tried to log it.
TOKEN = "1234567890:AAHfake_" + "x" * 27
assert len(TOKEN.split(":", 1)[1]) == 35

RUNNER = r"""
import importlib, os, sys
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "server.settings")
import django
django.setup()
from myapp.telegram import polling
bot_app = importlib.import_module(f"myapp.telegram.{sys.argv[2]}.app")
application = bot_app.build(os.environ["KK_TEST_TOKEN"], base_url=f"http://127.0.0.1:{sys.argv[3]}/bot")
print("runner: polling", flush=True)
polling.run(application, bot_app.ALLOWED_UPDATES)
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


class FakeBotAPI:
    """Minimal Bot API: getMe (200 or 401), getUpdates following ``script``
    ("502" → Bad Gateway, "drop" → connection closed without an answer), then
    empty results; every other method → ``true``."""

    def __init__(self, port=0, get_me_status=200, script=()):
        self.calls = {}
        self.paths = []
        self.script = list(script)
        self.lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                with api.lock:
                    api.calls[method] = api.calls.get(method, 0) + 1
                    api.paths.append(self.path)
                    step = api.script.pop(0) if method == "getUpdates" and api.script else None
                if method == "getMe":
                    if get_me_status == 401:
                        return self._json(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                    return self._json(200, {"ok": True, "result": {
                        "id": 1234567890, "is_bot": True, "first_name": "Test", "username": "test_bot"}})
                if method == "getUpdates":
                    if step == "502":
                        body = b"<html>502 Bad Gateway</html>"
                        self.send_response(502)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return None
                    if step == "drop":
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return None
                    time.sleep(0.2)
                    return self._json(200, {"ok": True, "result": []})
                return self._json(200, {"ok": True, "result": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def count(self, method):
        with self.lock:
            return self.calls.get(method, 0)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class BotProcess:
    def __init__(self, bot, port, log_level="INFO"):
        self.output_file = tempfile.TemporaryFile(mode="w+")
        env = {
            **os.environ,
            "KK_TEST_TOKEN": TOKEN,
            # Never the developer's own bot / mail settings from .env.
            "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_COMMUNITY_BOT_TOKEN": "",
            "SMTP_USER": "", "SMTP_PASSWORD": "",
            "DJANGO_LOG_LEVEL": log_level, "PYTHONUNBUFFERED": "1",
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-c", RUNNER, str(settings.BASE_DIR), bot, str(port)],
            stdout=self.output_file, stderr=subprocess.STDOUT, env=env, cwd=str(settings.BASE_DIR),
        )

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        """Stop (SIGTERM — what systemd sends) and return everything it wrote."""
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.output_file.seek(0)
        output = self.output_file.read()
        self.output_file.close()
        return output


class PollingHelperTests(SimpleTestCase):
    def test_both_commands_poll_through_the_helper_with_bootstrap_retries(self):
        app = mock.Mock()
        polling.run(app, ["message"])
        app.run_polling.assert_called_once_with(
            allowed_updates=["message"], drop_pending_updates=False, bootstrap_retries=-1)

        for command, module, setting in (
            ("run_telegram_bot", "myapp.telegram.main_bot.app", {"TELEGRAM_BOT_TOKEN": TOKEN}),
            ("run_community_bot", "myapp.telegram.community_bot.app", {}),
        ):
            with self.subTest(command=command), override_settings(**setting), \
                    mock.patch("myapp.services.community.configured", return_value=True), \
                    mock.patch("myapp.services.community.token", return_value=TOKEN), \
                    mock.patch("myapp.services.community.chats", return_value={"general": -1}), \
                    mock.patch(f"{module}.build") as build, \
                    mock.patch("myapp.telegram.polling.run") as run:
                call_command(command, stdout=open(os.devnull, "w"), stderr=open(os.devnull, "w"))
                run.assert_called_once_with(build.return_value, mock.ANY)

    def test_rejected_token_becomes_a_clean_command_error(self):
        from telegram.error import InvalidToken

        app = mock.Mock()
        app.run_polling.side_effect = InvalidToken(f"The token `{TOKEN}` was rejected by the server.")
        with self.assertRaises(CommandError) as ctx:
            polling.run(app, ["message"])
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)


class BotResilienceTests(SimpleTestCase):
    """Real PTB application in a subprocess against FakeBotAPI."""

    def _assert_no_token(self, output):
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(TOKEN.split(":", 1)[1], output)

    def test_network_down_at_start_then_back_bot_keeps_running_and_polls(self):
        port = _free_port()                      # nothing listens yet = Telegram unreachable
        bot = BotProcess("main_bot", port)
        api = None
        try:
            time.sleep(3)
            self.assertTrue(bot.alive(), "the bot exited while Telegram was unreachable")
            api = FakeBotAPI(port=port)          # the network comes back
            self.assertTrue(_wait(lambda: api.count("getUpdates") >= 2, 40),
                            "the bot did not start polling after the network came back")
            self.assertTrue(bot.alive())
            self.assertGreaterEqual(api.count("getMe"), 1)
        finally:
            output = bot.stop()
            if api:
                api.close()
        self._assert_no_token(output)

    def test_invalid_token_exits_at_once_without_retrying(self):
        api = FakeBotAPI(get_me_status=401)
        bot = BotProcess("main_bot", api.port)
        try:
            self.assertTrue(_wait(lambda: not bot.alive(), 20), "the bot kept running with a rejected token")
            self.assertNotEqual(bot.proc.returncode, 0)
            time.sleep(1)
            self.assertEqual(api.count("getMe"), 1)          # no retries
            self.assertEqual(api.count("getUpdates"), 0)
        finally:
            output = bot.stop()
            api.close()
        self.assertIn("Telegram rejected the bot token", output)
        # PTB's InvalidToken text contains the token; it must not reach the output.
        self._assert_no_token(output)

    def _run_with_network_errors(self, bot_name, log_level):
        api = FakeBotAPI(script=["502", "drop", "502", "drop"])
        bot = BotProcess(bot_name, api.port, log_level=log_level)
        try:
            self.assertTrue(_wait(lambda: api.count("getUpdates") >= 7, 60),
                            "the bot stopped polling after network errors")
            self.assertTrue(bot.alive(), "the bot exited on a network error")
        finally:
            output = bot.stop()
            api.close()
        # The token really is in every request URL — so any logged URL would leak it.
        self.assertTrue(all(TOKEN in path for path in api.paths))
        return output

    def test_token_never_in_logs_main_bot_normal_run_and_network_errors(self):
        for log_level in ("INFO", "DEBUG"):
            with self.subTest(log_level=log_level):
                output = self._run_with_network_errors("main_bot", log_level)
                self.assertIn("telegram main bot error: NetworkError", output)   # error path exercised
                self._assert_no_token(output)
                # Nothing even tried to log it (the redaction filter is only a safety net).
                self.assertNotIn("<redacted-token>", output)
                self.assertNotIn("HTTP Request:", output)                       # httpx stays quiet

    def test_token_never_in_logs_community_bot_network_errors(self):
        output = self._run_with_network_errors("community_bot", "INFO")
        self.assertIn("community bot error: NetworkError", output)
        self._assert_no_token(output)
        self.assertNotIn("<redacted-token>", output)
        self.assertNotIn("HTTP Request:", output)
