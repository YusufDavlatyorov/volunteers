/* About / landing page behaviour (templates/myapp/about.html).

   1. Language: the page is server-rendered in Russian. On every language
      switch (base.html's `gc:language` event) this re-picks plural keys
      ([data-plural] + [data-n] → "<base>.<Intl.PluralRules category>") and
      re-formats numbers ([data-num], [data-num-digits]) for that language.
   2. Map: the regions map is a MapLibre map (static/js/map.js, follows the
      light/dark theme). Nothing map-related is loaded with the page: MapLibre
      (unpkg with SRI, our vendor copy as the fallback — the same pair as
      partials/_maplibre_js.html), map.css and map.js are fetched only when the
      map box comes near the viewport. One label per region (name + volunteer
      count); a region button or label flies the map there. */
(function () {
    'use strict';

    const LOCALES = { ru: 'ru-RU', tj: 'tg-TJ', en: 'en-GB' };
    const MAPLIBRE = {
        js: 'https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.js',
        jsIntegrity: 'sha384-5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp',
        css: 'https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.css',
        cssIntegrity: 'sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK',
    };
    // Tajikistan, west/south → east/north
    const BOUNDS = [[67.3, 36.6], [75.2, 41.1]];

    function lang() {
        let l = null;
        try { l = localStorage.getItem('gc-lang'); } catch (e) { /* storage blocked */ }
        l = l || document.documentElement.getAttribute('lang') || 'ru';
        return (window.T && window.T[l]) ? l : 'ru';
    }

    function t(key, args) {
        const table = (window.T && window.T[lang()]) || {};
        const ru = (window.T && window.T.ru) || {};
        let text = table[key] || ru[key] || key;
        if (args) text = text.replace(/\{(\w+)\}/g, (m, k) => (k in args ? String(args[k]) : m));
        return text;
    }

    function pluralKey(base, n) {
        let cat = 'other';
        try { cat = new Intl.PluralRules(LOCALES[lang()] || 'ru-RU').select(n); } catch (e) { /* old browser */ }
        const table = (window.T && window.T[lang()]) || {};
        return table[`${base}.${cat}`] ? `${base}.${cat}` : `${base}.other`;
    }

    function formatNumber(value, digits) {
        try {
            return new Intl.NumberFormat(LOCALES[lang()] || 'ru-RU', {
                minimumFractionDigits: digits, maximumFractionDigits: digits,
            }).format(value);
        } catch (e) {
            return String(value);
        }
    }

    function relabel() {
        document.querySelectorAll('.about-page [data-plural]').forEach((el) => {
            const key = pluralKey(el.dataset.plural, Number(el.dataset.n || 0));
            el.setAttribute('data-i18n', key);
            el.textContent = t(key);
        });
        document.querySelectorAll('.about-page [data-num]').forEach((el) => {
            const value = Number(el.dataset.num);
            if (Number.isFinite(value)) el.textContent = formatNumber(value, Number(el.dataset.numDigits || 0));
        });
        pins.forEach((pin) => { pin.label.textContent = pinText(pin.region); });
    }

    /* ---------- map ---------- */

    const box = document.getElementById('ab-map');
    const dataEl = document.getElementById('ab-regions-data');
    const regions = dataEl ? JSON.parse(dataEl.textContent) : [];
    const buttons = Array.from(document.querySelectorAll('.ab-region[data-region]'));
    const pins = [];
    let map = null;
    let pending = null; // a region picked before the map was ready

    // map.js declares `const GCMap` — a global binding, but not a window property.
    function gc() { return typeof GCMap !== 'undefined' ? GCMap : null; }

    function pinText(region) {
        return `${t('region.' + region.key)} (${formatNumber(region.count, 0)})`;
    }

    function loadScript(src, integrity) {
        return new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = src;
            if (integrity) { s.integrity = integrity; s.crossOrigin = ''; }
            s.onload = resolve;
            s.onerror = reject;
            document.head.appendChild(s);
        });
    }

    function loadCss(href, integrity, fallback) {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = href;
        if (integrity) {
            link.integrity = integrity;
            link.crossOrigin = '';
            link.onerror = () => { link.onerror = null; link.removeAttribute('integrity'); link.href = fallback; };
        }
        document.head.appendChild(link);
    }

    function select(key, fly) {
        buttons.forEach((b) => b.classList.toggle('is-active', b.dataset.region === key));
        pins.forEach((p) => p.el.classList.toggle('is-active', p.region.key === key));
        if (!map) { pending = key; return; }
        const region = regions.find((r) => r.key === key);
        if (region && fly) {
            map.flyTo({ center: [region.lng, region.lat], zoom: 7, essential: false,
                        duration: gc() && gc()._reducedMotion() ? 0 : 1200 });
        }
    }

    function build() {
        const G = gc();
        if (!G || !G.available()) {
            if (G) G.showError(box, 'map.error_load');
            return;
        }
        map = G.createMap(box, { center: [38.86, 71.28], zoom: 5.3, allow3d: false, scrollZoom: false });
        if (!map) return;
        map.fitBounds(BOUNDS, { padding: 24, duration: 0 });
        // On touch screens one finger keeps scrolling the page; two fingers
        // still pan and zoom the map.
        if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) map.dragPan.disable();
        regions.forEach((region) => {
            // The marker root gets no CSS position (MapLibre positions it).
            const el = document.createElement('div');
            el.className = 'ab-pin';
            el.setAttribute('role', 'button');
            el.tabIndex = 0;
            const label = document.createElement('span');
            label.className = 'ab-pin__label';
            label.textContent = pinText(region);
            const tail = document.createElement('span');
            tail.className = 'ab-pin__tail';
            el.append(label, tail);
            el.addEventListener('click', () => select(region.key, true));
            el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(region.key, true); } });
            G.marker(map, [region.lat, region.lng], el);
            pins.push({ region, el, label });
        });
        if (pending) select(pending, true);
    }

    function loadMap() {
        loadCss(MAPLIBRE.css, MAPLIBRE.cssIntegrity, box.dataset.maplibreCss);
        loadCss(box.dataset.mapCss);
        loadScript(MAPLIBRE.js, MAPLIBRE.jsIntegrity)
            .catch(() => loadScript(box.dataset.maplibreJs))
            .then(() => loadScript(box.dataset.mapJs))
            .then(build)
            .catch(() => { if (gc()) gc().showError(box, 'map.error_load'); });
    }

    buttons.forEach((b) => b.addEventListener('click', () => select(b.dataset.region, true)));

    if (box && regions.length) {
        if ('IntersectionObserver' in window) {
            const io = new IntersectionObserver((entries) => {
                if (entries.some((e) => e.isIntersecting)) { io.disconnect(); loadMap(); }
            }, { rootMargin: '300px 0px' });
            io.observe(box);
        } else {
            loadMap();
        }
    }

    document.addEventListener('gc:language', relabel);
    relabel();
})();
