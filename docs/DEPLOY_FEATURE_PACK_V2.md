# Деплой Feature Pack v2 и план отката

Runbook для совместного деплоя на `khayrkhoh.tj` (31.25.236.54, `/opt/khayrkhoh`, пользователь
`deploy`). Сейчас в продакшене `main` @ `2467bc5`. Решение владельца (2026-10-03): **всё выкатывается
одним деплоем после завершения всех 8 этапов**. Runbook дополняется на каждом этапе; сейчас он
покрывает этапы 1–6: WebSocket, уведомления, доступность, трекинг и маршруты, чат и отзывы,
достижения и аналитика, Telegram-боты, лёгкие WebP-фото и подготовка к Cloudflare.

Ничего из этого не выполняется без явного «deploy» от владельца. Команды ниже — для сессии деплоя.

**Что меняется на сервере:**

| Что | Подробно |
|---|---|
| Новый контейнер `ws` | uvicorn, `127.0.0.1:8001`, лимит 256 МБ (≈ 70–90 МБ в простое, 137 МиБ при 300 открытых сокетах — нагрузочный тест этапа 6) |
| `celery_worker` | теперь монтирует `./media` (как и `telegram_bot` — бот сохраняет фото из чата задачи; без этого все варианты падали с `FileNotFoundError`, найдено на генеральной репетиции 2026-10-04) и строит WebP-варианты фото; одно фото за раз + JPEG `draft`: 4 одновременные загрузки 12 Мп — пик ≈ 120 МБ (без этого было ≈ 690 МБ при лимите 300 МБ). Новые оригиналы — не больше 2560 px, варианты строятся уже из них |
| `osrm_car` / `osrm_foot` | **в этом деплое не включаем** (решение владельца 2026-10-03) — отдельным деплоем позже; до тех пор маршруты по текущему `OSRM_BASE_URL` из `.env` и запасной расчёт по прямой |
| Миграции | `accounts` 0008–0012, `myapp` 0016–0024 (14 штук): только добавление столбцов/таблиц, без удаления данных |
| Образ | базовый образ закреплён: `python:3.14.8-slim-trixie` (раньше плавающий `3.14-slim`); на нём прогнан весь набор тестов, сборка `linux/amd64` |
| `telegram_bot` | та же команда `run_telegram_bot`, но теперь это бот на python-telegram-bot с меню по ролям (этап 5); память ≈ 90 МиБ (было 61 МБ, лимит 300 МБ) |
| Новый `community_bot` | бот сообщества волонтёров, профиль `community` — **в этом деплое не запускается**; включается отдельно, когда владелец создаст бота и группу (`docs/COMMUNITY_BOT.md`); ≈ 90 МиБ, лимит 200 МБ |
| nginx | `map $http_upgrade`, `location /ws/`, `/media/chat/` → 404, `internal` `/protected-media/` (DEPLOY.md §10a); этап 6: `open_file_cache`, `/media/` — кэш 30 дней + `nosniff` (DEPLOY.md §10) |
| cron | 7 новых задач (DEPLOY.md §13); две задачи сообщества ничего не делают, пока оно не настроено |
| Однократно | `backfill_achievements`; `backfill_image_variants` (WebP-варианты старых фото; с `--strip-metadata` — ещё и убрать GPS из старых оригиналов) |
| `.env` | новых обязательных переменных нет; **после деплоя меняется `TELEGRAM_BOT_TOKEN`** — новый токен через @BotFather (шаг 10a) |

Отрепетировано 2026-10-03 на `postgres:16` в Docker: схема `main` + демо-данные → миграции ветки
вперёд → откат миграций → старый код снова работает, данные сохранены → повторный накат. Во время
репетиции нашёлся и исправлен откат миграции 0021 (подробнее — в разделе «Откат»). Этап 6
(accounts 0012 + myapp 0024) отрепетирован так же 2026-10-03 на `postgres:16.15`: `main` + демо-данные →
14 миграций вперёд → откат всех 14 (1 с) → старый код создаёт пользователя и запрос, профиль,
рейтинг, «О нас», карточка запроса, админ-панель, фотоотчёты — 200 → повторный накат.

