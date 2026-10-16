/* About page — "volunteer network" section (templates/myapp/about.html #network).

   The landing page is the most-visited page, so MapLibre GL (~800 KB) and
   map.js are NOT loaded with it: this script fetches them only when the map
   is about to scroll into view (IntersectionObserver), with the same unpkg
   URLs + SRI and the same self-hosted fallback as the map pages
   (#gc-maplibre-lazy, myapp/templatetags/maplibre.py).

   Region list ↔ map: a click on a region (list or map label) highlights it in
   both places and flies the map there. The labels show "Region (N)" only when
   the server put `count` into #gc-network-map — the decision is made in
   services/network.py; this script never has the numbers otherwise. */
(function () {
    'use strict';
    const section = document.getElementById('network');
    const canvas = document.getElementById('networkMap');
    const cfgEl = document.getElementById('gc-maplibre-lazy');
    const dataEl = document.getElementById('gc-network-map');
    if (!section || !canvas || !cfgEl || !dataEl) return;

    let cfg, data;
    try {
        cfg = JSON.parse(cfgEl.textContent);
        data = JSON.parse(dataEl.textContent);
    } catch (e) {
        return;
    }
    const points = {};
    (data.points || []).forEach((p) => { points[p.key] = p; });
    const items = Array.from(section.querySelectorAll('.network-region'));
    const reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

    let map = null;
    let loading = null;
    let selected = null;
    const labels = {};

    /* ---------- lazy loading ---------- */

    function loadCss() {
        if (document.querySelector('link[data-gc-maplibre]')) return;
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = cfg.css;
        link.integrity = cfg.css_sri;
        link.crossOrigin = 'anonymous';
        link.setAttribute('data-gc-maplibre', '');
        link.onerror = () => { link.onerror = null; link.removeAttribute('integrity'); link.href = cfg.css_local; };
        document.head.appendChild(link);
    }

    function loadScript(src, integrity) {
        return new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = src;
            if (integrity) { script.integrity = integrity; script.crossOrigin = 'anonymous'; }
            script.onload = resolve;
            script.onerror = reject;
            document.head.appendChild(script);
        });
    }

    function hasGCMap() {
        return typeof GCMap !== 'undefined';   // map.js declares it with a top-level const
    }

    function load() {
        if (loading) return loading;
        loadCss();
        const maplibre = window.maplibregl
            ? Promise.resolve()
            : loadScript(cfg.js, cfg.js_sri).catch(() => loadScript(cfg.js_local));
        loading = maplibre
            .then(() => (hasGCMap() ? null : loadScript(cfg.map_js)))
            .then(init)
            .catch(() => {
                if (hasGCMap()) GCMap.showError(canvas, 'map.error_load');
                else canvas.classList.add('is-failed');
            });
        return loading;
    }

    /* ---------- map ---------- */

    function label(key) {
        const item = items.find((el) => el.dataset.region === key);
        const name = item && item.querySelector('.network-region__name');
        return name ? name.textContent.trim() : key;
    }

    function init() {
        map = GCMap.createMap(canvas, {
            center: [38.86, 71.25],
            zoom: 5.5,
            allow3d: false,
            cooperativeGestures: true,
        });
        if (!map) return;
        // Extra room on the left: Dushanbe's label sits west of its point
        // (anchor bottom-right) and would be clipped on a narrow phone map.
        if (data.bounds) map.fitBounds(data.bounds, { padding: { top: 44, right: 28, bottom: 24, left: 84 }, animate: false });

        Object.values(points).forEach((p) => {
            const dot = document.createElement('span');
            dot.className = 'network-dot';
            new maplibregl.Marker({ element: dot, anchor: 'center' }).setLngLat([p.lng, p.lat]).addTo(map);

            const pin = document.createElement('button');
            pin.type = 'button';
            pin.className = 'network-pin';
            pin.dataset.region = p.key;
            pin.setAttribute('aria-pressed', 'false');
            const name = document.createElement('span');
            name.setAttribute('data-i18n', 'region.' + p.key);   // relabelled by i18n.js setLanguage()
            name.textContent = label(p.key);
            pin.appendChild(name);
            if (typeof p.count === 'number') {
                const count = document.createElement('span');
                count.className = 'network-pin__count';
                count.textContent = '(' + p.count + ')';
                pin.appendChild(count);
            }
            pin.addEventListener('click', (e) => { e.stopPropagation(); select(p.key); });
            const offsets = { bottom: [0, -8], top: [0, 8], 'bottom-left': [6, -6], 'bottom-right': [-6, -6] };
            new maplibregl.Marker({ element: pin, anchor: p.anchor || 'bottom', offset: offsets[p.anchor] || [0, -8] })
                .setLngLat([p.lng, p.lat]).addTo(map);
            labels[p.key] = pin;
        });
        if (selected) select(selected);
    }

    /* ---------- list ↔ map ---------- */

    function select(key) {
        if (!points[key]) return;
        selected = key;
        items.forEach((el) => {
            const on = el.dataset.region === key;
            el.classList.toggle('is-active', on);
            el.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        Object.entries(labels).forEach(([k, el]) => {
            el.classList.toggle('is-active', k === key);
            el.setAttribute('aria-pressed', k === key ? 'true' : 'false');
        });
        if (!map) { load(); return; }
        const p = points[key];
        map.flyTo({ center: [p.lng, p.lat], zoom: p.zoom || 8, duration: reducedMotion ? 0 : 1200, essential: true });
    }

    items.forEach((el) => el.addEventListener('click', () => select(el.dataset.region)));

    if ('IntersectionObserver' in window) {
        const io = new IntersectionObserver((entries) => {
            if (entries.some((entry) => entry.isIntersecting)) {
                io.disconnect();
                load();
            }
        }, { rootMargin: '300px 0px' });
        io.observe(canvas);
    } else {
        load();
    }
})();
