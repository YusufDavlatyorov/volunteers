# Переезд с Docker на venv + systemd вместе с деплоем Feature Pack v2

Единый runbook дня деплоя для `khayrkhoh.tj` (31.25.236.54, Ubuntu 24.04, 1 vCPU, 3,8 ГБ,
`/opt/khayrkhoh`, пользователь `deploy`). Сейчас на сервере `main` @ `2467bc5` в Docker:
контейнеры `khayrkhoh_web` (gunicorn), `khayrkhoh_telegram_bot`, `khayrkhoh_celery_worker`,
`khayrkhoh_redis` и отдельный `shared_postgres` (postgres:16). После переезда — **ничего из
Docker**: Python 3.14 в `.venv`, PostgreSQL 16 и Redis из apt, nginx и certbot как были, четыре
systemd-юнита (`deploy/systemd/`), cron из `deploy/crontab.example`.

Решения владельца (2026-10-05): переезд и Feature Pack v2 — **одним окном, одним runbook**;
Python 3.14 из PPA deadsnakes; OSRM-контейнеры убраны из проекта; контейнеры приложения
останавливаются в начале окна простоя, а `shared_postgres` и `khayrkhoh_redis` остаются **точкой
отката** и удаляются (шаг 12) **не раньше чем через 7 дней стабильной работы** (минимум 3, решение
— за владельцем в тот день); перед боевым деплоем — **генеральная репетиция всего runbook** (шаг R).

Ничего из этого не выполняется без явного «deploy» от владельца. Справка по самому Feature
Pack v2 (что меняется, объяснения команд, история репетиций) — `docs/DEPLOY_FEATURE_PACK_V2.md`.

> **Демо-данных на сервере быть не должно.** `seed_demo`, `loaddata`, фикстуры и любые демо-команды
> на сервере **никогда не запускаются** (`seed_demo` сам откажется при `DEBUG=False`;
> `deploy/deploy.sh` ничего не наполняет). Проверки — только на чтение (шаги 4.5, 8, 11). **Если что-то
> демо найдено — ничего не удалять и не менять: остановиться и сказать владельцу.**

**Почему откат простой.** Новая база — это *копия* старой, восстановленная из дампа в родной
PostgreSQL. Миграции Feature Pack v2 идут только в новую базу; база в `shared_postgres` остаётся
ровно такой, какой была в момент остановки старого сайта. Пока шаг 12 не выполнен, вернуться к
старому сайту — это остановить юниты и запустить старые контейнеры (раздел «Откат», уровень 1).

| Пункт задания | Шаги здесь |
|---|---|
| a) бэкап БД и медиа, проверка | 2 (до окна) и 6.3 (финальный дамп в окне) |
| b) установка postgresql, redis-server, nginx, python3-venv | 3 |
| c) БД и пользователь, восстановление дампа | 4.4–4.5 (пробное, без простоя) и 6.4 (боевое) |
| d) запуск и проверка сайта, WebSocket, ботов | 6.8–7, 9, 10 |
| e) удаление Docker — только после проверки | 12 (не раньше чем через 7 дней) |
| f) план отката | «Откат» в конце |
| генеральная репетиция | R (до дня деплоя) |

**Простой**: только окно шага 6 — ожидаемо 3–5 минут (остановка, финальный дамп и восстановление
небольшой базы, 14 миграций, переименование старых фото, запуск юнитов). Точное время даст
репетиция. Все остальные шаги — при работающем старом сайте.

**Что поменяется на сервере** (кроме самого Feature Pack v2 — он в справке):

| Было (Docker) | Стало |
|---|---|
| `khayrkhoh_web` (gunicorn WSGI, :8000) + в FP v2 был бы ещё `ws` (uvicorn, :8001) | `khayrkhoh-web.service`: gunicorn + uvicorn-воркеры (ASGI), **страницы и `/ws/` на 127.0.0.1:8000**, 3 воркера, лимит 900 МБ |
| `khayrkhoh_celery_worker` | `khayrkhoh-celery.service` (`--pool=threads`, лимит 300 МБ) |
| `khayrkhoh_telegram_bot` | `khayrkhoh-telegram-bot.service` (лимит 300 МБ) |
| `community_bot` (профиль compose) | `khayrkhoh-community-bot.service`, установлен, **не включён** |
| `khayrkhoh_redis` (redis:7) | `redis-server` из apt, 127.0.0.1:6379, те же настройки (`deploy/redis/`) |
| `shared_postgres` (postgres:16) | `postgresql-16` из apt, только localhost, настройки под RAM (`deploy/postgresql/`) |
| переменные из `docker-compose.yml` | те же переменные — в `.env` (шаг 4.3) |
| cron `docker compose exec -T web …` | cron `.venv/bin/python manage.py …` (`deploy/crontab.example`) |
| логи `docker logs` | journald (`journalctl -u khayrkhoh-web`), лимит 500 МБ; logrotate для nginx и `cron.log` |
| миграции/статика через `docker compose run` | `deploy/deploy.sh` для следующих выкаток |

**Сессия на сервере.** Все шаги — от пользователя `deploy`, `cd /opt/khayrkhoh`, и **внутри
`tmux`** с шага 2 до конца: SSH до этого сервера рвётся (2026-10-05 — два обрыва за полчаса), а
обрыв во время `pg_dump`, `pg_restore` или `migrate` без tmux убивает команду посреди окна
простоя. В tmux команда продолжает работать, а функции и переменные ниже сохраняются:

```bash
tmux new -s deploy          # первый вход
tmux attach -t deploy       # после обрыва SSH — вернуться в ту же сессию
```

**sudo у `deploy` — с паролем** (решение владельца: sudoers не меняем). Первая команда с `sudo` в
каждом окне tmux спросит пароль пользователя `deploy`, дальше sudo помнит его ~15 минут. Пароль
нужно иметь под рукой до начала сессии — в окне простоя ждать его некогда.

Шпаргалка (выполнить в tmux; `olddb` — **без** `docker exec -i`: с `-i` контейнер забирает себе
весь ввод терминала, и вставленный блок команд «съедается» после первой строки — найдено
2026-10-05):

```bash
set -a; . ./.env; set +a     # загрузить DJANGO_DB_* в оболочку (значения не печатаются)
olddb() { docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -v ON_ERROR_STOP=1 "$@"; }
newdb() { PGPASSWORD="$DJANGO_DB_PASSWORD" psql -h 127.0.0.1 -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -v ON_ERROR_STOP=1 "$@"; }
manage() { /opt/khayrkhoh/.venv/bin/python manage.py "$@"; }
COUNTS="select 'users', count(*) from accounts_users union all select 'requests', count(*) from myapp_helprequest union all select 'migrations', count(*) from django_migrations"
B=/var/backups/khayrkhoh/pre-migration-<TS>   # путь из шага 2 (с шага 2)
```

Эти строки нужны в каждой новой SSH-сессии (после шага 4.3 — заново: `.env` изменится).

---

## R. Генеральная репетиция (обязательно, до дня деплоя)

Весь runbook — шаги 1–12 и оба уровня отката — проходится **целиком, по тексту этого файла**, на
копии сервера. Боевой деплой назначается только после репетиции без единой правки «на ходу».
Найденное исправляется в коде/runbook, исправленный кусок прогоняется ещё раз.

**Стенд** — виртуальная машина на Mac, максимально похожая на сервер:

```bash
brew install --cask multipass                       # один раз
multipass launch 24.04 --name kk-rehearsal --cpus 1 --memory 3.8G --disk 25G
multipass shell kk-rehearsal
```

