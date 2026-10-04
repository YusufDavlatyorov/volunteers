# Cloudflare перед khayrkhoh.tj — пошаговая инструкция для владельца

**Что это даёт.** Сейчас каждый посетитель скачивает картинки, стили и скрипты прямо с нашего
сервера, а у него медленный канал (≈ 270 мс до пользователей, мало пропускной способности).
Cloudflare (бесплатный тариф) держит копии этих файлов на своих серверах ближе к людям: после
первого посетителя файлы отдаются оттуда, сервер их больше не отправляет. Страницы с личными
данными (профиль, запросы, чат) Cloudflare **не кэширует** — они по-прежнему идут с нашего сервера.

**Что уже готово в коде (этап 6):**
- правильные заголовки кэша: статика с хешем в имени — год, `immutable`; фото — 30 дней; личные
  фото чата — `private, no-store` (Cloudflare их не сохраняет);
- на статике и фото нет cookie;
- настоящий IP посетителя для защиты от подбора пароля берётся из `CF-Connecting-IP`, но **только**
  если запрос действительно пришёл с адресов Cloudflare (`server/client_ip.py`);
- WebSocket держит соединение «пингом» каждые 25 с — меньше лимита простоя Cloudflare (100 с).

**Ничего не переключается само.** DNS меняете вы — я этого не делаю. Делать **после** основного
деплоя и суток наблюдения, отдельным шагом. Займёт ≈ 30 минут работы и до суток ожидания.

---

## Шаг 1. Аккаунт и сайт в Cloudflare

1. Зайдите на https://dash.cloudflare.com/sign-up, зарегистрируйтесь (почта + пароль), подтвердите почту.
2. **Add a site** (или «Add a domain») → введите `khayrkhoh.tj` → тариф **Free** → Continue.
3. Cloudflare сам прочитает текущие DNS-записи. **Проверьте список** — он должен совпасть с тем,
   что сейчас в navjucloud:

   | Тип | Имя | Значение | Proxy status |
   |---|---|---|---|
   | A | `khayrkhoh.tj` (или `@`) | `31.25.236.54` | **Proxied** (оранжевое облако) |
   | A | `www` | `31.25.236.54` | **Proxied** |
   | MX / TXT (если есть — почта, проверка домена) | как в navjucloud | как в navjucloud | DNS only (серое) |

   Если какой-то записи из navjucloud нет — добавьте её вручную (**Add record**). Особенно записи
   почты (MX, TXT со `spf`/`dkim`): без них перестанет работать почта на домене.
   Сделайте скриншот DNS-записей в navjucloud **до** изменений — пригодится для отката.

## Шаг 2. Настройки Cloudflare (до смены NS)

В левом меню сайта `khayrkhoh.tj`:

- **SSL/TLS → Overview → Full (strict)**. На сервере уже есть сертификат Let's Encrypt.
  ⚠️ **Не выбирайте «Flexible»** — сайт уйдёт в бесконечную переадресацию.
- **SSL/TLS → Edge Certificates:** «Always Use HTTPS» — **On**; «Minimum TLS Version» — **1.2**.
  HSTS здесь **не включать** (его уже отправляет сам сайт).
- **Network:** «WebSockets» — **On** (обычно уже включено); «HTTP/3» — On.
- **Caching → Configuration:** «Caching Level» — **Standard**; «Browser Cache TTL» —
  **Respect Existing Headers**.
- **Не включать:** «Cache Everything» (правила кэша для всех страниц — личные страницы попадут в
  кэш), **Rocket Loader** (ломает скрипты сайта), «Email Address Obfuscation»
  (Scrape Shield — меняет HTML страниц), «Bot Fight Mode» (может мешать Telegram-превью ссылок).

## Шаг 3. Смена NS в navjucloud

1. Cloudflare покажет **два адреса nameserver**, например `xxx.ns.cloudflare.com` и
   `yyy.ns.cloudflare.com`. Скопируйте их.
2. Войдите в панель **navjucloud** → домен `khayrkhoh.tj` → раздел NS / «Серверы имён» →
   замените текущие NS на два адреса из Cloudflare → сохранить. Старые NS запишите — это откат.
3. Ждите: обычно 1–4 часа, иногда до 24. Cloudflare пришлёт письмо «khayrkhoh.tj is now active».
   Пока идёт переключение, сайт работает — часть посетителей ещё ходит напрямую, часть уже через
   Cloudflare.

## Шаг 4. Включить настройку на сервере

После письма «active» (вместе со мной, в сессии на сервере):

```bash
cd /opt/khayrkhoh
nano .env      # добавить строку:  DJANGO_BEHIND_CLOUDFLARE=True   → Ctrl+O, Enter, Ctrl+X
docker compose up -d --force-recreate web ws   # .env перечитывается только при пересоздании; ≈ 10–20 с простоя
```

Без этой строки сайт работает, но защита от подбора пароля видит вместо посетителей адреса
Cloudflare. Включать её **до** перехода тоже безопасно: пока запросы идут не с адресов
Cloudflare, заголовок `CF-Connecting-IP` игнорируется.

## Шаг 5. Проверка

```bash
curl -sI https://khayrkhoh.tj/ | grep -iE "^server|cf-ray"                 # server: cloudflare, есть cf-ray
U=$(curl -s https://khayrkhoh.tj/ | grep -o '/static/css/style\.[0-9a-f]*\.css' | head -1)
curl -sI https://khayrkhoh.tj$U | grep -i cf-cache-status                  # 2-й-3-й раз: HIT
curl -sI https://khayrkhoh.tj/profile/ | grep -i cf-cache-status           # DYNAMIC (не кэшируется)
sudo certbot renew --dry-run                                               # продление сертификата работает
docker compose logs --since 10m web | grep -i "throttled" | head           # в строках — IP посетителей, не 172.x / 162.158.x
```

В браузере (телефон + компьютер): вход, создание запроса, чат (сообщение приходит сразу —
DevTools → Network → WS: `/ws/` со статусом 101), карта, фото в профиле. Telegram-бот от
Cloudflare не зависит (он ходит к Telegram, а не к сайту).

## Откат

- **Быстро (минуты):** Cloudflare → DNS → у записей `khayrkhoh.tj` и `www` нажать на оранжевое
  облако → **DNS only** (серое). Трафик пойдёт напрямую на сервер, NS менять не нужно.
- **Полностью:** в navjucloud вернуть старые NS (до 24 ч). `DJANGO_BEHIND_CLOUDFLARE` можно
  оставить — без Cloudflare он ничего не делает.

## Обслуживание

- Адреса Cloudflare меняются редко. Раз в полгода сверяйте https://www.cloudflare.com/ips/ со
  списком в `server/settings.py` (`CLOUDFLARE_IP_RANGES`, проверено 2026-10-03). Если появились
  новые — можно без нового релиза: в `.env` строка
  `DJANGO_CLOUDFLARE_IP_RANGES=` с полным списком через запятую, затем пересоздать `web` и `ws`.
- Если по ошибке в кэш попало что-то лишнее: Cloudflare → Caching → **Purge Everything**.
