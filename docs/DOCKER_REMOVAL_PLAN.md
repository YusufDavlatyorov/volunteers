# Отказ от Docker: отчёт, план и состояние работы

> **Статус 2026-10-05, вторая сессия: план выполнен** (§5 — все пункты, кроме коммита). Полный набор
> тестов: **1377 OK**; `manage.py check` чист; локальный smoke gunicorn/uvicorn + WS повторён.
> Изменения **не закоммичены**. Что осталось владельцу — §6.

Задача владельца: полностью убрать Docker из проекта и подготовить деплой без Docker на
Ubuntu-сервер (PostgreSQL, Redis, nginx, systemd, ASGI через gunicorn + uvicorn). Проект живой
(khayrkhoh.tj): ничего не должно сломаться, данные не должны потеряться, демо-данные никогда не
попадают на прод. **На сервере ничего не выполнялось.**

Работа остановлена по просьбе владельца посреди шага «документация». Ниже: что нашли, принятые
решения, что уже сделано (изменения на диске, **не закоммичены**), что осталось и открытые вопросы.

---

## 1. Что нашли (исследование до правок)

**Docker-файлы:** `Dockerfile`, `docker-compose.yml`, `.dockerignore`. Отдельного entrypoint не
было: migrate и collectstatic запускались разовыми `docker compose run` по DEPLOY.md. Makefile и
CI/CD нет.

**Сервисы compose:**
- `web`: gunicorn **WSGI**, :8000;
- `ws`: uvicorn ASGI, :8001, только `/ws/`, `--ws-max-size 16384`, 1 воркер;
- `celery_worker` (`--pool=threads`), `telegram_bot`, `community_bot` (профиль `community`);
- `redis` (AOF, 128mb, volatile-lru);
- `osrm_car` / `osrm_foot` (профиль `osrm`, не включены).

PostgreSQL жил в отдельном внешнем контейнере `shared_postgres` (postgres:16).

**Переменные, которые задавал compose, а не серверный `.env`:**
- `DJANGO_REDIS_URL`, `CELERY_BROKER_URL`, `DJANGO_CHANNEL_REDIS_URL` (`redis://redis:6379/0,1,2`);
- `DJANGO_DB_CACHE=False`, `DJANGO_STATIC_MANIFEST=True`, `DJANGO_MEDIA_X_ACCEL=True`.

В серверном `.env` при этом стоят `DJANGO_DB_HOST=<контейнер>`, `DJANGO_DB_CACHE=True` и
`SHARED_POSTGRES_NETWORK`. При переезде всё это нужно перенести в `.env` вручную.

**Docker на Mac:**
- `scripts/verify_backup.sh` проверял дамп во временном postgres:16;
- `scripts/osrm_build.sh` собирал данные OSRM;
- PG16-репетиции и amd64-прогон тестов в образе (CLAUDE.md, рутина этапов).

**Тесты, читавшие `docker-compose.yml`:** `ComposeMediaMountTests` (tests_m7_prep) и
`test_compose_points_every_app_container_at_redis_db2` (tests_m3_hardening).

**Документы с Docker:**
- DEPLOY.md (целиком под Docker), `docs/DEPLOY_FEATURE_PACK_V2.md` (≈ 80 мест), README;
- CLAUDE.md, docs/LOCAL_RUN.md, COMMUNITY_BOT.md, CLOUDFLARE.md, WS_PROTOCOL.md;
- deploy checklist в FEATURES_PROGRESS.md, `.claude/agents/*.md`;
- все cron-строки (`docker compose exec`).

**Комментарии в коде:** settings.py, asgi.py, celery.py, client_ip.py, log_filters.py,
accounts/views.py, команды check_realtime / run_telegram_bot / seed_demo / rename_legacy_uploads,
realtime/ratelimit.py.

**Что уже было готово:**
- `server/asgi.py` корректен (HTTP + Channels с проверкой Origin);
- Channels, Celery и кэш берут Redis только из переменных окружения;
- gunicorn и uvicorn уже были в requirements;
- celery beat нет: периодические задачи идут через cron, и это задокументированное решение.

## 2. Расхождения с ТЗ

1. Модуль называется `server.asgi:application`, а не `config.asgi`.
2. `uvicorn.workers.UvicornWorker` в uvicorn 0.54 deprecated. Используется пакет
   `uvicorn-worker==0.4.0` через подкласс `server/asgi_worker.py::UvicornWorker`. Он сохраняет
   флаги бывшего `ws`-сервиса:
   - `ws_max_size=16384`;
   - `proxy_headers=False`: IP посетителя решает только `server/client_ip.py`, по X-Real-IP;
   - `access_log=False`, `lifespan="off"`.

   В юните: `-k server.asgi_worker.UvicornWorker`.
