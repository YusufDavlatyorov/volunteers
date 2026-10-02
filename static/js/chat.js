/* KhayrKhoh task chat (Feature Pack v2, feature 8) — templates/myapp/chat.html,
   styles in static/css/chat.css. Protocol: docs/WS_PROTOCOL.md ("Chat").

   Transport: the shared socket (window.GCRealtime, realtime.js) — subscribe
   task_<id>, send chat.send / chat.typing / chat.read, render chat.message /
   chat.message_updated / chat.typing / chat.read. While the socket is down:
   POST to the send/read endpoints and poll messages/?after=<latest id> every
   10 s. A chat.send that gets no ack within 8 s is re-sent over HTTP with the
   same client_msg_id (the server dedupes it), so a half-dead socket never
   swallows a message.

   Safety: every message text goes in through textContent — links are never
   made clickable, nothing is parsed as HTML. Payloads carry display names
   only (a curator's post shows the "chat.staff_label" label to parties).

   Hooks: [data-chat] (task-id, is-staff, can-write, my-last-read, peer-read,
   cursor, *-url, review-url), [data-chat-scroll], [data-chat-log],
   [data-chat-typing], [data-chat-error], [data-chat-mode], [data-chat-form],
   [data-chat-upload], [data-chat-quick] [data-quick], [data-chat-large]
   (localStorage gc.chat.large), [data-chat-older]. */
