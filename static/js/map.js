/* Shared MapLibre GL helpers for KhayrKhoh map pages.
   Loaded on demand (not from base.html) by any page that embeds a map, right
   after partials/_maplibre_js.html. Requires the global `maplibregl`.

   Basemap: the vector style from the maps service (maps.vector_style(),
   OpenFreeMap by default — 3D buildings, tilt, rotation). If it can't load
   for a visitor, the map falls back to the raster tiles of maps.tile_layer()
   (OSM), then to their keyless CARTO `fallback`; if that fails too, a
   localized message replaces the blank area. */

const GCMap = {
    DEFAULT_CENTER: [38.5598, 68.7870], // Dushanbe
    DEFAULT_ZOOM: 12,
    PITCH_3D: 55,
    BEARING_3D: -17,
    ROUTE_COLOR: '#2A3BA5',
    ROUTE_CASING: '#ffffff',
    PREF_3D_KEY: 'gc-map-3d',
    _uid: 0,

    /* Map config comes from the maps service layer: base.html renders it as
       <script id="gc-maps-tile" type="application/json"> via json_script
       (myapp.context_processors.maps_config): the raster tile config + its
       keyless `fallback` + `vector` {url, provider}. The OSM object below is
       the fallback if that element is absent or the provider was misconfigured. */
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
            vector: {},
        };
        try {
            const el = document.getElementById('gc-maps-tile');
            const cfg = el ? JSON.parse(el.textContent) : null;
            return (cfg && cfg.url) ? cfg : Object.assign(OSM, { vector: (cfg && cfg.vector) || {} });
        } catch (e) {
            return OSM;
        }
    },

    available() {
        return typeof maplibregl !== 'undefined' && !!maplibregl.Map;
    },

    /* A MapLibre style made of one raster tile layer ({s} subdomains, {r} retina). */
    rasterStyle(c) {
        const retina = (window.devicePixelRatio || 1) > 1 ? '@2x' : '';
        const url = String(c.url || '').replace('{r}', retina);
        const subs = (c.subdomains || '').split('').filter(Boolean);
        const tiles = url.indexOf('{s}') !== -1 && subs.length ? subs.map((s) => url.replace('{s}', s)) : [url.replace('{s}', 'a')];
        return {
            version: 8,
            sources: {
                'gc-raster': { type: 'raster', tiles, tileSize: 256, maxzoom: c.max_zoom || 19, attribution: c.attribution || '' },
            },
            layers: [{ id: 'gc-raster', type: 'raster', source: 'gc-raster' }],
        };
    },

    /* A localized, non-blank failure state for a map container. Text comes from
       i18n.js (window.T) in the visitor's current language. */
    showError(container, key) {
        if (!container) return null;
        const existing = container.querySelector(':scope > .map-error');
        if (existing) return existing;
        if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
        const el = document.createElement('div');
        el.className = 'map-error';
        el.setAttribute('role', 'status');
        el.setAttribute('data-i18n', key);
        el.textContent = this.t(key, null, 'The map could not be loaded.');
        container.appendChild(el);
        return el;
    },

    /* Resolves a --css-variable to its current computed color (for GL paint
       properties, which can't read CSS variables). */
    resolveColor(colorVar, fallback) {
        const value = getComputedStyle(document.documentElement).getPropertyValue(colorVar).trim();
        return value || fallback || '#1b3a5c';
    },

    _reducedMotion() {
        return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    },

    _pref3d() {
        try { return localStorage.getItem(this.PREF_3D_KEY) !== '0'; } catch (e) { return true; }
    },

    _savePref3d(on) {
        try { localStorage.setItem(this.PREF_3D_KEY, on ? '1' : '0'); } catch (e) { /* storage blocked */ }
    },

    /* MapLibre's own control strings, in the current language. */
    _mapLocale() {
        return {
            'NavigationControl.ZoomIn': this.t('map.zoom_in', null, 'Zoom in'),
            'NavigationControl.ZoomOut': this.t('map.zoom_out', null, 'Zoom out'),
            'NavigationControl.ResetBearing': this.t('map.reset_bearing', null, 'Reset bearing to north'),
            'FullscreenControl.Enter': this.t('map.fullscreen', null, 'Full screen'),
            'FullscreenControl.Exit': this.t('map.exit_fullscreen', null, 'Exit full screen'),
            'AttributionControl.ToggleAttribution': this.t('map.attribution', null, 'Map data'),
            'Map.Title': this.t('map.title', null, 'Map'),
        };
    },

    /* Tags MapLibre's control buttons with i18n.js keys so a language switch
       (setLanguage) relabels them, not only the language at creation time. */
    _tagControls(container) {
        [
            ['.maplibregl-ctrl-zoom-in', 'map.zoom_in'],
            ['.maplibregl-ctrl-zoom-out', 'map.zoom_out'],
            ['.maplibregl-ctrl-compass', 'map.reset_bearing'],
            ['.maplibregl-ctrl-fullscreen', 'map.fullscreen'],
            ['.maplibregl-ctrl-shrink', 'map.exit_fullscreen'],
        ].forEach(([sel, key]) => {
            container.querySelectorAll(sel).forEach((btn) => {
                btn.setAttribute('data-i18n-title', key);
                btn.setAttribute('data-i18n-aria', key);
                const label = this.t(key);
                btn.setAttribute('title', label);
                btn.setAttribute('aria-label', label);
            });
        });
    },

    /* ---------- map factory ---------- */

    /* Runs fn(map) now if the style is ready and again after every style
       (re)load — the raster fallback swaps the style and drops custom layers. */
    onStyle(map, fn) {
        map.__gcStyleFns.push(fn);
        if (map.__gcStyleReady) {
            try { fn(map); } catch (e) { /* one layer must not break the map */ }
        }
    },

    /* opts:
         center [lat, lng], zoom
         allow3d     — default true: 3D buildings + the 2D/3D toggle (vector style only)
         start3d     — initial mode; default = the viewer's saved preference (3D)
         fullscreen  — add the full-screen control
         scrollZoom  — default true
       Returns a maplibregl.Map, or null (container missing / no WebGL — the
       container then shows the localized error). */
    createMap(target, opts) {
        opts = opts || {};
        const self = this;
        const container = this._el(target);
        if (!container) return null;
        if (!this.available()) { this.showError(container, 'map.error_load'); return null; }
        container.classList.add('gc-map');

        const cfg = this.tileConfig();
        const stages = [];
        if (cfg.vector && cfg.vector.url) stages.push({ kind: 'vector', style: cfg.vector.url });
        stages.push({ kind: 'raster', style: this.rasterStyle(cfg) });
        if (cfg.fallback && cfg.fallback.url) stages.push({ kind: 'raster', style: this.rasterStyle(cfg.fallback) });
        let stage = 0;

        const center = opts.center || this.DEFAULT_CENTER;
        const allow3d = opts.allow3d !== false;
        let is3d = allow3d && stages[0].kind === 'vector' && (opts.start3d != null ? !!opts.start3d : this._pref3d());

        let map;
        try {
            map = new maplibregl.Map({
                container,
                style: stages[0].style,
                center: [center[1], center[0]],
                zoom: opts.zoom != null ? opts.zoom : this.DEFAULT_ZOOM,
                pitch: is3d ? this.PITCH_3D : 0,
                bearing: is3d ? this.BEARING_3D : 0,
                maxPitch: is3d ? 70 : 0,
                attributionControl: { compact: true },
                scrollZoom: opts.scrollZoom !== false,
                locale: this._mapLocale(),
                fadeDuration: this._reducedMotion() ? 0 : 300,
            });
        } catch (e) {
            // No WebGL on this device / browser: the same readable message.
            this.showError(container, 'map.error_load');
            return null;
        }
        map.__gcStyleFns = [];
        map.__gcStyleReady = false;
        map.__gc3d = is3d;

        // --- style fallback chain -------------------------------------------
        let okTiles = 0;
        let failures = 0;
        let notice = null;
        const FAILS = 4;
        function nextStage() {
            stage += 1;
            okTiles = 0;
            failures = 0;
            if (stage >= stages.length) {
                if (!notice) notice = self.showError(container, 'map.error_load');
                return;
            }
            map.__gcStyleReady = false;
            if (stages[stage].kind !== 'vector') {
                is3d = false;
                map.__gc3d = false;
                map.setMaxPitch(0);
                map.jumpTo({ pitch: 0, bearing: 0 });
                const btn = container.querySelector('.gc-ctrl-3d');
                if (btn) btn.closest('.maplibregl-ctrl').style.display = 'none';
            }
            map.setStyle(stages[stage].style);
        }
        map.on('style.load', () => {
            map.__gcStyleReady = true;
            map.__gcStyleFns.forEach((fn) => { try { fn(map); } catch (e) { /* keep going */ } });
        });
        map.on('data', (e) => {
            if (e.dataType === 'source' && e.tile) {
                okTiles += 1;
                if (notice) { notice.remove(); notice = null; }
            }
        });
        map.on('error', (e) => {
            if (okTiles > 1) return;      // a stray tile on a working map
            // The style document itself failed (no sourceId, style not loaded)
            // → switch at once; tiles after a few. Sprite/glyph hiccups on a
            // loaded style don't count.
            if (e && e.sourceId) failures += 1;
            else if (!map.__gcStyleReady) failures += FAILS;
            if (failures >= FAILS) nextStage();
        });

        // --- buildings + 2D/3D --------------------------------------------
        this.onStyle(map, () => self._setupBuildings(map));

        // --- controls -----------------------------------------------------
        map.addControl(new maplibregl.NavigationControl({ visualizePitch: true, showCompass: true }), 'top-right');
        // 2D/3D and full screen sit top-left, so a short map never clips the
        // zoom/compass column on the right.
        if (allow3d && stages[0].kind === 'vector') map.addControl(this._toggle3dControl(), 'top-left');
        if (opts.fullscreen && maplibregl.FullscreenControl) {
            const fullscreen = new maplibregl.FullscreenControl({ container });
            map.addControl(fullscreen, 'top-left');
            // iPhone Safari has no element full screen: MapLibre then pins the
            // container with position:fixed. Lift it above the sticky topbar and
            // drop the transform of a .reveal card around it (a transformed
            // ancestor would otherwise trap a fixed element inside the card).
            // (The control, not the map, fires these events.)
            fullscreen.on('fullscreenstart', () => {
                if (container.classList.contains('maplibregl-pseudo-fullscreen')) {
                    document.documentElement.classList.add('gc-map-pseudo-fs');
                }
                self._tagControls(container);
            });
            fullscreen.on('fullscreenend', () => {
                document.documentElement.classList.remove('gc-map-pseudo-fs');
                self._tagControls(container);
            });
        }
        this._tagControls(container);
        return map;
    },

    /* Finds the style's building layers (OpenMapTiles `building` source-layer),
       adds an extrusion if the style has none, and applies the current mode. */
    _setupBuildings(map) {
        const layers = (map.getStyle() && map.getStyle().layers) || [];
        const isBuilding = (l) => l['source-layer'] === 'building';
        let ext = layers.find((l) => l.type === 'fill-extrusion' && isBuilding(l));
        const flat = layers.find((l) => l.type === 'fill' && isBuilding(l));
        if (!ext && flat) {
            const id = 'gc-buildings-3d';
            if (!map.getLayer(id)) {
                const at = layers.indexOf(flat);
                const after = layers[at + 1];
                map.addLayer({
                    id, type: 'fill-extrusion', source: flat.source, 'source-layer': 'building', minzoom: 14,
                    paint: {},
                }, after ? after.id : undefined);
            }
            ext = map.getLayer(id) ? { id, minzoom: 14 } : null;
        }
        if (ext) {
            // Calm, slightly brand-tinted massing: taller = a touch bluer.
            map.setPaintProperty(ext.id, 'fill-extrusion-color', [
                'interpolate', ['linear'], ['coalesce', ['get', 'render_height'], 5],
                0, '#e7e5e0', 30, '#d6dae8', 80, '#c3c9e2',
            ]);
            map.setPaintProperty(ext.id, 'fill-extrusion-height', ['coalesce', ['get', 'render_height'], ['get', 'height'], 5]);
            map.setPaintProperty(ext.id, 'fill-extrusion-base', ['coalesce', ['get', 'render_min_height'], ['get', 'min_height'], 0]);
            map.setPaintProperty(ext.id, 'fill-extrusion-opacity', 0.88);
            try { map.setPaintProperty(ext.id, 'fill-extrusion-vertical-gradient', true); } catch (e) { /* older style spec */ }
        }
        const ids = layers.map((l) => l.id);
        const candidates = [flat && flat.id, ext && ext.id].filter((id) => id && map.getLayer(id));
        candidates.sort((a, b) => ids.indexOf(a) - ids.indexOf(b));
        const firstSymbol = layers.find((l) => l.type === 'symbol');
        map.__gcBuildings = {
            ext: ext && ext.id,
            extMin: (ext && ext.minzoom) || 14,
            flat: flat && flat.id,
            flatMin: (flat && flat.minzoom) || 13,
            // Route / accuracy / heat layers go right under this id: under the
            // buildings (a road never draws over a house), and labels stay on top.
            below: candidates[0] || (firstSymbol && firstSymbol.id) || undefined,
        };
        this._apply3d(map, map.__gc3d, false);
    },

    _apply3d(map, on, animate) {
        map.__gc3d = !!on;
        const b = map.__gcBuildings;
        if (b && b.ext && map.getLayer(b.ext)) map.setLayoutProperty(b.ext, 'visibility', on ? 'visible' : 'none');
        // 3D: flat footprints only below the extrusion zoom; 2D: flat everywhere.
        if (b && b.flat && map.getLayer(b.flat)) map.setLayerZoomRange(b.flat, b.flatMin, on ? b.extMin : 24);
        map.setMaxPitch(on ? 70 : 0);
        const camera = { pitch: on ? this.PITCH_3D : 0, bearing: on ? (map.getBearing() || this.BEARING_3D) : 0 };
        if (animate && !this._reducedMotion()) map.easeTo(Object.assign(camera, { duration: 650 }));
        else map.jumpTo(camera);
        const btn = map.getContainer().querySelector('.gc-ctrl-3d');
        if (btn) this._label3d(btn, on);
    },

    /* The button names the view it switches to: "3D" while flat, "2D" while tilted. */
    _label3d(btn, on) {
        const key = on ? 'map.view_2d' : 'map.view_3d';
        btn.textContent = on ? '2D' : '3D';
        btn.setAttribute('data-i18n-title', key);
        btn.setAttribute('data-i18n-aria', key);
        btn.setAttribute('title', this.t(key));
        btn.setAttribute('aria-label', this.t(key));
    },

    set3d(map, on) {
        if (!map) return;
        this._apply3d(map, on, true);
        this._savePref3d(on);
    },

    _toggle3dControl() {
        const self = this;
        let el = null;
        return {
            onAdd(map) {
                el = document.createElement('div');
                el.className = 'maplibregl-ctrl maplibregl-ctrl-group gc-ctrl-3d-group';
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'gc-ctrl-3d';
                self._label3d(btn, map.__gc3d);
                btn.addEventListener('click', () => self.set3d(map, !map.__gc3d));
                el.appendChild(btn);
                return el;
            },
            onRemove() { if (el) el.remove(); },
        };
    },

    /* ---------- markers ---------- */

    /* The classic round-top pin (status colour + glyph) as a DOM element. */
    pinEl(colorVar, glyph, extraClass) {
        const wrap = document.createElement('div');
        wrap.className = 'gc-pin-wrap';
        const pin = document.createElement('span');
        pin.className = `gc-pin ${extraClass || ''}`.trim();
        pin.style.setProperty('--pin-color', `var(${colorVar || '--primary'})`);
        const g = document.createElement('span');
        g.className = 'gc-pin-glyph';
        g.textContent = glyph || '';
        pin.appendChild(g);
        wrap.appendChild(pin);
        return wrap;
    },

    /* The brand pin (the logo's heart in a map pin) for the route's two ends:
       'start' (volunteer: white pin, blue heart), 'end' (client: logo-gradient
       pin, white heart), 'live' (start + a pulse while positions stream in). */
    heartPinEl(kind, labelKey) {
        this._uid += 1;
        const id = `gc-heart-grad-${this._uid}`;
        const end = kind === 'end';
        const pinPath = 'M18 44.5C18 44.5 3.5 29.6 3.5 17.8a14.5 14.5 0 0 1 29 0C32.5 29.6 18 44.5 18 44.5Z';
        const heartPath = 'M32 52.5C32 52.5 11.5 39.8 11.5 25.8C11.5 19.1 16.6 14 22.9 14C27 14 30.3 16.3 32 19.6C33.7 16.3 37 14 41.1 14C47.4 14 52.5 19.1 52.5 25.8C52.5 39.8 32 52.5 32 52.5Z';
        const grad = `url(#${id})`;
        const el = document.createElement('div');
        el.className = `gc-heart-pin gc-heart-pin--${kind}`;
        if (labelKey) {
            el.setAttribute('role', 'img');
            el.setAttribute('data-i18n-aria', labelKey);
            el.setAttribute('aria-label', this.t(labelKey));
        }
        el.innerHTML = `<svg viewBox="0 0 36 46" width="36" height="46" aria-hidden="true" focusable="false">`
            + `<defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2A3BA5"/><stop offset="1" stop-color="#006a61"/></linearGradient></defs>`
            + `<path d="${pinPath}" fill="${end ? grad : '#fff'}" stroke="${end ? '#fff' : grad}" stroke-width="2.5"/>`
            + `<path d="${heartPath}" transform="translate(18 18.4) scale(0.36) translate(-32 -33.2)" fill="${end ? '#fff' : grad}"/>`
            + '</svg>';
        return el;
    },

    marker(map, latlng, el, extra) {
        const m = new maplibregl.Marker(Object.assign({ element: el, anchor: 'bottom' }, extra || {}))
            .setLngLat([latlng[1], latlng[0]]);
        return m.addTo(map);
    },

    /* Glides a marker to [lat, lng] (≈0.9 s) instead of jumping — live
       positions arrive every 10–15 s. Jumps when motion is reduced or the
       move is a teleport (> 2 km: a new fix after a gap). */
    moveMarker(marker, latlng, ms) {
        const to = { lng: latlng[1], lat: latlng[0] };
        if (marker.__gcAnim) cancelAnimationFrame(marker.__gcAnim);
        const from = marker.getLngLat();
        if (this._reducedMotion() || this.haversineKm([from.lat, from.lng], [to.lat, to.lng]) > 2) {
            marker.setLngLat([to.lng, to.lat]);
            return;
        }
        const duration = ms || 900;
        const start = performance.now();
        const step = (now) => {
            const t = Math.min(1, (now - start) / duration);
            const k = t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2;   // ease-in-out
            marker.setLngLat([from.lng + (to.lng - from.lng) * k, from.lat + (to.lat - from.lat) * k]);
            marker.__gcAnim = t < 1 ? requestAnimationFrame(step) : null;
        };
        marker.__gcAnim = requestAnimationFrame(step);
    },

    fitPoints(map, points, opts) {
        opts = opts || {};
        const pts = (points || []).filter((p) => p && isFinite(p[0]) && isFinite(p[1]));
        if (!pts.length) return;
        if (pts.length === 1) {
            map.jumpTo({ center: [pts[0][1], pts[0][0]], zoom: Math.min(opts.maxZoom || 15, 15) });
            return;
        }
        const bounds = new maplibregl.LngLatBounds([pts[0][1], pts[0][0]], [pts[0][1], pts[0][0]]);
        pts.forEach((p) => bounds.extend([p[1], p[0]]));
        map.fitBounds(bounds, {
            padding: opts.padding != null ? opts.padding : 48,
            maxZoom: opts.maxZoom || 15,
            animate: !!opts.animate && !this._reducedMotion(),
        });
    },

    /* ---------- route line (under the buildings) ---------- */

    ROUTE_ALT_COLOR: '#7b8394',
    ROUTE_DASH: [1, 2],   // on foot: a dashed line, so it never reads as the car route

    /* geometry: [[lat, lng], ...] of the chosen route, or null to clear.
       opts: dashed (walking), alternatives [{index, geometry}] — grey,
       half-transparent, under the main line; a click on one calls
       opts.onPick(index). Blue #2A3BA5 line, white casing, round caps; every
       layer goes in with beforeId = the first building layer, so no line
       ever draws over a house. Updates go through setData — the sources and
       layers are created once per style, never re-added. */
    setRoute(map, geometry, opts) {
        opts = opts || {};
        const line = (pts) => (pts || []).map((p) => [p[1], p[0]]);
        const coords = line(geometry);
        map.__gcRouteData = {
            type: 'FeatureCollection',
            features: coords.length > 1 ? [{ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: coords } }] : [],
        };
        map.__gcRouteAltData = {
            type: 'FeatureCollection',
            features: (opts.alternatives || []).filter((a) => a.geometry && a.geometry.length > 1).map((a) => ({
                type: 'Feature', properties: { idx: a.index }, geometry: { type: 'LineString', coordinates: line(a.geometry) },
            })),
        };
        map.__gcRouteDashed = !!opts.dashed;
        map.__gcRoutePick = opts.onPick || null;
        const self = this;
        const paintDash = () => {
            if (!map.getLayer('gc-route-line')) return;
            map.setPaintProperty('gc-route-line', 'line-dasharray', map.__gcRouteDashed ? self.ROUTE_DASH : undefined);
        };
        if (map.__gcRouteBound) {
            if (map.__gcStyleReady) {
                const src = map.getSource('gc-route');
                const alt = map.getSource('gc-route-alt');
                if (src) src.setData(map.__gcRouteData);
                if (alt) alt.setData(map.__gcRouteAltData);
                paintDash();
            }
            return;
        }
        map.__gcRouteBound = true;
        this.onStyle(map, () => {
            const before = map.__gcBuildings && map.__gcBuildings.below;
            [['gc-route-alt', map.__gcRouteAltData], ['gc-route', map.__gcRouteData]].forEach(([id, data]) => {
                if (!map.getSource(id)) map.addSource(id, { type: 'geojson', data });
                else map.getSource(id).setData(data);
            });
            const layout = { 'line-cap': 'round', 'line-join': 'round' };
            // Order matters: each layer is inserted right under `before`, so the
            // later ones sit on top — alternatives, then casing, then the line.
            if (!map.getLayer('gc-route-alt')) {
                map.addLayer({
                    id: 'gc-route-alt', type: 'line', source: 'gc-route-alt', layout,
                    paint: {
                        'line-color': this.ROUTE_ALT_COLOR,
                        'line-opacity': 0.55,
                        'line-width': ['interpolate', ['linear'], ['zoom'], 10, 4, 16, 5],
                    },
                }, before);
                // A wide invisible twin: the grey line is easy to tap on a phone.
                map.addLayer({
                    id: 'gc-route-alt-hit', type: 'line', source: 'gc-route-alt', layout,
                    paint: { 'line-color': '#000', 'line-opacity': 0, 'line-width': 22 },
                }, before);
            }
            if (!map.getLayer('gc-route-casing')) {
                map.addLayer({
                    id: 'gc-route-casing', type: 'line', source: 'gc-route', layout,
                    paint: {
                        'line-color': this.ROUTE_CASING,
                        'line-width': ['interpolate', ['linear'], ['zoom'], 10, 8, 16, 10],
                        'line-opacity': 0.95,
                    },
                }, before);
            }
            if (!map.getLayer('gc-route-line')) {
                map.addLayer({
                    id: 'gc-route-line', type: 'line', source: 'gc-route', layout,
                    paint: {
                        'line-color': this.ROUTE_COLOR,
                        'line-width': ['interpolate', ['linear'], ['zoom'], 10, 5, 16, 6],
                    },
                }, before);
            }
            paintDash();
        });
        // Layer listeners are keyed by layer id, so they survive a style swap.
        map.on('click', 'gc-route-alt-hit', (e) => {
            const f = e.features && e.features[0];
            if (f && map.__gcRoutePick) map.__gcRoutePick(Number(f.properties.idx));
        });
        map.on('mouseenter', 'gc-route-alt-hit', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'gc-route-alt-hit', () => { map.getCanvas().style.cursor = ''; });
    },

    /* Live-fix accuracy as a translucent circle (a 48-gon in metres). */
    setAccuracy(map, latlng, radiusM) {
        const features = [];
        if (latlng && radiusM > 0) {
            const lat0 = latlng[0];
            const kx = 111320 * Math.cos((lat0 * Math.PI) / 180);
            const ring = [];
            for (let i = 0; i <= 48; i += 1) {
                const a = (i / 48) * 2 * Math.PI;
                ring.push([latlng[1] + (radiusM * Math.cos(a)) / kx, lat0 + (radiusM * Math.sin(a)) / 110540]);
            }
            features.push({ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ring] } });
        }
        map.__gcAccData = { type: 'FeatureCollection', features };
        if (map.__gcAccBound) {
            const src = map.__gcStyleReady && map.getSource('gc-accuracy');
            if (src) src.setData(map.__gcAccData);
            return;
        }
        map.__gcAccBound = true;
        this.onStyle(map, () => {
            const color = this.resolveColor('--primary-2', '#006a61');
            const before = map.__gcBuildings && map.__gcBuildings.below;
            if (!map.getSource('gc-accuracy')) map.addSource('gc-accuracy', { type: 'geojson', data: map.__gcAccData });
            else map.getSource('gc-accuracy').setData(map.__gcAccData);
            if (!map.getLayer('gc-accuracy-fill')) {
                map.addLayer({ id: 'gc-accuracy-fill', type: 'fill', source: 'gc-accuracy', paint: { 'fill-color': color, 'fill-opacity': 0.12 } }, before);
                map.addLayer({ id: 'gc-accuracy-line', type: 'line', source: 'gc-accuracy', paint: { 'line-color': color, 'line-opacity': 0.45, 'line-width': 1 } }, before);
            }
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

    /* ---------- simple maps ---------- */

    /* One read-only point (task / SOS / pet location card). pin: {color, glyph, cls}. */
    initPointMap(target, lat, lng, pin) {
        if (!isFinite(lat) || !isFinite(lng)) return null;
        const map = this.createMap(target, { center: [lat, lng], zoom: 15 });
        if (!map) return null;
        pin = pin || {};
        this.marker(map, [lat, lng], this.pinEl(pin.color || '--primary', pin.glyph || '', pin.cls || ''));
        return map;
    },

    /* A single-marker picker map bound to two hidden numeric inputs. Click the
       map or drag the marker to set coordinates; an optional "use my
       location" button fills them from navigator.geolocation. Starts flat
       (2D) — easier to place a point precisely; 3D stays one click away. */
    initPicker(containerId, latInputId, lngInputId, opts) {
        opts = opts || {};
        const latInput = document.getElementById(latInputId);
        const lngInput = document.getElementById(lngInputId);
        const container = document.getElementById(containerId);
        if (!container || !latInput || !lngInput) return null;

        const hasStart = latInput.value && lngInput.value;
        const startLat = hasStart ? parseFloat(latInput.value) : this.DEFAULT_CENTER[0];
        const startLng = hasStart ? parseFloat(lngInput.value) : this.DEFAULT_CENTER[1];
        const map = this.createMap(container, {
            center: [startLat, startLng], zoom: hasStart ? 15 : this.DEFAULT_ZOOM, start3d: false,
        });
        if (!map) return null;

        let marker = null;
        const setMarker = (lat, lng) => {
            lat = Number(lat.toFixed(6));
            lng = Number(lng.toFixed(6));
            if (marker) {
                marker.setLngLat([lng, lat]);
            } else {
                marker = this.marker(map, [lat, lng], this.pinEl('--primary'), { draggable: true });
                marker.on('dragend', () => {
                    const pos = marker.getLngLat();
                    setMarker(pos.lat, pos.lng);
                });
            }
            latInput.value = lat;
            lngInput.value = lng;
        };

        if (hasStart) setMarker(startLat, startLng);
        map.on('click', (e) => setMarker(e.lngLat.lat, e.lngLat.lng));

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
                            map.jumpTo({ center: [longitude, latitude], zoom: 16 });
                            setMarker(latitude, longitude);
                            locateBtn.disabled = false;
                        },
                        () => {
                            locateBtn.disabled = false;
                            const table = (window.T && window.T[document.documentElement.lang]) || {};
                            alert(table[locateBtn.dataset.errorKey] || locateBtn.dataset.errorText || 'Could not determine your location.');
                        },
                    );
                });
            }
        }

        return map;
    },

    /* Analytics heatmap: a flat overview of Tajikistan with MapLibre's native
       heatmap layer. points: [[lat, lng, weight], ...]; colors: three CSS
       colours, low → high. Returns { map, setPoints(points, colors) }. */
    createHeatmap(target, opts) {
        opts = opts || {};
        const map = this.createMap(target, {
            center: opts.center || [38.6, 70.8], zoom: opts.zoom || 5.6, allow3d: false, scrollZoom: false,
        });
        if (!map) return null;
        const self = this;
        let data = { type: 'FeatureCollection', features: [] };
        // Light → dark ramp; the analytics page passes the activity calendar's
        // scale (GCChartTheme.heatColors()), any length ≥ 2 works.
        let colors = opts.colors || ['#C7CDF0', '#8B96DE', '#5361C7', '#2A3BA5'];
        this.onStyle(map, () => {
            if (!map.getSource('gc-heat')) map.addSource('gc-heat', { type: 'geojson', data });
            const before = map.__gcBuildings && map.__gcBuildings.below;
            if (!map.getLayer('gc-heat')) {
                map.addLayer({
                    id: 'gc-heat', type: 'heatmap', source: 'gc-heat', maxzoom: 15,
                    paint: {
                        'heatmap-weight': ['get', 'w'],
                        'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 5, 0.9, 12, 2],
                        'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 5, 16, 9, 26, 13, 40],
                        'heatmap-opacity': 0.85,
                    },
                }, before);
            }
            paint();
        });
        function paint() {
            if (!map.__gcStyleReady || !map.getLayer('gc-heat')) return;
            const ramp = ['interpolate', ['linear'], ['heatmap-density'], 0, 'rgba(0,0,0,0)'];
            colors.forEach((c, i) => ramp.push(0.2 + (0.8 * i) / Math.max(1, colors.length - 1), c));
            map.setPaintProperty('gc-heat', 'heatmap-color', ramp);
        }
        return {
            map,
            setPoints(points, nextColors) {
                if (nextColors) colors = nextColors;
                const max = Math.max(1, ...(points || []).map((p) => p[2] || 0));
                data = {
                    type: 'FeatureCollection',
                    features: (points || []).map((p) => ({
                        type: 'Feature', properties: { w: (p[2] || 0) / max },
                        geometry: { type: 'Point', coordinates: [p[1], p[0]] },
                    })),
                };
                const src = map.__gcStyleReady && map.getSource('gc-heat');
                if (src) src.setData(data);
                paint();
                if (points && points.length) self.fitPoints(map, points, { padding: 24, maxZoom: 11 });
            },
            // Theme switch: recolour without refitting the view.
            setColors(nextColors) {
                if (nextColors) colors = nextColors;
                paint();
            },
        };
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
        const map = this.createMap(containerId, { fullscreen: true });
        if (!map) return null;
        // Live volunteer positions (feature 7): their own marker set, so render()
        // (filters, data reloads) never wipes them. Fed by the staff-only
        // `map.marker {kind:"volunteer_live"}` WS event; harmless elsewhere.
        const live = this._liveMarkerLayer(map, opts);

        let allPoints = [];
        let predicate = () => true;
        let markers = [];
        let meMarker = null;
        const self = this;

        function render() {
            markers.forEach((m) => m.remove());
            markers = [];
            const bounds = [];
            allPoints.forEach((point) => {
                if (point.kind !== 'me' && !predicate(point)) return;
                const style = self._opsStyle(point);
                const el = self.pinEl(style.color, style.glyph, style.cls);
                el.addEventListener('click', (e) => { e.stopPropagation(); if (opts.onSelect) opts.onSelect(point); });
                markers.push(self.marker(map, [point.lat, point.lng], el));
                bounds.push([point.lat, point.lng]);
            });
            if (bounds.length) self.fitPoints(map, bounds, { padding: 48, maxZoom: 14 });
        }

        function locate() {
            if (!navigator.geolocation) {
                if (opts.onLocate) opts.onLocate('unavailable');
                return;
            }
            if (opts.onLocate) opts.onLocate('loading');
            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    const lat = pos.coords.latitude;
                    const lng = pos.coords.longitude;
                    if (meMarker) meMarker.setLngLat([lng, lat]);
                    else meMarker = self.marker(map, [lat, lng], self.pinEl('--primary', '•'));
                    map.easeTo({ center: [lng, lat], zoom: 15, duration: self._reducedMotion() ? 0 : 700 });
                    if (opts.onLocate) opts.onLocate('success', { lat, lng });
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
                    if (opts.onLocate) opts.onLocate(err && err.code === 1 ? 'denied' : 'unavailable');
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
         preference   — 'fastest' | 'shortest' (default: the server's — fastest by car, shortest on foot)
         stepsEl      (id|element) — an <ol>/<ul> the step list is rendered into
         live         — default true: follow GCRealtime location.updated / tracking.stopped
         onRoute(data)          — after each successful fetch and each variant pick (metrics:
                                  data.distance_km, data.eta_min, data.eta_estimated, data.deep_links,
                                  data.origin.source; data.routes = all variants, data.selected = the
                                  drawn one, data.shortest_index; data.success false = no road route,
                                  data.reason says why — nothing is drawn between the pins)
         onState(state, detail) — 'loading' | 'ready' | 'empty' {reason} | 'error' |
                                  'rerouting' | 'live' {lat,lng,ts} | 'live_stopped' {reason}
         onStep(index, step, distanceM) — current (next) maneuver changed / distance updated
       Returns { load(origin?), setProfile(p), setOptions({profile, preference}),
                 select(index), updatePosition(lat, lng, extra?), centerOn(lat, lng),
                 map, data, profile, preference, currentStep, destroy() }. */
    createRouteNav(opts) {
        opts = opts || {};
        const self = this;
        const mapEl = this._el(opts.mapEl);
        if (!mapEl || !opts.routeUrl) return null;
        if (!this.available()) { mapEl.hidden = false; this.showError(mapEl, 'map.error_load'); return null; }
        const stepsEl = this._el(opts.stepsEl);
        const emit = (state, detail) => { if (opts.onState) { try { opts.onState(state, detail || {}); } catch (e) { /* UI bug must not stop nav */ } } };

        let map = null;
        let originMarker = null;
        let destMarker = null;
        let liveMarker = null;
        let data = null;
        let raw = null;             // the last server answer (all variants)
        let profile = opts.profile === 'walking' ? 'walking' : 'driving';
        let preference = opts.preference === 'shortest' || opts.preference === 'fastest' ? opts.preference : null;
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
            map = self.createMap(mapEl, { fullscreen: true });
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
            if (!ensureMap()) return;
            const o = [data.origin.lat, data.origin.lng];
            const d = [data.destination.lat, data.destination.lng];
            if (!originMarker) originMarker = self.marker(map, o, self.heartPinEl('start', 'route.pin_volunteer'));
            else originMarker.setLngLat([o[1], o[0]]);
            if (!destMarker) destMarker = self.marker(map, d, self.heartPinEl('end', 'route.pin_client'));
            else destMarker.setLngLat([d[1], d[0]]);
            // The live marker replaces the origin pin once positions stream in.
            originMarker.getElement().style.visibility = liveMarker ? 'hidden' : '';
            const pts = [o, d];
            // Only a real road route is drawn — never a straight line between
            // the pins; the page says the road route is unavailable instead.
            const routed = !!(data.success && data.geometry && data.geometry.length > 1);
            const alternatives = routed ? (data.routes || [])
                .map((r, index) => ({ index, geometry: r.geometry }))
                .filter((a) => a.index !== data.selected) : [];
            self.setRoute(map, routed ? data.geometry : null, {
                dashed: data.profile === 'walking',
                alternatives,
                onPick: (index) => select(index),
            });
            if (routed) data.geometry.forEach((pt) => pts.push(pt));
            if (!fitted) {
                self.fitPoints(map, pts, { padding: 56, maxZoom: 16 });
                fitted = true;
            }
            renderSteps();
            if (lastPosition) track(lastPosition, true);
        }

        /* The variant `index` of the last answer becomes the drawn route
           (steps, metrics, off-route checks all follow it). */
        function view(json, index) {
            const routes = json.routes || [];
            if (!routes.length) return Object.assign({}, json, { selected: 0 });
            const i = Math.max(0, Math.min(routes.length - 1, index || 0));
            const r = routes[i];
            return Object.assign({}, json, {
                selected: i,
                distance_km: r.distance_km,
                duration_min: r.duration_min,
                eta_min: r.eta_min != null ? r.eta_min : r.duration_min,
                geometry: r.geometry,
                steps: r.steps || [],
            });
        }

        function select(index) {
            if (!raw || !raw.routes || !raw.routes[index] || (data && data.selected === index)) return;
            data = view(raw, index);
            render();
            if (lastPosition) track(lastPosition, true);
            if (opts.onRoute) { try { opts.onRoute(data); } catch (e) { /* ignore */ } }
        }

        function load(origin) {
            if (destroyed) return Promise.resolve(null);
            const params = new URLSearchParams({ profile });
            if (preference) params.set('preference', preference);
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
                    raw = json;
                    data = view(json, 0);
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
                // No road route yet: retry (and refresh distance/ETA) as the volunteer moves.
                off = self._metersBetween(pos, [data.origin.lat, data.origin.lng]) > self.NAV_OFF_ROUTE_M;
            }
            if (!off) return;
            lastRerouteAt = now;
            load(opts.isVolunteer ? { lat: pos[0], lng: pos[1] } : null);
        }

        function track(pos, quiet) {
            if (!data || !data.steps || !data.steps.length) {
                if (!quiet) maybeReroute(pos);
                return;
            }
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
            if (!ensureMap()) return;
            if (!liveMarker) {
                liveMarker = self.marker(map, pos, self.heartPinEl('live', 'route.pin_volunteer'));
                if (originMarker) originMarker.getElement().style.visibility = 'hidden';
            } else {
                self.moveMarker(liveMarker, pos);
            }
            if (extra.accuracy && extra.accuracy > 0) self.setAccuracy(map, pos, extra.accuracy);
            emit('live', { lat: pos[0], lng: pos[1], ts: extra.ts || null, accuracy: extra.accuracy || null });
            track(pos, false);
        }

        function clearLive(reason) {
            if (liveMarker) liveMarker.remove();
            if (map) self.setAccuracy(map, null, 0);
            liveMarker = null;
            lastPosition = null;
            if (originMarker) originMarker.getElement().style.visibility = '';
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
                return this.setOptions({ profile: p });
            },
            /* Change travel mode and/or shorter↔faster with one request. */
            setOptions(o) {
                o = o || {};
                const nextProfile = o.profile ? (o.profile === 'walking' ? 'walking' : 'driving') : profile;
                const nextPref = o.preference === 'shortest' || o.preference === 'fastest' ? o.preference : preference;
                if (nextProfile === profile && nextPref === preference && data) return Promise.resolve(data);
                if (nextProfile !== profile) fitted = false;
                profile = nextProfile;
                preference = nextPref;
                const origin = opts.isVolunteer && lastPosition ? { lat: lastPosition[0], lng: lastPosition[1] } : lastOrigin;
                return load(origin);
            },
            select,
            updatePosition,
            clearLive,
            centerOn(lat, lng, zoom) {
                if (ensureMap()) map.jumpTo({ center: [lng, lat], zoom: zoom || 15 });
            },
            get map() { return map; },
            get data() { return data; },
            get profile() { return profile; },
            get preference() { return (data && data.preference) || preference; },
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
        const markers = new Map();   // task_id -> {marker, seen, point}

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
            if (entry) entry.marker.remove();
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
                const el = self.pinEl('--primary-2', 'V', 'gc-pin--live');
                entry = { marker: self.marker(map, [p.lat, p.lng], el), seen: Date.now(), point: null };
                el.addEventListener('click', (e) => { e.stopPropagation(); if (opts.onSelect) opts.onSelect(entry.point); });
                markers.set(taskId, entry);
            } else {
                self.moveMarker(entry.marker, [p.lat, p.lng]);
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
            if (entry) {
                map.easeTo({
                    center: entry.marker.getLngLat(), zoom: Math.max(map.getZoom(), 15),
                    duration: self._reducedMotion() ? 0 : 700,
                });
            }
            return !!entry;
        }, get size() { return markers.size; } };
    },
};

/* If MapLibre itself never loaded (its CDN and the local fallback both blocked
   on this visitor's network), every map container would stay blank — show the
   localized message instead. */
document.addEventListener('DOMContentLoaded', () => {
    if (GCMap.available()) return;
    document.querySelectorAll('.ops-map__canvas, .map-mini:not([hidden]), .map-picker, .analytics-heat')
        .forEach((el) => GCMap.showError(el, 'map.error_load'));
});
