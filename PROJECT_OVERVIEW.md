# PROJECT_OVERVIEW.md

> **Generation Connect** — Django MVT volunteer platform for Tajikistan.  
> Last analyzed: 2026-06-13. Do not update manually — re-run analysis after major changes.

---

## 1. Project Name

**Generation Connect**

A volunteer management platform that connects elderly clients with volunteers in Tajikistan, coordinated by curators and administrators.

---

## 2. Main Idea

Generation Connect is a regional volunteer coordination platform. Its purpose is to help elderly people (clients) get assistance from volunteers in their region. The workflow:

1. A **client** creates a help request (e.g., buy groceries, go to a clinic).
2. **Volunteers** see free requests in their region and accept one at a time.
3. **Curators** organize events, send broadcast messages to volunteers, and monitor overdue tasks.
4. **Admins** have full visibility of all requests, users, and archive.
5. All participants receive notifications via **email and Telegram**.
6. An **AI assistant** (Groq API, llama-3.1-8b-instant) gives personalized advice — simple and caring for clients, practical for volunteers.
7. A **Telegram bot** allows users to link their Telegram ID for notifications and see their chat ID.

The platform covers 5 regions of Tajikistan: Dushanbe, Sogd, Khatlon, GBAO, RRP.

---

## 3. Django MVT Architecture

### Models
- `accounts.Users` — custom user model with 4 role flags
- `accounts.Profile` — one-to-one profile with avatar, bio, rating
- `myapp.HelpRequest` — core entity: a help request from client to volunteer
- `myapp.Event` — volunteer event created by curator
- `myapp.Broadcast` — mass notification to volunteers by region
- `myapp.PhotoReport` — photo evidence of completed help

### Views
All views are **function-based views (FBV)**. Access control uses a custom `role_required(*roles)` decorator and Django's `@login_required`. Located in `accounts/views.py` and `myapp/views.py`.

### Templates
All templates extend `templates/base.html`. Two sub-groups:
- `templates/accounts/` — auth and profile pages
- `templates/myapp/` — task management, admin panel, AI, photo reports, rating

### URLs
- `server/urls.py` — root: includes `accounts.urls` at `/` and `myapp.urls` at `/myapp/`
- `accounts/urls.py` — auth routes (login, register, profile, forgot/reset password, confirm email)
- `myapp/urls.py` — task management, admin panel, AI, events, broadcasts, reports, rating

### Forms
Located in `accounts/forms.py` and `myapp/forms.py`. All forms are either `ModelForm` or plain `Form`. Validation is present in all forms.

### Admin
Both apps register their models in Django admin with custom `ModelAdmin` classes. Located in `accounts/admin.py` and `myapp/admin.py`.

### Static Files
- `static/css/style.css` — complete custom design system (no Bootstrap in CSS)
- `static/js/i18n.js` — client-side translations (EN / RU / TJ)
- `static/videos/` — one background video file

### Settings
Located in `server/settings.py`. Django 5.2. SQLite database. Custom user model `accounts.Users`. SMTP email. Groq AI and Telegram tokens loaded from `.env` via `python-dotenv`.

---

## 4. Technologies Used

| Layer | Technology |
|---|---|
| Python | 3.13 (based on .venv compiled `.cp314` files — Python 3.14 dev) or 3.13 |
| Django | 5.2 |
| Database | SQLite (`Gen_connect.sqlite3`) |
| ORM | Django ORM |
| Forms | Django Forms + `crispy_forms` + `crispy_bootstrap5` |
| Filtering | `django-filter` |
| Auth | Custom AbstractBaseUser (`accounts.Users`) |
| Email | SMTP via Gmail (`django.core.mail`) |
| AI | Groq API — `llama-3.1-8b-instant` model |
| Telegram | Manual HTTP API (`requests` library) + custom management command |
| Frontend CSS | Custom CSS (CSS variables, light/dark theme, responsive) |
| Frontend JS | Vanilla JS (no frameworks) |
| Fonts | Google Fonts — Inter |
| Hero image | External Unsplash URL (hardcoded in CSS) |
| i18n (frontend) | Custom JS translation system (`i18n.js`) — EN / RU / TJ |
| Notifications | Email (`send_mail`) + Telegram HTTP API |
| Env config | `python-dotenv` |
| Image handling | Pillow |
| HTTP client | `requests` |
| Extra packages | `google-generativeai`, `psycopg2-binary` (installed but not used in current settings) |
| **No requirements.txt** | `needs verification` — no `requirements.txt` found in project root |

---

## 5. Django Apps Overview

### `accounts` app

**Purpose:** Custom user authentication, profile management, email verification, password reset.

**Models:** `Users`, `Profile`

**Views:** `register_view`, `login_view`, `logout_view`, `profile_view`, `edit_profile_view`, `forgot_password_view`, `reset_password_confirm_view`, `confirm_email_view`, `update_profile`

**URLs prefix:** `/` (root, included from `server/urls.py`)

**Forms:** `RegistrationForm`, `LoginForm`, `ForgotPasswordForm`, `ResetPasswordForm`, `ProfileForm`, `UserUpdateForm`

**Templates:** `accounts/login.html`, `accounts/register.html`, `accounts/profile.html`, `accounts/edit_profile.html`, `accounts/forgot_password.html`, `accounts/reset_password_confirm.html`, `accounts/email_confirmed.html`, `accounts/error.html`

**Admin:** `MyUserAdmin` (with `ProfileInline`), `ProfileAdmin`

