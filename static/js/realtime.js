/* KhayrKhoh realtime client (Feature Pack v2, feature 12) — protocol in docs/WS_PROTOCOL.md.

   One shared WebSocket per tab to /ws/, loaded (deferred) only for signed-in
   users. Exposes a tiny event bus:

     GCRealtime.on(type, fn)          → returns an "off" function; type "*" = every event
     GCRealtime.off(type, fn)
     GCRealtime.subscribeTask(id)     → joins task_<id> (re-sent after every reconnect)
     GCRealtime.unsubscribeTask(id)
     GCRealtime.send(type, payload, id?) → false when the socket isn't open
                                        (id ≤ 64 chars, echoed as `ref` in error/ack)
     GCRealtime.mode                  → "connecting" | "ws" | "poll" | "stopped"

   fn(payload, envelope) is called for every server frame of that type.
   Reconnects with exponential backoff (1 s → 30 s, ±20 % jitter); never on
   close code 4401 (not signed in); ≥ 30 s after 4408 (flooding / oversize).
   Sends presence.heartbeat every 25 s while the tab is visible.

   Polling fallback: after 3 failed connection attempts in a row (or when the
   browser has no WebSocket) it polls /myapp/realtime/poll/?since=<cursor>
   every ~30 s while the tab is visible, re-emitting the same
   `notification.new` envelopes plus a synthetic `notification.count
   {unread}`. The socket keeps retrying in the background; once it opens,
   polling stops. Silent by design: no console output.

   Test hook (QA only, never set by the app): window.__GC_RT_WS_URL__
   overrides the socket URL — e.g. point it at a dead port to exercise the
   polling fallback. */
