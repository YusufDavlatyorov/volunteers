"""Bare-metal deployment files (deploy/) — the project runs without Docker.

What used to be locked against docker-compose.yml is locked here against the
systemd units, nginx config, deploy script and crontab:

* every service runs the same checkout with the same .env (the Docker-era
  rehearsal bug: celery/bot without the shared media/ mount — now one
  WorkingDirectory for all, so MEDIA_ROOT is the same directory);
* the site is one ASGI app (gunicorn + uvicorn workers) on 127.0.0.1:8000 for
  pages *and* /ws/; the worker keeps the old WebSocket limits;
* the deploy script never seeds data and runs collectstatic before migrate;
* production .env expectations (local Redis DBs 0/1/2 …) — deploy/check_env.py.
"""

import importlib.util
import re
import runpy
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management import get_commands
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR)
DEPLOY = BASE / "deploy"
UNITS = sorted((DEPLOY / "systemd").glob("*.service"))


def _load_check_env():
    spec = importlib.util.spec_from_file_location("deploy_check_env", DEPLOY / "check_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _directives(text):
    """``Key=value`` lines of a unit file, continuation lines joined."""
    text = re.sub(r"\\\n\s*", " ", text)
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";", "[")):
            key, value = line.split("=", 1)
            out.setdefault(key.strip(), []).append(value.strip())
    return out


class NoDockerTests(SimpleTestCase):
    def test_no_docker_files(self):
        for name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", ".dockerignore"):
            with self.subTest(name=name):
                self.assertFalse((BASE / name).exists())

    def test_runnable_deploy_files_never_call_docker(self):
        for path in [*UNITS, DEPLOY / "deploy.sh", DEPLOY / "crontab.example", DEPLOY / "gunicorn.conf.py"]:
            with self.subTest(path=path.name):
                code = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                                 if not l.lstrip().startswith("#"))
                self.assertNotRegex(code.lower(), r"\bdocker\b")


class SystemdUnitTests(SimpleTestCase):
    EXPECTED = {
        "khayrkhoh-web.service", "khayrkhoh-celery.service",
        "khayrkhoh-telegram-bot.service", "khayrkhoh-community-bot.service",
    }

    def test_one_unit_per_process(self):
        self.assertEqual({p.name for p in UNITS}, self.EXPECTED)

    def test_every_unit_runs_the_same_checkout_env_and_user(self):
        for path in UNITS:
            with self.subTest(unit=path.name):
                d = _directives(path.read_text(encoding="utf-8"))
                self.assertEqual(d["User"], ["deploy"])
                self.assertEqual(d["WorkingDirectory"], ["/opt/khayrkhoh"])
                self.assertEqual(d["EnvironmentFile"], ["/opt/khayrkhoh/.env"])
                self.assertEqual(d["Restart"], ["always"])
                self.assertTrue(d["ExecStart"][0].startswith("/opt/khayrkhoh/.venv/bin/"))
                self.assertIn("multi-user.target", d["WantedBy"])
                self.assertTrue(d["MemoryMax"])

    def test_web_is_gunicorn_asgi_on_loopback(self):
        d = _directives((DEPLOY / "systemd/khayrkhoh-web.service").read_text(encoding="utf-8"))
        cmd = d["ExecStart"][0].split()
        self.assertEqual(cmd[0], "/opt/khayrkhoh/.venv/bin/gunicorn")
        self.assertIn("server.asgi:application", cmd)
        self.assertEqual(cmd[cmd.index("-k") + 1], "server.asgi_worker.UvicornWorker")
        self.assertEqual(cmd[cmd.index("--bind") + 1], "127.0.0.1:8000")
        self.assertEqual(cmd[cmd.index("-c") + 1], "deploy/gunicorn.conf.py")

    def test_celery_keeps_the_threads_pool(self):
        # Celery's prefork pool crashed every task on Python 3.14 (2026-10-01).
        d = _directives((DEPLOY / "systemd/khayrkhoh-celery.service").read_text(encoding="utf-8"))
        self.assertIn("--pool=threads", d["ExecStart"][0].split())
        self.assertNotIn("beat", d["ExecStart"][0])

    def test_bot_units_run_existing_commands(self):
        commands = get_commands()
        for unit, command in (("khayrkhoh-telegram-bot.service", "run_telegram_bot"),
                              ("khayrkhoh-community-bot.service", "run_community_bot")):
            with self.subTest(unit=unit):
                d = _directives((DEPLOY / "systemd" / unit).read_text(encoding="utf-8"))
                self.assertTrue(d["ExecStart"][0].endswith(f"manage.py {command}"))
                self.assertIn(command, commands)