**Signals:** `create_user_profile` (creates Profile on user creation), `send_welcome_email` (sends welcome email on user creation)

**Connects with:** `myapp` — uses `Users` as ForeignKey in all myapp models

---

### `myapp` app

**Purpose:** Core business logic — help requests, events, broadcasts, photo reports, rating, AI assistant, Telegram bot.

**Models:** `HelpRequest`, `Event`, `Broadcast`, `PhotoReport`

**Views:** `about_view`, `dashboard_view`, `admin_panel_view`, `task_list_view`, `task_detail_view`, `accept_task_view`, `complete_task_view`, `create_request_view`, `completed_tasks_view`, `rating_view`, `ai_assistant_view`, `ai_chat_view`, `create_event_view`, `broadcast_view`, `photo_reports_view`, `check_overdue_view`

**URLs prefix:** `/myapp/`

**Forms:** `HelpRequestForm`, `EventForm`, `BroadcastForm`, `PhotoReportForm`, `HelpRequestFilterForm`

**Templates:** `myapp/about.html`, `myapp/admin_panel.html`, `myapp/task_list.html`, `myapp/task_detail.html`, `myapp/task_form.html` (shared), `myapp/completed_tasks.html`, `myapp/rating.html`, `myapp/ai_assistant.html`, `myapp/ai_advisor.html` (stub), `myapp/photo_reports.html`, `myapp/task_confirm_delete.html` (orphan — no view uses it)

**Admin:** `HelpRequestAdmin`, `EventAdmin`, `BroadcastAdmin`, `PhotoReportAdmin`

**Signals:** `myapp/signals.py` is intentionally empty — notifications are sent explicitly in views to avoid duplicate sends on every model save.

**Management commands:**
- `run_telegram_bot` — long-polling Telegram bot (run as a separate process)
- `seed_demo` — creates full demo dataset (15 volunteers, 6 clients, 3 curators, 1 admin, events, requests, broadcasts, photo reports)

**Notifications module:** `myapp/notifications.py` — `send_email`, `send_telegram_message`, `notify_users`, `volunteer_queryset_for_region`

---

## 6. User Roles / Permissions

There are **4 roles** implemented via boolean flags on the `Users` model.

### Admin (superuser)
- Flag: `is_superuser = True`
- **Can do:** Everything — full access to Django admin, admin panel, all requests, broadcasts, events, archive, overdue check, create events.
- **Pages accessible:** All pages.
- **Restriction:** No other role flag needed. `role_required("admin")` passes if `user.is_superuser`.
- **Where checked:** `role_required` decorator in `myapp/views.py`, Django admin `get_readonly_fields`.

### Curator
- Flag: `is_curator = True`
- **Can do:** View admin panel, create events, send broadcasts, view all requests, check overdue tasks, view archive.
- **Pages accessible:** `/myapp/panel/`, `/myapp/events/create/`, `/myapp/broadcast/`, `/myapp/archive/`, `/myapp/archive/check-overdue/`, task detail for all tasks.
- **Restrictions:** Cannot accept/complete tasks (those are volunteer-only).
- **Where checked:** `@role_required("admin", "curator")` decorator.

### Volunteer
- Flag: `is_volunteer = True`
- **Can do:** View free requests (filtered to their region), accept one request at a time, complete their active request, add photo reports, view rating.
- **Pages accessible:** `/myapp/tasks/`, `/myapp/tasks/<pk>/accept/`, `/myapp/tasks/<pk>/complete/`, `/myapp/reports/`, `/myapp/rating/`, `/myapp/about/`, AI assistant, profile.
- **Restrictions:** Can only hold **one active request** at a time. Sees only tasks in their region (or tasks with empty region).
- **Where checked:** `@role_required("volunteer")` and active-task check in `accept_task_view`.

### Client
- Flag: `is_client = True`
- **Can do:** Create help requests, view their own requests on profile page.
- **Pages accessible:** `/myapp/request/create/`, profile, AI assistant (client mode), rating, about.
- **Restrictions:** Cannot see the task list or admin panel.
- **Where checked:** `@role_required("client")` in `create_request_view`.

### Role assignment rules
- At registration: user selects "Volunteer" or "Client" via radio button in `RegistrationForm`.
- Curator and Admin roles are **only assignable via Django admin** (staff/curator flags).
- A user **cannot have multiple roles** — enforced in `Users.clean()`.

---

## 7. Models Overview

### `accounts.Users`
Custom user model. Extends `AbstractBaseUser`, `PermissionsMixin`.

| Field | Type | Notes |
|---|---|---|
| `id` | BigAutoField | PK |
| `username` | CharField(150) | unique |
| `email` | EmailField | unique |
| `is_email_verified` | BooleanField | default False |
| `is_active` | BooleanField | default True |
| `is_staff` | BooleanField | default False |
| `is_curator` | BooleanField | role flag |
| `is_volunteer` | BooleanField | role flag |
| `is_client` | BooleanField | role flag |
| `telegram_id` | BigIntegerField | unique, null/blank — for Telegram notifications |
| `region` | CharField(100) | choices: dushanbe/sogd/khatlon/gbao/rrp |
| `email_verification_token` | CharField(100) | null/blank — UUID |
| `email_verification_token_created_at` | DateTimeField | null/blank |
| `reset_password_token` | CharField(100) | null/blank — UUID |
| `reset_password_token_created_at` | DateTimeField | null/blank |
| `date_joined` | DateTimeField | auto_now_add |