**Генеральная репетиция всего runbook — 2026-10-04.** Копия «сервера» на Mac: клон из локального
«GitHub» на `2467bc5`, тот же `docker-compose.yml`, образы `linux/amd64` (как на сервере, под
эмуляцией), `shared_postgres` = `postgres:16`, nginx 1.27 с живым конфигом из DEPLOY.md §10 и
самоподписанным сертификатом для `khayrkhoh.tj`, `.env` с `DEBUG=False`. Старый код наполнен
демо-данными и фото с GPS. Пройдено по порядку: шаги 0–11, откат уровня 1, повторный накат, откат
уровня 2. **Нашлось и исправлено три ошибки, каждая остановила бы или испортила настоящий деплой:**
1. `collectstatic` падал на производственном хранилище (вендорный Chart.js ссылался на
   несуществующий `.map`). Исправлено; шаг статики перенесён **до** миграций; тест.
2. У `celery_worker` и `telegram_bot` не было `./media` — все WebP-варианты падали с
   `FileNotFoundError`, а фото из чата бота сохранялись бы внутри контейнера. Исправлено в
   `docker-compose.yml`; тест.
3. Откат уровня 2 (`pg_restore --clean`) не восстанавливал данные и оставлял базу испорченной при
   зелёном `/health/ready/`. Переписан (ниже); проверен.

Остальное прошло как написано. 14 миграций вперёд за 26 с. Простой при `up -d` — около 30 с (под
эмуляцией; на сервере ожидаем 10–20 с). Шаг 8: `check_realtime` OK в `web` и `ws`, WS 101, чужой
Origin 403, ошибок в логах нет. Все 9 cron-команд выполняются (код 0). `--force-recreate`
перечитывает `.env`, а `restart` — нет (шаг 10a). Сквозная проверка в браузере через nginx:
- клиент создал запрос; волонтёр не видит телефон до принятия и видит после;
- чат в обе стороны в реальном времени по WS;
- фото чата: приватный адрес, X-Accel, 2560 px, без GPS; постороннему — 404;
- аватар: случайное имя, 2560 px, без GPS, WebP-варианты от celery, `/media/` с кэшем 30 дней и
  `nosniff`;
- обход всех ссылок за клиента, волонтёра, куратора и админа: 24–70 страниц, все 200, ошибок JS нет.

Откат уровня 1 — 83 с: старый код пишет и читает, данные, созданные на новой версии, сохранились.
Повторный накат — значки созданы заново. Уровень 2 (исправленный) — 30 с, ровно данные дампа.

---

## 0. До сессии деплоя (локально, на Mac)

**Как код попадает на сервер.** Сервер — это клон `https://github.com/YusufDavlatyorov/volunteers.git`
(DEPLOY.md §2) и обновляется командой `git pull` из `origin/main`. Значит, коммиты нужно
**отправить на GitHub в `main`**. Ветка `feature-pack-v2` (коммиты поверх `2467bc5`) вливается
в `main` перемоткой (fast-forward), без merge-коммита. С 2026-10-04 сама ветка уже лежит на GitHub
как внешний бэкап (решение владельца) — в `main` до «deploy» ничего не отправляется.

Репозиторий **публичный** (API GitHub отвечает без авторизации). Всё, что отправляем, станет
видно всем. Проверено 2026-10-03 и 2026-10-04: в диффе нет секретов (`.env`, дампы, `osrm-data/`
в git не попадают; в тестах только фиктивный токен `123:abc`); 2026-10-04 дополнительно — ни одно
значение секретов из `.env` / `.env.production` не встречается ни в одном коммите, и в документах
нет путей `/Users/<имя>`. Если публиковать не хочется, есть вариант
без GitHub: `git bundle create fp2.bundle main` → `scp` на сервер → `git pull fp2.bundle main`.

1. Полный набор тестов зелёный: `python manage.py test --parallel 4`.
2. В сессии деплоя, после «deploy»:

```bash
git checkout main
git merge --ff-only feature-pack-v2      # ошибка = в main появилось что-то новое → остановиться и разобраться
git push origin main
git rev-parse --short HEAD               # записать как NEW
git push origin feature-pack-v2          # ветка уже на GitHub (бэкап) — просто обновить
```

OSRM в этом деплое не включаем, поэтому данные `osrm-data/` на сервер пока не копируем.

## 1. Предварительная проверка (на сервере, только чтение)