class AsgiWorkerTests(SimpleTestCase):
    def test_worker_keeps_the_old_ws_service_settings(self):
        from server.asgi_worker import UvicornWorker

        kwargs = UvicornWorker.CONFIG_KWARGS
        self.assertEqual(kwargs["ws_max_size"], 16384)
        # The peer stays nginx; server/client_ip.py alone reads proxy headers.
        self.assertIs(kwargs["proxy_headers"], False)
        self.assertIs(kwargs["access_log"], False)
        self.assertEqual(kwargs["lifespan"], "off")

    def test_asgi_app_routes_http_and_websocket(self):
        from server.asgi import application

        self.assertEqual(set(application.application_mapping), {"http", "websocket"})

    def test_gunicorn_config(self):
        conf = runpy.run_path(str(DEPLOY / "gunicorn.conf.py"))
        self.assertEqual(conf["workers"], 3)
        # Persistent DB connections are unsafe under ASGI — forced off for web.
        self.assertIn("DJANGO_DB_CONN_MAX_AGE=0", conf["raw_env"])
        self.assertEqual(conf["errorlog"], "-")


class NginxConfigTests(SimpleTestCase):
    def setUp(self):
        self.conf = (DEPLOY / "nginx/khayrkhoh.conf").read_text(encoding="utf-8")

    def _location(self, head):
        match = re.search(r"location " + re.escape(head) + r" \{(.*?)\n    \}", self.conf, re.S)
        self.assertIsNotNone(match, head)
        return match.group(1)

    def test_websocket_upgrade_to_the_one_app(self):
        block = self._location("/ws/")
        self.assertIn("proxy_pass http://127.0.0.1:8000;", block)
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", block)
        self.assertIn("proxy_set_header Connection $connection_upgrade;", block)
        self.assertIn("proxy_http_version 1.1;", block)
        self.assertIn("map $http_upgrade $connection_upgrade", self.conf)
        self.assertNotIn("8001", self.conf)

    def test_app_gets_the_visitor_ip_and_scheme(self):
        block = self._location("/")
        self.assertIn("proxy_pass http://127.0.0.1:8000;", block)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", block)
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", block)

    def test_files_served_from_the_checkout(self):
        self.assertIn("alias /opt/khayrkhoh/staticfiles/", self.conf)
        self.assertIn("alias /opt/khayrkhoh/media/;", self._location("/media/"))

    def test_chat_photos_stay_private(self):
        self.assertIn("return 404;", self.conf.split("location ^~ /media/chat/", 1)[1].split("\n", 1)[0])
        self.assertIn("internal;", self._location("^~ /protected-media/"))

    def test_https_and_canonical_host(self):
        self.assertIn("return 301 https://khayrkhoh.tj$request_uri;", self.conf)
        self.assertIn("/etc/letsencrypt/live/khayrkhoh.tj/fullchain.pem", self.conf)


class DeployScriptTests(SimpleTestCase):
    def setUp(self):
        self.script = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
        self.code = "\n".join(l for l in self.script.splitlines() if not l.lstrip().startswith("#"))

    def test_bash_syntax(self):
        subprocess.run(["bash", "-n", str(DEPLOY / "deploy.sh")], check=True)
        self.assertIn("set -euo pipefail", self.script)

    def test_never_seeds_data(self):
        for word in ("seed_demo", "loaddata", "flush", "fixture", "--allow-without-debug"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.code)

    def test_order(self):
        steps = ["git merge --ff-only", "pip install", "deploy/check_env.py", "check --deploy",
                 "collectstatic --noinput", "migrate --noinput", "systemctl restart",
                 "/health/ready/", "check_demo_data"]
        positions = [self.code.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))

    def test_deploy_files_are_executable_where_needed(self):
        self.assertTrue((DEPLOY / "deploy.sh").stat().st_mode & 0o111)