(arm64 вместо amd64 — пакеты apt и колёса Python те же версии; эмуляция не нужна.)

1. **Сделать из ВМ «старый сервер»** по старому `DEPLOY.md` из коммита `2467bc5`
   (`git show 2467bc5:DEPLOY.md`): пользователь `deploy`, Docker, `git clone` в `/opt/khayrkhoh` и
   `git checkout 2467bc5`, `shared_postgres` (postgres:16), `docker compose up -d --build`, nginx с
   конфигом §10 того коммита. Сертификат — самоподписанный, но **по тем же путям**:
   `/etc/letsencrypt/live/khayrkhoh.tj/{fullchain,privkey}.pem`, плюс файлы
   `/etc/letsencrypt/options-ssl-nginx.conf` и `/etc/letsencrypt/ssl-dhparams.pem`
   (`openssl dhparam -out … 2048`). Старый crontab из того DEPLOY.md, каталог `/var/log/khayrkhoh`.
2. **Данные.** Как на репетиции 2026-10-04: старый код + `seed_demo` (в старом коде он ещё не
   отказывается при `DEBUG=False`) + несколько фото с GPS и «именными» файлами (`Ivanov_Ivan.jpg`),
   одно фото без записи (для карантина). **Реальные данные прода на стенд не заливать**: в них
   настоящие почты и Telegram-чаты людей.
3. **`.env` стенда**: `DEBUG=False`, хосты `khayrkhoh.tj`, **`SMTP_*` пустые** (письма — в лог),
   `TELEGRAM_BOT_TOKEN` — только **тестовый** бот (docs/TEST_BOT.md) или пусто, никогда боевой.
4. **Код для `git pull`**: стенд тянет `main` из GitHub. На репетиции вместо шага 0 — локальный
   bare-репозиторий на ВМ с веткой `main` = `feature-pack-v2` (как 2026-10-04), чтобы ничего не
   отправлять на GitHub до «deploy».
5. **Пройти шаги 1–12 буквально**, с записью времени каждого шага (особенно окна 6.1–6.9).
   Шаг 2.5 (копия на Mac) — тоже: `multipass transfer` или `rsync` → `scripts/verify_backup.sh`.
   Шаг 10 (браузер) — на Mac в Chrome с
   `--host-resolver-rules="MAP khayrkhoh.tj <IP ВМ>, MAP www.khayrkhoh.tj <IP ВМ>"` **только
   навигацией** (Playwright `context.request` это правило игнорирует и уходит на настоящий сайт —
   случай 2026-10-04); убедиться, что ответы приходят с IP ВМ. Шаг 9 (смена токена) —
   на тестовом боте.
6. **Откаты**: после шага 8 — **уровень 1** (назад в Docker) → проверка старого сайта → снова
   вперёд с шага 4.3; затем через «пару дней» (данные, созданные на новом сайте) — **уровень 2** →
   проверка, что новые запросы/пользователи видны в старом сайте → снова вперёд.
7. **Шаг 12 на стенде** — включая `docker system prune -a --volumes` и `apt purge`, затем
   **перезагрузка ВМ** (`sudo reboot`): после неё все юниты, PostgreSQL, Redis и nginx должны
   подняться сами, `/health/ready/` — 200, бот отвечает.
8. **`deploy/deploy.sh`** — дважды: без изменений в `main` (ничего не меняет, всё зелёное) и с
   пустым коммитом поверх (перезапуск, `/health/ready/` 200).
9. **Проверка 409**: с запущенным старым ботом-контейнером включить юнит бота с тем же тестовым
   токеном — в журнале `Conflict: terminated by other getUpdates`; это подтверждает, почему в окне
   старый бот останавливается первым.

Результат записать сюда, в раздел «Журнал репетиций» в конце файла: дата, время окна, время
каждого отката, что нашли и исправили. Стенд удалить: `multipass delete --purge kk-rehearsal`.

## 0. До сессии деплоя (локально, на Mac)

Сервер — клон `https://github.com/YusufDavlatyorov/volunteers.git`, обновляется `git pull` из
`origin/main`. Ветка `feature-pack-v2` вливается в `main` перемоткой. Репозиторий **публичный** —
перед отправкой проверить, что в диффе нет секретов (`.env*`, дампов, `osrm-data/`).

1. Генеральная репетиция (шаг R) пройдена, журнал заполнен.
2. Полный набор тестов зелёный: `python manage.py test --parallel 4`.
3. В сессии деплоя, после «deploy»:

```bash
git checkout main
git merge --ff-only feature-pack-v2      # ошибка = в main появилось что-то новое → остановиться
git push origin main
git rev-parse --short HEAD               # записать как NEW
git push origin feature-pack-v2
```

## 1. Предварительная проверка (на сервере, только чтение)

```bash
cd /opt/khayrkhoh
command -v tmux || echo "tmux: нет — поставить в шаге 3"
sudo -v                                # пароль deploy (sudo без пароля не настроен — так и задумано)
git status --short                     # пусто
git rev-parse --short HEAD             # 2467bc5 — ROLLBACK_COMMIT
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
#   ожидаем ровно: khayrkhoh_web, khayrkhoh_telegram_bot, khayrkhoh_celery_worker, khayrkhoh_redis,
#   shared_postgres — все Up. ЛЮБОЙ другой контейнер = на сервере есть чужой проект в Docker:
#   записать, сказать владельцу (шаг 12 тогда меняется — Docker целиком не удаляется).
docker volume ls --format '{{.Name}}'  # записать список (шаг 12)
sudo nginx -t
curl -fsS https://khayrkhoh.tj/health/ready/
grep -c '\$' .env                      # 0 — без "$" в значениях
grep -c 'DJANGO_BEHIND_TLS_PROXY=True' .env   # 1
grep -cE '^(DJANGO_REDIS_URL|CELERY_BROKER_URL|DJANGO_CHANNEL_REDIS_URL|DJANGO_STATIC_MANIFEST|DJANGO_MEDIA_X_ACCEL)=' .env
#   0 — эти переменные задавал docker-compose.yml; в шаге 4.3 они добавятся в .env

set -a; . ./.env; set +a
olddb() { docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -v ON_ERROR_STOP=1 "$@"; }
# Какие базы живут в shared_postgres? Ожидаем только нашу + postgres/template0/template1.
olddb -Atc "select datname from pg_database order by 1"
#   есть чужая база → записать, сказать владельцу (шаг 12: shared_postgres тогда НЕ удаляется).
# Версия и локаль — новая база создаётся с теми же (шаг 4.4)
docker exec shared_postgres postgres -V
olddb -Atc "select datcollate, datctype, pg_encoding_to_char(encoding) from pg_database where datname = current_database()"
# Настройки, отличные от умолчаний образа postgres:16 — перенести в deploy/postgresql/khayrkhoh.conf
olddb -Atc "select name, setting, unit, source from pg_settings where source not in ('default','override','client','session') order by 1"
#   ожидаем только служебное от образа (listen_addresses='*' и т. п.). shared_buffers / work_mem /
#   max_connections в списке → сравнить с deploy/postgresql/khayrkhoh.conf и сказать мне.
docker inspect shared_postgres --format '{{json .Config.Cmd}}'   # ["postgres"] = без -c флагов
olddb -Atc "select pg_size_pretty(pg_database_size(current_database()))"

# Демо-пароль seed_demo публичный — не должен подходить ни к одному аккаунту (только чтение).
docker exec khayrkhoh_web python manage.py shell -c "from django.contrib.auth.hashers import check_password as c; from accounts.models import Users as U; print('demo:', [u.username for u in U.objects.only('username','password') if u.has_usable_password() and c('Volunteer2026!', u.password)], 'demo users:', list(U.objects.filter(username__in=['admin_demo','malika_s','client_zamira','curator_samira']).values_list('username', flat=True)), 'of', U.objects.count())"
#   ожидаем: demo: [] demo users: [] of N. Иначе — СТОП, сказать владельцу.

# Пользователь и права на файлы: контейнер писал media/ и staticfiles/ как uid 1000.
id -u deploy                           # 1000 → в шагах 4.4 и 6.5 chown не нужен
stat -c '%u:%g %a %n' media staticfiles
# Порты: родные PostgreSQL/Redis займут 5432/6379 на 127.0.0.1 — у контейнеров они не опубликованы.
sudo ss -ltnp | grep -E ':(5432|6379|8000|8099) '   # только :8000 (docker-proxy)
# Память и диск
free -h; df -h / /var/lib/docker /var/backups 2>/dev/null; du -sh media staticfiles
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}"
lsb_release -ds                        # Ubuntu 24.04
```