```bash
cd /opt/khayrkhoh
git status --short                     # должно быть пусто
git rev-parse --short HEAD             # ожидаем 2467bc5 — это ROLLBACK_COMMIT
docker compose ps                      # web, telegram_bot, celery_worker, redis — Up
sudo nginx -t
curl -fsS https://khayrkhoh.tj/health/ready/
grep -c '\$' .env                      # 0 — без "$" в значениях (проверено 2026-10-02)
grep -c 'DJANGO_BEHIND_TLS_PROXY=True' .env   # 1 — с этапа 6 по нему же читается IP посетителя
sudo grep -n "X-Real-IP" /etc/nginx/sites-available/khayrkhoh   # есть в location / (иначе IP посетителя подделывается)

# права на схему для отката уровня 2 (DROP SCHEMA public) — только чтение
set -a; . ./.env; set +a
docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -Atc \
  "select rolsuper from pg_roles where rolname = current_user; select pg_get_userbyid(nspowner) from pg_namespace where nspname = 'public'"
#   ожидаем: t (суперпользователь, как создаёт DEPLOY.md §4) и pg_database_owner. Если f — сказать мне до деплоя:
#   тогда уровень 2 делается через dropdb/createdb от суперпользователя postgres.

# демо-пароль seed_demo публичный (README, GitHub) — он не должен подходить ни к одному аккаунту.
# Только чтение: сравнение без пересохранения хеша; печатает только логины. ≈ 0,1–1 с на аккаунт.
docker compose exec -T web python manage.py shell -c "from django.contrib.auth.hashers import check_password as c; from accounts.models import Users as U; print('demo:', [u.username for u in U.objects.only('username','password') if u.has_usable_password() and c('Volunteer2026!', u.password)], 'demo users:', list(U.objects.filter(username__in=['admin_demo','malika_s','client_zamira','curator_samira']).values_list('username', flat=True)), 'of', U.objects.count())"
#   ожидаем: demo: [] demo users: [] of N. Если список не пустой — владелец меняет пароль этих
#   аккаунтов (/admin/ → пользователь → «Изменить пароль») ДО деплоя; деплой это не блокирует.

# память и диск
free -h                                # смотреть столбец "available"
df -h / /var/lib/docker /var/backups 2>/dev/null
docker system df                       # образы / build cache / тома
docker image ls khayrkhoh              # текущий образ
set -a; . ./.env; set +a
docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -Atc \
  "select pg_size_pretty(pg_database_size(current_database()))"
du -sh media staticfiles
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}"
```

**Сколько займёт диск** (оценка; точные числа дадут команды выше):

| Что | Оценка | Откуда |
|---|---|---|
| `db.dump` | 20–40 % от `pg_database_size` (формат custom сжимает); для текущих объёмов — единицы–десятки МБ | демо-база на 53 запроса: 128 КБ |
| `media.tgz` | ≈ размер `media/` (JPEG/WebP почти не сжимаются) | `du -sh media` |
| Новый образ | ≈ 300 МБ в распакованном виде против ≈ 265 МБ сейчас (Python-пакеты 126 → 155 МБ: channels, uvicorn). Старый остаётся тегом `rollback-2467bc5`, общий базовый слой не дублируется, поэтому прирост ≈ 200 МБ | сборка `linux/amd64` 2026-10-03 |
| Build cache | до ≈ 300 МБ, можно почистить после деплоя (`docker builder prune`) | — |
| Статика | + ≈ 1 МБ (Chart.js, значки, новые JS/CSS) | — |

**Итого нужно ≈ 0,6 ГБ + размер дампа и media.** Продолжаем, если свободно **не меньше 2 ГБ**
(запас на логи и повторную сборку); если меньше 1 ГБ — сначала `docker image prune`
(удаляет только неиспользуемые образы) и разобраться.

**Память — на будущее, для OSRM.** После этого деплоя добавится `ws` (≈ 70–130 МБ). Для
следующего деплоя с OSRM нужно ещё ≈ 0,5 ГБ RAM (замеры: `osrm_car` 223 МиБ + `osrm_foot`
261 МиБ, лимиты по 400 МБ) и ≈ 0,5 ГБ на диске под данные. Ориентир: если после этого деплоя
и суток работы в `free -h` столбец «available» **больше 1,2 ГБ**, OSRM помещается с запасом;
если меньше 0,8 ГБ — включать только `osrm_car` (пешие маршруты останутся расчётом по прямой).

## 2. Бэкапы — без них не продолжать

```bash
cd /opt/khayrkhoh
TS=$(date +%F-%H%M); B=/var/backups/khayrkhoh/pre-fp2-$TS; sudo install -d -o deploy -g deploy "$B"
set -a; . ./.env; set +a
# 2.1 БД (формат custom, для pg_restore)
docker exec shared_postgres pg_dump -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$B/db.dump"
ls -lh "$B/db.dump"                    # не 0 байт
# 2.2 media (аватары, фотоотчёты, животные)
tar -C /opt/khayrkhoh -czf "$B/media.tgz" media
# 2.3 образ, манифест статики, nginx, crontab
docker tag khayrkhoh:latest khayrkhoh:rollback-2467bc5
cp staticfiles/staticfiles.json "$B/staticfiles.json"
sudo cp /etc/nginx/sites-available/khayrkhoh "$B/nginx-khayrkhoh.conf"
crontab -l > "$B/crontab.txt"
# 2.4 контрольные суммы — по ним Mac проверит, что копия совпадает побайтно
(cd "$B" && sha256sum db.dump media.tgz > SHA256SUMS && cat SHA256SUMS)
ls -lh "$B"; echo "$B"                 # запомнить путь (TS) к бэкапу
```