3. HTTP и WS обслуживает один gunicorn на :8000, сервиса на :8001 нет. 3 воркера: синхронные
   вьюхи под ASGI идут по одному потоку на воркер, то есть параллельность та же, что у прежних
   3 sync-воркеров. Состояние WS-консьюмеров хранится на уровне соединения, presence лежит в
   БД/Redis, поэтому несколько процессов безопасны.
4. Под ASGI постоянные соединения с БД небезопасны: `deploy/gunicorn.conf.py` принудительно
   ставит `raw_env = ["DJANGO_DB_CONN_MAX_AGE=0"]` только для web.
5. На шаге d нужен простой в несколько минут. Причины: порт :8000 занят старым web, два бота с
   одним токеном получают 409 Conflict, а дамп до остановки потерял бы последние записи. Поэтому
   контейнеры приложения останавливаются в начале окна, а `shared_postgres` и `khayrkhoh_redis`
   остаются точкой отката.
6. Медиа лежат на хосте (bind-mount `/opt/khayrkhoh/media`), поэтому бэкап делается через
   `tar`, `docker cp` не нужен.
7. Опасность в шаге e: `shared_postgres` может держать базы или тома других проектов, и
   `docker system prune -a --volumes` их уничтожит. В MIGRATION.md перед удалением стоит жёсткий
   СТОП-контроль: `docker ps -a`, `docker volume ls`, список баз.
8. `docker compose up` после правки `.env` пересоздал бы старые контейнеры с новым окружением.
   Поэтому для отката используется только `docker start/stop` по именам контейнеров.

## 3. Решения владельца

**2026-10-05, первый ответ («ок, по рекомендациям»):**
1. Переезд **в одной сессии с деплоем Feature Pack v2, одним ранбуком** (`deploy/MIGRATION.md`).
   Миграции FP v2 идут в новую родную БД (восстановленную копию), а старая база в
   `shared_postgres` не меняется, поэтому откат простой.
2. Python 3.14 из PPA deadsnakes (`python3.14`, `python3.14-venv`).
3. `verify_backup.sh` и PG16-репетиции перевести на Homebrew `postgresql@16`. `osrm_build.sh` и
   OSRM-контейнеры удалить; свой OSRM, если понадобится, ставится позже нативно.
4. Порядок: контейнеры приложения останавливаются в начале окна (шаг d), `shared_postgres` и
   redis удаляются в шаге e.

**Дополнения владельца:**
1. Шаг e выполняется не раньше чем через несколько дней стабильной работы. В MIGRATION.md
   записано «не раньше 7 дней, минимум 3, решение за владельцем».
2. Настройки Postgres под RAM сервера; параметры контейнера перенести, если они заданы.
3. Ограничение логов: journald SystemMaxUse и logrotate для nginx и логов приложения.
4. Генеральная репетиция всего ранбука (переезд + FP v2) отдельным пунктом перед боевым деплоем.

## 4. Что уже сделано (на диске, НЕ закоммичено, ветка `feature-pack-v2`)

`git status` на момент паузы показывает удалённые файлы:
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `scripts/osrm_build.sh` (через `git rm`).

**Новые файлы:**
- `server/asgi_worker.py`: воркер (см. §2.2);
- `deploy/gunicorn.conf.py`: 3 воркера, timeout 60, graceful 30, errorlog → stderr, без access
  log, `DJANGO_DB_CONN_MAX_AGE=0`;
- `deploy/systemd/khayrkhoh-web.service`: Type=notify,
  `gunicorn server.asgi:application -k server.asgi_worker.UvicornWorker --bind 127.0.0.1:8000
  -c deploy/gunicorn.conf.py`, `User=deploy`, `EnvironmentFile=/opt/khayrkhoh/.env`,
  `Restart=always`, `MemoryMax=900M`;
- `deploy/systemd/khayrkhoh-celery.service` (`--pool=threads`, 300M),
  `khayrkhoh-telegram-bot.service` (300M), `khayrkhoh-community-bot.service` (200M, не
  включается, пока бот сообщества не настроен). Celery beat нет: cron;
- `deploy/nginx/khayrkhoh.conf`: копия живого конфига DEPLOY.md §10 + §10a (certbot, редиректы
  на канонический хост, gzip/brotli, статика и медиа с диска, `/protected-media/` internal,
  `/media/chat/` 404), `map $connection_upgrade`, `/ws/` → 127.0.0.1:8000 с Upgrade/Connection;
- `deploy/postgresql/khayrkhoh.conf`: заложено на 3,8 ГБ RAM, общей с приложением:
  - `shared_buffers=512MB`, `effective_cache_size=1GB`, `work_mem=8MB`;
  - `maintenance_work_mem=128MB`, `max_connections=50`;
  - `listen_addresses=localhost`, `log_min_duration_statement=1000`.

  Параметры `shared_postgres` с сервера ещё не сняты. Если сервер ни разу не перенастраивали,
  это умолчания образа: 128MB / 4MB / 100. Чтение `pg_settings` стоит в шаге 1 MIGRATION.md;
