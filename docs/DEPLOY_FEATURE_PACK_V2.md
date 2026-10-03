# Деплой Feature Pack v2 (этапы 1–4) и план отката

Runbook для совместного деплоя на `khayrkhoh.tj` (31.25.236.54, `/opt/khayrkhoh`, пользователь
`deploy`). Сейчас в продакшене `main` @ `2467bc5`. Выкатываются **этапы 1–4 вместе** — этап 1
(WebSocket, уведомления, доступность, «не беспокоить», отмена запросов) тоже ещё не на сервере,
а этапы 2–4 от него зависят.

Ничего из этого не выполняется без явного «deploy» от владельца. Команды ниже — для сессии деплоя.

**Что меняется на сервере:**

| Что | Подробно |
|---|---|
| Новый контейнер `ws` | uvicorn, `127.0.0.1:8001`, лимит 256 МБ (≈ 70 МБ в простое) |
| Опционально `osrm_car` / `osrm_foot` | профиль `osrm`, по ≈ 220–260 МБ |
| Миграции | `accounts` 0008–0010, `myapp` 0016–0022: только добавление столбцов/таблиц, без удаления данных |
| nginx | `map $http_upgrade`, `location /ws/`, `/media/chat/` → 404, `internal` `/protected-media/` (DEPLOY.md §10a) |
| cron | 5 новых задач (DEPLOY.md §13) |
| Однократно | `backfill_achievements` |
| `.env` | обязательных новых переменных нет; `OSRM_*` — только с профилем `osrm` |

Отрепетировано 2026-10-03 на `postgres:16` в Docker: схема `main` + демо-данные → миграции ветки
вперёд → откат миграций → старый код снова работает, данные сохранены → повторный накат. Во время
репетиции нашёлся и исправлен откат миграции 0021 (подробнее — в разделе «Откат»).

---

## 0. До сессии деплоя (локально, на Mac)

1. Полный набор тестов зелёный: `python manage.py test --parallel 4`.
2. В сессии деплоя, после «deploy»: влить `feature-pack-v2` в `main` и отправить —
   `git checkout main && git merge --ff-only feature-pack-v2 && git push origin main`.
   Записать новый хеш: `NEW=$(git rev-parse --short HEAD)`.
3. Опционально (OSRM): данные `osrm-data/{car,foot}` (~440 МБ) скопировать заранее, приложение
   их не трогает, пока не включён профиль:
   `rsync -a --info=progress2 osrm-data/ deploy@31.25.236.54:/opt/khayrkhoh/osrm-data/`.

## 1. Предварительная проверка (на сервере, только чтение)

