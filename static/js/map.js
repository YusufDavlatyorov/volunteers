/* Shared Leaflet helpers for KhayrKhoh map pages.
   Loaded on demand (not from base.html) by any page that embeds a map, right
   after the Leaflet CDN script. Requires the global `L` from Leaflet. */
const GCMap = {
    DEFAULT_CENTER: [38.5598, 68.7870], // Dushanbe
    DEFAULT_ZOOM: 12,

    /* Tile config comes from the maps service layer: base.html renders it as
       <script id="gc-maps-tile" type="application/json"> via json_script
       (myapp.context_processors.maps_config). The OSM object below is the
       fallback if that element is absent or the provider was misconfigured. */
    tileConfig() {
        const OSM = {
            url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            max_zoom: 19,
            subdomains: '',
            fallback: {
                url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
                max_zoom: 20,
                subdomains: 'abcd',
            },
        };
        try {
            const el = document.getElementById('gc-maps-tile');
            const cfg = el ? JSON.parse(el.textContent) : null;
            return (cfg && cfg.url) ? cfg : OSM;
        } catch (e) {
            return OSM;
        }
    },

    /* Adds the provider's tile layer. If its tiles keep failing before any
       load (provider blocking this visitor — e.g. OSM's 403 "Access blocked" —
       or unreachable from their network), it switches once to cfg.fallback
       (CARTO); if that fails too, a localized message replaces the blank area. */
    TILE_FAILURES_BEFORE_SWITCH: 4,

    tileLayer(map) {
        const cfg = this.tileConfig();
        const make = (c) => L.tileLayer(c.url, {
            maxZoom: c.max_zoom || 19,
            attribution: c.attribution || '',
            subdomains: c.subdomains || 'abc',
        });
        let notice = null;
        const watch = (layer, canFallBack) => {
            let loaded = 0;
            let failed = 0;
            layer.on('tileload', () => {
                loaded += 1;
                if (notice) { notice.remove(); notice = null; }
            });
            layer.on('tileerror', () => {
                failed += 1;
                if (failed < this.TILE_FAILURES_BEFORE_SWITCH || loaded > 1) return;
                if (canFallBack && cfg.fallback) {
                    canFallBack = false;
                    map.removeLayer(layer);
                    watch(make(cfg.fallback).addTo(map), false);
                } else if (!notice) {
                    notice = this.showError(map.getContainer(), 'map.error_load');
                }
            });
        };
        watch(make(cfg).addTo(map), true);
        return map;
    },

    /* A localized, non-blank failure state for a map container. Text comes from
       i18n.js (window.T) in the visitor's current language. */
    showError(container, key) {
        if (!container) return null;
        let lang = 'ru';
        try { lang = localStorage.getItem('gc-lang') || 'ru'; } catch (e) { /* storage blocked */ }
        const table = (window.T && (window.T[lang] || window.T.ru)) || {};
        if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
        const el = document.createElement('div');
        el.className = 'map-error';
        el.setAttribute('role', 'status');
        el.setAttribute('data-i18n', key);
        el.textContent = table[key] || 'The map could not be loaded.';
        container.appendChild(el);
        return el;
    },

    /* Resolves a --css-variable to its current computed color so it can be
       handed to Leaflet's SVG renderer (which sets plain attributes, not
       style properties, so `var(...)` strings don't resolve there reliably). */
    resolveColor(colorVar, fallback) {
        const value = getComputedStyle(document.documentElement).getPropertyValue(colorVar).trim();
        return value || fallback || '#1b3a5c';
    },

    /* Draws an OSRM route (an array of [lat, lng] points) as a polyline. */
    drawRoute(map, geometry, colorVar) {
        if (!geometry || !geometry.length) return null;
        return L.polyline(geometry, {
            color: this.resolveColor(colorVar || '--primary'),
            weight: 4,
            opacity: 0.85,
        }).addTo(map);
    },

    pinIcon(colorVar, glyph, extraClass) {
        return L.divIcon({
            className: 'gc-pin-wrap',
            html: `<span class="gc-pin ${extraClass || ''}" style="--pin-color: var(${colorVar})"><span class="gc-pin-glyph">${glyph || ''}</span></span>`,
            iconSize: [28, 36],
            iconAnchor: [14, 34],
            popupAnchor: [0, -32],
        });
    },

    /* Great-circle distance in km between two [lat, lng] pairs — for the ops
       panel's "distance from you" readout when the viewer has a location. */
    haversineKm(a, b) {
        const R = 6371.0088;
        const toRad = (d) => (d * Math.PI) / 180;
        const dLat = toRad(b[0] - a[0]);
        const dLng = toRad(b[1] - a[1]);
        const s =
            Math.sin(dLat / 2) ** 2 +
            Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLng / 2) ** 2;
        return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
    },

    /* A single-marker picker map bound to two hidden numeric inputs. Click the
       map or drag the marker to set coordinates; an optional "use my
       location" button fills them from navigator.geolocation. */
    initPicker(containerId, latInputId, lngInputId, opts) {
        opts = opts || {};
        const latInput = document.getElementById(latInputId);
        const lngInput = document.getElementById(lngInputId);
        const container = document.getElementById(containerId);
        if (!container || !latInput || !lngInput || typeof L === 'undefined') return null;

        const hasStart = latInput.value && lngInput.value;
        const startLat = hasStart ? parseFloat(latInput.value) : this.DEFAULT_CENTER[0];
        const startLng = hasStart ? parseFloat(lngInput.value) : this.DEFAULT_CENTER[1];
        const map = L.map(containerId).setView([startLat, startLng], hasStart ? 14 : this.DEFAULT_ZOOM);
        this.tileLayer(map);

        let marker = null;
        const setMarker = (lat, lng) => {
            lat = Number(lat.toFixed(6));
            lng = Number(lng.toFixed(6));
            if (marker) {
                marker.setLatLng([lat, lng]);
            } else {
                marker = L.marker([lat, lng], { draggable: true, icon: this.pinIcon('--primary') }).addTo(map);
                marker.on('dragend', () => {
                    const pos = marker.getLatLng();
                    setMarker(pos.lat, pos.lng);
                });
            }
            latInput.value = lat;
            lngInput.value = lng;
        };

        if (hasStart) setMarker(startLat, startLng);
        map.on('click', (e) => setMarker(e.latlng.lat, e.latlng.lng));

        const locateBtn = opts.locateButtonId && document.getElementById(opts.locateButtonId);
        if (locateBtn) {
            if (!navigator.geolocation) {
                locateBtn.disabled = true;
            } else {
                locateBtn.addEventListener('click', () => {
                    locateBtn.disabled = true;
                    navigator.geolocation.getCurrentPosition(
                        (pos) => {
                            const { latitude, longitude } = pos.coords;
                            map.setView([latitude, longitude], 15);
                            setMarker(latitude, longitude);
                            locateBtn.disabled = false;
                        },
                        () => {
                            locateBtn.disabled = false;
                            alert(locateBtn.dataset.errorText || 'Could not determine your location.');
                        },
                    );
                });
            }
        }

        return map;
    },

    /* A read-only map that fetches GeoJSON-ish points from `dataUrl` and
       renders them as colored pins with popups. `dataUrl` must resolve to
       {"points": [{lat, lng, title, subtitle, color, glyph, url}, ...]}. */
    async initMarkersMap(containerId, dataUrl, opts) {
        opts = opts || {};
        const container = document.getElementById(containerId);
        if (!container || typeof L === 'undefined') return null;

        const map = L.map(containerId).setView(this.DEFAULT_CENTER, this.DEFAULT_ZOOM);
        this.tileLayer(map);

        let response;
        try {
            response = await fetch(dataUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
        } catch (e) {
            return map;
        }
        if (!response.ok) return map;
        const data = await response.json();
        const points = data.points || [];
        const bounds = [];

        points.forEach((point) => {
            const icon = this.pinIcon(point.color || '--primary', point.glyph || '');
            const marker = L.marker([point.lat, point.lng], { icon }).addTo(map);
            const titleHtml = point.url
                ? `<a href="${point.url}">${point.title}</a>`
                : `<strong>${point.title}</strong>`;
            // Volunteer marker with do-not-disturb on (staff map payload `away`).
            let awayHtml = '';
            if (point.away) {
                const lang = document.documentElement.getAttribute('lang') || 'ru';
                const label = (window.T && window.T[lang] && window.T[lang]['availability.away']) || 'Do not disturb';
                awayHtml = `<br><span class="tag away">${label}</span>`;
            }
            marker.bindPopup(
                `<div class="gc-popup">${titleHtml}${point.subtitle ? `<br><span class="gc-popup-sub">${point.subtitle}</span>` : ''}${awayHtml}</div>`,
            );
            bounds.push([point.lat, point.lng]);
        });

        if (bounds.length) {
            map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
        }
        return map;
    },

    /* ===== OPERATIONAL MAP =====
       An interactive map controller for the operations page: styled markers by
       kind/status/priority, marker-click -> onSelect(point), live client-side
       filtering, and geolocation with explicit state callbacks. Returns a
       controller object; does not fetch data itself (the page owns the fetch). */
    _opsStyle(point) {
        if (point.kind === 'me') return { color: '--primary', glyph: '•', cls: '' };
        if (point.kind === 'emergency') return { color: '--danger', glyph: '!', cls: 'gc-pin--emergency' };
        if (point.kind === 'pet') {
            // Lost pets still need finding (amber highlight); found pets are good news (teal).
            return { color: point.report_type === 'found' ? '--ok' : '--accent', glyph: 'P', cls: '' };
        }
        if (point.kind === 'volunteer') {
            return ({
                available: { color: '--ok', glyph: 'V', cls: '' },
                busy: { color: '--accent', glyph: 'V', cls: '' },
                offline: { color: '--muted', glyph: 'V', cls: '' },
            }[point.status] || { color: '--muted', glyph: 'V', cls: '' });
        }
        // task
        if (point.priority === 'emergency') return { color: '--danger', glyph: '!', cls: 'gc-pin--emergency' };
        if (point.is_overdue) return { color: '--danger', glyph: '⏱', cls: 'gc-pin--overdue' };
        return ({
            pending: { color: '--primary-2', glyph: '', cls: '' },
            active: { color: '--info', glyph: '', cls: '' },
            completed: { color: '--ok', glyph: '✓', cls: '' },
            cancelled: { color: '--muted', glyph: '', cls: '' },
        }[point.status] || { color: '--primary-2', glyph: '', cls: '' });
    },

    createOpsMap(containerId, opts) {
        opts = opts || {};
        const container = document.getElementById(containerId);
        if (!container || typeof L === 'undefined') return null;

        const map = L.map(containerId).setView(this.DEFAULT_CENTER, this.DEFAULT_ZOOM);
        this.tileLayer(map);
        const layer = L.layerGroup().addTo(map);
        // Live volunteer positions (feature 7): their own layer, so render()
        // (filters, data reloads) never wipes them. Fed by the staff-only
        // `map.marker {kind:"volunteer_live"}` WS event; harmless elsewhere.
        const live = this._liveMarkerLayer(map, opts);

        let allPoints = [];
        let predicate = () => true;
        let meMarker = null;
        const self = this;

        function render() {
            layer.clearLayers();
            const bounds = [];
            allPoints.forEach((point) => {
                if (point.kind !== 'me' && !predicate(point)) return;
                const style = self._opsStyle(point);
                const marker = L.marker([point.lat, point.lng], {
                    icon: self.pinIcon(style.color, style.glyph, style.cls),
                }).addTo(layer);
                marker.on('click', () => opts.onSelect && opts.onSelect(point));
                bounds.push([point.lat, point.lng]);
            });
            if (bounds.length) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
        }

        function locate() {
            if (!navigator.geolocation) {
                opts.onLocate && opts.onLocate('unavailable');
                return;
            }
            opts.onLocate && opts.onLocate('loading');
            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    const lat = pos.coords.latitude;
                    const lng = pos.coords.longitude;
                    if (meMarker) {
                        meMarker.setLatLng([lat, lng]);
                    } else {
                        meMarker = L.marker([lat, lng], {
                            icon: self.pinIcon('--primary', '•'),
                        }).addTo(map);
                    }
                    map.setView([lat, lng], 14);
                    opts.onLocate && opts.onLocate('success', { lat, lng });
                    // Persist through the existing endpoint so matching/routing
                    // can use it later. Best-effort; ignore failures.
                    if (opts.updateUrl && opts.csrfToken) {
                        fetch(opts.updateUrl, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': opts.csrfToken },
                            body: JSON.stringify({ latitude: lat, longitude: lng }),
                        }).catch(() => {});
                    }
                },
                (err) => {
                    opts.onLocate && opts.onLocate(err && err.code === 1 ? 'denied' : 'unavailable');
                },
                { enableHighAccuracy: true, timeout: 10000 },
            );
        }

        return {
            map,
            render(points) { allPoints = points || []; render(); },
            applyFilter(fn) { predicate = fn || (() => true); render(); },
            locate,
            get points() { return allPoints; },
            /* Live markers: setLive([{task_id, lat, lng, ts}]) for an initial
               load (e.g. tracking.live_markers() in the map JSON), then the WS
               keeps them fresh. */
            setLive(list) { (list || []).forEach((p) => live.upsert(p)); },
            focusLive(taskId) { return live.focus(taskId); },
            get liveList() { return live.list(); },
            get liveCount() { return live.size; },
        };
    },

    /* ===== ROUTE NAVIGATION + LIVE TRACKING (Feature Pack v2, features 2 + 7) =====
       Behaviour only — the task-detail template owns the markup (consent
       modal, "your location is visible" indicator, panels). Public API:

         GCMap.createRouteNav(opts)   → route map + steps + live marker + re-route
         GCMap.createTracker(opts)    → the assigned volunteer's watchPosition sender
         GCMap.setTrackingConsent(url, give, csrf) → Promise<bool consent>
         GCMap.stepText(step) / GCMap.stepKey(step) / GCMap.formatDistance(m)
         GCMap.whenRealtime(fn)       → fn(GCRealtime) once realtime.js is ready

       Every visible string comes from i18n.js (`nav.*`, `route.*`) and is
       rendered with data-i18n / data-i18n-args, so a language switch
       re-localizes the step list without a re-fetch. Street names are set via
       textContent / JSON args (never innerHTML). */

    NAV_OFF_ROUTE_M: 50,
    NAV_REROUTE_DEBOUNCE_MS: 15000,
    NAV_ARRIVE_RADIUS_M: 30,

    _lang() {
        let lang = null;
        try { lang = localStorage.getItem('gc-lang'); } catch (e) { /* storage blocked */ }
        lang = lang || document.documentElement.getAttribute('lang') || 'ru';
        return (window.T && window.T[lang]) ? lang : 'ru';
    },

    /* Translate `key` with {placeholder} args; `fallback` if the key is missing. */
    t(key, args, fallback) {
        const table = (window.T && window.T[this._lang()]) || {};
        const en = (window.T && window.T.en) || {};
        const text = table[key] || en[key] || fallback || key;
        if (!args) return text;
        return text.replace(/\{(\w+)\}/g, (m, name) => (name in args ? String(args[name]) : m));
    },

    /* Sets text + data-i18n(-args) so i18n.js setLanguage() re-localizes it. */
    _i18nEl(tag, cls, key, args, fallback) {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        el.setAttribute('data-i18n', key);
        if (args) el.setAttribute('data-i18n-args', JSON.stringify(args));
        el.textContent = this.t(key, args, fallback);
        return el;
    },

    _el(target) {
        return typeof target === 'string' ? document.getElementById(target) : (target || null);
    },

    whenRealtime(fn) {
        if (window.GCRealtime) { fn(window.GCRealtime); return; }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => {
                if (window.GCRealtime) fn(window.GCRealtime);
            }, { once: true });
        }
    },

    csrfToken(explicit) {
        if (explicit) return explicit;
        const input = document.querySelector('[name=csrfmiddlewaretoken]');
        if (input && input.value) return input.value;
        const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    },

    postJSON(url, body, csrf) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                Accept: 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRFToken': this.csrfToken(csrf),
            },
            body: JSON.stringify(body || {}),
        });
    },

    /* POST /myapp/tracking/consent/ — resolves to the consent state after the call. */
    setTrackingConsent(url, give, csrf) {
        return this.postJSON(url, { action: give ? 'give' : 'revoke' }, csrf)
            .then((r) => (r.ok ? r.json() : { consent: false }))
            .then((d) => !!d.consent)
            .catch(() => false);
    },

    /* ---------- maneuver text ---------- */

    /* OSRM maneuver {type, modifier} → nav.* i18n key. */
    stepKey(step) {
        const type = (step.maneuver || '').replace(/ /g, '_');
        const mod = (step.modifier || '').replace(/ /g, '_');
        const side = mod.indexOf('left') !== -1 ? 'left' : (mod.indexOf('right') !== -1 ? 'right' : '');
        switch (type) {
            case 'depart': return 'nav.depart';
            case 'arrive': return 'nav.arrive';
            case 'merge': return 'nav.merge';
            case 'on_ramp': return 'nav.on_ramp';
            case 'off_ramp': return 'nav.off_ramp';
            case 'roundabout':
            case 'rotary':
                return step.exit ? 'nav.roundabout_exit_n' : 'nav.roundabout';
            case 'exit_roundabout':
            case 'exit_rotary':
                return 'nav.roundabout_leave';
            case 'fork':
                return side ? `nav.keep_${side}` : 'nav.continue';
            case 'turn':
            case 'end_of_road':
            case 'roundabout_turn':
                if (mod === 'straight' || !mod) return 'nav.continue';
                return `nav.turn_${mod}`;   // left/right/slight_*/sharp_*/uturn
            default:   // continue, new_name, use_lane, notification, unknown
                if (mod === 'uturn') return 'nav.turn_uturn';
                if (mod === 'left' || mod === 'right' || mod === 'sharp_left' || mod === 'sharp_right') return `nav.turn_${mod}`;
                if (side) return `nav.keep_${side}`;
                return 'nav.continue';
        }
    },

    stepText(step) {
        const base = this.t(this.stepKey(step), { exit: step.exit || '' });
        if (!step.name || step.maneuver === 'arrive') return base;
        return `${base} ${this.t('nav.onto', { name: step.name }, 'onto {name}')}`;
    },

    formatDistance(m) {
        if (m == null || !isFinite(m)) return '—';
        if (m < 1000) return this.t('nav.dist_m', { n: Math.max(0, Math.round(m / 10) * 10) }, '{n} m');
        return this.t('nav.dist_km', { n: (m / 1000).toFixed(1) }, '{n} km');
    },

    /* HH:MM in the page language (24 h), for "updated at" labels. */
    clockTime(value) {
        const d = value instanceof Date ? value : new Date(value);
        if (isNaN(d)) return '';
        const locale = { ru: 'ru-RU', tj: 'tg-TJ', en: 'en-GB' }[this._lang()] || 'ru-RU';
        try {
            return d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false });
        } catch (e) {
            return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        }
    },

    formatEta(min) {
        if (min == null || !isFinite(min)) return '—';
        const total = Math.max(1, Math.round(min));
        if (total < 60) return this.t('nav.min', { n: total }, '{n} min');
        return this.t('nav.hours_min', { h: Math.floor(total / 60), m: total % 60 }, '{h} h {m} min');
    },

    /* ---------- geometry ---------- */

    /* Distance (m) from p to the polyline + index of the closest segment. */
    _nearestOnLine(p, line) {
        if (!line || line.length < 2) return { distance: Infinity, index: 0 };
        const lat0 = (p[0] * Math.PI) / 180;
        const kx = 111320 * Math.cos(lat0);
        const ky = 110540;
        const px = p[1] * kx;
        const py = p[0] * ky;
        let best = Infinity;
        let bestIdx = 0;
        for (let i = 0; i < line.length - 1; i += 1) {
            const ax = line[i][1] * kx; const ay = line[i][0] * ky;
            const bx = line[i + 1][1] * kx; const by = line[i + 1][0] * ky;
            const dx = bx - ax; const dy = by - ay;
            const len2 = dx * dx + dy * dy;
            let t = len2 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
            t = Math.max(0, Math.min(1, t));
            const ex = ax + t * dx - px; const ey = ay + t * dy - py;
            const d = Math.sqrt(ex * ex + ey * ey);
            if (d < best) { best = d; bestIdx = i; }
        }
        return { distance: best, index: bestIdx };
    },

    _metersBetween(a, b) {
        return this.haversineKm(a, b) * 1000;
    },

    /* ---------- route + navigation controller ---------- */

    /* opts:
         mapEl        (id|element, required) — the map container
         routeUrl     (required) — /myapp/tasks/<id>/route/
         taskId       — subscribes task_<id> for live positions when `live`
         isVolunteer  — this viewer is the assigned volunteer (may send lat/lng as origin)
         profile      — 'driving' (default) | 'walking'
         stepsEl      (id|element) — an <ol>/<ul> the step list is rendered into
         live         — default true: follow GCRealtime location.updated / tracking.stopped
         onRoute(data)          — after each successful fetch (metrics: data.distance_km,
                                  data.eta_min, data.eta_estimated, data.deep_links, data.origin.source)
         onState(state, detail) — 'loading' | 'ready' | 'empty' {reason} | 'error' |
                                  'rerouting' | 'live' {lat,lng,ts} | 'live_stopped' {reason}
         onStep(index, step, distanceM) — current (next) maneuver changed / distance updated
       Returns { load(origin?), setProfile(p), updatePosition(lat, lng, extra?),
                 map, data, profile, currentStep, destroy() }. */
    createRouteNav(opts) {
        opts = opts || {};
        const self = this;
        const mapEl = this._el(opts.mapEl);
        if (!mapEl || typeof L === 'undefined' || !opts.routeUrl) return null;
        const stepsEl = this._el(opts.stepsEl);
        const emit = (state, detail) => { if (opts.onState) { try { opts.onState(state, detail || {}); } catch (e) { /* UI bug must not stop nav */ } } };

        let map = null;
        let routeLayer = null;
        let originMarker = null;
        let destMarker = null;
        let liveMarker = null;
        let accuracyCircle = null;
        let data = null;
        let profile = opts.profile === 'walking' ? 'walking' : 'driving';
        let lastOrigin = null;      // {lat, lng} last live origin the volunteer sent
        let lastPosition = null;    // [lat, lng] latest known volunteer position
        let lastRerouteAt = 0;
        let fitted = false;
        let inflight = null;
        let currentStep = -1;
        let stepIdx = [];
        let destroyed = false;
        const offs = [];

        function ensureMap() {
            if (map) return map;
            mapEl.hidden = false;
            map = L.map(mapEl);
            self.tileLayer(map);
            return map;
        }

        function renderSteps() {
            stepIdx = [];
            currentStep = -1;
            if (!data || !data.steps) return;
            const line = data.geometry || [];
            stepIdx = data.steps.map((s) => (line.length > 1 ? self._nearestOnLine(s.location, line).index : 0));
            if (!stepsEl) return;
            stepsEl.textContent = '';
            data.steps.forEach((step, i) => {
                const li = document.createElement('li');
                li.className = 'nav-step';
                li.dataset.step = String(i);
                li.dataset.maneuver = step.maneuver || '';
                if (step.modifier) li.dataset.modifier = step.modifier;
                const text = document.createElement('span');
                text.className = 'nav-step__text';
                text.appendChild(self._i18nEl('span', 'nav-step__action', self.stepKey(step), { exit: step.exit || '' }));
                if (step.name && step.maneuver !== 'arrive') {
                    text.appendChild(document.createTextNode(' '));
                    text.appendChild(self._i18nEl('span', 'nav-step__street', 'nav.onto', { name: step.name }, 'onto {name}'));
                }
                li.appendChild(text);
                if (step.distance_m > 0) {
                    const dist = step.distance_m < 1000
                        ? self._i18nEl('span', 'nav-step__dist', 'nav.dist_m', { n: Math.round(step.distance_m / 10) * 10 }, '{n} m')
                        : self._i18nEl('span', 'nav-step__dist', 'nav.dist_km', { n: (step.distance_m / 1000).toFixed(1) }, '{n} km');
                    li.appendChild(dist);
                }
                stepsEl.appendChild(li);
            });
            stepsEl.hidden = !data.steps.length;
        }

        function highlight(index, distanceM) {
            if (index !== currentStep && stepsEl) {
                stepsEl.querySelectorAll('.nav-step').forEach((li) => {
                    const i = Number(li.dataset.step);
                    li.classList.toggle('is-current', i === index);
                    li.classList.toggle('is-done', i < index);
                    if (i === index) li.setAttribute('aria-current', 'step');
                    else li.removeAttribute('aria-current');
                });
                const li = stepsEl.querySelector('.nav-step.is-current');
                if (li && stepsEl.scrollHeight > stepsEl.clientHeight) {
                    stepsEl.scrollTop = Math.max(0, li.offsetTop - stepsEl.offsetTop - 8);
                }
            }
            const changed = index !== currentStep;
            currentStep = index;
            if (opts.onStep && data && data.steps[index]) {
                try { opts.onStep(index, data.steps[index], distanceM, changed); } catch (e) { /* ignore */ }
            }
        }

        function render() {
            ensureMap();
            if (routeLayer) { map.removeLayer(routeLayer); routeLayer = null; }
            const o = [data.origin.lat, data.origin.lng];
            const d = [data.destination.lat, data.destination.lng];
            if (!originMarker) originMarker = L.marker(o, { icon: self.pinIcon('--primary-2', 'V') }).addTo(map);
            else originMarker.setLatLng(o);
            if (!destMarker) destMarker = L.marker(d, { icon: self.pinIcon('--accent', 'C') }).addTo(map);
            else destMarker.setLatLng(d);
            // The live marker replaces the origin pin once positions stream in.
            if (liveMarker) originMarker.setOpacity(0);
            const bounds = L.latLngBounds([o, d]);
            if (data.success && data.geometry && data.geometry.length) {
                routeLayer = self.drawRoute(map, data.geometry, '--primary');
                data.geometry.forEach((pt) => bounds.extend(pt));
            }
            if (!fitted) {
                map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
                fitted = true;
            }
            setTimeout(() => map && map.invalidateSize(), 50);
            renderSteps();
            if (lastPosition) track(lastPosition, true);
        }

        function load(origin) {
            if (destroyed) return Promise.resolve(null);
            const params = new URLSearchParams({ profile });
            if (opts.isVolunteer && origin && isFinite(origin.lat) && isFinite(origin.lng)) {
                params.set('lat', Number(origin.lat).toFixed(6));
                params.set('lng', Number(origin.lng).toFixed(6));
                lastOrigin = { lat: origin.lat, lng: origin.lng };
            }
            emit(data ? 'rerouting' : 'loading');
            const sep = opts.routeUrl.indexOf('?') === -1 ? '?' : '&';
            const req = fetch(`${opts.routeUrl}${sep}${params}`, {
                credentials: 'same-origin',
                headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            })
                .then((r) => r.json())
                .then((json) => {
                    if (req !== inflight || destroyed) return null;   // superseded
                    if (!json.origin || !json.destination) {
                        emit('empty', { reason: json.reason || null });
                        return null;
                    }
                    data = json;
                    render();
                    if (opts.onRoute) { try { opts.onRoute(json); } catch (e) { /* ignore */ } }
                    emit('ready', json);
                    return json;
                })
                .catch(() => { if (req === inflight) emit('error'); return null; });
            inflight = req;
            return req;
        }

        function maybeReroute(pos) {
            if (!data) return;
            const now = Date.now();
            if (now - lastRerouteAt < self.NAV_REROUTE_DEBOUNCE_MS) return;
            let off;
            if (data.success && data.geometry && data.geometry.length > 1) {
                off = self._nearestOnLine(pos, data.geometry).distance > self.NAV_OFF_ROUTE_M;
            } else {
                // Straight-line fallback: refresh distance/ETA as the volunteer moves.
                off = self._metersBetween(pos, [data.origin.lat, data.origin.lng]) > self.NAV_OFF_ROUTE_M;
            }
            if (!off) return;
            lastRerouteAt = now;
            load(opts.isVolunteer ? { lat: pos[0], lng: pos[1] } : null);
        }

        function track(pos, quiet) {
            if (!data || !data.steps || !data.steps.length) return;
            const dest = [data.destination.lat, data.destination.lng];
            const last = data.steps.length - 1;
            if (self._metersBetween(pos, dest) <= self.NAV_ARRIVE_RADIUS_M) {
                highlight(last, self._metersBetween(pos, dest));
                return;
            }
            const line = data.geometry || [];
            const at = line.length > 1 ? self._nearestOnLine(pos, line).index : 0;
            let next = last;
            for (let i = 1; i < data.steps.length; i += 1) {
                if (stepIdx[i] > at) { next = i; break; }
            }
            highlight(next, self._metersBetween(pos, data.steps[next].location));
            if (!quiet) maybeReroute(pos);
        }

        /* A new volunteer position (own GPS via the tracker, or location.updated). */
        function updatePosition(lat, lng, extra) {
            if (destroyed || !isFinite(lat) || !isFinite(lng)) return;
            extra = extra || {};
            const pos = [Number(lat), Number(lng)];
            lastPosition = pos;
            ensureMap();
            if (!liveMarker) {
                liveMarker = L.marker(pos, { icon: self.pinIcon('--primary-2', 'V', 'gc-pin--live'), zIndexOffset: 500 }).addTo(map);
                if (originMarker) originMarker.setOpacity(0);
            } else {
                liveMarker.setLatLng(pos);
            }
            if (extra.accuracy && extra.accuracy > 0) {
                if (!accuracyCircle) {
                    accuracyCircle = L.circle(pos, {
                        radius: extra.accuracy, weight: 1, opacity: 0.4, fillOpacity: 0.08,
                        color: self.resolveColor('--primary-2'), interactive: false,
                        className: 'gc-accuracy',   // map.css themes it per light/dark
                    }).addTo(map);
                } else {
                    accuracyCircle.setLatLng(pos).setRadius(extra.accuracy);
                }
            }
            emit('live', { lat: pos[0], lng: pos[1], ts: extra.ts || null, accuracy: extra.accuracy || null });
            track(pos, false);
        }

        function clearLive(reason) {
            if (liveMarker && map) map.removeLayer(liveMarker);
            if (accuracyCircle && map) map.removeLayer(accuracyCircle);
            liveMarker = null;
            accuracyCircle = null;
            lastPosition = null;
            if (originMarker) originMarker.setOpacity(1);
            emit('live_stopped', { reason: reason || null });
        }

        if (opts.live !== false && opts.taskId) {
            const taskId = Number(opts.taskId);
            self.whenRealtime((rt) => {
                rt.subscribeTask(taskId);
                offs.push(rt.on('location.updated', (p) => {
                    if (Number(p.task_id) !== taskId) return;
                    // The volunteer's own page is fed by its tracker directly.
                    if (opts.isVolunteer && opts.ownPositions) return;
                    updatePosition(p.lat, p.lng, { ts: p.ts, accuracy: p.accuracy });
                }));
                offs.push(rt.on('tracking.stopped', (p) => {
                    if (Number(p.task_id) === taskId) clearLive(p.reason);
                }));
            });
        }

        return {
            load,
            setProfile(p) {
                const next = p === 'walking' ? 'walking' : 'driving';
                if (next === profile && data) return Promise.resolve(data);
                profile = next;
                fitted = false;
                const origin = opts.isVolunteer && lastPosition ? { lat: lastPosition[0], lng: lastPosition[1] } : lastOrigin;
                return load(origin);
            },
            updatePosition,
            clearLive,
            get map() { return map; },
            get data() { return data; },
            get profile() { return profile; },
            get currentStep() { return currentStep; },
            destroy() {
                destroyed = true;
                offs.forEach((off) => { try { off(); } catch (e) { /* ignore */ } });
                if (map) map.remove();
                map = null;
            },
        };
    },

    /* ---------- live tracking sender (assigned volunteer only) ---------- */

    /* opts:
         taskId       (required)
         locationUrl  — POST fallback /myapp/tasks/<id>/location/ (when WS is down)
         stopUrl      — POST /myapp/tasks/<id>/tracking/stop/
         csrfToken    — optional; read from the page / cookie otherwise
         nav          — a createRouteNav controller to feed with own positions
         intervalMs   — resend cadence while moving slowly (default 12000; 10–15 s)
         minMoveM     — send early after moving this far (default 25)
         onState(state, detail) — 'starting' | 'active' | 'sent' {via:'ws'|'http'} |
                                  'dropped' {code} | 'denied' | 'unavailable' |
                                  'forbidden' | 'stopped' {reason}
         onPosition(fix)        — every GPS fix {lat, lng, accuracy, ts}
       Returns { start(), stop({notifyServer = true}), running, lastFix }.
       Start it only after consent (GCMap.setTrackingConsent) on an active task. */
    createTracker(opts) {
        opts = opts || {};
        const self = this;
        const taskId = Number(opts.taskId);
        const INTERVAL = Math.min(15000, Math.max(10000, opts.intervalMs || 12000));
        const MIN_MOVE = opts.minMoveM || 25;
        const MIN_GAP = 5000;          // the server drops points < 5 s apart
        const MAX_ACCURACY = 200;      // ... and fixes worse than 200 m
        const emit = (state, detail) => { if (opts.onState) { try { opts.onState(state, detail || {}); } catch (e) { /* ignore */ } } };

        let watchId = null;
        let keepalive = null;
        let lastFix = null;
        let lastSent = null;
        let lastSentAt = 0;
        let running = false;
        let offs = [];
        let seq = 0;
        const REF_PREFIX = `loc-${taskId}-`;

        function payload(fix) {
            const p = { task_id: taskId, lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy };
            if (fix.speed != null && isFinite(fix.speed)) p.speed = fix.speed;
            if (fix.heading != null && isFinite(fix.heading)) p.heading = fix.heading;
            return p;
        }

        function send(fix) {
            lastSent = fix;
            lastSentAt = Date.now();
            const body = payload(fix);
            const rt = window.GCRealtime;
            seq += 1;
            if (rt && rt.mode === 'ws' && rt.send('location.update', body, `${REF_PREFIX}${seq}`)) {
                emit('sent', { via: 'ws' });
                return;
            }
            if (!opts.locationUrl) return;
            self.postJSON(opts.locationUrl, body, opts.csrfToken)
                .then((r) => {
                    if (r.status === 403) { halt('forbidden', false); emit('forbidden'); return null; }
                    return r.json();
                })
                .then((d) => {
                    if (!d) return;
                    if (d.ok) emit('sent', { via: 'http' });
                    else emit('dropped', { code: d.code });
                })
                .catch(() => { /* offline: the next fix retries */ });
        }

        function onFix(pos) {
            const c = pos.coords;
            const fix = {
                lat: c.latitude, lng: c.longitude,
                accuracy: c.accuracy != null ? Math.round(c.accuracy) : null,
                speed: c.speed, heading: c.heading, ts: pos.timestamp || Date.now(),
            };
            lastFix = fix;
            if (opts.onPosition) { try { opts.onPosition(fix); } catch (e) { /* ignore */ } }
            if (opts.nav) opts.nav.updatePosition(fix.lat, fix.lng, { accuracy: fix.accuracy, ts: new Date(fix.ts).toISOString() });
            if (fix.accuracy != null && fix.accuracy > MAX_ACCURACY) return;
            const elapsed = Date.now() - lastSentAt;
            const moved = lastSent ? self._metersBetween([lastSent.lat, lastSent.lng], [fix.lat, fix.lng]) : Infinity;
            if (elapsed >= MIN_GAP && (moved > MIN_MOVE || elapsed >= INTERVAL)) send(fix);
            emit('active');
        }

        function onError(err) {
            if (err && err.code === 1) {   // PERMISSION_DENIED
                halt('denied', false);
                emit('denied');
                return;
            }
            emit('unavailable');            // timeout / no fix: keep watching
        }

        function halt(reason, notifyServer) {
            if (!running) return;
            running = false;
            if (watchId !== null && navigator.geolocation) navigator.geolocation.clearWatch(watchId);
            watchId = null;
            if (keepalive) clearInterval(keepalive);
            keepalive = null;
            offs.forEach((off) => { try { off(); } catch (e) { /* ignore */ } });
            offs = [];
            if (notifyServer) {
                const rt = window.GCRealtime;
                const viaWs = rt && rt.mode === 'ws' && rt.send('tracking.stop', { task_id: taskId }, `stop-${taskId}`);
                if (!viaWs && opts.stopUrl) self.postJSON(opts.stopUrl, {}, opts.csrfToken).catch(() => {});
            }
            if (reason !== 'denied' && reason !== 'forbidden') emit('stopped', { reason });
        }

        function start() {
            if (running) return true;
            if (!navigator.geolocation) { emit('unavailable'); return false; }
            running = true;
            emit('starting');
            watchId = navigator.geolocation.watchPosition(onFix, onError, {
                enableHighAccuracy: true, maximumAge: 5000, timeout: 30000,
            });
            // Standing still produces no watchPosition callbacks: re-send the
            // last good fix so the client/staff see the position is current.
            keepalive = setInterval(() => {
                if (lastFix && Date.now() - lastSentAt >= INTERVAL
                    && !(lastFix.accuracy != null && lastFix.accuracy > MAX_ACCURACY)) send(lastFix);
            }, 5000);
            self.whenRealtime((rt) => {
                if (!running) return;
                rt.subscribeTask(taskId);
                offs.push(rt.on('tracking.stopped', (p) => {
                    if (Number(p.task_id) === taskId) halt(p.reason || 'stopped', false);
                }));
                offs.push(rt.on('task.updated', (p) => {
                    if (Number(p.task_id) === taskId && p.status && p.status !== 'active') halt('task_ended', false);
                }));
                // A WS-rejected point: error {code, ref} where ref is our frame id.
                offs.push(rt.on('error', (p) => {
                    const ref = p && p.ref != null ? String(p.ref) : '';
                    if (ref.indexOf(REF_PREFIX) !== 0) return;
                    if (p.code === 'forbidden') { halt('forbidden', false); emit('forbidden'); return; }
                    emit('dropped', { code: p.code });
                }));
            });
            return true;
        }

        return {
            start,
            stop(o) { halt('volunteer', !(o && o.notifyServer === false)); },
            get running() { return running; },
            get lastFix() { return lastFix; },
        };
    },

    /* ---------- staff ops map: live volunteer markers ---------- */
    LIVE_MARKER_TTL_MS: 10 * 60 * 1000,

    _liveMarkerLayer(map, opts) {
        const self = this;
        const group = L.layerGroup().addTo(map);
        const markers = new Map();   // task_id -> {marker, seen}

        function point(p) {
            const time = p.ts ? new Date(p.ts) : null;
            return {
                kind: 'volunteer_live',
                id: p.task_id,
                task_id: p.task_id,
                lat: p.lat,
                lng: p.lng,
                ts: p.ts || null,
                title: self.t('map.live_volunteer', null, 'Volunteer (live)'),
                subtitle: `#${p.task_id}${time && !isNaN(time) ? ` · ${self.clockTime(time)}` : ''}`,
                url: `/myapp/tasks/${p.task_id}/`,
            };
        }

        function list() {
            return Array.from(markers.values()).map((entry) => entry.point)
                .sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
        }

        function changed() {
            if (opts.onLiveChange) { try { opts.onLiveChange(list()); } catch (e) { /* UI bug must not stop the map */ } }
        }

        function remove(taskId) {
            const entry = markers.get(taskId);
            if (entry) group.removeLayer(entry.marker);
            const had = markers.delete(taskId);
            if (had) changed();
        }

        function upsert(p) {
            const taskId = Number(p && p.task_id);
            if (!taskId) return;
            if (p.removed) { remove(taskId); return; }
            if (!isFinite(p.lat) || !isFinite(p.lng)) return;
            let entry = markers.get(taskId);
            if (!entry) {
                const marker = L.marker([p.lat, p.lng], {
                    icon: self.pinIcon('--primary-2', 'V', 'gc-pin--live'), zIndexOffset: 400,
                }).addTo(group);
                entry = { marker, seen: Date.now(), point: null };
                marker.on('click', () => opts.onSelect && opts.onSelect(entry.point));
                markers.set(taskId, entry);
            } else {
                entry.marker.setLatLng([p.lat, p.lng]);
            }
            entry.seen = Date.now();
            entry.point = point(p);
            changed();
        }

        if (opts.live !== false) {
            this.whenRealtime((rt) => {
                rt.on('map.marker', (p) => { if (p && p.kind === 'volunteer_live') upsert(p); });
            });
            setInterval(() => {
                const now = Date.now();
                markers.forEach((entry, taskId) => { if (now - entry.seen > self.LIVE_MARKER_TTL_MS) remove(taskId); });
            }, 60000);
        }

        return { upsert, remove, list, focus(taskId) {
            const entry = markers.get(Number(taskId));
            if (entry) map.setView(entry.marker.getLatLng(), Math.max(map.getZoom(), 15));
            return !!entry;
        }, get size() { return markers.size; } };
    },
};

/* If Leaflet itself never loaded (its CDN and the local fallback both blocked
   on this visitor's network), every map container would stay blank — show the
   localized message instead. */
document.addEventListener('DOMContentLoaded', () => {
    if (typeof L !== 'undefined') return;
    document.querySelectorAll('.ops-map__canvas, .map-mini:not([hidden]), .map-picker')
        .forEach((el) => GCMap.showError(el, 'map.error_load'));
});