- `deploy/redis/khayrkhoh.conf`: как у контейнера (bind 127.0.0.1, AOF, `save ""`, 128mb,
  volatile-lru), подключается через `include` в конце `/etc/redis/redis.conf`;
- `deploy/journald/khayrkhoh.conf`: SystemMaxUse=500M, SystemKeepFree=2G, файл 50M, хранение 1 месяц;
- `deploy/logrotate/khayrkhoh` (cron.log: weekly ×8, maxsize 50M) и `deploy/logrotate/nginx`
  (стоковый + maxsize 100M);
- `deploy/crontab.example`: 9 cron-задач через `/opt/khayrkhoh/.venv/bin/python`, с flock;
- `deploy/check_env.py`: запускается через `systemd-run` с тем же EnvironmentFile. Сравнивает,
  как `.env` читают systemd и python-dotenv, и проверяет прод-значения: локальные БД и Redis
  0/1/2, MANIFEST, X_ACCEL, BEHIND_TLS_PROXY, DEBUG=False. Выводит только имена переменных;
- `deploy/deploy.sh` (исполняемый):
  1. git fetch + `merge --ff-only` → pip в `.venv` → check_env → синхронизация изменённых
     юнитов (nginx только предупреждение);
  2. `check --deploy` → **collectstatic до migrate** → migrate;
  3. рестарт юнитов → ожидание `/health/ready/` (Host + X-Forwarded-Proto) → `check_demo_data`
     (только чтение).

  Сидов и фикстур нет;
- `deploy/MIGRATION.md`: полный объединённый ранбук на русском. Шаги:
  - R: генеральная репетиция на ВМ Multipass;
  - 0: Mac;
  - 1: проверка только на чтение, включая чужие контейнеры/базы, параметры и локаль PG, uid deploy;
  - 2: бэкапы + копия на Mac;
  - 3: пакеты и конфиги, сайт работает;
  - 4: код, venv, `.env`, пробное восстановление и миграции на копии, временный юнит на :8099;
  - 5: юниты и nginx;
  - 6: окно простоя;
  - 7: проверки, включая WS 101/403;
  - 8: однократные команды FP v2;
  - 9: новый токен бота;
  - 10: браузер;
  - 11: наблюдение;
  - 12: удаление Docker (≥ 7 дней, с проверками и перезагрузкой);
  - 13: карантин через 30 дней.

  Откат: уровни 0/1/2, отдельно «после шага 12». В конце пустой «Журнал репетиций»;
- `myapp/tests_deploy.py`: 28 тестов на `deploy/` (заменили два compose-теста), все зелёные.

**Изменённые файлы:**
- `requirements.txt`: + `uvicorn-worker==0.4.0`, комментарии. В локальный `.venv` пакет уже
  установлен;
- `.env.example`:
  - прод-значения `DJANGO_DB_HOST=127.0.0.1`, Redis `127.0.0.1:6379/0,1,2`;
  - новые `DJANGO_STATIC_MANIFEST` и `DJANGO_MEDIA_X_ACCEL`;
  - убраны блок Docker / `SHARED_POSTGRES_NETWORK` и OSRM-контейнеры;
- `server/settings.py`: дефолт `DJANGO_DB_HOST` → `127.0.0.1`, комментарии;
- комментарии и докстринги без изменения логики: `server/{asgi,celery,client_ip,log_filters}.py`,
  `accounts/views.py`, `myapp/realtime/ratelimit.py`, команды check_realtime (там же текст
  ошибки), run_telegram_bot, seed_demo, rename_legacy_uploads;
- тесты: из `myapp/tests_m7_prep.py` и `myapp/tests_m3_hardening.py` убраны compose-тесты;
- `scripts/verify_backup.sh`: временный кластер Homebrew `postgresql@16` в `/tmp` без TCP вместо
  Docker. **Не проверен в работе**: `postgresql@16` на Mac ещё не установлен;
- `docs/DEPLOY_FEATURE_PACK_V2.md`: переписан в справку (что меняет FP v2, объяснения, история
  репетиций), команды перенесены в MIGRATION.md;
- `DEPLOY.md`: переписан под чистый сервер без Docker (apt → venv → `.env` + check_env →
  PG → Redis → статика/миграции/начальные аккаунты → юниты → nginx/certbot → ufw/ssh →
  cron и логи → проверка → redeploy через deploy.sh);
- `README.md`: стек, структура, cron, тесты, раздел Deployment (таблица `deploy/`), health
  checks, бэкапы (ночной `pg_dump` через `~/.pgpass`), DR, ограничения. **Не закончен:**
  таблица переменных окружения, строки 99–111, ещё ссылается на Docker (см. §5).