**`USERNAME_FIELD`:** `username`  
**`REQUIRED_FIELDS`:** `["email"]`  
**`__str__`:** `"{username} ({role_display})"`

**Properties:** `role` (returns string: admin/curator/volunteer/client/guest), `role_display` (human-readable)

**Methods:** `generate_email_verification_token()`, `email_verification_token_is_valid()` (24h TTL), `confirm_email()`, `generate_reset_password_token()`, `reset_password_token_is_valid()` (1h TTL), `clear_reset_password_token()`

**Migration:** `accounts/migrations/0001_initial.py`

---

### `accounts.Profile`
OneToOne extension of Users.

| Field | Type | Notes |
|---|---|---|
| `id` | BigAutoField | PK |
| `user` | OneToOneField(Users) | CASCADE, related_name="profile" |
| `full_name` | CharField(255) | blank |
| `age` | PositiveIntegerField | null/blank |
| `image` | ImageField | upload_to="avatars/", blank |
| `bio` | TextField | blank |
| `rating` | PositiveIntegerField | default 0 |
| `created_at` | DateTimeField | auto_now_add |
| `updated_at` | DateTimeField | auto_now |

**`__str__`:** `"Профиль {username}"`

**Methods:** `add_points(points=3)` — adds to rating and saves

**Created automatically** via `accounts.signals.create_user_profile` on `post_save`.

**Migration:** `accounts/migrations/0001_initial.py`

---

### `myapp.HelpRequest`
Core entity.

| Field | Type | Notes |
|---|---|---|
| `id` | BigAutoField | PK |
| `client` | ForeignKey(Users) | CASCADE, limit_choices_to={is_client: True}, related_name="client_requests" |
| `volunteer` | ForeignKey(Users) | SET_NULL, null/blank, limit_choices_to={is_volunteer: True}, related_name="volunteer_tasks" |
| `help_type` | CharField(50) | choices: medical/grocery/transport/household/emotional/documents/other |
| `description` | TextField | |
| `address` | CharField(255) | |
| `phone` | CharField(30) | |
| `region` | CharField(100) | choices: REGION_CHOICES, blank |
| `status` | CharField(20) | choices: pending/active/completed/cancelled, default "pending" |
| `is_urgent` | BooleanField | default False |
| `alarm_sent` | BooleanField | default False — tracks if overdue alarm was sent |
| `created_at` | DateTimeField | auto_now_add |
| `updated_at` | DateTimeField | auto_now |
| `accepted_at` | DateTimeField | null/blank — set when volunteer accepts |
| `completed_at` | DateTimeField | null/blank — set when completed |

**Meta:** ordering `["-created_at"]`  
**`__str__`:** `"{help_type_display} для {client.username}"`

**Methods:** `accept(volunteer)` — assigns volunteer, sets status to "active", sets `accepted_at`. `complete()` — sets status to "completed", sets `completed_at`.

---

### `myapp.Event`

| Field | Type | Notes |
|---|---|---|
| `id` | BigAutoField | PK |
| `curator` | ForeignKey(Users) | CASCADE, limit_choices_to={is_curator: True}, related_name="events" |
| `title` | CharField(255) | |
| `description` | TextField | |
| `region` | CharField(100) | EVENT_REGION_CHOICES (includes "all"), default "all" |
| `date` | DateTimeField | |
| `created_at` | DateTimeField | auto_now_add |
| `notifications_sent` | BooleanField | default False |

**Meta:** ordering `["-date"]`  
**`__str__`:** `self.title`

---

### `myapp.Broadcast`

| Field | Type | Notes |
|---|---|---|
| `id` | BigAutoField | PK |
| `sender` | ForeignKey(Users) | CASCADE, related_name="broadcasts" |
| `subject` | CharField(255) | |
| `message` | TextField | |
| `region` | CharField(100) | EVENT_REGION_CHOICES, default "all" |
| `created_at` | DateTimeField | auto_now_add |
| `sent_count` | PositiveIntegerField | default 0 |

**Meta:** ordering `["-created_at"]`  
**`__str__`:** `self.subject`

---

### `myapp.PhotoReport`

| Field | Type | Notes |
|---|---|---|
| `id` | BigAutoField | PK |
| `author` | ForeignKey(Users) | SET_NULL, null, related_name="photo_reports" |
| `title` | CharField(255) | |
| `description` | TextField | blank |
| `image` | ImageField | upload_to="reports/" |
| `region` | CharField(100) | REGION_CHOICES, blank |
| `event` | ForeignKey(Event) | SET_NULL, null/blank, related_name="photo_reports" |
| `help_request` | ForeignKey(HelpRequest) | SET_NULL, null/blank, related_name="photo_reports" |
| `created_at` | DateTimeField | auto_now_add |

**Meta:** ordering `["-created_at"]`  
**`__str__`:** `self.title`

---

## 8. Views Overview

### `accounts` views

| View | Type | URL | Method | Auth | Template |
|---|---|---|---|---|---|
| `register_view` | FBV | `/register/` | GET, POST | redirect if authenticated | `accounts/register.html` |
| `login_view` | FBV | `/login/`, `/` (home) | GET, POST | redirect if authenticated | `accounts/login.html` |
| `logout_view` | FBV | `/logout/` | GET | any | redirect to `login` |
| `profile_view` | FBV | `/profile/` | GET | `@login_required` | `accounts/profile.html` |
| `edit_profile_view` | FBV | `/profile/edit/` | GET, POST | `@login_required` | `accounts/edit_profile.html` |
| `forgot_password_view` | FBV | `/forgot-password/` | GET, POST | none | `accounts/forgot_password.html` |
| `reset_password_confirm_view` | FBV | `/reset-password/<token>/` | GET, POST | none | `accounts/reset_password_confirm.html` |
| `confirm_email_view` | FBV | `/confirm-email/<token>/` | GET | none | redirect |
| `update_profile` | FBV | `/update-profile/` | POST only | `@login_required` | JsonResponse |