**Диск**: нужно ≈ 1 ГБ (пакеты ≈ 300 МБ, venv ≈ 250 МБ, база в двух копиях, дампы) плюс размер
`media.tgz`. Продолжаем, если свободно **не меньше 3 ГБ** (до шага 12 одновременно живут Docker
и новая установка). **Память** — до шага 12 контейнеры `shared_postgres` и `khayrkhoh_redis`
продолжают работать (≈ 150–250 МБ) рядом с новыми сервисами; если «available» в `free -h` меньше
1,2 ГБ — сказать мне до продолжения.

## 2. Бэкапы — без них не продолжать (шаг a, до окна)

```bash
cd /opt/khayrkhoh
TS=$(date +%F-%H%M); B=/var/backups/khayrkhoh/pre-migration-$TS; sudo install -d -o deploy -g deploy -m 700 "$B"
set -a; . ./.env; set +a
# 2.1 БД (формат custom, для pg_restore) — из контейнера
docker exec shared_postgres pg_dump -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$B/db.dump"
ls -lh "$B/db.dump"                    # не 0 байт
docker exec -i shared_postgres pg_restore --list < "$B/db.dump" | grep -cE "TABLE DATA public (accounts_users|myapp_helprequest) "   # 2
# 2.2 media — это каталог на хосте (bind-mount), не внутри контейнера: docker cp не нужен
tar -C /opt/khayrkhoh -czf "$B/media.tgz" media
tar -tzf "$B/media.tgz" | grep -vc '/$'                       # число файлов
find media -type f | wc -l                                      # то же число
# 2.3 всё, что нужно для отката в Docker
cp staticfiles/staticfiles.json "$B/staticfiles.json"           # старый манифест статики
cp -p .env "$B/env-docker-era"                                   # права 600, как у .env
sudo cp /etc/nginx/sites-available/khayrkhoh "$B/nginx-khayrkhoh.conf"
crontab -l > "$B/crontab.txt"
install -d "$B/docker"; cp docker-compose.yml Dockerfile "$B/docker/"   # на всякий случай (из 2467bc5)
# 2.4 контрольные суммы
(cd "$B" && sha256sum db.dump media.tgz > SHA256SUMS && cat SHA256SUMS)
ls -lh "$B"; echo "$B"                 # запомнить путь (TS)
```

### 2.5 Копия на Mac и проверка — в отдельном окне терминала на Mac

```bash
TS=<из пути шага 2.4>
mkdir -p ~/khayrkhoh-backups
rsync -av --progress deploy@31.25.236.54:/var/backups/khayrkhoh/pre-migration-$TS ~/khayrkhoh-backups/
cd ~/Desktop/volunteers && scripts/verify_backup.sh ~/khayrkhoh-backups/pre-migration-$TS
```

`scripts/verify_backup.sh` (нужен `brew install postgresql@16`, Docker не нужен): сверяет
контрольные суммы, читает `media.tgz` целиком и **пробно восстанавливает `db.dump`** во временный
PostgreSQL 16 в `/tmp` (без TCP-порта), печатает число пользователей, запросов и миграций, потом
удаляет кластер. Конец вывода: `OK: the dump restores and the tables are readable.` **Без
проверенной копии на Mac дальше не идём.**

## 3. Пакеты и системные настройки (шаг b, сайт работает)

```bash
sudo apt-get update
# Python 3.14 — та же версия, на которой прогнаны тесты (в Ubuntu 24.04 python3 = 3.12)
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get install -y python3.14 python3.14-venv
python3.14 -V                          # Python 3.14.x
# PostgreSQL 16 — из репозитория PGDG (apt.postgresql.org), как образ контейнера (16.15 PGDG на
# 2026-10-05): версия не ниже контейнерной, и pg_restore точно читает его дампы. У Ubuntu своя
# сборка может отставать на минорную версию.
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
. /etc/os-release
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
sudo apt-get update
apt-cache policy postgresql-16 | head -4   # Candidate: 16.x-…pgdg24.04… (из apt.postgresql.org)
sudo apt-get install -y postgresql-16 redis-server tmux
pg_restore -V                          # 16.15 или новее — не ниже контейнерной (шаг 1); иначе СТОП
# nginx, brotli, certbot, cron уже стоят (DEPLOY.md старого образца); на всякий случай:
sudo apt-get install -y nginx libnginx-mod-http-brotli-filter certbot python3-certbot-nginx cron git curl
```

PostgreSQL слушает только localhost (так в Ubuntu по умолчанию), Redis — только 127.0.0.1 и ::1;
контейнерам эти порты не мешают (у них нет опубликованных портов). Настройки:

```bash
cd /opt/khayrkhoh
git fetch origin main                  # код ещё НЕ переключаем — только читаем файлы deploy/ из NEW
git show origin/main:deploy/postgresql/khayrkhoh.conf | sudo tee /etc/postgresql/16/main/conf.d/khayrkhoh.conf >/dev/null
sudo systemctl restart postgresql
sudo -u postgres psql -Atc "show shared_buffers; show work_mem; show max_connections; show listen_addresses"
#   512MB / 8MB / 50 / localhost

git show origin/main:deploy/redis/khayrkhoh.conf | sudo tee /etc/redis/khayrkhoh.conf >/dev/null
grep -q '^include /etc/redis/khayrkhoh.conf' /etc/redis/redis.conf \
  || echo 'include /etc/redis/khayrkhoh.conf' | sudo tee -a /etc/redis/redis.conf >/dev/null
sudo systemctl restart redis-server
redis-cli ping; redis-cli config get maxmemory-policy; redis-cli config get appendonly   # PONG / volatile-lru / yes

sudo install -d /etc/systemd/journald.conf.d
git show origin/main:deploy/journald/khayrkhoh.conf | sudo tee /etc/systemd/journald.conf.d/khayrkhoh.conf >/dev/null
sudo systemctl restart systemd-journald
journalctl --disk-usage

sudo cp /etc/logrotate.d/nginx "$B/logrotate-nginx.orig"
git show origin/main:deploy/logrotate/nginx | sudo tee /etc/logrotate.d/nginx >/dev/null
git show origin/main:deploy/logrotate/khayrkhoh | sudo tee /etc/logrotate.d/khayrkhoh >/dev/null
sudo logrotate --debug /etc/logrotate.d/nginx 2>&1 | tail -3      # без "error"
sudo logrotate --debug /etc/logrotate.d/khayrkhoh 2>&1 | tail -3

sudo systemctl is-enabled postgresql redis-server nginx cron        # всё enabled — поднимутся после перезагрузки
```