(function () {
    'use strict';

    const root = document.querySelector('[data-chat]');
    if (!root) return;
    root.classList.add('is-js');

    const RT = window.GCRealtime;
    const taskId = Number(root.dataset.taskId);
    const isStaff = root.dataset.isStaff === '1';
    let canWrite = root.dataset.canWrite === '1';
    const maxLength = Number(root.dataset.maxLength || 1000);
    const MAX_UPLOAD = 5 * 1024 * 1024;
    const ACK_TIMEOUT_MS = 8000;
    const POLL_MS = 10000;

    const scroller = root.querySelector('[data-chat-scroll]');
    const log = root.querySelector('[data-chat-log]');
    const typingEl = root.querySelector('[data-chat-typing]');
    const errorEl = root.querySelector('[data-chat-error]');
    const modeEl = root.querySelector('[data-chat-mode]');
    const form = root.querySelector('[data-chat-form]');
    const uploadForm = root.querySelector('[data-chat-upload]');
    const textarea = form ? form.querySelector('textarea') : null;
    const fileInput = uploadForm ? uploadForm.querySelector('input[type=file]') : null;

    let cursor = root.dataset.cursor ? Number(root.dataset.cursor) : null;
    let myLastRead = Number(root.dataset.myLastRead || 0);
    let peerRead = Number(root.dataset.peerRead || 0);
    let typingTimer = null;
    let lastTypingSent = 0;
    let readTimer = null;
    let errorTimer = null;
    const pending = new Map();   // client_msg_id -> {text, timer}
    // chat.message_updated that raced ahead of its bubble (e.g. the WebP thumb
    // is ready before the upload's HTTP answer arrives): applied on insert.
    const updates = new Map();

    /* ---------- helpers ---------- */
    function lang() {
        const l = document.documentElement.getAttribute('lang') || 'ru';
        return window.T && window.T[l] ? l : 'ru';
    }

    function t(key, fallback, params) {
        const table = (window.T && window.T[lang()]) || {};
        const en = (window.T && window.T.en) || {};
        let text = table[key] || en[key] || fallback || key;
        if (params) text = text.replace(/\{(\w+)\}/g, (m, n) => (n in params ? String(params[n]) : m));
        return text;
    }

    function csrfToken() {
        const input = root.querySelector('input[name=csrfmiddlewaretoken]') || document.querySelector('input[name=csrfmiddlewaretoken]');
        if (input) return input.value;
        const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    function post(url, body) {
        const isForm = body instanceof FormData;
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: Object.assign(
                { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json' },
                isForm ? {} : { 'Content-Type': 'application/json' },
            ),
            body: isForm ? body : JSON.stringify(body || {}),
        }).then((r) => r.json().catch(() => ({ ok: false, code: r.status === 413 ? 'too_large' : 'server_error' })));
    }

    function flash(text, isError) {
        if (!errorEl) return;
        errorEl.textContent = text;
        errorEl.classList.toggle('is-ok', !isError);
        errorEl.hidden = false;
        clearTimeout(errorTimer);
        errorTimer = setTimeout(() => { errorEl.hidden = true; }, isError ? 8000 : 5000);
    }

    function showError(code) {
        flash(t(`chat.error.${code}`, t('chat.error.generic', 'Something went wrong.')), true);
    }

    function newClientId() {
        if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
        return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
    }

    function wsOpen() {
        return Boolean(RT && RT.mode === 'ws');
    }

    function urlFor(template, id) {
        return template.replace('/0/', `/${id}/`);
    }

    function latestId() {
        let max = 0;
        log.querySelectorAll('[data-msg-id]').forEach((li) => { max = Math.max(max, Number(li.dataset.msgId) || 0); });
        return max;
    }

    function nearBottom() {
        return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
    }

    function scrollDown() {
        scroller.scrollTop = scroller.scrollHeight;
    }

    /* ---------- rendering (mirrors chat.html) ---------- */
    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    }

    function keyed(tag, cls, key, fallback) {
        const node = el(tag, cls, t(key, fallback));
        node.dataset.i18n = key;
        return node;
    }

    function pad(n) { return String(n).padStart(2, '0'); }

    function timeText(iso) {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    function actionForm(label, key, hook, onSubmit) {
        const f = el('form');
        f.dataset[hook] = '';
        const b = keyed('button', 'chat-msg__action', key, label);
        b.type = 'submit';
        f.appendChild(b);
        f.addEventListener('submit', (e) => { e.preventDefault(); onSubmit(); });
        return f;
    }

    function photoLabels(scope) {
        (scope || root).querySelectorAll('[data-photo-link]').forEach((a) => {
            a.setAttribute('aria-label', t('chatpage.open_photo', 'Open the photo full size'));
            const img = a.querySelector('img');
            if (img) img.alt = t('chatpage.photo_alt', 'Photo from the chat');
        });
    }

    function render(m) {
        const li = el('li', 'chat-msg');
        li.dataset.msgId = m.id;
        if (m.kind === 'system') {
            li.classList.add('is-system');
            li.appendChild(keyed('p', 'chat-msg__system', m.body, m.body));
            return li;
        }
        li.classList.add(m.mine ? 'is-mine' : 'is-theirs');
        if (m.is_staff_post) li.classList.add('is-staff');
        if (m.hidden) li.classList.add('is-hidden');

        const bubble = el('div', 'chat-msg__bubble');
        if (!m.mine) {
            const who = el('p', 'chat-msg__who');
            if (m.label_key) {
                who.appendChild(keyed('span', '', m.label_key, m.sender_display));
                if (isStaff && m.is_staff_post && m.sender_display) who.appendChild(el('span', 'chat-msg__real', ` · ${m.sender_display}`));
            } else {
                who.textContent = m.sender_display || '';
            }
            bubble.appendChild(who);
        } else if (isStaff && m.is_staff_post) {
            const who = el('p', 'chat-msg__who');
            who.appendChild(keyed('span', '', 'chat.staff_label', 'Curator'));
            bubble.appendChild(who);
        }

        if (m.hidden && !isStaff) {
            bubble.appendChild(keyed('p', 'chat-msg__body chat-msg__stub', 'chat.hidden_stub', 'Message hidden by a moderator.'));
        } else {
            if (m.hidden) bubble.appendChild(keyed('span', 'tag muted', 'chat.hidden_tag', 'Hidden'));
            if (m.image_url) {
                const a = el('a', 'chat-msg__photo');
                a.href = m.image_url;
                a.target = '_blank';
                a.rel = 'noopener';
                a.dataset.photoLink = '';
                const img = el('img', 'chat-msg__img');
                img.src = m.thumb_url || m.image_url;
                img.width = 320;
                img.height = 240;
                img.loading = 'lazy';
                img.decoding = 'async';
                a.appendChild(img);
                bubble.appendChild(a);
                photoLabels(bubble);
            }
            if (m.body) bubble.appendChild(el('p', 'chat-msg__body', m.body));
        }

        const meta = el('p', 'chat-msg__meta');
        const time = el('time', '', timeText(m.created_at));
        time.dateTime = m.created_at || '';
        meta.appendChild(time);
        if (m.mine) {
            const seen = el('span', 'chat-msg__seen');
            seen.dataset.seen = '';
            meta.appendChild(seen);
        }
        bubble.appendChild(meta);
        li.appendChild(bubble);

        if (isStaff || (!m.mine && !m.hidden)) {
            const actions = el('div', 'chat-msg__actions');
            if (!m.mine && !isStaff && !m.hidden) actions.appendChild(actionForm('Report', 'chat.report', 'chatReport', () => report(m.id)));
            if (isStaff) {
                actions.appendChild(actionForm(
                    m.hidden ? 'Show' : 'Hide', m.hidden ? 'chat.unhide' : 'chat.hide', 'chatHide',
                    () => moderate(m.id, m.hidden ? 'unhide' : 'hide'),
                ));
            }
            li.appendChild(actions);
        }
        return li;
    }

    function upsert(m, opts) {
        if (!m || Number(m.task_id) !== taskId) return;
        if (updates.has(m.id)) { m = updates.get(m.id); updates.delete(m.id); }
        const empty = log.querySelector('[data-chat-empty]');
        if (empty) empty.remove();
        const node = render(m);
        const existing = log.querySelector(`[data-msg-id="${m.id}"]`);
        // An HTTP answer may be older than what the socket already delivered
        // (thumb ready, hidden): it only fills a gap, never replaces.
        if (existing && opts && opts.ifMissing) return;
        if (existing) {
            existing.replaceWith(node);
        } else if (opts && opts.prepend) {
            log.insertBefore(node, log.firstChild);
        } else {
            const stick = nearBottom() || m.mine;
            // keep id order (a late event may arrive after a newer one)
            const later = Array.from(log.querySelectorAll('[data-msg-id]')).find((li) => Number(li.dataset.msgId) > m.id);
            log.insertBefore(node, later || null);
            if (stick) scrollDown();
        }
        updateSeen();
    }

    /* Read receipts: ✓ sent, ✓✓ read; the newest read message of mine also
       says "Seen" in words (elderly-friendly, one label, not on every bubble). */
    function updateSeen() {
        const mine = Array.from(log.querySelectorAll('.is-mine'));
        let lastRead = null;
        mine.forEach((li) => { if (Number(li.dataset.msgId) <= peerRead) lastRead = li; });
        mine.forEach((li) => {
            const mark = li.querySelector('[data-seen]');
            if (!mark) return;
            const read = Number(li.dataset.msgId) <= peerRead;
            mark.classList.toggle('is-read', read);
            mark.textContent = '';
            mark.appendChild(el('span', 'chat-msg__tick', read ? '✓✓' : '✓')).setAttribute('aria-hidden', 'true');
            if (read) {
                const label = el('span', li === lastRead ? 'chat-msg__seen-text' : 'visually-hidden', t('chat.seen', 'Seen'));
                mark.appendChild(label);
            }
        });
    }

    /* ---------- sending ---------- */
    function settle(cmid) {
        const entry = pending.get(cmid);
        if (!entry) return null;
        clearTimeout(entry.timer);
        pending.delete(cmid);
        return entry;
    }

    function restoreText(text) {
        if (textarea && !textarea.value.trim()) { textarea.value = text; autosize(); }
    }

    function sendHttp(text, cmid) {
        return post(root.dataset.sendUrl, { text, client_msg_id: cmid }).then((data) => {
            settle(cmid);
            if (data.ok && data.message) upsert(data.message, { ifMissing: true });
            else if (!data.ok) { showError(data.code || 'generic'); restoreText(text); }
        }).catch(() => { settle(cmid); showError('generic'); restoreText(text); });
    }

    function sendText(text) {
        text = (text || '').trim();
        if (!text || !canWrite) return false;
        if (text.length > maxLength) { showError('too_long'); return false; }
        const cmid = newClientId();
        if (wsOpen() && RT.send('chat.send', { task_id: taskId, text, client_msg_id: cmid }, cmid)) {
            pending.set(cmid, { text, timer: setTimeout(() => { if (pending.has(cmid)) sendHttp(text, cmid); }, ACK_TIMEOUT_MS) });
        } else {
            pending.set(cmid, { text, timer: null });
            sendHttp(text, cmid);
        }
        return true;
    }

    function report(id) {
        if (!window.confirm(t('chat.report_confirm', 'Report this message to the curators?'))) return;
        post(urlFor(root.dataset.reportUrl, id), {}).then((data) => {
            if (data.ok) flash(t('chat.reported', 'Thank you — the curators will review it.'), false);
            else showError(data.code || 'generic');
        }).catch(() => showError('generic'));
    }

    function moderate(id, action) {
        post(urlFor(root.dataset.hideUrl, id), { action }).then((data) => {
            if (data.ok && data.message) upsert(data.message);
            else if (!data.ok) showError(data.code || 'generic');
        }).catch(() => showError('generic'));
    }

    function markRead() {
        if (document.visibilityState === 'hidden') return;
        const last = latestId();
        if (!last || last <= myLastRead) return;
        myLastRead = last;
        if (wsOpen() && RT.send('chat.read', { task_id: taskId, last_id: last })) return;
        post(root.dataset.readUrl, { last_id: last }).catch(() => {});
    }

    function scheduleRead() {
        clearTimeout(readTimer);
        readTimer = setTimeout(markRead, 800);
    }

    function loadOlder(button) {
        if (cursor == null) return;
        button.disabled = true;
        log.setAttribute('aria-busy', 'true');
        fetch(`${root.dataset.historyUrl}?before=${cursor}`, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then((r) => r.json())
            .then((data) => {
                const keep = scroller.scrollHeight - scroller.scrollTop;
                (data.messages || []).slice().reverse().forEach((m) => upsert(m, { prepend: true }));
                scroller.scrollTop = scroller.scrollHeight - keep;
                cursor = data.cursor;
                button.disabled = false;
                if (!data.has_more) button.remove();
            })
            .catch(() => { button.disabled = false; showError('generic'); })
            .finally(() => log.removeAttribute('aria-busy'));
    }

    function poll(force) {
        if ((wsOpen() && force !== true) || document.visibilityState === 'hidden') return;
        fetch(`${root.dataset.historyUrl}?after=${latestId()}`, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => {
                if (!data) return;
                (data.messages || []).forEach((m) => upsert(m));
                (data.receipts || []).forEach((r) => { peerRead = Math.max(peerRead, r.last_id || 0); });
                updateSeen();
                if (data.can_write === false && canWrite) closeComposer();
                scheduleRead();
            })
            .catch(() => {});
    }

    function setMode(mode) {
        if (!modeEl) return;
        modeEl.hidden = !(mode === 'poll' || mode === 'stopped' || !RT);
    }

    /* The request ended: the composer goes, a read-only notice (and, for the
       client, the "rate the help" button) appears. The server's system
       message arrives as a normal chat.message. */
    function closeComposer() {
        canWrite = false;
        root.dataset.canWrite = '0';
        const composer = root.querySelector('[data-chat-composer]');
        if (composer) composer.remove();
        if (root.querySelector('[data-chat-notice]')) return;
        const notice = el('p', 'chat__notice');
        notice.dataset.chatNotice = '';
        notice.appendChild(keyed('span', '', 'chat.read_only', 'The request is finished — the chat is read-only.'));
        if (root.dataset.reviewUrl) {
            const a = keyed('a', 'button primary chat__review', 'review.title_new', 'Rate the help');
            a.href = root.dataset.reviewUrl;
            notice.appendChild(a);
        }
        scroller.parentNode.insertBefore(notice, scroller);
        setTimeout(scrollDown, 50);   // the log just grew — keep the closing system message in view
    }

    /* ---------- full-height layout ----------
       Fit the page to the *visible* viewport (minus the sticky topbar), so the
       composer stays on screen when the on-screen keyboard opens. */
    const page = root.closest('.chat-page');
    function fit() {
        if (!page) return;
        const vv = window.visualViewport;
        const topbar = document.querySelector('.topbar');
        page.style.setProperty('--chat-vh', `${Math.round(vv ? vv.height : window.innerHeight)}px`);
        if (topbar) page.style.setProperty('--chat-top', `${topbar.offsetHeight}px`);
        if (nearBottom()) scrollDown();
    }
    fit();
    window.addEventListener('resize', fit);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', fit);

    /* ---------- wiring ---------- */
    function autosize() {
        if (!textarea) return;
        textarea.style.height = 'auto';
        textarea.style.height = `${Math.min(textarea.scrollHeight + 2, 200)}px`;
    }

    if (form && textarea) {
        const touch = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            if (sendText(textarea.value)) { textarea.value = ''; autosize(); }
            textarea.focus();
        });
        textarea.addEventListener('keydown', (e) => {
            // Desktop: Enter sends, Shift+Enter = new line. Touch keyboards: Enter = new line.
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !touch) {
                e.preventDefault();
                if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event('submit', { cancelable: true }));
            }
        });
        textarea.addEventListener('input', () => {
            autosize();
            const now = Date.now();
            if (wsOpen() && textarea.value.trim() && now - lastTypingSent > 2500) {
                lastTypingSent = now;
                RT.send('chat.typing', { task_id: taskId });
            }
        });
    }

    root.querySelectorAll('[data-quick]').forEach((button) => {
        button.addEventListener('click', (e) => {
            e.preventDefault();
            sendText(t(button.dataset.quick, button.value));
        });
    });

    if (uploadForm && fileInput) {
        const chosen = uploadForm.querySelector('[data-chat-chosen]');
        const nameEl = uploadForm.querySelector('[data-chat-filename]');
        const syncChosen = () => {
            const file = fileInput.files && fileInput.files[0];
            uploadForm.classList.toggle('has-file', Boolean(file));
            if (chosen) chosen.hidden = !file;
            if (nameEl) nameEl.textContent = file ? file.name : '';
        };
        fileInput.addEventListener('change', () => {
            const file = fileInput.files && fileInput.files[0];
            if (file && file.size > MAX_UPLOAD) {
                showError('too_large');
                uploadForm.reset();
            }
            syncChosen();
        });
        // The camera button (a <label for>) is the keyboard target; the real
        // file input is visually hidden and skipped in the tab order.
        const attach = root.querySelector('[data-chat-attach]');
        if (attach) {
            attach.tabIndex = 0;
            attach.setAttribute('role', 'button');
            fileInput.tabIndex = -1;
            attach.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
            });
        }
        uploadForm.addEventListener('submit', (e) => {
            e.preventDefault();
            if (!fileInput.files || !fileInput.files[0]) { showError('empty'); return; }
            const data = new FormData(uploadForm);
            data.append('client_msg_id', newClientId());
            const button = uploadForm.querySelector('button[type=submit]');
            if (button) { button.disabled = true; button.textContent = t('chatpage.sending', 'Sending…'); }
            post(root.dataset.uploadUrl, data).then((res) => {
                if (res.ok && res.message) { upsert(res.message, { ifMissing: true }); uploadForm.reset(); syncChosen(); }
                else showError(res.code || 'generic');
            }).catch(() => showError('generic')).finally(() => {
                if (button) { button.disabled = false; button.textContent = t('chat.upload', 'Send photo'); }
            });
        });
        syncChosen();
    }

    // Server-rendered report / hide forms → JSON (keep working without JS).
    root.querySelectorAll('[data-chat-report]').forEach((f) => {
        f.addEventListener('submit', (e) => {
            e.preventDefault();
            report(Number(f.closest('[data-msg-id]').dataset.msgId));
        });
    });
    root.querySelectorAll('[data-chat-hide]').forEach((f) => {
        f.addEventListener('submit', (e) => {
            e.preventDefault();
            moderate(Number(f.closest('[data-msg-id]').dataset.msgId), f.querySelector('[name=action]').value);
        });
    });

    const older = root.querySelector('[data-chat-older]');
    if (older) older.addEventListener('click', () => loadOlder(older));

    const large = root.querySelector('[data-chat-large]');
    if (large) {
        let on = false;
        try { on = localStorage.getItem('gc.chat.large') === '1'; } catch (e) { /* storage blocked */ }
        const apply = () => {
            root.classList.toggle('chat--large', on);
            large.setAttribute('aria-pressed', on ? 'true' : 'false');
            autosize();
        };
        apply();
        large.addEventListener('click', () => {
            const stick = nearBottom();
            on = !on;
            try { localStorage.setItem('gc.chat.large', on ? '1' : '0'); } catch (e) { /* ignore */ }
            apply();
            if (stick) scrollDown();
        });
    }

    if (RT) {
        RT.subscribeTask(taskId);
        RT.on('chat.message', (p) => {
            upsert(p);
            if (Number(p.task_id) === taskId && !p.mine) {
                scheduleRead();
                if (typingEl) typingEl.textContent = '';
            }
        });
        RT.on('chat.message_updated', (p) => {
            if (Number(p.task_id) !== taskId) return;
            if (log.querySelector(`[data-msg-id="${p.id}"]`)) upsert(p);
            else updates.set(p.id, p);
        });
        RT.on('chat.typing', (p) => {
            if (Number(p.task_id) !== taskId || !typingEl) return;
            const name = p.sender_display === 'Куратор' ? t('chat.staff_label', 'Curator') : p.sender_display;
            typingEl.textContent = t('chat.typing', '{name} is typing…', { name });
            clearTimeout(typingTimer);
            typingTimer = setTimeout(() => { typingEl.textContent = ''; }, 4000);
        });
        RT.on('chat.read', (p) => {
            if (Number(p.task_id) !== taskId || p.mine) return;
            peerRead = Math.max(peerRead, p.last_id || 0);
            updateSeen();
        });
        RT.on('ack', (p) => {
            if (p.for === 'chat.send' && p.client_msg_id) settle(p.client_msg_id);
        });
        RT.on('error', (p) => {
            // Only errors of our own chat.send frames carry a ref we are waiting for.
            const entry = p && typeof p.ref === 'string' ? settle(p.ref) : null;
            if (!entry) return;
            showError(p.code || 'generic');
            restoreText(entry.text);
        });
        RT.on('task.updated', (p) => {
            if (Number(p.task_id) === taskId && (p.status === 'completed' || p.status === 'cancelled')) closeComposer();
        });
        RT.on('realtime.mode', (p) => {
            setMode(p.mode);
            if (p.mode === 'ws') poll(true);   // catch up on what arrived while the socket was down
        });
        setTimeout(() => setMode(RT.mode), 6000);
    } else {
        setMode('poll');
    }

    document.addEventListener('gc:language', () => { photoLabels(); updateSeen(); });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { poll(); scheduleRead(); } });
    scroller.addEventListener('scroll', () => { if (nearBottom()) scheduleRead(); }, { passive: true });

    setInterval(poll, POLL_MS);
    photoLabels();
    scrollDown();
    updateSeen();
    scheduleRead();
}());