**Notes:**
- `profile_view`: passes `my_tasks` (last 5) for volunteers, `my_requests` (last 5) for clients.
- `confirm_email_view`: does NOT render a template on success — redirects to `profile`. `email_confirmed.html` is NOT called by any view (see Known Issues).
- `update_profile`: AJAX endpoint, returns JSON `{"success": true/false}`.

---

### `myapp` views

| View | Type | URL (under `/myapp/`) | Method | Auth/Role | Template |
|---|---|---|---|---|---|
| `about_view` | FBV | `about/` | GET | none (public) | `myapp/about.html` |
| `dashboard_view` | FBV | `dashboard/` | GET | `@login_required` | redirect by role |
| `admin_panel_view` | FBV | `panel/` | GET | `admin`, `curator` | `myapp/admin_panel.html` |
| `task_list_view` | FBV | `tasks/` | GET | `volunteer`, `admin`, `curator` | `myapp/task_list.html` |
| `task_detail_view` | FBV | `tasks/<pk>/` | GET | `@login_required` | `myapp/task_detail.html` |
| `accept_task_view` | FBV | `tasks/<pk>/accept/` | GET | `volunteer` | redirect to `task_list` |
| `complete_task_view` | FBV | `tasks/<pk>/complete/` | GET | `volunteer` | redirect to `task_list` |
| `create_request_view` | FBV | `request/create/` | GET, POST | `client` | `myapp/task_form.html` |
| `completed_tasks_view` | FBV | `archive/` | GET | `admin`, `curator` | `myapp/completed_tasks.html` |
| `check_overdue_view` | FBV | `archive/check-overdue/` | GET | `admin`, `curator` | redirect to `admin_panel` |
| `rating_view` | FBV | `rating/` | GET | none (public) | `myapp/rating.html` |
| `ai_assistant_view` | FBV | `ai/` | GET | `@login_required` | `myapp/ai_assistant.html` |
| `ai_chat_view` | FBV | `ai/chat/` | POST | `@login_required` | JsonResponse |
| `create_event_view` | FBV | `events/create/` | GET, POST | `admin`, `curator` | `myapp/task_form.html` |
| `broadcast_view` | FBV | `broadcast/` | GET, POST | `admin`, `curator` | `myapp/task_form.html` |
| `photo_reports_view` | FBV | `reports/` | GET, POST | `@login_required` | `myapp/photo_reports.html` |

**`role_required` decorator:** Defined in `myapp/views.py`. Redirects unauthenticated users to `login`. Redirects users with wrong role to `profile` with an error message. Admins (`is_superuser`) pass any `"admin"` role check.

**Points on task completion:** Urgent task = 5 points. Regular task = 3 points. Applied via `profile.add_points()`.

**AI chat:** Uses Groq API (endpoint: `https://api.groq.com/openai/v1/chat/completions`). Model: `llama-3.1-8b-instant`. System prompt changes based on whether user is client or volunteer. If no API key, returns a hardcoded fallback response (does not crash).

---

## 9. URLs Overview

### Project-level `server/urls.py`

| Pattern | Includes / View | Notes |
|---|---|---|
| `admin/` | Django admin | |
| `` (empty) | `accounts.urls` | all auth routes at root |
| `myapp/` | `myapp.urls` | all main app routes |
| Media files | served in DEBUG mode | `settings.MEDIA_URL` |

---

### `accounts/urls.py`

| Name | Path | View |
|---|---|---|
| `home` | `` | `login_view` |
| `login` | `login/` | `login_view` |
| `register` | `register/` | `register_view` |
| `logout` | `logout/` | `logout_view` |
| `profile` | `profile/` | `profile_view` |
| `edit_profile` | `profile/edit/` | `edit_profile_view` |
| `forgot_password` | `forgot-password/` | `forgot_password_view` |
| `reset_password` | `reset-password/<str:token>/` | `reset_password_confirm_view` |
| `confirm_email` | `confirm-email/<str:token>/` | `confirm_email_view` |
| `update_profile` | `update-profile/` | `update_profile` |

---

### `myapp/urls.py`

| Name | Path (under `/myapp/`) | View |
|---|---|---|
| `dashboard` | `dashboard/` | `dashboard_view` |
| `admin_panel` | `panel/` | `admin_panel_view` |
| `task_list` | `tasks/` | `task_list_view` |
| `accept_task` | `tasks/<int:pk>/accept/` | `accept_task_view` |
| `complete_task` | `tasks/<int:pk>/complete/` | `complete_task_view` |
| `task_detail` | `tasks/<int:pk>/` | `task_detail_view` |
| `create_request` | `request/create/` | `create_request_view` |
| `completed_tasks` | `archive/` | `completed_tasks_view` |
| `check_overdue` | `archive/check-overdue/` | `check_overdue_view` |
| `rating` | `rating/` | `rating_view` |
| `about` | `about/` | `about_view` |
| `ai_assistant` | `ai/` | `ai_assistant_view` |
| `ai_chat` | `ai/chat/` | `ai_chat_view` |
| `create_event` | `events/create/` | `create_event_view` |
| `broadcast` | `broadcast/` | `broadcast_view` |
| `photo_reports` | `reports/` | `photo_reports_view` |