### 2.5 Копия на Mac и проверка — в отдельном окне терминала на Mac

Бэкап на том же диске не переживёт его потерю, поэтому **без проверенной копии на Mac дальше не идём**:

```bash
TS=<как в пути из шага 2.4, например 2026-10-07-0830>
mkdir -p ~/khayrkhoh-backups
rsync -av --progress deploy@31.25.236.54:/var/backups/khayrkhoh/pre-fp2-$TS ~/khayrkhoh-backups/
cd ~/Desktop/volunteers && scripts/verify_backup.sh ~/khayrkhoh-backups/pre-fp2-$TS
```

`scripts/verify_backup.sh` (нужен запущенный Docker Desktop):
1. сверяет контрольные суммы с `SHA256SUMS` с сервера;
2. проверяет, что `media.tgz` читается целиком, и считает файлы;
3. **пробно восстанавливает `db.dump`** во временный `postgres:16` (та же мажорная версия, что
   `shared_postgres`) и печатает число пользователей, запросов и применённых миграций; потом
   удаляет контейнер. Ничего никуда не отправляется.

Ожидаемый конец вывода: `OK: the dump restores and the tables are readable.` Число пользователей
и запросов должно совпадать с админ-панелью. Скрипт проверен 2026-10-03 на полной цепочке
`pg_dump` → `rsync` → проверка; подменённый файл ловится на шаге контрольных сумм.

## 3. Код и сборка

```bash
git fetch origin && git checkout main && git pull --ff-only
git rev-parse --short HEAD             # = NEW из шага 0
docker compose build                   # 3–6 мин на 1 vCPU
docker image ls khayrkhoh              # latest (новый) + rollback-2467bc5 (старый)
df -h /                                # сверить с оценкой из шага 1
```

`.env` не трогаем: OSRM в этом деплое не включается.

Работающий сайт продолжает обслуживать старый образ: `build` контейнеры не пересоздаёт.

## 4. Статика — до миграций

```bash
docker compose run --rm web python manage.py collectstatic --noinput
#   последняя строка: "N static files copied to '/app/staticfiles', M post-processed."
```

**Если здесь ошибка — СТОП, деплой не продолжать.** Ничего ещё не изменено: схема БД старая,
работающий `web` держит старый манифест в памяти. Разобраться локально и начать с шага 3. Шаг
стоит **до** миграций именно поэтому: на репетиции 2026-10-04 `collectstatic` упал
(`chart.umd.js.map could not be found` — вендорный Chart.js ссылался на несуществующую карту),
а при старом порядке это случилось бы уже после миграций. Исправлено, и тест
`myapp.tests_m7_prep.ProductionCollectstaticTests` теперь прогоняет `collectstatic` с
производственным хранилищем при каждом запуске тестов.

Новые файлы: `vendor/chartjs-4.4.7/`, `vendor/leaflet-heat-0.2.0/`, `images/badges/*.svg`,
`css/analytics.css`, `js/{realtime,live-ui,chat,task-route,profile-location,achievements,crm-analytics}.js`
и другие. Ни один старый файл не удалён, старые хешированные копии остаются — поэтому и откат
статики, и случайный перезапуск старого `web` между шагами 4 и 7 безопасны.

## 5. Миграции (новым образом, до перезапуска)

```bash
docker compose run --rm web python manage.py showmigrations accounts myapp | grep '\[ \]'
#   accounts 0008 … 0012; myapp 0016 … 0024 — ровно 14 строк
docker compose run --rm web python manage.py migrate --noinput
docker compose run --rm web python manage.py showmigrations accounts myapp | grep -c '\[ \]'   # 0
```

Старый код на новой схеме открывает страницы (репетиция 2026-10-04: главная, вход, «О нас»,
`/health/ready/` — 200), но **не может записывать** в изменённые таблицы (новые столбцы `NOT NULL`,
например `Profile.is_away`). Поэтому шаги 5 → 7 идут подряд, без пауз. Если `migrate` упал на
середине — сразу «Откат», уровень 1, с шага 1.2.