Сайт всё это время работает из контейнеров и ничего из этого не видит.

## 4. Код, окружение и пробный прогон на копии данных (шаг c, сайт работает)

### 4.1 Код и временный crontab

`git pull` удаляет из каталога `docker-compose.yml`, а старый crontab вызывает
`docker compose exec`. Поэтому сначала переводим cron на `docker exec` по имени контейнера — до
окна он продолжает работать со старым сайтом:

```bash
cd /opt/khayrkhoh
crontab -l | sed 's/docker compose exec -T web /docker exec khayrkhoh_web /' > "$B/crontab-docker-exec.txt"
diff "$B/crontab.txt" "$B/crontab-docker-exec.txt"; crontab "$B/crontab-docker-exec.txt"
git pull --ff-only origin main
git rev-parse --short HEAD             # = NEW из шага 0
ls docker-compose.yml Dockerfile 2>&1 | grep -c "No such"   # 2 — Docker-файлов в коде больше нет
```

Работающие контейнеры от кода в каталоге не зависят (код у них внутри образа), так что сайт не
замечает `git pull`. **Никаких `docker compose up` / `build` с этого момента** — только
`docker stop` / `docker start` по именам.

### 4.2 venv

```bash
python3.14 -m venv .venv
.venv/bin/pip install --disable-pip-version-check -r requirements.txt     # 1–3 мин
.venv/bin/python -V                    # 3.14.x
```

### 4.3 `.env` — то, что раньше задавал docker-compose.yml

Старые контейнеры прочитали `.env` при создании и больше его не читают (`docker start` не
пересоздаёт контейнер), поэтому менять файл сейчас безопасно. Значения в терминал не выводим.

```bash
cp -p .env "$B/env-before-edit"
sed -i -e 's/^DJANGO_DB_HOST=.*/DJANGO_DB_HOST=127.0.0.1/' \
       -e 's/^DJANGO_DB_CACHE=.*/DJANGO_DB_CACHE=False/' \
       -e '/^SHARED_POSTGRES_NETWORK=/d' .env
cat >> .env <<'EOF'

# --- bare metal (deploy/MIGRATION.md): what docker-compose.yml used to set ---
DJANGO_REDIS_URL=redis://127.0.0.1:6379/0
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
DJANGO_CHANNEL_REDIS_URL=redis://127.0.0.1:6379/2
DJANGO_STATIC_MANIFEST=True
DJANGO_MEDIA_X_ACCEL=True
EOF
stat -c '%a %U' .env                  # 600 deploy
sudo systemd-run --quiet --wait --pipe --collect \
  -p User=deploy -p EnvironmentFile=/opt/khayrkhoh/.env -p WorkingDirectory=/opt/khayrkhoh \
  /opt/khayrkhoh/.venv/bin/python deploy/check_env.py
#   "OK: .env is consistent …". FAIL с именем переменной — systemd и python-dotenv читают её
#   по-разному (кавычки, пробелы, "$", "\") или значение не то; поправить в nano и повторить.
set -a; . ./.env; set +a              # обновить переменные в оболочке
newdb() { PGPASSWORD="$DJANGO_DB_PASSWORD" psql -h 127.0.0.1 -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -v ON_ERROR_STOP=1 "$@"; }
manage() { /opt/khayrkhoh/.venv/bin/python manage.py "$@"; }
COUNTS="select 'users', count(*) from accounts_users union all select 'requests', count(*) from myapp_helprequest union all select 'migrations', count(*) from django_migrations"
```

### 4.4 Пользователь и база в родном PostgreSQL, статика

Та же роль, тот же пароль и то же имя базы, что в `.env` (пароль передаётся через переменную, в
историю команд не попадает). Владелец базы — роль приложения, суперпользователем она **не**
является (в контейнере была).

```bash
# локаль — как у старой базы (шаг 1); у образа postgres это обычно en_US.utf8
locale -a | grep -i en_us.utf8 || { sudo locale-gen en_US.UTF-8 && sudo systemctl restart postgresql; }
printf 'CREATE ROLE :"u" LOGIN PASSWORD :%s;\n' "'pw'" \
  | sudo -u postgres psql -v ON_ERROR_STOP=1 -v u="$DJANGO_DB_USER" -v pw="$DJANGO_DB_PASSWORD"
sudo -u postgres createdb -O "$DJANGO_DB_USER" -E UTF8 -T template0 \
  --lc-collate=en_US.UTF-8 --lc-ctype=en_US.UTF-8 "$DJANGO_DB_NAME"   # = datcollate/datctype из шага 1
newdb -Atc "select current_user, current_database()"               # вход по паролю через 127.0.0.1 работает

# Права на файлы, если deploy — не uid 1000 (шаг 1). staticfiles старый контейнер только читает —
# можно сейчас; media он ещё пишет — это в окне (6.5).
[ "$(id -u deploy)" = 1000 ] || sudo chown -R deploy:deploy staticfiles

# Статика — до миграций и до окна (как в FP v2 runbook). Работающий старый web держит старый
# манифест в памяти; старые хешированные файлы остаются на месте.
manage collectstatic --noinput | tail -n 1
#   ошибка → СТОП. Ничего ещё не изменено; разобраться локально.
```

### 4.5 Пробный прогон на копии данных (без простоя)

Восстанавливаем **бэкап из шага 2** в новую базу и проверяем на реальных данных всё, что в окне
будет делаться по-настоящему: формат дампа, права роли, демо-проверку, 14 миграций, запуск
нового приложения. Сайт работает из контейнеров и эту базу не видит.

**В этом шаге — только команды, которые не меняют файлы на диске**: `rename_legacy_uploads` —
только с `--dry-run`, никаких `backfill_*` (media/ общий с живым сайтом).

```bash
PGPASSWORD="$DJANGO_DB_PASSWORD" pg_restore --no-owner --no-privileges --exit-on-error \
  -h 127.0.0.1 -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" "$B/db.dump"; echo "restore rc=$?"   # 0
newdb -Atc "$COUNTS"                   # = числам verify_backup.sh из шага 2.5
manage check_demo_data                 # старая схема, новый код: "OK: no demo data" (иначе СТОП)
manage showmigrations accounts myapp | grep -c '\[ \]'   # 14
time manage migrate --noinput          # записать время — это часть окна
manage rename_legacy_uploads --dry-run # "total: would rename N …" — сколько будет в окне
# новое приложение на копии, на отдельном порту, временным юнитом (бот и celery НЕ запускаем)
sudo systemd-run --unit kk-preview -p User=deploy -p EnvironmentFile=/opt/khayrkhoh/.env \
  -p WorkingDirectory=/opt/khayrkhoh -p Environment=PYTHONUNBUFFERED=1 \
  /opt/khayrkhoh/.venv/bin/gunicorn server.asgi:application -k server.asgi_worker.UvicornWorker \
  --bind 127.0.0.1:8099 -c deploy/gunicorn.conf.py
sleep 5
curl -fsS -H "Host: khayrkhoh.tj" -H "X-Forwarded-Proto: https" http://127.0.0.1:8099/health/ready/; echo
#   {"status": "ready", … "migrations": "ok", "schema": "ok", … "cache": "ok"}
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: khayrkhoh.tj" -H "X-Forwarded-Proto: https" http://127.0.0.1:8099/   # 200
manage check_realtime                  # backend: channels_redis… / round trip: OK
journalctl -u kk-preview --no-pager | grep -iE "error|traceback" | grep -v "was sent SIGTERM" | head
sudo systemctl stop kk-preview
```