---

## 10. Templates Overview

### Base template: `templates/base.html`
- Loads `static/css/style.css` and `static/js/i18n.js`
- Google Fonts (Inter)
- **Navbar** with role-based links (`data-i18n` attributes for translations)
- **Toast messages** with auto-hide after 4 seconds
- **Theme toggle** (light/dark via `data-theme` on `<html>`, persisted in `localStorage`)
- **Language switcher** (EN / RU / TJ, persisted in `localStorage`)
- **Mobile menu toggle** (hamburger button)
- **Scroll reveal animation** via IntersectionObserver
- `{% block content %}` and `{% block extra_js %}`

### Template inheritance
All templates use `{% extends "base.html" %}` except `myapp/ai_advisor.html` which extends `myapp/ai_assistant.html`.

### Pages

| Template | URL | Purpose |
|---|---|---|
| `accounts/login.html` | `/login/` | Login form |
| `accounts/register.html` | `/register/` | Registration with role selection |
| `accounts/profile.html` | `/profile/` | User profile with quick actions by role |
| `accounts/edit_profile.html` | `/profile/edit/` | Edit email, region, telegram_id, full_name, age, bio, image |
| `accounts/forgot_password.html` | `/forgot-password/` | Request password reset by email |
| `accounts/reset_password_confirm.html` | `/reset-password/<token>/` | Set new password |
| `accounts/email_confirmed.html` | not called by any view | **ORPHAN** — has wrong content (forgot password form) |
| `accounts/error.html` | not wired to any URL/handler | **ORPHAN** — 404-style error page, not connected |
| `myapp/about.html` | `/myapp/about/` | Landing page with stats and photo reports |
| `myapp/admin_panel.html` | `/myapp/panel/` | Admin/curator: free & busy requests, quick actions |
| `myapp/task_list.html` | `/myapp/tasks/` | Volunteer: free requests + my active request |
| `myapp/task_detail.html` | `/myapp/tasks/<pk>/` | Single request detail |
| `myapp/task_form.html` | shared | Reused for: create request, create event, broadcast |
| `myapp/completed_tasks.html` | `/myapp/archive/` | Archive of completed requests |
| `myapp/rating.html` | `/myapp/rating/` | Top 30 volunteers by rating |
| `myapp/photo_reports.html` | `/myapp/reports/` | List + upload form for photo reports |
| `myapp/ai_assistant.html` | `/myapp/ai/` | AI chat interface |
| `myapp/ai_advisor.html` | not directly linked | Stub — just `{% extends "myapp/ai_assistant.html" %}` |
| `myapp/task_confirm_delete.html` | not linked to any URL | **ORPHAN** — no delete view exists |

### Context variables (from views)
- `profile_view` → `profile`, `my_tasks`, `my_requests`
- `admin_panel_view` → `filter_form`, `free_requests`, `busy_requests`, `archive_count`, `broadcasts`, `events`
- `task_list_view` → `tasks`, `filter_form`, `my_active`
- `about_view` → `stats` (dict), `reports`
- `rating_view` → `volunteers`, `by_region`
- `photo_reports_view` → `reports`, `form`

---

## 11. Forms Overview

### `accounts/forms.py`

| Form | Type | Model | Fields | Used In |
|---|---|---|---|---|
| `RegistrationForm` | ModelForm | `Users` | username, email, region, telegram_id + role (ChoiceField), password, confirm_password | `register_view` |
| `LoginForm` | Form | — | username, password | `login_view` |
| `ForgotPasswordForm` | Form | — | email | `forgot_password_view` |
| `ResetPasswordForm` | Form | — | new_password, confirm_password | `reset_password_confirm_view` |
| `ProfileForm` | ModelForm | `Profile` | full_name, age, image, bio | `edit_profile_view` |
| `UserUpdateForm` | ModelForm | `Users` | email, region, telegram_id | `edit_profile_view` |

**Validation notes:**
- `RegistrationForm.clean_username`: min 4 chars, unique check
- `RegistrationForm.clean_email`: lowercase, unique check
- `RegistrationForm.clean_password`: Django's `validate_password`
- `RegistrationForm.clean`: password == confirm_password
- `RegistrationForm.save`: sets `is_volunteer` or `is_client` based on role choice
- `ResetPasswordForm.clean`: password match + min 8 chars
- `UserUpdateForm.clean_email`: unique check excluding self

---

### `myapp/forms.py`

| Form | Type | Model | Fields | Used In |
|---|---|---|---|---|
| `HelpRequestForm` | ModelForm | `HelpRequest` | help_type, description, address, phone, is_urgent | `create_request_view` |
| `EventForm` | ModelForm | `Event` | title, description, region, date | `create_event_view` |
| `BroadcastForm` | ModelForm | `Broadcast` | subject, message, region | `broadcast_view` |
| `PhotoReportForm` | ModelForm | `PhotoReport` | title, description, image, region, event, help_request | `photo_reports_view` |
| `HelpRequestFilterForm` | Form | — | help_type, region, is_urgent | `admin_panel_view`, `task_list_view` |

---

## 12. Admin Panel Overview

### `accounts/admin.py`