(function () {
    'use strict';
    if (window.GCRealtime) return;

    const script = document.currentScript;
    const POLL_URL = (script && script.dataset.pollUrl) || '/myapp/realtime/poll/';
    const WS_PATH = (script && script.dataset.wsPath) || '/ws/';
    const HEARTBEAT_MS = 25000;
    const POLL_MS = 30000;
    const BACKOFF_MIN = 1000;
    const BACKOFF_MAX = 30000;
    const FAILS_BEFORE_POLL = 3;

    const handlers = new Map();
    const tasks = new Set();
    let socket = null;
    let failures = 0;
    let reconnectTimer = null;
    let heartbeatTimer = null;
    let pollTimer = null;
    let polling = false;
    let pollCursor = null;   // null = not primed yet
    let stopped = false;
    let unloading = false;
    let mode = 'connecting';

    function emit(type, payload, env) {
        [type, '*'].forEach((key) => {
            const set = handlers.get(key);
            if (!set) return;
            set.forEach((fn) => {
                try { fn(payload || {}, env || { type, payload }); } catch (e) { /* a broken listener must not break the bus */ }
            });
        });
    }

    function on(type, fn) {
        if (!handlers.has(type)) handlers.set(type, new Set());
        handlers.get(type).add(fn);
        return () => off(type, fn);
    }

    function off(type, fn) {
        const set = handlers.get(type);
        if (set) set.delete(fn);
    }

    function isOpen() {
        return socket && socket.readyState === 1;
    }

    function send(type, payload, id) {
        if (!isOpen()) return false;
        try {
            const frame = { type, payload: payload || {} };
            // Optional client id: echoed back as `ref` in the matching error/ack.
            if (id != null) frame.id = id;
            socket.send(JSON.stringify(frame));
            return true;
        } catch (e) {
            return false;
        }
    }

    function visible() {
        return document.visibilityState !== 'hidden';
    }

    /* ---------- heartbeat ---------- */
    function startHeartbeat() {
        stopHeartbeat();
        heartbeatTimer = setInterval(() => {
            if (visible()) send('presence.heartbeat', { visible: true });
        }, HEARTBEAT_MS);
    }

    function stopHeartbeat() {
        if (heartbeatTimer) clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }

    /* ---------- polling fallback ---------- */
    function pollOnce() {
        if (!polling || !visible()) return;
        const since = pollCursor === null ? 0 : pollCursor;
        fetch(`${POLL_URL}?since=${encodeURIComponent(since)}`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then((r) => {
                if (r.status === 401 || r.status === 403 || r.redirected) {
                    // Session gone — nothing to poll for.
                    stopPolling();
                    stopped = true;
                    mode = 'stopped';
                    return null;
                }
                return r.ok ? r.json() : null;
            })
            .then((data) => {
                if (!data) return;
                const primed = pollCursor !== null;
                pollCursor = typeof data.cursor === 'number' ? data.cursor : since;
                // The first answer is only the starting point: the unread backlog
                // was already rendered by the page, so no toast storm on load.
                if (primed) {
                    (data.events || []).forEach((env) => emit(env.type, env.payload, env));
                }
                if (typeof data.unread_count === 'number') {
                    emit('notification.count', { unread: data.unread_count });
                }
            })
            .catch(() => { /* offline — the next tick retries */ });
    }

    function startPolling() {
        if (polling) return;
        polling = true;
        mode = 'poll';
        emit('realtime.mode', { mode });
        pollOnce();
        pollTimer = setInterval(pollOnce, POLL_MS);
    }

    function stopPolling() {
        polling = false;
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
    }

    /* ---------- websocket ---------- */
    function socketUrl() {
        if (window.__GC_RT_WS_URL__) return window.__GC_RT_WS_URL__;
        const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${scheme}//${window.location.host}${WS_PATH}`;
    }

    function scheduleReconnect(minDelay) {
        if (stopped || unloading || reconnectTimer) return;
        const base = Math.min(BACKOFF_MAX, BACKOFF_MIN * Math.pow(2, Math.max(0, failures - 1)));
        const jittered = base * (0.8 + Math.random() * 0.4);
        const delay = Math.max(minDelay || 0, Math.min(BACKOFF_MAX, jittered));
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connect();
        }, delay);
    }

    function connect() {
        if (stopped || unloading) return;
        if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
        if (!('WebSocket' in window)) {
            startPolling();
            return;
        }
        let ws;
        try {
            ws = new WebSocket(socketUrl());
        } catch (e) {
            failures += 1;
            if (failures >= FAILS_BEFORE_POLL) startPolling();
            scheduleReconnect();
            return;
        }
        socket = ws;
        let opened = false;

        ws.onopen = () => {
            opened = true;
            failures = 0;
            mode = 'ws';
            if (polling) {
                stopPolling();
                pollCursor = null;
            }
            emit('realtime.mode', { mode });
            tasks.forEach((id) => send('subscribe', { topic: 'task', id }));
            if (visible()) send('presence.heartbeat', { visible: true });
            startHeartbeat();
        };

        ws.onmessage = (event) => {
            let env;
            try { env = JSON.parse(event.data); } catch (e) { return; }
            if (!env || typeof env.type !== 'string') return;
            emit(env.type, env.payload, env);
        };

        ws.onclose = (event) => {
            if (socket === ws) socket = null;
            stopHeartbeat();
            if (unloading) return;
            if (event.code === 4401) {
                // Not signed in (or the session expired): stop until a new page load.
                stopped = true;
                stopPolling();
                mode = 'stopped';
                emit('realtime.mode', { mode });
                return;
            }
            if (!opened) failures += 1;
            else failures = 1;   // an established socket dropped: retry quickly
            if (failures >= FAILS_BEFORE_POLL) startPolling();
            else if (!polling) mode = 'connecting';
            scheduleReconnect(event.code === 4408 ? BACKOFF_MAX : 0);
        };

        ws.onerror = () => { /* onclose follows and handles the retry */ };
    }

    /* ---------- tasks ---------- */
    function subscribeTask(id) {
        const taskId = parseInt(id, 10);
        if (!taskId) return;
        tasks.add(taskId);
        send('subscribe', { topic: 'task', id: taskId });
    }

    function unsubscribeTask(id) {
        const taskId = parseInt(id, 10);
        if (!tasks.delete(taskId)) return;
        send('unsubscribe', { topic: 'task', id: taskId });
    }

    /* ---------- lifecycle ---------- */
    document.addEventListener('visibilitychange', () => {
        if (!visible()) return;
        if (isOpen()) send('presence.heartbeat', { visible: true });
        else if (polling) pollOnce();
        if (!isOpen() && !stopped && !reconnectTimer) connect();
    });
    window.addEventListener('pagehide', () => {
        unloading = true;
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = null;
    });
    window.addEventListener('pageshow', (event) => {
        if (!event.persisted) return;   // restored from the back/forward cache
        unloading = false;
        connect();
    });
    window.addEventListener('online', () => {
        if (!isOpen() && !stopped) {
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = null;
            connect();
        }
    });

    window.GCRealtime = {
        on,
        off,
        send,
        subscribeTask,
        unsubscribeTask,
        get mode() { return mode; },
    };

    connect();
})();