```bash
cd /opt/khayrkhoh
git status --short                     # должно быть пусто
git rev-parse --short HEAD             # ожидаем 2467bc5 — это ROLLBACK_COMMIT
docker compose ps                      # web, telegram_bot, celery_worker, redis — Up
df -h / ; free -m                      # ≥ 3 ГБ на диске, ≥ 1 ГБ свободной памяти
sudo nginx -t
curl -fsS https://khayrkhoh.tj/health/ready/
grep -c '\$' .env                      # 0 — без "$" в значениях (проверено 2026-10-02)
```

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
echo "$B"                              # запомнить путь к бэкапу
```

Копию `db.dump` сразу забрать с сервера (`scp` на Mac) — бэкап на том же диске не переживёт его потерю.

## 3. Код и сборка

```bash
git fetch origin && git checkout main && git pull --ff-only
git rev-parse --short HEAD             # = NEW из шага 0
docker compose build                   # 3–6 мин на 1 vCPU
# опционально OSRM: добавить в .env
#   OSRM_BASE_URL=http://osrm_car:5000
#   OSRM_FOOT_BASE_URL=http://osrm_foot:5000
# и docker compose --profile osrm pull
```

Работающий сайт продолжает обслуживать старый образ: `build` контейнеры не пересоздаёт.

## 4. Миграции (новым образом, до перезапуска)

```bash
docker compose run --rm web python manage.py showmigrations accounts myapp | grep '\[ \]'
#   accounts 0008, 0009, 0010; myapp 0016 … 0022 — ровно 10 строк
docker compose run --rm web python manage.py migrate --noinput
docker compose run --rm web python manage.py showmigrations accounts myapp | grep -c '\[ \]'   # 0
```

Старый код **не работает** на новой схеме (новые столбцы `NOT NULL`, например `Profile.is_away`;
на репетиции это проверено). Поэтому шаги 4 → 7 идут подряд, без пауз. Если `migrate` упал на
середине — сразу «Откат», уровень 1, с шага 1.2.

## 5. Статика

```bash
docker compose run --rm web python manage.py collectstatic --noinput
```

Новые файлы: `vendor/chartjs-4.4.7/`, `vendor/leaflet-heat-0.2.0/`, `images/badges/*.svg`,
`css/analytics.css`, `js/{realtime,live-ui,chat,task-route,profile-location,achievements,crm-analytics}.js`
и другие. Старые хешированные файлы остаются, поэтому откат статики безопасен.

## 6. nginx — до `up -d`

Новый `web` отдаёт фото чата через `X-Accel-Redirect` (`DJANGO_MEDIA_X_ACCEL=True`), поэтому
`/protected-media/` должен существовать **до** перезапуска. Старое приложение к `/ws/` и
`/protected-media/` не обращается, так что раньше их включать безопасно.

1. Создать `/etc/nginx/conf.d/websocket-map.conf` со строкой `map` из DEPLOY.md §10a.
2. В `/etc/nginx/sites-available/khayrkhoh`, в блоке `server_name khayrkhoh.tj` (443), **выше**
   `location /media/` и `location /`, вставить `location /ws/`, `location ^~ /media/chat/` и
   `location ^~ /protected-media/` из DEPLOY.md §10a.
3. Проверить и перезагрузить:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/media/chat/x.jpg        # 404
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/protected-media/x.jpg   # 404
curl -fsS https://khayrkhoh.tj/health/ready/                                         # 200, сайт жив
```

## 7. Перезапуск

```bash
docker compose up -d                       # пересоздаёт web/telegram_bot/celery_worker, поднимает ws
# или с маршрутами: docker compose --profile osrm up -d
docker compose ps                          # web, ws, telegram_bot, celery_worker, redis (+ osrm_*) — Up
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
docker stats --no-stream                                       # ws ≈ 70–130 МБ, суммарно < 1 ГБ (+ OSRM)
# OSRM (если включён): маршрут из Душанбе внутри города
docker compose exec -T web python -c "import requests;print(requests.get('http://osrm_car:5000/route/v1/driving/68.78,38.56;68.80,38.58',timeout=5).json()['code'])"   # Ok
```

## 9. Однократные команды

```bash
docker compose exec -T web python manage.py backfill_achievements --dry-run    # посмотреть, сколько значков выдастся
docker compose exec -T web python manage.py backfill_achievements              # тихо, без уведомлений
docker compose exec -T web python manage.py purge_chats --dry-run              # 0/0 — чатов ещё нет
```

## 10. cron

```bash
crontab -e
```

Добавить 5 строк из DEPLOY.md §13 («Feature Pack v2 adds five more…»): `sweep_availability`
каждые 5 минут, `purge_tracking` раз в час, `prune_notifications` и `purge_chats` раз в день,
`remind_stale_locations` раз в день в 10:00 по Душанбе. Через 5–10 минут проверить:
`tail -n 30 /var/log/khayrkhoh/cron.log` — без трейсбеков.

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
| Маршруты OSRM | убрать `OSRM_*` из `.env`, затем `docker compose up -d web ws` и `docker compose --profile osrm stop` | маршрут по прямой с оценкой времени (haversine) |
| Мешает одна cron-задача | закомментировать её в `crontab -e` | — |
| Ошибка в «Аналитике» | страница только для сотрудников, ничего не ломает | — |

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

Займёт около 5–10 минут. На репетиции (`postgres:16`) откат 10 миграций прошёл за секунды,
и после этого старый код создавал пользователей и запросы и открывал профиль, рейтинг, «О нас»,
карточку запроса и админ-панель.

Мелочи после отката. В очереди Celery может остаться задача с новым именем
(`make_chat_image_variants`): старый воркер запишет «unregistered task» и отбросит её — это
безвредно. Фото чата в `media/chat/` останутся файлами без записей в БД; их можно удалить вручную.
Ключи channel layer в Redis DB 2 истекают сами через 60 секунд.

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
docker exec -i shared_postgres pg_restore --clean --if-exists --no-owner \
  -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" < "$B/db.dump"
git checkout 2467bc5 && docker tag khayrkhoh:rollback-2467bc5 khayrkhoh:latest
docker compose run --rm web python manage.py collectstatic --noinput
docker compose up -d --remove-orphans
sudo cp "$B/nginx-khayrkhoh.conf" /etc/nginx/sites-available/khayrkhoh && sudo nginx -t && sudo systemctl reload nginx
crontab "$B/crontab.txt"
curl -fsS https://khayrkhoh.tj/health/ready/
```

### После отката

Записать в FEATURES_PROGRESS.md, что именно сломалось (логи, время, шаг), починить локально,
добавить тест и повторить деплой с шага 0. Повторный накат после отката уровня 1 проверен на
репетиции: стартовые значки создаются заново.