**`MyUserAdmin`** (for `Users`):
- `list_display`: username, email, role, region, is_email_verified, is_active, date_joined
- `list_filter`: is_volunteer, is_client, is_curator, is_staff, is_email_verified, region
- `search_fields`: username, email
- `readonly_fields`: date_joined, last_login
- `fieldsets`: organized into sections (Personal, Roles & Access, Security, Dates)
- `inlines`: `ProfileInline` (StackedInline — shows profile fields inline on user page)
- `get_readonly_fields`: superusers cannot be edited by non-superusers

**`ProfileAdmin`** (for `Profile`):
- `list_display`: user, full_name, rating, age, created_at
- `search_fields`: user__username, full_name
- `list_filter`: created_at
- `readonly_fields`: created_at, updated_at

---

### `myapp/admin.py`

**`HelpRequestAdmin`**:
- `list_display`: id, client, volunteer, help_type, status, region, is_urgent, created_at
- `list_filter`: status, help_type, region, is_urgent
- `search_fields`: client__username, volunteer__username, address, phone
- `readonly_fields`: created_at, updated_at, accepted_at, completed_at

**`EventAdmin`**:
- `list_display`: title, curator, region, date, notifications_sent
- `list_filter`: region, notifications_sent, date
- `search_fields`: title, curator__username

**`BroadcastAdmin`**:
- `list_display`: subject, sender, region, sent_count, created_at
- `list_filter`: region, created_at
- `search_fields`: subject, message

**`PhotoReportAdmin`**:
- `list_display`: title, author, region, created_at
- `list_filter`: region, created_at
- `search_fields`: title, description

---

## 13. Authentication Flow

### Registration
- URL: `/register/`
- Form: `RegistrationForm` — username, email, password, confirm_password, role (volunteer/client), region, telegram_id
- After save: generates email verification token, sends verification email via SMTP, logs user in immediately, redirects to `profile`
- Email verification is **not required** to log in — user is active immediately

### Login
- URL: `/login/`
- Form: `LoginForm` — username + password
- Uses Django's `authenticate()` and `login()`
- Checks `user.is_active`
- Redirect: `profile` (configured via `LOGIN_REDIRECT_URL = 'profile'`)

### Logout
- URL: `/logout/`
- GET request — calls Django's `logout()`, redirects to `login`

### Email Verification
- Token: UUID stored in `email_verification_token`, expires in 24 hours
- URL: `/confirm-email/<token>/`
- On success: sets `is_email_verified = True`, clears token, redirects to `profile`
- On failure: redirects to `login` with error message
- **Note:** User is already logged in even before confirming email

### Forgot Password
- URL: `/forgot-password/`
- Form: `ForgotPasswordForm` — email only
- Sends reset link to email (if user exists — no user enumeration: always shows "link sent" message)
- Token expires in 1 hour

### Password Reset
- URL: `/reset-password/<token>/`
- Form: `ResetPasswordForm` — new_password, confirm_password
- On success: calls `user.set_password()`, clears token, redirects to `login`

### Profile / Edit
- URL: `/profile/` — `@login_required`
- URL: `/profile/edit/` — `@login_required`
- URL: `/update-profile/` — `@login_required`, POST only, AJAX JSON

### Access restrictions
- `LOGIN_URL = 'login'` — unauthenticated users are redirected to login
- `@login_required` — standard Django decorator
- `@role_required(*roles)` — custom decorator in `myapp/views.py`

### Custom user model
- `AUTH_USER_MODEL = 'accounts.Users'`
- `AbstractBaseUser` + `PermissionsMixin`
- `USERNAME_FIELD = "username"`

---

## 14. Static Files / UI Overview

### CSS (`static/css/style.css`)
- **Full custom design system** — no Bootstrap or other CSS framework
- **Light/Dark theme** via CSS variables on `[data-theme]` attribute
  - Light: white backgrounds, blue primary, teal secondary
  - Dark: dark navy backgrounds, lighter blue/teal
- **Responsive** — uses CSS Grid with `auto-fill`, `clamp()` for fluid typography and spacing, media queries for mobile menu
- **Glassmorphism** effects on navbar, cards in dark mode
- **Animations:** `fadeSlideUp`, scroll reveal via IntersectionObserver
- **Components:** `.topbar`, `.card`, `.button` variants (primary/secondary/ghost/danger), `.hero`, `.section`, `.grid`, `.two-col`, `.tag`, `.request-row`, `.auth-shell`, `.form-shell`, `.toast`, `.metric`, `.chat-box`, `.bubble`, `.avatar`, `.report-img`
- Hero background: external Unsplash image URL (hardcoded in CSS)

### JavaScript (`static/js/i18n.js`)
- **Client-side i18n** — translations for EN, RU, TJ stored as a JS object `T`
- `setLanguage(lang)` — swaps all `[data-i18n]` element text content, all `[data-i18n-ph]` placeholders
- Language persisted in `localStorage` as `gc-lang`
- Applied on `DOMContentLoaded`

### JavaScript (inline in `base.html`)
- **Theme toggle** — reads/writes `localStorage` key `gc-theme`
- **Mobile menu** — toggles `.open` class on `.nav-links`
- **Scroll reveal** — IntersectionObserver on `.reveal` elements
- **Toast auto-hide** — removes `.toast` elements after 4 seconds

### JavaScript (inline in `ai_assistant.html`)
- **AI chat** — fetches `/myapp/ai/chat/` via `fetch()` POST with JSON, displays bubbles

### Media files
- `media/avatars/` — SVG avatar files for demo users (generated by `seed_demo`)
- `media/reports/` — SVG photo report images for demo data
- `static/videos/` — one MP4 video (not used in any current template)

---

## 15. Database / Migrations Overview

