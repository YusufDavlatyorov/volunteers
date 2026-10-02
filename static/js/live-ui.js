/* KhayrKhoh live UI (Feature Pack v2, features 11/12/3) — signed-in users only,
   loaded deferred right after realtime.js from base.html.

   * Navbar bell: unread badge, dropdown with the latest 10 (fetched on open
     from the bell JSON), "mark all read" (POST + CSRF), each item opens through
     the notification_read POST form with open=1 (marks read, then redirects).
   * Live: notification.new → badge +1, prepend to an open dropdown, toast;
     notification.read / notification.count → badge.
   * Language sync: a click on EN/RU/TJ also POSTs the choice to
     profile_language (failures ignored).
   * Staff pages: elements with [data-availability-user="<id>"] are redrawn on
     availability.changed.

   Notification text is rendered client-side from i18n.js keys + params using
   the same {placeholder} interpolation as data-i18n-args; everything goes in
   through textContent (params carry display names). */
(function () {
    'use strict';

    const RT = window.GCRealtime;
    const bell = document.querySelector('[data-bell]');

    function lang() {
        const l = document.documentElement.getAttribute('lang') || 'ru';
        return window.T && window.T[l] ? l : 'ru';
    }

    function t(key, params) {
        const table = (window.T && window.T[lang()]) || {};
        const fallback = (window.T && window.T.en) || {};
        const text = table[key] || fallback[key] || '';
        if (!params) return text;
        return text.replace(/\{(\w+)\}/g, (m, name) => (name in params ? String(params[name]) : m));
    }

    function csrfToken() {
        const input = document.querySelector('[data-bell] input[name=csrfmiddlewaretoken], input[name=csrfmiddlewaretoken]');
        if (input) return input.value;
        const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    function formatTime(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        const locale = lang() === 'en' ? 'en-GB' : 'ru-RU';
        try {
            return d.toLocaleString(locale, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
        } catch (e) {
            return d.toISOString().slice(0, 16).replace('T', ' ');
        }
    }

    /* ===================== Toasts ===================== */
    function toastStack() {
        let stack = document.querySelector('.toast-stack');
        if (!stack) {
            stack = document.createElement('div');
            stack.className = 'toast-stack';
            document.body.appendChild(stack);
        }
        return stack;
    }

    function showToast(title, body) {
        const toast = document.createElement('div');
        toast.className = 'toast toast--live';
        toast.setAttribute('role', 'status');
        const strong = document.createElement('strong');
        strong.textContent = title;
        toast.appendChild(strong);
        if (body) {
            const p = document.createElement('p');
            p.textContent = body;
            toast.appendChild(p);
        }
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'toast__close';
        close.setAttribute('aria-label', t('common.close') || 'Close');
        close.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
        toast.appendChild(close);
        const remove = () => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(30px)';
            setTimeout(() => toast.remove(), 400);
        };
        close.addEventListener('click', remove);
        toastStack().appendChild(toast);
        // Keep at most 3 live toasts on screen.
        const live = document.querySelectorAll('.toast--live');
        if (live.length > 3) live[0].remove();
        setTimeout(remove, 7000);
    }

    /* ===================== Bell ===================== */
    let count = 0;
    let items = [];
    let open = false;

    const btn = bell && bell.querySelector('[data-bell-button]');
    const badge = bell && bell.querySelector('[data-bell-badge]');
    const panel = bell && bell.querySelector('[data-bell-panel]');
    const list = bell && bell.querySelector('[data-bell-list]');
    const status = bell && bell.querySelector('[data-bell-status]');
    const readAllForm = bell && bell.querySelector('[data-bell-read-all]');

    function renderBadge() {
        if (!bell) return;
        badge.textContent = count > 9 ? '9+' : String(count);
        badge.hidden = count <= 0;
        const label = t('bell.label') || 'Notifications';
        btn.setAttribute('aria-label', count > 0 ? `${label} — ${t('bell.unread', { count })}` : label);
        btn.setAttribute('title', label);
        if (readAllForm) readAllForm.hidden = count <= 0;
    }

    function setCount(n) {
        count = Math.max(0, parseInt(n, 10) || 0);
        renderBadge();
    }

    function setStatus(key) {
        if (!status) return;
        status.hidden = !key;
        status.textContent = key ? t(key) : '';
    }

    function itemNode(n) {
        const li = document.createElement('li');
        const form = document.createElement('form');
        form.method = 'post';
        form.action = bell.dataset.readUrl.replace('/0/', `/${encodeURIComponent(n.id)}/`);
        const csrf = document.createElement('input');
        csrf.type = 'hidden';
        csrf.name = 'csrfmiddlewaretoken';
        csrf.value = csrfToken();
        const openInput = document.createElement('input');
        openInput.type = 'hidden';
        openInput.name = 'open';
        openInput.value = '1';
        const button = document.createElement('button');
        button.type = 'submit';
        button.className = 'bell__item' + (n.read ? '' : ' is-unread');
        button.dataset.notificationId = n.id;
        const title = document.createElement('span');
        title.className = 'bell__item-title';
        title.textContent = t(n.title_key, n.params) || n.title_key;
        const body = document.createElement('span');
        body.className = 'bell__item-body';
        body.textContent = t(n.body_key, n.params);
        const time = document.createElement('time');
        time.className = 'bell__item-time';
        if (n.created_at) time.dateTime = n.created_at;
        time.textContent = formatTime(n.created_at);
        button.append(title, body, time);
        form.append(csrf, openInput, button);
        li.appendChild(form);
        return li;
    }

    function renderList() {
        if (!list) return;
        list.textContent = '';
        items.slice(0, 10).forEach((n) => list.appendChild(itemNode(n)));
        if (!items.length) setStatus('bell.empty');
    }

    function load() {
        setStatus('bell.loading');
        fetch(bell.dataset.bellUrl, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
            .then((data) => {
                items = Array.isArray(data.items) ? data.items : [];
                setCount(data.unread_count);
                setStatus(null);
                renderList();
            })
            .catch(() => setStatus('bell.error'));
    }

    function refreshCount() {
        fetch(bell.dataset.bellUrl, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => {
                if (!data) return;
                setCount(data.unread_count);
                if (open) {
                    items = data.items || [];
                    renderList();
                }
            })
            .catch(() => {});
    }

    function setOpen(next, { focusButton = false } = {}) {
        open = next;
        panel.hidden = !open;
        btn.setAttribute('aria-expanded', String(open));
        bell.classList.toggle('is-open', open);
        if (open) load();
        else if (focusButton) btn.focus();
    }

    if (bell) {
        setCount(bell.dataset.unread);

        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            setOpen(!open);
        });
        document.addEventListener('click', (e) => {
            if (open && !bell.contains(e.target)) setOpen(false);
        });
        document.addEventListener('keydown', (e) => {
            if (open && e.key === 'Escape') {
                e.preventDefault();
                setOpen(false, { focusButton: true });
            }
        });
        bell.addEventListener('focusout', (e) => {
            if (open && e.relatedTarget && !bell.contains(e.relatedTarget)) setOpen(false);
        });

        if (readAllForm) {
            readAllForm.addEventListener('submit', (e) => {
                e.preventDefault();
                fetch(readAllForm.action, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
                })
                    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
                    .then(() => {
                        items.forEach((n) => { n.read = true; });
                        setCount(0);
                        renderList();
                        btn.focus();
                    })
                    .catch(() => readAllForm.submit());
            });
        }

        document.addEventListener('gc:language', () => {
            renderBadge();
            if (open) renderList();
        });
    }

    /* ===================== Realtime wiring ===================== */
    if (RT) {
        let helloCount = 0;
        RT.on('hello', () => {
            // After a reconnect, re-sync whatever we might have missed.
            helloCount += 1;
            if (helloCount > 1 && bell) refreshCount();
        });
        RT.on('notification.new', (n) => {
            if (!n || !n.id) return;
            if (items.some((x) => x.id === n.id)) return;
            if (!n.read) setCount(count + 1);
            items.unshift(n);
            items = items.slice(0, 10);
            if (open) {
                setStatus(null);
                renderList();
            }
            // The open dropdown already shows it — a toast on top would cover it.
            // Nor toast a chat message for the chat the user is reading right now.
            const chatEl = document.querySelector('[data-chat]');
            const inThisChat = chatEl && n.type === 'chat_message' && n.params
                && String(n.params.task_id) === String(chatEl.dataset.taskId);
            if (!open && !inThisChat) showToast(t(n.title_key, n.params) || t('bell.label'), t(n.body_key, n.params));
        });
        RT.on('notification.read', (p) => {
            if (typeof p.unread === 'number') setCount(p.unread);
            if (p.all) items.forEach((n) => { n.read = true; });
            else (p.ids || []).forEach((id) => {
                const n = items.find((x) => x.id === id);
                if (n) n.read = true;
            });
            if (typeof p.unread !== 'number' && p.all) setCount(0);
            if (open) renderList();
        });
        RT.on('notification.count', (p) => {
            if (typeof p.unread === 'number') setCount(p.unread);
        });

        // Staff: live availability / do-not-disturb tags.
        RT.on('availability.changed', (p) => {
            if (!p || !p.user_id) return;
            document.querySelectorAll(`[data-availability-user="${CSS.escape(String(p.user_id))}"]`).forEach((el) => {
                const tag = el.querySelector('[data-availability-status]');
                if (tag && p.status) {
                    tag.dataset.availabilityStatus = p.status;
                    tag.className = 'tag ' + (p.status === 'available' ? 'success' : 'muted');
                    tag.setAttribute('data-i18n', `availability.${p.status}`);
                    tag.textContent = t(`availability.${p.status}`) || p.status;
                }
                const away = el.querySelector('[data-availability-away]');
                if (away && typeof p.away === 'boolean') away.hidden = !p.away;
            });
        });
    }

    /* ===================== Chat unread badges (feature 8) =====================
       [data-chat-unread][data-task-id] badges (myapp/_chat_button.html, the
       client's request lists): subscribe to those task groups (≤ 20, the
       server's per-socket cap) and count chat.message frames that aren't ours.
       Not on the chat page itself (it marks messages read). In polling mode
       the throttled chat_message notification bumps the badge instead. */
    const badges = Array.from(document.querySelectorAll('[data-chat-unread][data-task-id]'));
    if (RT && badges.length && !document.querySelector('[data-chat]')) {
        const seen = new Set();
        const byTask = new Map();
        badges.forEach((b) => {
            const id = Number(b.dataset.taskId);
            if (!byTask.has(id)) byTask.set(id, []);
            byTask.get(id).push(b);
        });
        const bump = (taskId) => {
            (byTask.get(Number(taskId)) || []).forEach((b) => {
                const n = (Number(b.dataset.count) || 0) + 1;
                b.dataset.count = String(n);
                b.setAttribute('data-i18n-args', JSON.stringify({ count: n }));
                b.textContent = t('chat.unread', { count: n }) || String(n);
                b.hidden = false;
            });
        };
        Array.from(byTask.keys()).slice(0, 20).forEach((id) => RT.subscribeTask(id));
        RT.on('chat.message', (m) => {
            if (!m || m.mine || m.hidden || seen.has(m.id) || !byTask.has(Number(m.task_id))) return;
            seen.add(m.id);
            bump(m.task_id);
        });
        RT.on('notification.new', (n) => {
            if (RT.mode === 'ws' || !n || n.type !== 'chat_message' || !n.params) return;
            bump(n.params.task_id);
        });
    }

    /* ===================== Language sync ===================== */
    const syncUrl = document.body.dataset.languageUrl;
    if (syncUrl) {
        document.querySelectorAll('.lang-btn[data-lang]').forEach((b) => {
            b.addEventListener('click', () => {
                const body = new URLSearchParams({ language: b.dataset.lang });
                fetch(syncUrl, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
                    body,
                }).catch(() => {});
            });
        });
    }
})();
