/* Shared Leaflet helpers for Generation Connect map pages.
   Loaded on demand (not from base.html) by any page that embeds a map, right
   after the Leaflet CDN script. Requires the global `L` from Leaflet. */
const GCMap = {
    DEFAULT_CENTER: [38.5598, 68.7870], // Dushanbe
    DEFAULT_ZOOM: 12,

    tileLayer(map) {
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
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

    pinIcon(colorVar, glyph) {
        return L.divIcon({
            className: 'gc-pin-wrap',
            html: `<span class="gc-pin" style="--pin-color: var(${colorVar})"><span class="gc-pin-glyph">${glyph || ''}</span></span>`,
            iconSize: [28, 36],
            iconAnchor: [14, 34],
            popupAnchor: [0, -32],
        });
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
};