### Database
- **Type:** SQLite
- **File:** `Gen_connect.sqlite3` (in project root)
- Configured in `settings.py`: `DATABASES['default']['ENGINE'] = 'django.db.backends.sqlite3'`

### Migration files

| App | Migration | Models Created |
|---|---|---|
| `accounts` | `0001_initial.py` | `Users`, `Profile` |
| `myapp` | `0001_initial.py` | `HelpRequest`, `Event`, `Broadcast`, `PhotoReport` |

Both migrations are initial migrations created on `2026-05-13`.

### Key relationships
- `Profile` → `Users` (OneToOne, CASCADE)
- `HelpRequest.client` → `Users` (FK, CASCADE)
- `HelpRequest.volunteer` → `Users` (FK, SET_NULL)
- `Event.curator` → `Users` (FK, CASCADE)
- `Broadcast.sender` → `Users` (FK, CASCADE)
- `PhotoReport.author` → `Users` (FK, SET_NULL)
- `PhotoReport.event` → `Event` (FK, SET_NULL)
- `PhotoReport.help_request` → `HelpRequest` (FK, SET_NULL)

### Migration notes
- Only 1 migration per app — no partial or squashed migrations.
- `psycopg2-binary` is installed (PostgreSQL driver) but SQLite is used — easy to switch if needed.

---

## 16. Tests Overview

**Tests are not implemented.**

- `accounts/tests.py` — empty (`# Create your tests here.`)
- `myapp/tests.py` — empty (`# Create your tests here.`)

No test configuration beyond Django's default `TestCase` import.

**To run tests (when written):**
```bash
python manage.py test
```

**Missing test coverage (high priority):**
- User registration and role assignment
- Login / logout flow
- Email verification token logic (24h expiry)
- Password reset token logic (1h expiry)
- `role_required` decorator behavior for each role
- Volunteer accepting a task (one-at-a-time constraint)
- Volunteer completing a task (points added)
- Client creating a request
- Notifications being sent (email + Telegram)
- AI chat fallback when API key missing
- Admin panel access control

---

## 17. Current Project Status

### Fully implemented and functional:
- Custom user model with 4 roles
- Registration, login, logout
- Email verification (token-based, 24h)
- Password reset (token-based, 1h)
- Profile page with role-based quick actions
- Edit profile (user + profile data, avatar upload)
- AJAX profile update endpoint
- Help request creation by clients
- Task list for volunteers (region-filtered)
- Task detail view
- Accept task (one-at-a-time enforcement)
- Complete task (with rating points)
- Admin panel for admin/curator
- Archive of completed tasks
- Overdue check (tasks active > 3 hours)
- Rating page (top 30 volunteers)
- Events creation by curator
- Broadcasts to volunteers by region
- Photo reports (list + upload)
- AI assistant (Groq API + fallback)
- Telegram bot management command
- Email + Telegram notification system
- Demo data seeder (`seed_demo`)
- Light/dark theme
- EN/RU/TJ i18n (client-side)
- Mobile-responsive navbar
- Django admin for all models

### Partially implemented / needs verification:
- `email_confirmed.html` — exists but has wrong content and is not called by any view
- `ai_advisor.html` — stub, just re-extends ai_assistant template, not linked to any URL
- `task_confirm_delete.html` — exists but no delete view exists
- `error.html` — 404-style page exists but is not connected to Django's error handlers
- GROQ_API_KEY bug in settings — see Known Issues

### Not implemented:
- No `requirements.txt` — dependencies not documented
- No unit tests
- No 404/500 custom error handlers configured
- No task deletion view (template exists but no view or URL)
- No pagination on any list views

---

## 18. Known Issues / Risks

### Critical

1. **SMTP credentials hardcoded in `settings.py`** (lines 125-135):
   ```python
   SMTP_USER = '<redacted>'
   SMTP_PASSWORD = '<redacted>'
   ```
   Real Gmail credentials were visible in source code. **Fixed:** moved to `.env` (see `SMTP_USER` /
   `SMTP_PASSWORD` in `.env.example`); the app now falls back to the console email backend when unset.

2. **SECRET_KEY hardcoded in `settings.py`** (line 23):
   ```python
   SECRET_KEY = 'django-insecure-_@((a*iz^...'
   ```
   Must be moved to `.env` before any production use.

3. **`GROQ_API_KEY` bug in `settings.py`** (line 162):
   ```python
   GROQ_API_KEY = os.getenv('GEMINI_API_KEY') or os.getenv('GEMINI_API_KEY')
   ```
   Both sides read `GEMINI_API_KEY`. The variable named `GROQ_API_KEY` in `.env` is never read. AI chat currently works only because `views.py` also reads `GEMINI_API_KEY` directly.

4. **`.env` file contains real API tokens** — `GEMINI_API_KEY` (actually a Groq key) and `TELEGRAM_BOT_TOKEN`. These should not be committed to version control.

5. **`strart` file contains plaintext passwords** (`Volunteer2026!`) and demo credentials. This file should not exist in the project.

### Important

6. **`DEBUG = True` and `ALLOWED_HOSTS = ['*']`** in `settings.py` — not safe for production.

7. **`email_confirmed.html` is broken** — the template has a `<h1>Reset Password</h1>` title (wrong) and renders a forgot-password form — it is not called by `confirm_email_view` (which redirects directly). Template is misleading/orphaned.

8. **No `requirements.txt`** — new developers cannot reproduce the environment. Must be generated with `pip freeze > requirements.txt`.