class CrontabTests(SimpleTestCase):
    def test_every_job_is_an_existing_command_run_from_the_venv(self):
        lines = [l for l in (DEPLOY / "crontab.example").read_text(encoding="utf-8").splitlines()
                 if l and not l.startswith("#") and not re.match(r"^[A-Z]+=", l)]
        self.assertEqual(len(lines), 9)
        commands = get_commands()
        self.assertIn("PY=/opt/khayrkhoh/.venv/bin/python", (DEPLOY / "crontab.example").read_text())
        for line in lines:
            with self.subTest(line=line):
                self.assertIn("cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-", line)
                command = re.search(r"\$PY manage\.py (\w+)", line).group(1)
                self.assertIn(command, commands)


class ServiceConfigTests(SimpleTestCase):
    def _settings(self, path, sep):
        out = {}
        for line in (DEPLOY / path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("["):
                key, _, value = line.partition(sep)
                out[key.strip()] = value.strip()
        return out

    def test_postgres_sized_and_local(self):
        conf = self._settings("postgresql/khayrkhoh.conf", "=")
        for key in ("shared_buffers", "work_mem", "max_connections", "effective_cache_size"):
            self.assertIn(key, conf)
        self.assertEqual(conf["listen_addresses"], "'localhost'")

    def test_redis_like_the_old_container(self):
        conf = self._settings("redis/khayrkhoh.conf", " ")
        self.assertEqual(conf["appendonly"], "yes")
        self.assertEqual(conf["maxmemory"], "128mb")
        self.assertEqual(conf["maxmemory-policy"], "volatile-lru")
        self.assertTrue(conf["bind"].startswith("127.0.0.1"))

    def test_log_limits(self):
        self.assertIn("SystemMaxUse", self._settings("journald/khayrkhoh.conf", "="))
        for name in ("khayrkhoh", "nginx"):
            with self.subTest(name=name):
                text = (DEPLOY / "logrotate" / name).read_text(encoding="utf-8")
                self.assertRegex(text, r"\n\s+maxsize \d+M")
                self.assertRegex(text, r"\n\s+rotate \d+")


class CheckEnvTests(SimpleTestCase):
    GOOD = {
        "DJANGO_SECRET_KEY": "x" * 50, "DJANGO_DEBUG": "False",
        "DJANGO_DB_NAME": "khayrkhoh", "DJANGO_DB_HOST": "127.0.0.1",
        "DJANGO_REDIS_URL": "redis://127.0.0.1:6379/0",
        "CELERY_BROKER_URL": "redis://127.0.0.1:6379/1",
        "DJANGO_CHANNEL_REDIS_URL": "redis://127.0.0.1:6379/2",
        "DJANGO_STATIC_MANIFEST": "True", "DJANGO_MEDIA_X_ACCEL": "True",
        "DJANGO_BEHIND_TLS_PROXY": "True",
    }

    def setUp(self):
        self.mod = _load_check_env()

    def test_good_production_env(self):
        self.assertEqual(self.mod.production_problems(self.GOOD), ([], []))

    def test_docker_era_values_are_caught(self):
        env = {**self.GOOD, "DJANGO_DB_HOST": "shared_postgres",
               "DJANGO_REDIS_URL": "redis://redis:6379/0", "DJANGO_STATIC_MANIFEST": "",
               "SHARED_POSTGRES_NETWORK": "shared_postgres_net", "DJANGO_DB_CACHE": "True"}
        errors, notes = self.mod.production_problems(env)
        joined = " ".join(errors)
        for name in ("DJANGO_DB_HOST", "DJANGO_REDIS_URL", "DJANGO_STATIC_MANIFEST"):
            self.assertIn(name, joined)
        self.assertTrue(any("SHARED_POSTGRES_NETWORK" in n for n in notes))
        self.assertTrue(any("DJANGO_DB_CACHE" in n for n in notes))

    def test_redis_databases_must_not_be_swapped(self):
        env = {**self.GOOD, "CELERY_BROKER_URL": "redis://127.0.0.1:6379/0"}
        errors, _ = self.mod.production_problems(env)
        self.assertEqual(len(errors), 1)
        self.assertIn("CELERY_BROKER_URL", errors[0])

    def test_debug_on_is_an_error(self):
        errors, _ = self.mod.production_problems({**self.GOOD, "DJANGO_DEBUG": "True"})
        self.assertTrue(any("DJANGO_DEBUG" in e for e in errors))

    def test_parse_mismatches_report_names_only(self):
        dotenv = {"A": "same", "B": "pass word", "C": "x", "D": None}
        environ = {"A": "same", "B": "'pass word'"}
        self.assertEqual(self.mod.parse_mismatches(dotenv, environ), ["B", "C"])