Если `restore rc` не 0 — **СТОП**: прочитать первую ошибку (обычно права роли или версия формата
дампа), сказать мне. Пробная база остаётся до окна и там пересоздаётся.

Если `check_demo_data` нашёл демо-данные — **СТОП**: на живом сайте ничего не изменено, сказать
владельцу.

## 5. systemd-юниты и nginx (сайт работает)

```bash
cd /opt/khayrkhoh
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
systemctl list-unit-files 'khayrkhoh-*'    # 4 юнита, все disabled — пока не включаем

sudo diff /etc/nginx/sites-available/khayrkhoh deploy/nginx/khayrkhoh.conf
#   ожидаемые отличия: map $http_upgrade, open_file_cache, location /ws/ (→ :8000),
#   /media/chat/ 404, /protected-media/ internal, /media/ c 30d + nosniff. Строки ssl_* и
#   server_name должны совпасть; иначе — не копировать, сказать мне.
sudo cp deploy/nginx/khayrkhoh.conf /etc/nginx/sites-available/khayrkhoh
ls /etc/nginx/conf.d/                      # не должно быть второго map $connection_upgrade
sudo nginx -t && sudo systemctl reload nginx
curl -fsS https://khayrkhoh.tj/health/ready/; echo                                   # старый сайт жив
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/media/chat/x.jpg        # 404
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/protected-media/x.jpg   # 404
```

Старый сайт к `/ws/` и `/protected-media/` не обращается, поэтому новый конфиг ему не мешает.

## 6. Окно простоя: старые контейнеры → финальный дамп → новые сервисы (шаги a, c, d)

Шаги 6.1–6.9 идут **подряд, без пауз**. Начало и конец записать (время простоя). nginx в это
время отвечает 502, как при любом перезапуске раньше. **Только внутри tmux** — если SSH оборвётся,
`tmux attach -t deploy` и продолжить с того места; ничего не запускать повторно, не посмотрев, чем
закончилась прерванная команда.

```bash
cd /opt/khayrkhoh
[ -n "$TMUX" ] && echo "tmux: ok" || echo "СТОП: вы не в tmux"
sudo -v                                # пароль заранее — чтобы sudo не остановил окно на полпути
# 6.1 остановить сайт и бота (бот — до запуска нового: два опроса одним токеном = 409 Conflict)
date; docker stop khayrkhoh_web khayrkhoh_telegram_bot
# 6.2 дать celery дослать очередь (обычно 0 сразу; ждать не больше минуты), потом остановить
for i in $(seq 1 30); do n=$(docker exec khayrkhoh_redis redis-cli -n 1 llen celery); echo "queue: $n"; [ "$n" = 0 ] && break; sleep 2; done
docker stop khayrkhoh_celery_worker
#   shared_postgres и khayrkhoh_redis НЕ останавливаем — это точка отката (до шага 12)
# 6.3 финальный дамп — после остановки сайта в старую базу больше никто не пишет
docker exec shared_postgres pg_dump -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$B/db-final.dump"
ls -lh "$B/db-final.dump"              # не 0 байт
olddb -Atc "$COUNTS"                   # записать: OLD_COUNTS
# 6.4 пересоздать новую базу из финального дампа
sudo -u postgres dropdb "$DJANGO_DB_NAME"
sudo -u postgres createdb -O "$DJANGO_DB_USER" -E UTF8 -T template0 \
  --lc-collate=en_US.UTF-8 --lc-ctype=en_US.UTF-8 "$DJANGO_DB_NAME"
PGPASSWORD="$DJANGO_DB_PASSWORD" pg_restore --no-owner --no-privileges --exit-on-error \
  -h 127.0.0.1 -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" "$B/db-final.dump"; echo "restore rc=$?"   # 0
newdb -Atc "$COUNTS"                   # ровно OLD_COUNTS (иначе — СТОП, «Откат», уровень 1)
# 6.5 media — писать теперь будет deploy (если это не uid 1000, шаг 1)
[ "$(id -u deploy)" = 1000 ] || sudo chown -R deploy:deploy media
# 6.6 миграции Feature Pack v2 — только в новую базу
manage migrate --noinput
manage showmigrations accounts myapp | grep -c '\[ \]'   # 0
# 6.7 старые «именные» фото → случайные имена (сайт остановлен — никто не загрузит фото поверх)
manage rename_legacy_uploads
#   "total: renamed N, changed meanwhile 0, failed 0". failed > 0 — не останавливаться, разобраться
#   после (в media.tgz файлы есть). Подробно — docs/DEPLOY_FEATURE_PACK_V2.md, «rename_legacy_uploads».
# 6.8 запустить новые сервисы (и включить автозапуск после перезагрузки)
sudo systemctl enable --now khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot
sleep 5; systemctl --no-pager status khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot | grep -E "^●|Active:"
curl -fsS https://khayrkhoh.tj/health/ready/; echo; date    # 200 — конец простоя
# 6.9 новый crontab (9 задач из .venv)
crontab deploy/crontab.example && crontab -l | grep -c manage.py   # 9
```

Если в 6.1–6.8 что-то пошло не так и за ~10 минут не чинится — **«Откат», уровень 1**: старая
база не тронута, старые контейнеры просто запускаются снова.

## 7. Проверка (сразу, шаг d)

`/health/ready/` отвечает 200 только если в порядке БД, миграции (все применены, лишних нет),
схема (все таблицы и столбцы моделей), конфиг и Redis; иначе 503 с одной строкой
`WARNING server.health: readiness: …` в журнале (подробно — справка FP v2, «Проверка»).

```bash
curl -fsS https://khayrkhoh.tj/health/ ; curl -fsS https://khayrkhoh.tj/health/ready/; echo
#   {"status": "ready", "checks": {"database": "ok", "migrations": "ok", "schema": "ok", "config": "ok", "cache": "ok"}}
manage check_realtime                                        # channels_redis… / round trip: OK
# WebSocket через nginx: 101 (curl ждёт и выходит с кодом 28 через 5 с — норма; анонимный сокет закрывается 4401)
curl -sk -o /dev/null -w "%{http_code}\n" --http1.1 -m 5 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Origin: https://khayrkhoh.tj" \
  https://khayrkhoh.tj/ws/
# чужой Origin: 403
curl -sk -o /dev/null -w "%{http_code}\n" --http1.1 -m 5 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Origin: https://evil.example" \
  https://khayrkhoh.tj/ws/
# ошибки в журналах (строки gunicorn "Worker … was sent SIGTERM!" при остановке/перезапуске — штатные)
journalctl -u 'khayrkhoh-*' --since -10min --no-pager | grep -iE "error|traceback" | grep -v "was sent SIGTERM" | head
journalctl -u khayrkhoh-telegram-bot --since -10min --no-pager | tail -n 5   # без Conflict / Unauthorized
journalctl -u khayrkhoh-celery --since -10min --no-pager | grep -m1 "ready"
redis-cli -n 1 llen celery                                   # 0
# IP посетителя доходит до приложения (rate limits): X-Real-IP от nginx с 127.0.0.1
sudo grep -c "proxy_set_header X-Real-IP" /etc/nginx/sites-available/khayrkhoh   # 2 (/ и /ws/)
# память
systemctl show -p MemoryCurrent khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot
free -h                                                      # записать "available" (решение по OSRM)
```

