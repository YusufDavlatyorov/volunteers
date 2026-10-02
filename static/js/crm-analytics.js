/* CRM analytics charts (Feature Pack v2, feature 10).
 *
 * Reads the server payload (#analyticsData, built by services/analytics_v2 —
 * aggregate queries, cached 5 min) and draws one Chart.js chart per
 * .chart-card[data-chart]. Colours come from CSS tokens on .analytics-root
 * (selected per theme, palette validated for colour-blind safety); labels and
 * series names from window.T (i18n.js) in the current language. A theme or
 * language switch (data-theme / lang on <html>) restyles every chart in
 * place. Each card also gets a table view (accessibility + the contrast
 * relief for the lighter series colours). Rules: one y-axis, thin marks,
 * legend only for ≥ 2 series, hover tooltips, no number on every point.
 */
(function () {
    'use strict';

    const root = document.getElementById('analyticsRoot');
    const dataNode = document.getElementById('analyticsData');
    if (!root || !dataNode) return;
    let payload;
    try { payload = JSON.parse(dataNode.textContent); } catch (e) { return; }

    function lang() {
        const l = document.documentElement.getAttribute('lang') || 'ru';
        return window.T && window.T[l] ? l : 'ru';
    }

    function t(key, fallback) {
        if (!key) return fallback;
        const table = (window.T && window.T[lang()]) || {};
        const en = (window.T && window.T.en) || {};
        return table[key] || en[key] || fallback || key;
    }

    function token(name) {
        return getComputedStyle(root).getPropertyValue(name).trim();
    }

    function pad(n) { return String(n).padStart(2, '0'); }

    function dateLabel(iso, weekly) {
        const parts = iso.split('-');
        if (parts.length !== 3) return iso;
        const d = `${parts[2]}.${parts[1]}`;
        if (!weekly) return d;
        const end = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2] + 6));
        return `${d}–${pad(end.getUTCDate())}.${pad(end.getUTCMonth() + 1)}`;
    }

    // Long category names wrap onto two lines on a narrow card instead of
    // being cut off at the axis edge (Chart.js draws an array as lines).
    function wrap(text, max) {
        if (text.length <= max || text.indexOf(' ') < 0) return text;
        const words = text.split(' ');
        let first = '';
        while (words.length && (first + ' ' + words[0]).trim().length <= max) first = (first + ' ' + words.shift()).trim();
        if (!first) first = words.shift();
        return words.length ? [first, words.join(' ')] : first;
    }

    function labelsOf(chart, forAxis) {
        if (chart.label_keys) {
            const narrow = forAxis && window.innerWidth < 700;
            return chart.labels.map((code, i) => {
                const text = t(chart.label_keys[i], code);
                return narrow ? wrap(text, 14) : text;
            });
        }
        return chart.labels.map((iso) => dateLabel(iso, payload.weekly));
    }

    function isEmpty(chart) {
        return !chart.series.length || chart.series.every((s) => s.values.every((v) => !v));
    }

    function colors() {
        return {
            series: [token('--series-1'), token('--series-2'), token('--series-3'), token('--series-4')],
            text: token('--chart-text'),
            muted: token('--chart-muted'),
            grid: token('--chart-grid'),
            axis: token('--chart-axis'),
            surface: getComputedStyle(document.body).getPropertyValue('--card-bg').trim() || '#fff',
        };
    }

    function datasets(chart, c) {
        return chart.series.map((s, i) => {
            const color = c.series[i % c.series.length];
            const base = { label: t(s.label_key, s.key), data: s.values, borderColor: color, backgroundColor: color };
            if (chart.kind === 'line') {
                return Object.assign(base, {
                    // monotone: smooth but never overshoots below 0 / above the data
                    borderWidth: 2, cubicInterpolationMode: 'monotone', spanGaps: true,
                    pointRadius: chart.labels.length > 14 ? 0 : 3, pointHoverRadius: 5,
                    pointBackgroundColor: color, pointBorderColor: c.surface, pointBorderWidth: 2,
                });
            }
            return Object.assign(base, {
                borderWidth: 0, borderRadius: 4, borderSkipped: 'start',
                maxBarThickness: 26, categoryPercentage: 0.75, barPercentage: 0.9,
            });
        });
    }

    function options(chart, c) {
        const horizontal = chart.kind === 'hbar';
        const valueAxis = {
            beginAtZero: true,
            grid: { color: c.grid, drawTicks: false },
            border: { display: false },
            ticks: { color: c.muted, precision: 0, padding: 6, font: { size: 11 } },
        };
        if (chart.y_max) Object.assign(valueAxis, { max: chart.y_max, ticks: Object.assign(valueAxis.ticks, { stepSize: 1 }) });
        const categoryAxis = {
            grid: { display: false },
            border: { color: c.axis },
            // Category names are never skipped (the card grows instead); dates may be.
            ticks: { color: c.muted, font: { size: 11 }, autoSkip: !chart.label_keys, maxRotation: 0, autoSkipPadding: 10 },
        };
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? false : { duration: 300 },
            indexAxis: horizontal ? 'y' : 'x',
            interaction: chart.kind === 'line' ? { mode: 'index', intersect: false } : { mode: 'nearest', intersect: true },
            layout: { padding: { top: 4, right: 8 } },
            scales: horizontal ? { x: valueAxis, y: categoryAxis } : { x: categoryAxis, y: valueAxis },
            plugins: {
                legend: {
                    display: chart.series.length > 1,
                    position: 'bottom',
                    labels: { color: c.text, usePointStyle: true, pointStyle: 'circle', boxWidth: 8, boxHeight: 8, padding: 14, font: { size: 12 } },
                },
                tooltip: {
                    backgroundColor: c.surface, titleColor: c.text, bodyColor: c.text,
                    borderColor: c.axis, borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
                },
            },
        };
    }

    function buildTable(card, chart) {
        const holder = card.querySelector('[data-chart-table]');
        if (!holder) return;
        const labels = labelsOf(chart);
        const table = document.createElement('table');
        table.className = 'analytics-table';
        const head = document.createElement('tr');
        const first = document.createElement('th');
        first.textContent = t(chart.label_keys ? 'an.col.category' : 'an.col.period', '');
        head.appendChild(first);
        chart.series.forEach((s) => {
            const th = document.createElement('th');
            th.className = 'num';
            th.textContent = t(s.label_key, s.key);
            head.appendChild(th);
        });
        const thead = document.createElement('thead');
        thead.appendChild(head);
        const tbody = document.createElement('tbody');
        labels.forEach((label, i) => {
            const tr = document.createElement('tr');
            const cell = document.createElement('td');
            cell.textContent = label;
            cell.setAttribute('data-label', first.textContent);
            tr.appendChild(cell);
            chart.series.forEach((s) => {
                const td = document.createElement('td');
                td.className = 'num';
                const v = s.values[i];
                td.textContent = v === null || v === undefined ? '—' : String(v);
                td.setAttribute('data-label', t(s.label_key, s.key));
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(thead);
        table.appendChild(tbody);
        holder.replaceChildren(table);
    }

    const instances = [];

    function draw() {
        const c = colors();
        document.querySelectorAll('.chart-card[data-chart]').forEach((card) => {
            const chart = payload.charts[card.dataset.chart];
            if (!chart) return;
            const canvas = card.querySelector('canvas');
            const empty = card.querySelector('.chart-empty');
            buildTable(card, chart);
            if (isEmpty(chart)) {
                canvas.parentElement.hidden = true;
                if (empty) empty.hidden = false;
                return;
            }
            if (typeof window.Chart !== 'function') return;
            if (chart.kind === 'hbar') {
                // One readable row per category: 28 px each + axis/legend room.
                const rows = chart.labels.length * Math.max(chart.series.length, 1);
                canvas.parentElement.style.height = `${Math.max(200, rows * 22 + chart.labels.length * 10 + 70)}px`;
            }
            const config = {
                type: chart.kind === 'line' ? 'line' : 'bar',
                data: { labels: labelsOf(chart, true), datasets: datasets(chart, c) },
                options: options(chart, c),
            };
            instances.push({ chart, instance: new window.Chart(canvas, config) });
        });
    }

    function restyle() {
        const c = colors();
        instances.forEach(({ chart, instance }) => {
            instance.data.labels = labelsOf(chart, true);
            const fresh = datasets(chart, c);
            instance.data.datasets.forEach((ds, i) => Object.assign(ds, fresh[i]));
            instance.options = options(chart, c);
            instance.update('none');
        });
        document.querySelectorAll('.chart-card[data-chart]').forEach((card) => {
            const chart = payload.charts[card.dataset.chart];
            if (chart) buildTable(card, chart);
        });
    }

    // CSV links carry the current UI language for the column / category names.
    function syncCsvLinks() {
        document.querySelectorAll('a[data-csv]').forEach((a) => {
            const url = new URL(a.href, window.location.href);
            url.searchParams.set('lang', lang());
            a.href = url.pathname + url.search;
        });
    }

    function heatmap() {
        const node = document.getElementById('analyticsHeat');
        if (!node || !window.L) return;
        const map = window.L.map(node, { scrollWheelZoom: false, attributionControl: true }).setView([38.6, 70.8], 6);
        if (typeof GCMap !== 'undefined' && GCMap.tileLayer) {
            GCMap.tileLayer(map);
        } else {
            window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 18, attribution: '&copy; OpenStreetMap',
            }).addTo(map);
        }
        fetch(node.dataset.url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then((r) => (r.ok ? r.json() : { points: [] }))
            .then((data) => {
                const points = data.points || [];
                if (!points.length || typeof window.L.heatLayer !== 'function') return;
                const max = Math.max(...points.map((p) => p[2]), 1);
                window.L.heatLayer(points.map((p) => [p[0], p[1], p[2] / max]), {
                    radius: 26, blur: 18, minOpacity: 0.5, maxZoom: 11,
                    gradient: { 0.3: token('--chart-heat-1'), 0.65: token('--chart-heat-2'), 1: token('--chart-heat-3') },
                }).addTo(map);
                map.fitBounds(points.map((p) => [p[0], p[1]]), { padding: [24, 24], maxZoom: 12 });
            })
            .catch(() => {});
    }

    function init() {
        if (window.Chart) {
            window.Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
        }
        draw();
        syncCsvLinks();
        heatmap();
        new MutationObserver(() => { restyle(); syncCsvLinks(); }).observe(document.documentElement, {
            attributes: true, attributeFilter: ['data-theme', 'lang'],
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