## 6. nginx — до `up -d`

Новый `web` отдаёт фото чата через `X-Accel-Redirect` (`DJANGO_MEDIA_X_ACCEL=True`), поэтому
`/protected-media/` должен существовать **до** перезапуска. Старое приложение к `/ws/` и
`/protected-media/` не обращается, так что раньше их включать безопасно.

1. Создать `/etc/nginx/conf.d/websocket-map.conf` со строкой `map` из DEPLOY.md §10a.
2. В `/etc/nginx/sites-available/khayrkhoh`, в блоке `server_name khayrkhoh.tj` (443), **выше**
   `location /media/` и `location /`, вставить `location /ws/`, `location ^~ /media/chat/` и
   `location ^~ /protected-media/` из DEPLOY.md §10a.
3. Этап 6, в том же блоке 443: после `client_max_body_size` добавить 4 строки `open_file_cache…`, а
   `location /media/` заменить новой версией (`expires 30d`, `nosniff`, `sendfile`) — обе из DEPLOY.md §10
   (проверено на `nginx:1.27-alpine` 2026-10-03). Убедиться, что в `location /` есть
   `proxy_set_header X-Real-IP $remote_addr;`.
4. Проверить и перезагрузить:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/media/chat/x.jpg        # 404
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/protected-media/x.jpg   # 404
curl -fsS https://khayrkhoh.tj/health/ready/                                         # 200, сайт жив
```

## 7. Перезапуск

```bash
docker compose up -d                       # пересоздаёт web/telegram_bot/celery_worker, поднимает ws
docker compose ps                          # web, ws, telegram_bot, celery_worker, redis — Up (osrm_* не запускаются: профиль не включён)
```

Простой — примерно 10–20 секунд, пока пересоздаётся `web`.

## 8. Проверка (сразу)

```bash
curl -fsS https://khayrkhoh.tj/health/ ; curl -fsS https://khayrkhoh.tj/health/ready/
docker compose exec -T web python manage.py check_realtime     # channels_redis… / round trip: OK
docker compose exec -T ws  python manage.py check_realtime
curl -sk -o /dev/null -w "%{http_code}\n" --http1.1 -m 5 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Origin: https://khayrkhoh.tj" \
  https://khayrkhoh.tj/ws/                                     # 101 (exit 28 после 5 с — норма)