**Локальная проверка, уже выполненная:**
- `manage.py test myapp.tests_deploy myapp.tests_m7_prep myapp.tests_m3_hardening`: OK;
- gunicorn + `server.asgi_worker.UvicornWorker` локально с временным Redis:
  - `/`, `/health/ready/` (все ok) и статика: 200;
  - WS 101, чужой Origin 403 (при прод-подобном `ALLOWED_HOSTS`; в локальном `.env` стоит `*`);
  - авторизованный сокет: hello и pong;
  - фрейм 20 KB → 1009 (лимит работает);
- `check_realtime` через Redis: OK;
- Celery-воркер доставил задачу;
- `check --deploy` с `DEBUG=False`: 0 проблем.

Строки gunicorn `Worker … was sent SIGTERM!` при остановке штатные.

**Полный набор тестов после правок ещё НЕ прогонялся.**

## 5. Доделано во второй сессии (2026-10-05)

1. README: таблица переменных (Redis/Celery/Channels по 127.0.0.1, `DJANGO_STATIC_MANIFEST`,
   `DJANGO_MEDIA_X_ACCEL`, без `SHARED_POSTGRES_NETWORK`).
2. CLAUDE.md: раздел **«No Docker — a hard rule»** в начале; Commands без Docker-прогона, с
   командами сервера (`deploy.sh`, `journalctl`); Security (Redis, client_ip), Production
   (переписан под `deploy/`), Templates/static.
3. docs/LOCAL_RUN.md, COMMUNITY_BOT.md (юнит вместо профиля compose), CLOUDFLARE.md
   (`systemctl restart`, `journalctl`), WS_PROTOCOL.md (`server/asgi_worker.py`).
4. FEATURES_PROGRESS.md: запись в логе + deploy checklist → краткая форма `deploy/MIGRATION.md`.
5. `.claude/agents/*` и `.gitignore`.
6. Ссылки на шаги runbook в командах `rename_legacy_uploads` / `check_demo_password` /
   `check_demo_data` и в `tests_predeploy.py`; константа `DOCKER_GATEWAY` → `PRIVATE_PEER` в
   `tests_m6_speed.py`.
7. `grep -ri docker`: остались только история (FEATURES_PLAN/SPEC/PROGRESS, справка FP v2),
   MIGRATION.md / DEPLOY.md / CLAUDE.md (правило «без Docker»), проверки в `tests_deploy.py` /
   `check_env.py` и исторические фразы в комментариях.
8. Проверки: 1377 тестов OK (было 1351: −2 compose-теста, +28 `tests_deploy`), `check` чист,
   smoke: `/` 200, `/health/ready/` все ok, WS 101, чужой Origin 403, `check_realtime` OK.
9. Память Claude: новая запись `no-docker`, обновлены `production-deployment`, `feature-pack-v2`,
   рутина этапов.
10. Вопрос 6 из §6 (ночной бэкап) решён по рекомендации: в MIGRATION.md добавлен шаг 11.1 с
    командами (`~/.pgpass`, cron `pg_dump` в `/var/backups/khayrkhoh/nightly`, 14 дней).

## 6. Открытые вопросы и что нужно от владельца

1. **`brew install postgresql@16` на Mac**, чтобы проверить новый `scripts/verify_backup.sh` и
   проводить PG16-репетиции миграций без Docker.
2. **Генеральная репетиция (MIGRATION.md, шаг R)** нужна ВМ Multipass
   (`brew install --cask multipass`). Внутри ВМ «старый сервер» временно ставится с Docker:
   иначе переезд с Docker не отрепетировать. На стенде используются только демо-данные, пустой
   SMTP и тестовый бот.
3. **Сервер, только чтение, когда владелец разрешит** (сейчас по правилу без «deploy» доступа нет):
   - есть ли в Docker чужие контейнеры/тома/базы (от этого зависит шаг 12);
   - не-дефолтные параметры `shared_postgres` (перенести в `deploy/postgresql/khayrkhoh.conf`);
   - локаль старой базы (en_US.UTF-8 заложен в 4.4/6.4);
   - `id -u deploy` (chown, если не 1000);
   - есть ли у `deploy` sudo без пароля (`deploy.sh` вызывает sudo);
   - размер диска (под `SystemKeepFree=2G`).
4. Срок шага 12: владелец — «через несколько дней стабильной работы»; в MIGRATION.md: «≥ 7 дней, минимум 3, решение в тот день за владельцем».
5. Локальный коммит (без push) и `scripts/backup_repo.sh` — по команде владельца.
6. ~~Ночной бэкап родной БД~~ — добавлен в MIGRATION.md шагом 11.1.