## 8. Однократные команды Feature Pack v2

Объяснения каждой — в справке FP v2 (`docs/DEPLOY_FEATURE_PACK_V2.md`).

```bash
cd /opt/khayrkhoh
manage backfill_achievements --dry-run && manage backfill_achievements    # тихо, без уведомлений
manage purge_chats --dry-run                                              # 0/0 — чатов ещё нет
manage check_demo_data                                                    # "OK: no demo data" (иначе СТОП)

# Старые файлы без ссылок → карантин ВНЕ media/ (решение 2026-10-04: удалить через 30 дней, шаг 13).
# Команда сначала проверяет, что media.tgz из шага 2 читается целиком и содержит каждый файл с теми
# же байтами; если нет — ничего не переносит (код 1) → сказать владельцу.
Q=/var/backups/khayrkhoh/media-quarantine-$(date +%F); echo "$Q"     # записать путь
sudo install -d -o deploy -g deploy -m 700 "$Q"
manage rename_legacy_uploads --quarantine-unreferenced "$Q" --verify-backup "$B/media.tgz" --dry-run
#   "backup check: OK, all K …" + "would move K"
manage rename_legacy_uploads --quarantine-unreferenced "$Q" --verify-backup "$B/media.tgz"
#   "moved to quarantine: K"
manage rename_legacy_uploads --dry-run              # "unreferenced non-random files: 0"
sudo find "$Q" -type f | wc -l                      # = K
sudo grep -nE "^\s*(root|alias) " /etc/nginx/sites-available/khayrkhoh   # только staticfiles/ и media/

# WebP-варианты старых фото и удаление GPS из старых оригиналов (решение 2026-10-04: да)
manage backfill_image_variants --dry-run --strip-metadata
manage backfill_image_variants --strip-metadata
manage backfill_image_variants                      # повторно: "total: variants 0, stripped 0, failed 0"
curl -s https://khayrkhoh.tj/ | grep -c 'type="image/webp"'   # > 0, если есть фотоотчёты
```

## 9. Новый токен основного бота (обязательно, сразу после шагов 7–8)

Старые логи могут содержать прежний токен (до этапа 5 он попадал в текст ошибок): `docker logs`
контейнеров, `/var/log/khayrkhoh/cron.log`. Новый токен делает старый бесполезным. После
`/revoke` старый токен перестаёт работать **сразу** — шаги 1–4 подряд (≈ 3 минуты).

0. Для отчёта (только чтение, токен не печатается):
   ```bash
   for c in khayrkhoh_web khayrkhoh_telegram_bot khayrkhoh_celery_worker; do
     printf "%s: " $c; docker logs $c 2>&1 | grep -cE "bot[0-9]{5,}:[A-Za-z0-9_-]{30,}"
   done
   ```
1. **В Telegram владельца**: @BotFather → `/revoke` → основной бот (тот же `TELEGRAM_BOT_USERNAME`)
   → новый токен `1234567890:AA…`. Никому не пересылать, в чат со мной не вставлять.
2. **На сервере**:
   ```bash
   cd /opt/khayrkhoh
   cp -p .env "$B/env-before-token-rotation"
   nano .env            # TELEGRAM_BOT_TOKEN=… → новое значение; Ctrl+O, Enter, Ctrl+X
   grep -c '^TELEGRAM_BOT_TOKEN=[0-9]\{5,\}:' .env   # 1
   grep -c '\$' .env                                  # 0
   ```
3. **Перезапустить всё, что читает токен** (systemd перечитывает `EnvironmentFile` при каждом
   запуске; уведомления шлёт celery, а web — сам, если Redis недоступен):
   ```bash
   sudo systemctl restart khayrkhoh-telegram-bot khayrkhoh-celery khayrkhoh-web
   journalctl -u khayrkhoh-telegram-bot --since -2min --no-pager | tail -n 20   # без Unauthorized / InvalidToken
   ```
4. **Проверить** (вместе, на телефоне владельца): `/start` в боте → меню роли; под учёткой
   клиента создать запрос «тест, не выполнять» → волонтёру (Telegram владельца) за ≤ 10 с приходит
   «Новый запрос помощи» → отменить запрос; `redis-cli -n 1 llen celery` → 0;
   `journalctl -u khayrkhoh-celery --since -5min | grep -c "Telegram send failed"` → 0.

**Бот в статусе `failed` с кодом 78** (`systemctl status khayrkhoh-telegram-bot` → `Active: failed
(Result: exit-code)`, `status=78`; в журнале `CommandError: Telegram rejected the bot token`) —
Telegram отверг токен; systemd такой бот специально не перезапускает (`RestartPreventExitStatus=78`).
Проверить токен в `.env` (`grep -c '^TELEGRAM_BOT_TOKEN=[0-9]\{5,\}:' .env` → 1; без пробелов и
кавычек вокруг значения), исправить в `nano .env`, затем:

```bash
sudo systemctl reset-failed khayrkhoh-telegram-bot
sudo systemctl start khayrkhoh-telegram-bot
systemctl --no-pager status khayrkhoh-telegram-bot | grep Active      # active (running)
sudo systemctl restart khayrkhoh-celery khayrkhoh-web                  # они тоже читают токен
```

Откат этого шага невозможен (старый токен отозван) — только вписать актуальный токен ещё раз.
**Если после этого шага понадобится откат в Docker** — в `$B/env-docker-era` старый токен:
перед `docker start` контейнеры придётся пересоздать с новым токеном — см. «Откат», примечание.

## 10. Проверка в браузере (вдвоём, 15–20 мин)

Демо-аккаунтов в продакшене нет — используем начальные учётки.

- **Клиент:** создать запрос → уведомление в колокольчике; после принятия — чат («Я в пути»,
  фото); после завершения — оценить помощь.
- **Волонтёр:** принять запрос, согласие на геолокацию, маршрут, этапы работы, сообщение в чате,
  завершить; в профиле — значок «Первая помощь».
- **Куратор:** флаг на сообщении с «переведите деньги», кнопка «Проверено»; «Аналитика» —
  графики, CSV, тепловая карта; «Отзывы».
- **Админ:** админ-панель, заявки волонтёров, карта с живым маркером волонтёра.
- DevTools → Network → WS: одно соединение `/ws/`, статус 101, кадры `hello`/`pong`. В консоли
  главной ошибок нет.
- «Рейтинг» — без мест и баллов, по регионам. Старые фото видны; адрес вида
  `/media/reports/<32 символа>.jpg`.
- **Telegram:** привязка через «Открыть в Telegram» → меню роли; волонтёр — «Запросы рядом» →
  принять → «Я в пути» → геопозиция → завершить; клиент — мастер «Новый запрос»; куратор —
  «Без волонтёра» → «Подобрать волонтёра»; двойное нажатие срабатывает один раз.

## 11. Наблюдение (первые сутки — плотно, потом раз в день до шага 12)

```bash
systemctl --no-pager status 'khayrkhoh-*' | grep -E "^●|Active:"     # active (running), без рестартов
systemctl show -p NRestarts khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot   # 0 у всех
systemctl show -p MemoryCurrent khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot
journalctl -u 'khayrkhoh-*' --since -1h --no-pager | grep -iE "error|traceback" | grep -v "was sent SIGTERM" | head
tail -n 30 /var/log/khayrkhoh/cron.log                               # без трейсбеков
redis-cli -n 1 llen celery                                           # обычно 0
curl -fsS https://khayrkhoh.tj/health/ready/
journalctl --disk-usage; df -h /
free -h
```