docker compose logs --since 5m web ws celery_worker | grep -iE "error|traceback" | head
docker stats --no-stream                                       # ws ≈ 70–130 МБ, всё вместе < 1 ГБ
free -h                                                        # записать "available" — понадобится для решения по OSRM
```

## 9. Однократные команды

```bash
docker compose exec -T web python manage.py backfill_achievements --dry-run    # посмотреть, сколько значков выдастся
docker compose exec -T web python manage.py backfill_achievements              # тихо, без уведомлений
docker compose exec -T web python manage.py purge_chats --dry-run              # 0/0 — чатов ещё нет
docker compose exec -T web python manage.py check_demo_password                # "OK: no account accepts the demo password" (полная проверка всех аккаунтов, ≈ 0,1–1 с на аккаунт)
# Этап 6: WebP-варианты старых фото (аватары, фотоотчёты, животные). Бэкап media уже есть (шаг 2).
docker compose exec -T web python manage.py backfill_image_variants --dry-run --strip-metadata
docker compose exec -T web python manage.py backfill_image_variants --strip-metadata   # решение владельца 2026-10-04: да
docker compose exec -T web python manage.py backfill_image_variants                    # повторно: "total: variants 0, stripped 0, failed 0"
curl -s https://khayrkhoh.tj/ | grep -c 'type="image/webp"'                    # > 0, если есть фотоотчёты
```

`--strip-metadata` перезаписывает **старые** оригиналы, в которых остались EXIF/GPS, по тому же
пути (адреса не меняются, качество JPEG 90, размер в пикселях тот же). Новые загрузки очищаются
сами с этапа 6 и уменьшаются до 2560 px по длинной стороне (решение владельца 2026-10-04); старые
файлы бэкфилл не уменьшает. Без флага
команда только строит варианты. Строки `failed` — файл записи пропал с диска; это не ошибка
деплоя, страница покажет запись без фото, как и раньше.

## 10. cron

```bash
crontab -e
```

Добавить 7 строк из DEPLOY.md §13 («Feature Pack v2 adds…»):
- `sweep_availability` — каждые 5 минут;
- `purge_tracking` — раз в час;
- `prune_notifications` и `purge_chats` — раз в день;
- `remind_stale_locations` — раз в день в 10:00 по Душанбе;
- `community_sweep` (раз в час) и `community_weekly` (по понедельникам) — пока сообщество не
  настроено, они только пишут «community not configured».

Через 5–10 минут проверить `tail -n 30 /var/log/khayrkhoh/cron.log` — без трейсбеков.

## 10a. Новый токен основного бота (обязательно, сразу после шагов 8–10)

**Зачем.** До этапа 5 при ошибке отправки в Telegram код писал в лог текст ошибки вместе с
адресом запроса, а в адресе есть токен бота. Исправлено на этапе 5 (и дополнительно на этапе 6:
фильтр `server/log_filters.py` вырезает токен из любой строки лога). Но **старые логи на сервере
могут содержать прежний токен** (`docker logs` контейнеров, journald, `/var/log/khayrkhoh/cron.log`).
Новый токен делает старый бесполезным — чистить логи не нужно.

**Важно:** после `/revoke` старый токен перестаёт работать **сразу** — бот и уведомления в Telegram
молчат, пока новый токен не вписан и контейнеры не пересозданы. Делаем шаги 1–4 подряд (≈ 3 минуты).

0. (Для отчёта, только чтение — сколько строк старых логов содержали токен; сам токен не печатается.)

   ```bash
   cd /opt/khayrkhoh
   for c in web telegram_bot celery_worker; do
     printf "%s: " $c; docker compose logs --no-color $c 2>/dev/null | grep -cE "bot[0-9]{5,}:[A-Za-z0-9_-]{30,}"
   done
   ```

1. **На телефоне/компьютере в Telegram** (аккаунт владельца бота): открыть **@BotFather** →
   отправить `/revoke` → выбрать основного бота (тот же, что `TELEGRAM_BOT_USERNAME`) → BotFather
   пришлёт **новый токен** вида `1234567890:AA…`. Никому его не пересылать, в чат со мной не вставлять.
   Имя бота, его чаты и привязки пользователей не меняются — меняется только ключ.

2. **На сервере** вписать новый токен (редактор `nano`; токен не печатается в терминал):

   ```bash
   cd /opt/khayrkhoh
   cp -p .env /var/backups/khayrkhoh/pre-fp2-<TS>/env-before-token-rotation   # права 600, как у .env
   nano .env            # строка TELEGRAM_BOT_TOKEN=… → заменить значение на новый токен; Ctrl+O, Enter, Ctrl+X
   grep -c '^TELEGRAM_BOT_TOKEN=[0-9]\{5,\}:' .env   # 1 — строка есть и похожа на токен
   grep -c '\$' .env                                  # 0 — без "$"
   ```

3. **Пересоздать контейнеры, которые читают токен.** `docker compose restart` **не** перечитывает
   `.env` — нужен `up -d --force-recreate`. Токен использует не только бот: `celery_worker`
   доставляет уведомления в Telegram, а `web`/`ws` отправляют сами, если очередь Redis недоступна.

   ```bash
   docker compose up -d --force-recreate telegram_bot celery_worker web ws   # web ≈ 10–20 с простоя
   docker compose ps                                                          # все Up
   docker compose logs --since 2m telegram_bot | tail -n 20                    # без "Unauthorized" / "InvalidToken"
   ```

4. **Проверить уведомления** (вместе, на телефоне владельца):
   - в боте отправить `/start` — бот отвечает меню роли (значит, токен рабочий);
   - Telegram владельца привязан к начальной учётке **волонтёра**; в браузере под учёткой
     **клиента** (та же область) создать запрос «тест, не выполнять» → волонтёру в Telegram за ≤ 10 с
     приходит «Новый запрос помощи». (Если волонтёр в режиме «не беспокоить», он такие уведомления
     не получает — тогда админ → «Рассылки» → область волонтёра → короткий текст.) Потом отменить
     тестовый запрос под клиентом;
   - `docker compose exec -T redis redis-cli -n 1 llen celery` → `0` (очередь пуста — всё доставлено);
   - `docker compose logs --since 5m celery_worker | grep -c "Telegram send failed"` → `0`.

Если бот не отвечает: проверить, что в `.env` нет пробела/кавычек вокруг токена, и повторить шаг 3.
Откат этого шага невозможен (старый токен уже отозван) — только вписать актуальный токен ещё раз.
Бот сообщества (`TELEGRAM_COMMUNITY_BOT_TOKEN`) в этом деплое не запущен и в логах не был — его не трогаем.

## 11. Проверка в браузере (вдвоём, 15–20 мин)

На телефоне и компьютере, демо-аккаунтов в продакшене нет — используем 4 начальные учётки:

- **Клиент:** создать запрос → в колокольчике уведомления. После принятия — чат: «Я в пути»,
  фото. После завершения — оценить помощь.
- **Волонтёр:** принять запрос, согласие на геолокацию, маршрут и навигация, этапы работы,
  сообщение в чате, завершить. В профиле должны появиться значок «Первая помощь» и окно значка.
- **Куратор:** в чате видно метку флага на сообщении с «переведите деньги» и работает кнопка
  «Проверено». В «Аналитике» строятся все графики, CSV скачивается, тепловая карта отображается.
  В «Отзывах» — низкая оценка с комментарием.
- **Админ:** админ-панель, заявки волонтёров, карта с живым маркером волонтёра.
- В DevTools → Network → WS одно соединение `/ws/` со статусом 101. На главной в консоли нет ошибок.
- Публичная страница «Рейтинг» — без мест и баллов, сгруппирована по регионам.
- **Telegram (основной бот, на ваших телефонах):** на сайте Профиль → «Привязать Telegram» →
  «Открыть в Telegram» → бот сразу показывает меню роли.
  - Волонтёр: «Запросы рядом» → принять → «Я в пути» → геопозиция → завершить.
  - Клиент: «Новый запрос» через мастер с кнопкой «Отправить мой номер»; в чате ответить
    командой `/chat_<id>` из уведомления.
  - Куратор: «Без волонтёра» → «Подобрать волонтёра» → назначить; «Сигналы SOS» → «Принять».
  - Двойное нажатие любой кнопки срабатывает один раз.

## 12. Наблюдение (24 часа)

- `docker stats --no-stream` утром и вечером: память `ws` и `celery_worker`.
- `docker compose logs --since 1h ws | grep -i error`; `/var/log/khayrkhoh/cron.log`.
- `docker compose exec -T redis redis-cli -n 1 llen celery` — очередь доставки обычно 0.
- `curl https://khayrkhoh.tj/health/ready/` — 200.

