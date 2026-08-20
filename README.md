# Generation Connect

**Generation Connect** is a Django web platform that connects elderly people who need help
(**clients**) with **volunteers** in their region, coordinated by **curators** and **admins**.
Built for the 5 regions of Tajikistan: Dushanbe, Sogd, Khatlon, GBAO and RRP.

## How it works

1. A **client** creates a help request (groceries, medical escort, transport, household, documents,
   emotional support, etc.).
2. **Volunteers** see the free requests in their region and accept one at a time.
3. On completion the volunteer earns rating points, and the client is notified.
4. **Curators / admins** manage events, send broadcasts to volunteers, review the archive and flag
   overdue tasks.
5. Participants are notified by **email and Telegram**.
6. An **AI assistant** (Groq, OpenAI-compatible API) gives context-aware advice — gentle for clients,
   practical for volunteers — and gracefully falls back to a canned tip when no API key is set.
7. A **Telegram bot** lets users link their chat ID to receive notifications.

## Tech stack

- **Python 3.12+** (developed on 3.14) / **Django 5.2** (function-based views, MVT)
- **SQLite** database (default)
- **django-filter**, **django-crispy-forms** + **crispy-bootstrap5**
- **requests** (Groq AI + Telegram), **python-dotenv** (config), **Pillow** (image uploads)
- Custom user model `accounts.Users` with roles: admin / curator / volunteer / client

## Project layout

```
server/      Django project (settings, urls, wsgi/asgi)
accounts/    Users, Profile, auth views, registration, password reset
myapp/       Help requests, events, broadcasts, photo reports, rating, AI, Telegram bot
templates/   HTML templates (base + accounts/ + myapp/)
static/      CSS, JS, video
media/       User-uploaded avatars and photo reports (not committed)
```

## Getting started

### 1. Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd generation-connect

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example file and edit the values:

```bash
cp .env.example .env
```

Generate a Django secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

All variables are optional for local development and have safe fallbacks:

| Variable | Purpose | If unset |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key | insecure dev key |
| `DJANGO_DEBUG` | Debug mode (`True`/`False`) | `True` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts | `*` |
| `SMTP_USER`, `SMTP_PASSWORD` | Gmail SMTP credentials | emails print to console |
| `GEMINI_API_KEY` | Groq API key for the AI assistant | AI returns a fallback tip |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | Telegram features disabled |

> **Never commit your real `.env`.** It is already listed in `.gitignore`.

### 4. Apply migrations

```bash
python manage.py migrate
```

### 5. (Optional) Load demo data

Creates admins, curators, volunteers, clients, requests, events and photo reports:

```bash
python manage.py seed_demo
```

Demo login (all demo users share the same password):

- `admin_demo` / `Volunteer2026!` (admin)
- `malika_s` / `Volunteer2026!` (volunteer)
- `client_zamira` / `Volunteer2026!` (client)

### 6. Run the server

```bash
python manage.py runserver
```

Open http://127.0.0.1:8000/ . To create your own admin instead of demo data:

```bash
python manage.py createsuperuser
```

### 7. (Optional) Run the Telegram bot

Requires `TELEGRAM_BOT_TOKEN` in `.env`:

```bash
python manage.py run_telegram_bot
```

## Running tests

```bash
python manage.py test
```

The suite covers user creation and roles, registration, password-reset (regression test), the
client → volunteer request lifecycle, rating updates and the AI fallback path.

## Notes

- The SQLite database and uploaded media are intentionally **not** tracked in git; run `migrate`
  (and optionally `seed_demo`) after cloning to recreate them.
- Set `DJANGO_DEBUG=False` and a real `DJANGO_SECRET_KEY` / `DJANGO_ALLOWED_HOSTS` before deploying.