**Бот: перезапусков быть не должно.** Исходное значение для сравнения: старый контейнер
`khayrkhoh_telegram_bot` перезапустился **86 раз** с 2026-10-01 по 2026-10-05 (≈ раз в час,
`docker inspect … RestartCount`, без OOM; `web` и `celery` — 0). Критерий для нового бота **строже**,
чем «не хуже старого»: `NRestarts` у `khayrkhoh-telegram-bot` = **0 за первые часы после деплоя**
и дальше не растёт регулярно. Любой перезапуск — разобрать по журналу до шага 12:
`journalctl -u khayrkhoh-telegram-bot --since -2h --no-pager | sed -E 's/[0-9]{8,10}:[A-Za-z0-9_-]{35}/<TOKEN>/g' | tail -100`.
Исключение — **`failed` с кодом 78** (`systemctl status khayrkhoh-telegram-bot` → `status=78`;
то же для `khayrkhoh-community-bot`): это не перезапуск, а остановка из-за отвергнутого токена —
systemd её не повторяет. Проверить токен в `.env`, исправить, затем
`sudo systemctl reset-failed khayrkhoh-telegram-bot && sudo systemctl start khayrkhoh-telegram-bot`
(как в шаге 9).

Через сутки: `manage check_demo_data` — снова "OK: no demo data" (не 0 → ничего не удалять,
сказать владельцу).

### 11.1 Ночной бэкап новой базы (в день деплоя, сразу после шага 10)

С этого дня данные пишутся в родной PostgreSQL — дамп из контейнера их больше не содержит.
Пароль — в `~/.pgpass` (права 600), не в командной строке и не в crontab:

```bash
cd /opt/khayrkhoh; set -a; . ./.env; set +a
umask 077; printf '127.0.0.1:5432:%s:%s:%s\n' "$DJANGO_DB_NAME" "$DJANGO_DB_USER" "$DJANGO_DB_PASSWORD" > ~/.pgpass
stat -c '%a' ~/.pgpass                                     # 600
sudo install -d -o deploy -g deploy -m 700 /var/backups/khayrkhoh/nightly
pg_dump -h 127.0.0.1 -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > /var/backups/khayrkhoh/nightly/db-test.dump \
  && ls -lh /var/backups/khayrkhoh/nightly/db-test.dump && rm /var/backups/khayrkhoh/nightly/db-test.dump   # без запроса пароля
( crontab -l; echo "# KhayrKhoh — nightly DB dump (02:15 UTC), keep 14 days"; \
  echo "15 2 * * * pg_dump -h 127.0.0.1 -U $DJANGO_DB_USER -Fc $DJANGO_DB_NAME > /var/backups/khayrkhoh/nightly/db-\$(date +\%F).dump 2>> /var/log/khayrkhoh/cron.log && find /var/backups/khayrkhoh/nightly -name 'db-*.dump' -mtime +14 -delete" ) | crontab -
crontab -l | tail -n 2
```

На следующий день: `ls -lh /var/backups/khayrkhoh/nightly/` — файл за ночь, не 0 байт. Копию вне
сервера (Mac / другое хранилище) владелец забирает по своему расписанию — README → «Backup &
recovery».

## 12. Удаление Docker (шаг e) — не раньше чем через 7 дней стабильной работы

До этого шага `shared_postgres` и `khayrkhoh_redis` работают, старые контейнеры приложения стоят
остановленными — это точка отката. Шаг 12 делается **только** если за эти дни: `/health/ready/`
всегда 200, `NRestarts` = 0 у web и celery, у бота нет регулярных перезапусков (шаг 11; каждый
единичный — объяснён), пользователи не сообщали о проблемах,
решение «удаляем» — от владельца. **После этого шага вернуться к старому сайту уже нельзя**
(старый код рассчитан на Docker; откат кода — только на более поздние коммиты, см. «Откат»).

```bash
cd /opt/khayrkhoh
set -a; . ./.env; set +a
olddb() { docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -v ON_ERROR_STOP=1 "$@"; }
# 12.1 Ещё раз: в Docker нет ничего чужого (шаг 1). Любое отличие — СТОП, сказать владельцу.
docker ps -a --format '{{.Names}}'          # только khayrkhoh_* и shared_postgres
docker volume ls --format '{{.Name}}'       # только тома этого проекта и shared_postgres_data
olddb -Atc "select datname from pg_database order by 1"   # только наша база + postgres/template0/template1
# 12.2 Последний архивный дамп старой базы (хранить 30 дней) и бэкап новой
F=/var/backups/khayrkhoh/docker-final-$(date +%F); install -d -m 700 "$F"
docker exec shared_postgres pg_dump -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$F/shared_postgres.dump"
PGPASSWORD="$DJANGO_DB_PASSWORD" pg_dump -h 127.0.0.1 -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$F/native.dump"
ls -lh "$F"                                 # оба не 0 байт
# 12.3 Остановить и удалить контейнеры, образы, тома, кэш сборки
docker stop $(docker ps -q) 2>/dev/null; docker rm $(docker ps -aq)
docker system prune -a --volumes -f
docker system df                            # всё 0
# 12.4 Удалить сам Docker
dpkg -l | grep -E 'docker|containerd' | awk '{print $2}'   # что стоит
sudo apt-get purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras
sudo apt-get autoremove --dry-run           # посмотреть список; если там только зависимости Docker:
sudo apt-get autoremove -y
sudo rm -rf /var/lib/docker /var/lib/containerd /etc/docker
sudo rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.gpg
getent group docker && sudo groupdel docker
sudo apt-get update
# 12.5 Проверка: всё работает без Docker, в том числе после перезагрузки
command -v docker || echo "docker: none"
sudo ufw status                             # 22/80/443 как раньше (Docker своих правил больше не держит)
curl -fsS https://khayrkhoh.tj/health/ready/; echo
sudo reboot                                 # в тихое время; через 1–2 минуты:
#   ssh снова → systemctl --no-pager status postgresql redis-server nginx 'khayrkhoh-*' | grep Active
#   → curl -fsS https://khayrkhoh.tj/health/ready/ → 200; бот отвечает на /start
```

Каталог `$B` (бэкапы до переезда) хранить не меньше 30 дней после шага 12; `$F` — 30 дней.
Записать в FEATURES_PROGRESS.md дату удаления Docker.

## 13. Через 30 дней после деплоя: удалить карантин старых файлов

Только если никто не сообщил о пропавшей фотографии (иначе сначала вернуть файл из `$Q` или из
`media.tgz`).

```bash
Q=/var/backups/khayrkhoh/media-quarantine-<дата деплоя>     # путь из шага 8
sudo find "$Q" -type f | wc -l                               # то же K
sudo rm -rf "$Q"
```

---

## Откат (шаг f)

**Когда.** `/health/ready/` даёт 503, или вход / создание / принятие / завершение запроса не
работают, и починить за ~15 минут не получается. Сломана одна новая функция — сначала уровень 0.
Уровни 1 и 2 возможны **только до шага 12**.

### Уровень 0 — выключить одну функцию