---

## Откат

**Когда откатываться.** Если `/health/ready/` даёт 503 или вход, создание/принятие/завершение
запроса не работают и починить за ~15 минут не получается — уровень 1. Если сломана одна новая
функция — сначала уровень 0.

### Уровень 0 — выключить одну функцию, ничего не откатывая

| Проблема | Действие | Что видит пользователь |
|---|---|---|
| WebSocket / `ws` падает | `docker compose stop ws` | страницы переходят на опрос раз в 10–30 с; уведомления, чат и трекинг работают медленнее |
| Мешает одна cron-задача | закомментировать её в `crontab -e` | — |
| Ошибка в «Аналитике» | страница только для сотрудников, ничего не ломает | — |
| Основной бот падает или ведёт себя неверно | `docker compose stop telegram_bot` | сайт работает; уведомления в Telegram по-прежнему приходят (их отправляет `celery_worker`, не бот); не работают только меню и ответы в боте |
| Бот сообщества | `docker compose --profile community stop community_bot` | группа остаётся, модерации и приветствий нет |

### Уровень 1 — полный откат кода с откатом миграций (основной путь)

Данные старых функций сохраняются: пользователи, запросы, акции, пожертвования, животные
и т. д. Данные **новых** функций удаляются вместе с их таблицами и столбцами: уведомления, чат, отзывы,
значки, трек, «не беспокоить», причины отмены. Перед откатом их можно сохранить отдельным дампом.

```bash
cd /opt/khayrkhoh
B=/var/backups/khayrkhoh/pre-fp2-<TS>          # путь из шага 2
set -a; . ./.env; set +a
# 1.1 сохранить то, что успели создать после деплоя
docker exec shared_postgres pg_dump -U "$DJANGO_DB_USER" -Fc "$DJANGO_DB_NAME" > "$B/db-before-rollback.dump"
# 1.2 миграции назад — ЕЩЁ НОВЫМ образом (только в нём есть файлы миграций)
docker compose run --rm web python manage.py migrate myapp 0015
docker compose run --rm web python manage.py migrate accounts 0007
#     порядок важен: сначала myapp, потом accounts. Если первая упала — СТОП, переходить к уровню 2.
# 1.3 вернуть код и образ
crontab "$B/crontab.txt"                      # убрать новые cron-задачи
git checkout 2467bc5
docker tag khayrkhoh:rollback-2467bc5 khayrkhoh:latest
docker compose run --rm web python manage.py collectstatic --noinput   # старый манифест
docker compose up -d --remove-orphans          # уберёт ws (и osrm_*)
# 1.4 nginx
sudo cp "$B/nginx-khayrkhoh.conf" /etc/nginx/sites-available/khayrkhoh
sudo nginx -t && sudo systemctl reload nginx   # websocket-map.conf можно оставить — он безвреден
# 1.5 проверка
curl -fsS https://khayrkhoh.tj/health/ready/
docker compose run --rm web python manage.py showmigrations accounts myapp | grep -c '\[ \]'   # 0
```

