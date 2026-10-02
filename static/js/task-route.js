/* Task detail — route, turn-by-turn navigation and live tracking UI
   (Feature Pack v2, features 2 + 7). Loaded only when the server rendered
   #routeCard (myapp/task_detail.html, show_route = access.can_view_route on an
   ACTIVE task: staff, the task's client, the assigned volunteer).

   Behaviour lives in static/js/map.js (GCMap.createRouteNav / createTracker /
   setTrackingConsent); this file only wires it to the page:

   * everyone: driving/walking toggle, distance + ETA (≈ when estimated),
     localized empty/error states;
   * assigned volunteer: first-time consent <dialog>, the "your location is
     visible" banner with Stop / Share again, step list + "next maneuver" box,
     "Open in" navigator links;
   * client / staff: the volunteer's live marker, "updated N min ago", and a
     clear "not sharing" state.

   Every visible string is an i18n.js key (data-i18n + data-i18n-args), so the
   EN/RU/TJ switcher re-localizes dynamic text too. */
(function () {
    'use strict';

    const card = document.getElementById('routeCard');
    if (!card || typeof GCMap === 'undefined') return;

    const isVolunteer = card.dataset.isVolunteer === '1';
    const taskId = Number(card.dataset.taskId);
    const csrfInput = card.querySelector('[name=csrfmiddlewaretoken]');
    const csrf = csrfInput ? csrfInput.value : '';
    const $ = (id) => document.getElementById(id);

    const statusEl = $('routeStatus');
    const metricsEl = $('routeMetrics');
    const mapEl = $('routeMap');
    const emptyEl = $('routeEmpty');
    const emptyTextEl = $('routeEmptyText');
    const distanceEl = $('routeDistance');
    const etaEl = $('routeEta');
    const etaApproxEl = $('routeEtaApprox');
    const sourceEl = $('routeSource');
    const modeButtons = Array.from(card.querySelectorAll('.seg-toggle__btn'));

    /* ---------- small i18n helpers ---------- */

    // Put a key (+ args) on an element and render it in the current language.
    function setText(el, key, args) {
        if (!el) return;
        el.setAttribute('data-i18n', key);
        if (args) el.setAttribute('data-i18n-args', JSON.stringify(args));
        else el.removeAttribute('data-i18n-args');
        el.textContent = GCMap.t(key, args);
    }

    function distanceArgs(m) {
        if (m == null || !isFinite(m)) return null;
        if (m < 1000) return ['nav.dist_m', { n: Math.max(0, Math.round(m / 10) * 10) }];
        return ['nav.dist_km', { n: (m / 1000).toFixed(1) }];
    }

    function etaArgs(min) {
        if (min == null || !isFinite(min)) return null;
        const total = Math.max(1, Math.round(min));
        if (total < 60) return ['nav.min', { n: total }];
        return ['nav.hours_min', { h: Math.floor(total / 60), m: total % 60 }];
    }

    function setMetric(el, pair) {
        if (pair) { setText(el, pair[0], pair[1]); return; }
        el.removeAttribute('data-i18n');
        el.removeAttribute('data-i18n-args');
        el.textContent = '—';
    }

    function storage(kind) {
        try { return window[kind]; } catch (e) { return null; }
    }

    function stored(kind, key) {
        const s = storage(kind);
        try { return s ? s.getItem(key) : null; } catch (e) { return null; }
    }

    function store(kind, key, value) {
        const s = storage(kind);
        try { if (s) s.setItem(key, value); } catch (e) { /* storage blocked */ }
    }

    /* ---------- route panel ---------- */

    const REASON_KEYS = {
        no_task_location: 'route.reason_no_task_location',
        no_volunteer_assigned: 'route.reason_no_volunteer_assigned',
        no_volunteer_location: 'route.reason_no_volunteer_location',
        forbidden: 'route.reason_forbidden',
        task_not_active: 'route.task_not_active',
        invalid_coordinates: 'route.reason_error',
    };
    const SOURCE_KEYS = {
        live: 'route.source_live',
        tracking: 'route.source_tracking',
        saved: 'route.source_saved',
    };

    let profile = stored('localStorage', 'gc-route-profile') === 'walking' ? 'walking' : 'driving';
    let nav = null;

    /* Moving along the route never triggers a re-route, so distance + ETA are
       refreshed quietly from the latest position at most every REFRESH_MS. */
    const REFRESH_MS = 30000;
    let lastRouteAt = 0;
    let silent = false;
    let latestFix = null;   // volunteer's own last GPS fix (their live origin)

    function refreshRoute() {
        if (!nav || !nav.data || Date.now() - lastRouteAt < REFRESH_MS) return;
        lastRouteAt = Date.now();
        silent = true;
        nav.load(isVolunteer && latestFix ? latestFix : null);
    }

    function showStatus(key) {
        statusEl.hidden = false;
        setText(statusEl, key);
    }

    function showEmpty(reason) {
        statusEl.hidden = true;
        metricsEl.hidden = true;
        mapEl.hidden = true;
        emptyEl.hidden = false;
        setText(emptyTextEl, REASON_KEYS[reason] || (reason === 'error' ? 'route.reason_error' : 'route.unavailable_desc'));
        const next = $('navNext');
        if (next) next.hidden = true;
        const steps = $('routeStepsWrap');
        if (steps) steps.hidden = true;
    }

    function renderMetrics(data) {
        statusEl.hidden = true;
        emptyEl.hidden = true;
        metricsEl.hidden = false;
        setMetric(distanceEl, distanceArgs(data.distance_km != null ? data.distance_km * 1000 : null));
        setMetric(etaEl, etaArgs(data.eta_min != null ? data.eta_min : data.duration_min));
        etaApproxEl.hidden = !(data.eta_estimated && data.eta_min != null);
        const source = data.origin && data.origin.source;
        setText(sourceEl, SOURCE_KEYS[source] || 'route.source_saved');
    }

    function renderDeepLinks(links) {
        const box = $('routeDeepLinks');
        if (!box || !links) return;
        box.querySelectorAll('a[data-app]').forEach((a) => {
            const href = links[a.dataset.app];
            if (typeof href === 'string' && /^(https:\/\/|yandexnavi:\/\/)/.test(href)) a.href = href;
        });
    }

    function renderStepsBox(data) {
        const wrap = $('routeStepsWrap');
        const list = $('routeSteps');
        const has = !!(data.steps && data.steps.length);
        if (wrap) wrap.hidden = !has;
        if (list) list.hidden = !has;
        const next = $('navNext');
        if (!next) return;
        if (!has) { next.hidden = true; return; }
        // Until a GPS fix says where along the route the volunteer is, the
        // first maneuver ("Head out onto …") is the next one.
        if (nav && nav.currentStep < 0) showNextStep(data.steps[0], data.steps[0].distance_m);
    }

    function showNextStep(step, distanceM) {
        const next = $('navNext');
        const textEl = $('navNextText');
        if (!next || !textEl || !step) return;
        textEl.textContent = '';
        textEl.appendChild(GCMap._i18nEl('span', '', GCMap.stepKey(step), { exit: step.exit || '' }));
        if (step.name && step.maneuver !== 'arrive') {
            textEl.appendChild(document.createTextNode(' '));
            textEl.appendChild(GCMap._i18nEl('span', '', 'nav.onto', { name: step.name }));
        }
        const dist = step.maneuver === 'arrive' && distanceM != null && distanceM <= GCMap.NAV_ARRIVE_RADIUS_M
            ? null : distanceArgs(distanceM);
        setMetric($('navNextDist'), dist);
        if (!dist) $('navNextDist').textContent = '';
        next.hidden = false;
    }

    function setMode(p) {
        profile = p === 'walking' ? 'walking' : 'driving';
        modeButtons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.profile === profile)));
    }

    setMode(profile);

    /* ---------- client / staff: watching the volunteer ---------- */

    const watch = isVolunteer ? null : (function () {
        const box = $('trackWatch');
        const title = $('trackWatchTitle');
        const hint = $('trackWatchHint');
        let lastTs = null;
        let state = 'loading';
        const STALE_MIN = 10;

        function agoText() {
            if (!lastTs) { hint.hidden = true; return; }
            const min = Math.floor((Date.now() - lastTs) / 60000);
            if (min < 1) setText(hint, 'track.watch_updated_now');
            else setText(hint, 'track.watch_updated', { n: min });
            hint.hidden = false;
            box.classList.toggle('is-stale', min >= STALE_MIN);
        }

        function set(next, detail) {
            state = next;
            box.dataset.state = next;
            box.classList.remove('is-stale');
            if (next === 'live') {
                if (detail && detail.ts) {
                    const t = Date.parse(detail.ts);
                    lastTs = isNaN(t) ? Date.now() : Math.min(t, Date.now());
                } else {
                    lastTs = Date.now();
                }
                setText(title, 'track.watch_live');
                agoText();
            } else if (next === 'off') {
                lastTs = null;
                setText(title, 'track.watch_off');
                setText(hint, 'track.watch_off_hint');
                hint.hidden = false;
            } else if (next === 'ended') {
                lastTs = null;
                setText(title, 'track.watch_ended');
                hint.hidden = true;
            }
        }

        setInterval(() => { if (state === 'live') agoText(); }, 30000);
        return { set, get state() { return state; } };
    })();

    /* ---------- route + navigation controller ---------- */

    nav = GCMap.createRouteNav({
        mapEl,
        routeUrl: card.dataset.routeUrl,
        taskId,
        isVolunteer,
        ownPositions: isVolunteer,
        live: true,
        profile,
        stepsEl: isVolunteer ? $('routeSteps') : null,
        onRoute(data) {
            lastRouteAt = Date.now();
            renderMetrics(data);
            renderDeepLinks(data.deep_links);
            if (isVolunteer) renderStepsBox(data);
        },
        onState(state, detail) {
            if (state !== 'live' && state !== 'live_stopped' && state !== 'rerouting' && state !== 'loading') silent = false;
            if (state === 'loading') showStatus('route.loading');
            else if (state === 'rerouting') { if (!silent) showStatus('route.rerouting'); }
            else if (state === 'ready') statusEl.hidden = true;
            else if (state === 'empty') showEmpty(detail.reason);
            else if (state === 'error') {
                if (nav && nav.data) statusEl.hidden = true;
                else showEmpty('error');
            } else if (state === 'live' && watch) {
                // A position before the first route (no map view yet): centre on it.
                if (nav && nav.map && !nav.data) nav.map.setView([detail.lat, detail.lng], 14);
                watch.set('live', detail);
                refreshRoute();
            } else if (state === 'live_stopped' && watch) {
                watch.set(detail.reason === 'task_ended' ? 'ended' : 'off');
            }
        },
        onStep(index, step, distanceM) {
            if (isVolunteer) showNextStep(step, distanceM);
        },
    });
    if (!nav) return;

    modeButtons.forEach((btn) => {
        btn.addEventListener('click', () => {
            if (btn.dataset.profile === profile) return;
            setMode(btn.dataset.profile);
            store('localStorage', 'gc-route-profile', profile);
            nav.setProfile(profile);
        });
    });

    const firstLoad = nav.load();

    if (watch) {
        // Latest shared position (if any) once the route has given the map a view.
        Promise.resolve(firstLoad).then(() => fetch(card.dataset.locationUrl, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        }))
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => {
                if (watch.state !== 'loading') return;   // a live event got here first
                if (d && d.latest && isFinite(d.latest.lat) && isFinite(d.latest.lng)) {
                    nav.updatePosition(d.latest.lat, d.latest.lng, { ts: d.latest.ts });
                } else {
                    watch.set('off');
                }
            })
            .catch(() => { if (watch.state === 'loading') watch.set('off'); });
    }

    /* ---------- assigned volunteer: tracking + consent ---------- */

    if (!isVolunteer) return;

    const banner = $('trackBanner');
    const bannerTitle = $('trackBannerTitle');
    const bannerHint = $('trackBannerHint');
    const stopBtn = $('trackStopBtn');
    const shareBtn = $('trackShareBtn');
    const locateBtn = $('routeLocateBtn');
    const dialog = $('trackConsentDialog');
    const DECLINE_KEY = `gc-track-declined-${taskId}`;
    let consent = card.dataset.consent === '1';
    let firstFix = true;

    function setBanner(state, hintKey) {
        banner.dataset.state = state;
        const titles = {
            on: 'track.visible', off: 'track.off_title', stopped: 'track.stopped_title',
            denied: 'track.denied_title', forbidden: 'track.off_title', ended: 'track.ended',
        };
        setText(bannerTitle, titles[state] || 'track.off_title');
        const hints = {
            off: 'track.off_hint', stopped: 'track.stopped_hint', denied: 'track.state_denied',
            forbidden: 'track.state_forbidden',
        };
        const hint = hintKey || hints[state];
        if (hint) { setText(bannerHint, hint); bannerHint.hidden = false; } else { bannerHint.hidden = true; }
        stopBtn.hidden = state !== 'on';
        shareBtn.hidden = !(state === 'off' || state === 'stopped' || state === 'denied');
        setText(shareBtn, state === 'off' ? 'track.share' : 'track.resume');
    }

    const tracker = GCMap.createTracker({
        taskId,
        locationUrl: card.dataset.locationUrl,
        stopUrl: card.dataset.stopUrl,
        csrfToken: csrf,
        nav,
        onPosition(fix) {
            latestFix = { lat: fix.lat, lng: fix.lng };
            // No route yet (e.g. no saved location): the first GPS fix is the origin.
            if (firstFix && !nav.data) nav.load(latestFix);
            else refreshRoute();
            firstFix = false;
        },
        onState(state, detail) {
            if (state === 'starting') setBanner('on', 'track.state_starting');
            else if (state === 'active' || state === 'sent') {
                if (banner.dataset.hint !== 'active') setBanner('on', 'track.state_active');
                banner.dataset.hint = 'active';
                return;
            } else if (state === 'dropped') {
                if (detail.code === 'inaccurate') setBanner('on', 'track.state_weak');
            } else if (state === 'unavailable') setBanner('on', 'track.state_unavailable');
            else if (state === 'denied') setBanner('denied');
            else if (state === 'forbidden') setBanner('forbidden');
            else if (state === 'stopped') {
                if (detail.reason === 'task_ended') {
                    setBanner('ended');
                    nav.clearLive('task_ended');
                } else if (detail.reason === 'consent_revoked') {
                    // Consent withdrawn (another tab / the bot): ask again before sharing.
                    consent = false;
                    card.dataset.consent = '0';
                    setBanner('off');
                    nav.clearLive('consent_revoked');
                } else {
                    setBanner('stopped');
                    nav.clearLive('volunteer');
                }
            }
            banner.dataset.hint = state;
        },
    });

    function startSharing() {
        firstFix = !nav.data;
        if (!tracker.start()) setBanner('denied', 'track.state_unavailable');
    }

    /* Consent dialog: Esc and "Not now" decline (nothing breaks); focus goes
       back to whatever opened it, or to the "Share my location" button. */
    let opener = null;

    function openConsent(fromUser) {
        if (!dialog) { startSharing(); return; }
        opener = fromUser ? document.activeElement : null;
        $('trackConsentError').hidden = true;
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');
    }

    function closeConsent() {
        if (typeof dialog.close === 'function' && dialog.open) dialog.close();
        else { dialog.removeAttribute('open'); onConsentClosed(); }
    }

    function onConsentClosed() {
        const target = opener && document.contains(opener) && !opener.hidden ? opener : shareBtn;
        opener = null;
        if (target && !target.hidden) target.focus();
        else if (stopBtn && !stopBtn.hidden) stopBtn.focus();
    }

    if (dialog) {
        dialog.addEventListener('close', onConsentClosed);
        dialog.addEventListener('cancel', () => store('sessionStorage', DECLINE_KEY, '1'));
        $('trackConsentDecline').addEventListener('click', () => {
            store('sessionStorage', DECLINE_KEY, '1');
            closeConsent();
        });
        const agree = $('trackConsentAgree');
        agree.addEventListener('click', () => {
            agree.disabled = true;
            GCMap.setTrackingConsent(card.dataset.consentUrl, true, csrf).then((ok) => {
                agree.disabled = false;
                if (!ok) { $('trackConsentError').hidden = false; return; }
                consent = true;
                card.dataset.consent = '1';
                closeConsent();
                startSharing();
                stopBtn.focus();
            });
        });
        // A click on the dimmed backdrop (outside the panel) counts as "Not now".
        dialog.addEventListener('click', (e) => {
            if (e.target === dialog) {
                store('sessionStorage', DECLINE_KEY, '1');
                closeConsent();
            }
        });
    }

    shareBtn.addEventListener('click', () => {
        if (consent) startSharing();
        else openConsent(true);
    });
    stopBtn.addEventListener('click', () => {
        tracker.stop();
        shareBtn.focus();
    });

    if (consent && card.dataset.stopped !== '1') {
        startSharing();
    } else if (consent) {
        setBanner('stopped');
    } else {
        setBanner('off');
        if (stored('sessionStorage', DECLINE_KEY) !== '1') openConsent(false);
    }

    /* "Use my location": a one-off fix for the route origin (also saved as the
       profile location, as before). Independent of live sharing. */
    if (locateBtn) {
        if (!navigator.geolocation) {
            locateBtn.disabled = true;
        } else {
            locateBtn.addEventListener('click', () => {
                locateBtn.disabled = true;
                navigator.geolocation.getCurrentPosition(
                    (pos) => {
                        locateBtn.disabled = false;
                        nav.load({ lat: pos.coords.latitude, lng: pos.coords.longitude });
                        fetch(card.dataset.updateLocationUrl, {
                            method: 'POST',
                            credentials: 'same-origin',
                            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
                            body: JSON.stringify({ latitude: pos.coords.latitude, longitude: pos.coords.longitude }),
                        }).catch(() => {});
                    },
                    (err) => {
                        locateBtn.disabled = false;
                        showStatus(err && err.code === 1 ? 'map.locate_denied' : 'map.locate_unavailable');
                    },
                    { enableHighAccuracy: true, timeout: 10000 },
                );
            });
        }
    }
})();