| Проблема | Действие | Что видит пользователь |
|---|---|---|
| WebSocket ломает работу | в `/etc/nginx/sites-available/khayrkhoh` в начало `location /ws/ {` вписать `return 404;` → `sudo nginx -t && sudo systemctl reload nginx` | страницы переходят на опрос раз в 10–30 с; уведомления, чат и трекинг медленнее |
| Мешает одна cron-задача | закомментировать её в `crontab -e` | — |
| Основной бот | `sudo systemctl stop khayrkhoh-telegram-bot` | сайт работает; уведомления в Telegram идут (их шлёт celery), не работают меню и ответы бота |
| Бот сообщества | `sudo systemctl disable --now khayrkhoh-community-bot` | группа остаётся без модерации |
| Ошибка в «Аналитике» | страница только для сотрудников | — |

### Уровень 1 — назад в Docker, к старой базе (основной путь в первые часы)

Старый сайт возвращается **ровно в состояние момента остановки** (шаг 6.1): база
`shared_postgres` не менялась. Всё, что записано на новом сайте после окна (запросы, регистрации,
чаты), в старый сайт **не попадает** — оно сохраняется отдельным дампом (1.1), и его можно
перенести вручную или следующим деплоем. Если новых данных уже много и они важны — уровень 2.

```bash
cd /opt/khayrkhoh
B=/var/backups/khayrkhoh/pre-migration-<TS>
set -a; . ./.env; set +a
# 1.1 сохранить данные нового сайта
PGPASSWORD="$DJANGO_DB_PASSWORD" pg_dump -h 127.0.0.1 -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$B/db-after-switch.dump"
ls -lh "$B/db-after-switch.dump"
# 1.2 остановить новое (и убрать из автозапуска)
sudo systemctl disable --now khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot
# 1.3 вернуть старое окружение: манифест статики, фото со старыми именами, nginx, cron
cp "$B/staticfiles.json" staticfiles/staticfiles.json      # старые хешированные файлы на месте
sudo tar -C /opt/khayrkhoh -xzpf "$B/media.tgz"             # старая база ссылается на старые имена фото
[ "$(id -u deploy)" = 1000 ] || sudo chown -R 1000:deploy media staticfiles   # контейнер пишет как uid 1000
sudo cp "$B/nginx-khayrkhoh.conf" /etc/nginx/sites-available/khayrkhoh
sudo nginx -t && sudo systemctl reload nginx
crontab "$B/crontab-docker-exec.txt"                        # docker exec по имени — compose-файла в каталоге нет
# 1.4 запустить старые контейнеры (start, НЕ compose up: они сохранили своё старое окружение)
docker start khayrkhoh_celery_worker khayrkhoh_web khayrkhoh_telegram_bot
sleep 10; curl -fsS https://khayrkhoh.tj/health/ready/; echo
docker ps --format '{{.Names}}\t{{.Status}}'                # 5 контейнеров Up
```

Займёт 3–5 минут. Файлы, загруженные на новом сайте, остаются в `media/` лишними — безвредно.
`.env` трогать не нужно: контейнеры его не перечитывают.

**Примечание — токен бота.** Если откат после шага 9: старые контейнеры знают только
**отозванный** токен, бот и уведомления в Telegram молчат. Тогда вместо 1.4 нужно пересоздать
контейнеры со старым окружением и новым токеном: `cp "$B/env-docker-era" /tmp/env-rollback`,
вписать в него новый токен, `install -m 600 /tmp/env-rollback .env` (сохранив новый `.env` как
`.env.bare-metal`), затем `docker compose -f "$B/docker/docker-compose.yml" -p khayrkhoh
--project-directory /opt/khayrkhoh up -d --no-build web telegram_bot celery_worker`. Это
проверяется на репетиции.

**Снова вперёд после уровня 1**: починить, затем с шага 4.3 (`.env` из `.env.bare-metal`, если
меняли) → 6.

### Уровень 2 — назад в Docker вместе с данными нового сайта (до шага 12)

Когда новый сайт проработал дни и его данные нужны. Миграции Feature Pack v2 откатываются в
**новой** базе (новым кодом — только в нём есть файлы миграций), её дамп заливается в
`shared_postgres`. Теряются данные **новых функций** (уведомления, чат, отзывы, значки, трек,
«не беспокоить», причины отмены); пользователи, запросы, акции, пожертвования, животные — нет.

```bash
cd /opt/khayrkhoh
B=/var/backups/khayrkhoh/pre-migration-<TS>
set -a; . ./.env; set +a
olddb() { docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -v ON_ERROR_STOP=1 "$@"; }
manage() { /opt/khayrkhoh/.venv/bin/python manage.py "$@"; }
sudo systemctl disable --now khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot
crontab -r                                                   # чтобы cron не писал в базу во время отката
PGPASSWORD="$DJANGO_DB_PASSWORD" pg_dump -h 127.0.0.1 -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$B/db-before-rollback2.dump"
# 2.1 миграции назад в новой базе: сначала myapp, потом accounts. Упало — СТОП, сказать мне.
manage migrate myapp 0015
manage migrate accounts 0007
PGPASSWORD="$DJANGO_DB_PASSWORD" pg_dump -h 127.0.0.1 -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$B/db-rollback2.dump"
# 2.2 залить в shared_postgres: сначала очистить схему ТОЛЬКО нашей базы, потом восстановить
#     (не pg_restore --clean — почему, см. справку FP v2)
olddb -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
docker exec -i shared_postgres pg_restore --no-owner --exit-on-error \
  -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" < "$B/db-rollback2.dump"; echo "restore rc=$?"   # 0
olddb -Atc "select count(*) from accounts_users; select max(name) from django_migrations where app='myapp'"
#   пользователей — как в новой базе; миграция myapp = 0015_…
# 2.3 остальное — как уровень 1, шаги 1.3 и 1.4, НО БЕЗ распаковки media.tgz: в базе уже новые
#     (случайные) имена фото, а media.tgz вернул бы старые оригиналы с GPS поверх очищенных.
cp "$B/staticfiles.json" staticfiles/staticfiles.json
[ "$(id -u deploy)" = 1000 ] || sudo chown -R 1000:deploy media staticfiles
sudo cp "$B/nginx-khayrkhoh.conf" /etc/nginx/sites-available/khayrkhoh && sudo nginx -t && sudo systemctl reload nginx
crontab "$B/crontab-docker-exec.txt"
docker start khayrkhoh_celery_worker khayrkhoh_web khayrkhoh_telegram_bot
sleep 10; curl -fsS https://khayrkhoh.tj/health/ready/; echo
```

Мелочи после отката (как в справке FP v2): в очереди celery может остаться задача нового типа —
старый воркер её отбросит; фото чата и `*_thumb/_medium.webp` остаются файлами без записей.

### После шага 12 (Docker удалён)

Вернуться к `2467bc5` нельзя. Ошибки исправляются вперёд: фикс → `deploy/deploy.sh`. Если нужно
вернуть код на предыдущий коммит **после** переезда: `git checkout <коммит>` → миграции назад, если
коммит старше миграций (`manage migrate <app> <номер>` новым кодом **до** checkout) →
`sudo systemctl restart khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot`. Дамп для
крайнего случая — ночной бэкап и `$F/native.dump` (шаг 12.2).

### После любого отката

Записать в FEATURES_PROGRESS.md, что сломалось (журналы, время, шаг), починить локально, добавить
тест, повторить соответствующий кусок на стенде репетиции, затем деплой с шага 0.

---

## Журнал репетиций

_Пока пусто — заполняется на шаге R._