Займёт около 5–10 минут. На репетиции (`postgres:16`) откат 14 миграций прошёл за секунды,
и после этого старый код создавал пользователей и запросы и открывал профиль, рейтинг, «О нас»,
карточку запроса и админ-панель.

Мелочи после отката. В очереди Celery может остаться задача с новым именем
(`make_chat_image_variants`): старый воркер запишет «unregistered task» и отбросит её — это
безвредно. Фото чата в `media/chat/` останутся файлами без записей в БД; их можно удалить вручную.
Ключи channel layer в Redis DB 2 истекают сами через 60 секунд.
Этап 6: файлы `*_thumb.webp` / `*_medium.webp` рядом с фото остаются на диске (старый код их не
видит, место — десятки КБ на фото). Фото, загруженные после деплоя, имеют случайные имена и без
EXIF — старый код показывает их как обычно. Очистка GPS из старых оригиналов (`--strip-metadata`)
откатом не возвращается; оригиналы с метаданными есть в `media.tgz` из шага 2. Строку
`open_file_cache` и новый `location /media/` старый код не замечает — nginx можно не откатывать.

Найдено на репетиции и исправлено (2026-10-03): на PostgreSQL откат миграции 0021 падал с
`cannot DROP TABLE "myapp_achievement" because it has pending trigger events` — удаление
стартовых достижений и удаление таблицы шли в одной транзакции. Теперь обратный шаг — no-op.

### Уровень 2 — восстановление БД из дампа (только если повреждена схема или данные)

Теряется **всё**, что записано после шага 2, включая новые запросы клиентов. Использовать, только
если уровень 1 невозможен — например, `migrate … 0015` упал.

```bash
cd /opt/khayrkhoh
docker compose stop web ws telegram_bot celery_worker
set -a; . ./.env; set +a
# Сначала очистить схему ТОЛЬКО нашей базы ($DJANGO_DB_NAME; другие базы в shared_postgres не трогаются),
# потом восстановить с остановкой на первой ошибке.
docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -v ON_ERROR_STOP=1 \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
docker exec -i shared_postgres pg_restore --no-owner --exit-on-error \
  -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" < "$B/db.dump"
echo "restore rc=$?"                           # 0 — иначе СТОП, не запускать сайт
docker exec shared_postgres psql -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -Atc \
  "select count(*) from accounts_users; select max(name) from django_migrations where app='myapp'"
#   число пользователей = как в шаге 2.5 (verify_backup.sh); миграция myapp = 0015_…
git checkout 2467bc5 && docker tag khayrkhoh:rollback-2467bc5 khayrkhoh:latest
docker compose run --rm web python manage.py collectstatic --noinput
docker compose up -d --remove-orphans
sudo cp "$B/nginx-khayrkhoh.conf" /etc/nginx/sites-available/khayrkhoh && sudo nginx -t && sudo systemctl reload nginx
crontab "$B/crontab.txt"
curl -fsS https://khayrkhoh.tj/health/ready/
```

**Почему не `pg_restore --clean`** (так было здесь до 2026-10-04). Генеральная репетиция показала:
`--clean` удаляет только объекты из дампа, а таблицы новых функций (уведомления, чат, отзывы,
значки…) остаются и своими внешними ключами не дают удалить `accounts_users` и
`myapp_helprequest`. Итог — 12 ошибок, пользователи и запросы **не** восстановлены, в
`django_migrations` снова 0015, а новые столбцы (`cancel_reason NOT NULL`) остались. При этом
`/health/ready/` отвечал 200, то есть база была тихо испорчена. Вариант выше проверен на том же
стенде: `rc=0`, ровно данные дампа (25 пользователей, 8 запросов), 0015, таблиц новых функций нет,
старый код пишет и читает, весь обход страниц — 200. Заняло 30 с.

### После отката

Записать в FEATURES_PROGRESS.md, что именно сломалось (логи, время, шаг), починить локально,
добавить тест и повторить деплой с шага 0. Повторный накат после отката уровня 1 проверен на
репетиции: стартовые значки создаются заново.