9. **`role_required` wrapper lacks `@functools.wraps`** — the wrapped view loses its `__name__` and `__doc__`. This can cause issues with Django's URL introspection and some middleware.

10. **`accept_task_view` and `complete_task_view` use GET requests for state-changing operations** — accepting or completing a task should use POST to prevent CSRF issues (links in templates call these via `<a href>`, not forms).

11. **`task_confirm_delete.html` is orphaned** — a delete confirmation template exists but there is no delete view or URL. If task deletion is needed, the view and URL are missing.

12. **`error.html` is not connected** — a 404 page exists (`accounts/error.html`) but no custom error handlers are registered in `server/urls.py` (`handler404 = ...`).

13. **`ai_advisor.html` is a stub** — just `{% extends "myapp/ai_assistant.html" %}`. No URL points to it. Either connect it or remove it.

14. **Hero image loaded from external URL** — `style.css` references `https://images.unsplash.com/...`. If the image is unavailable, the hero section breaks.

15. **`photo_reports_view` allows any authenticated user to upload reports** — no role restriction. Clients, volunteers, curators, and admins can all post reports.

16. **No pagination** on `task_list`, `admin_panel`, `completed_tasks`, `photo_reports`, `rating` — could become slow with large datasets.

17. **`psycopg2-binary` is installed but unused** — switching to PostgreSQL would require changing `settings.py` `DATABASES` config only.

### Minor / Polish

18. **`admin_panel.html` has a duplicate button** (line 13 in template): `<a class="button ghost" href="{% url 'check_overdue' %}">New button</a>` — leftover placeholder.

19. **Media files are served by Django in DEBUG mode** — fine for development, but needs a web server (Nginx/Whitenoise) for production.

20. **`OPENROUTER_API_KEY`** is loaded from `.env` in `settings.py` but never used anywhere in the code.

21. **`google-generativeai` and related packages** are installed but no Gemini API integration exists in the current code (only Groq is used).

---

## 19. Suggested Next Steps

### Critical

- [ ] Move `SECRET_KEY`, `SMTP_USER`, `SMTP_PASSWORD` to `.env` and load them via `os.getenv()`
- [ ] Fix `GROQ_API_KEY` settings bug: `os.getenv('GROQ_API_KEY')` instead of `os.getenv('GEMINI_API_KEY')`
- [ ] Add `requirements.txt` (`pip freeze > requirements.txt`)
- [ ] Delete or secure the `strart` file (contains plaintext passwords)
- [ ] Add `.gitignore` (if not present) to exclude `.env`, `strart`, `*.sqlite3`, `media/`, `.venv/`
- [ ] Fix `accept_task_view` and `complete_task_view` to use POST forms instead of GET links (CSRF safety)

### Important

- [ ] Fix `email_confirmed.html` — either wire it to `confirm_email_view` or fix its content/remove it
- [ ] Connect `error.html` as the project's custom 404 handler (`handler404` in `server/urls.py`)
- [ ] Add `@functools.wraps` to `role_required` decorator wrapper function
- [ ] Implement task deletion view and URL (or remove `task_confirm_delete.html`)
- [ ] Decide on `ai_advisor.html` — connect to a URL or remove
- [ ] Write basic unit tests for: registration, login, role access, task accept/complete
- [ ] Add pagination to list views (task list, archive, photo reports, rating)

### Polish

- [ ] Remove the "New button" placeholder in `admin_panel.html`
- [ ] Add profile page link to `email_confirmed.html` confirmation flow
- [ ] Host the hero background image locally (remove external Unsplash dependency)
- [ ] Add `role` property display to the admin panel user list (already in `list_display` — verify it works as a callable)
- [ ] Consider adding filter to `completed_tasks_view` (same as admin_panel)

---

## 20. Rules For Future Development

1. **Do not use assumptions from other projects.** Analyze only the current files in this directory.
2. **Follow Django MVT architecture.** Models in `models.py`, views in `views.py`, templates in `templates/`.
3. **Do not remove working features** unless explicitly asked.
4. **Do not break existing URLs, templates, forms, or models** without explicit instruction.
5. **Do not change database schema unless explicitly asked.** Never touch migrations unless specifically requested.
6. **Keep the existing template inheritance structure.** All new templates must extend `base.html`.
7. **Keep authentication and permissions working.** Every new protected page must use `@login_required` or `@role_required`.
8. **Every form must have validation and visible error messages** in the template.
9. **Every protected page must check authentication/permissions** using existing decorators.
10. **Use the existing notification system** (`myapp/notifications.py`) for email/Telegram notifications.
11. **Do not add role-checks in signals** — notifications are sent explicitly in views to avoid duplicates.
12. **The `role_required` decorator lives in `myapp/views.py`** — do not duplicate it.
13. **Do not hardcode secrets** — all API keys, passwords, tokens go in `.env` and are loaded via `os.getenv()`.
14. **Do not introduce new external CSS frameworks** — the project uses a custom design system.
15. **i18n strings go in `static/js/i18n.js`** for all three languages (EN/RU/TJ).
16. **Any important change should be documented** — update this file or add a comment explaining the non-obvious reason.
17. **If something is unclear, write `needs verification`** — do not invent or assume.
18. **One volunteer holds at most one active task at a time** — this constraint is enforced in `accept_task_view` and must not be broken.
19. **The `seed_demo` command is for development only** — do not run it in production.
20. **The Telegram bot runs as a separate process** (`manage.py run_telegram_bot`) — it is not part of the Django request/response cycle.
