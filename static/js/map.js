/* Shared Leaflet helpers for Generation Connect map pages.
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
            url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            max_zoom: 19,
            subdomains: 'abc',
        };
        try {
            const el = document.getElementById('gc-maps-tile');
            const cfg = el ? JSON.parse(el.textContent) : null;
            return (cfg && cfg.url) ? cfg : OSM;
        } catch (e) {
            return OSM;
        }
    },

    tileLayer(map) {
        const cfg = this.tileConfig();
        L.tileLayer(cfg.url, {
            maxZoom: cfg.max_zoom || 19,
            attribution: cfg.attribution || '',
            subdomains: cfg.subdomains || 'abc',
        }).addTo(map);
        return map;
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
            marker.bindPopup(
                `<div class="gc-popup">${titleHtml}${point.subtitle ? `<br><span class="gc-popup-sub">${point.subtitle}</span>` : ''}</div>`,
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
        };
    },
};
