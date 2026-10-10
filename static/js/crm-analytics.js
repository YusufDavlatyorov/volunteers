/* CRM analytics charts (Feature Pack v2, feature 10; look: stage 4).
 *
 * Reads the server payload (#analyticsData — services/analytics_v2, aggregate
 * queries cached 5 min) and draws one Chart.js chart per .chart-card[data-chart].
 * Everything visual — palette, fonts, grid, legend, tooltip, number and date
 * formats — comes from static/js/charts-theme.js (GCChartTheme); this file only
 * decides what each card shows. The card's data-display (set by the view) is
 * line | bars | rank (horizontal, sorted by value) | stack | doughnut.
 * Box heights and the empty state are rendered by the server, so nothing jumps
 * when the charts arrive or the filters change. A theme or language switch
 * restyles every chart in place; each card also has a table view.
 */
(function () {
    'use strict';

    const root = document.getElementById('analyticsRoot');
    const dataNode = document.getElementById('analyticsData');
    const theme = window.GCChartTheme;
    if (!root || !dataNode || !theme) return;
    let payload;
    try { payload = JSON.parse(dataNode.textContent); } catch (e) { return; }
    const t = theme.t;
    const ISO = /^\d{4}-\d{2}-\d{2}$/;

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

    function isDates(chart) {
        return !chart.label_keys && chart.labels.length > 0 && ISO.test(chart.labels[0]);
    }

    // The chart in the visitor's language, in display order (rank/stack: by value, descending).
    function spec(chart, display) {
        const dates = isDates(chart);
        let order = chart.labels.map((_, i) => i);
        if (display === 'rank' || display === 'stack') {
            const weight = (i) => (display === 'stack'
                ? chart.series.reduce((sum, s) => sum + (s.values[i] || 0), 0)
                : (chart.series[0] ? chart.series[0].values[i] || 0 : 0));
            order = order.slice().sort((a, b) => weight(b) - weight(a) || a - b);
        }
        const names = order.map((i) => {
            const code = chart.labels[i];
            if (dates) return code;
            return chart.label_keys ? t(chart.label_keys[i], code) : code;
        });
        return {
            dates,
            categorical: !dates,
            codes: order.map((i) => chart.labels[i]),
            names,
            labels: names.map((name) => {
                if (dates) return theme.shortDate(name);
                return window.innerWidth < 700 && (display === 'rank' || display === 'stack') ? wrap(name, 16) : name;
            }),
            title: (index) => {
                if (!dates) return names[index];
                return payload.weekly ? theme.weekRange(names[index]) : theme.fullDate(names[index]);
            },
            series: chart.series.map((s) => ({
                key: s.key, label: t(s.label_key, s.key), values: order.map((i) => s.values[i]),
            })),
        };
    }

    function isEmpty(chart) {
        return !chart.series.length || chart.series.every((s) => s.values.every((v) => !v));
    }

    const DECIMALS = { ratings_timeline: 2 };

    function config(name, chart, display) {
        const s = spec(chart, display);
        const extra = { title: s.title, decimals: DECIMALS[name], max: chart.y_max };
        const type = display === 'line' ? 'line' : display === 'doughnut' ? 'doughnut' : 'bar';
        return {
            spec: s,
            type,
            data: { labels: s.labels, datasets: theme.datasets(s, display) },
            options: theme.options(s, display, extra),
        };
    }

    function buildTable(card, name, chart) {
        const holder = card.querySelector('[data-chart-table]');
        if (!holder) return;
        const s = spec(chart, card.dataset.display || 'line');
        const table = document.createElement('table');
        table.className = 'analytics-table';
        const head = document.createElement('tr');
        const first = document.createElement('th');
        first.textContent = t(chart.label_keys ? 'an.col.category' : 'an.col.period', '');
        head.appendChild(first);
        s.series.forEach((series) => {
            const th = document.createElement('th');
            th.className = 'num';
            th.textContent = series.label;
            head.appendChild(th);
        });
        const thead = document.createElement('thead');
        thead.appendChild(head);
        const tbody = document.createElement('tbody');
        s.names.forEach((label, i) => {
            const tr = document.createElement('tr');
            const cell = document.createElement('td');
            cell.textContent = s.dates ? s.title(i) : label;
            cell.setAttribute('data-label', first.textContent);
            tr.appendChild(cell);
            s.series.forEach((series) => {
                const td = document.createElement('td');
                td.className = 'num';
                td.textContent = theme.num(series.values[i], DECIMALS[name]);
                td.setAttribute('data-label', series.label);
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(thead);
        table.appendChild(tbody);
        holder.replaceChildren(table);
    }

    // Numbers rendered by the server (headline, KPI tiles, change chips) in
    // the visitor's language: thousands separators, decimal comma, %.
    function relabelNumbers() {
        root.querySelectorAll('[data-num]').forEach((el) => {
            const decimals = el.dataset.decimals === undefined ? undefined : parseInt(el.dataset.decimals, 10);
            el.textContent = theme.num(el.dataset.num, decimals);
        });
        root.querySelectorAll('[data-pct]').forEach((el) => {
            el.textContent = theme.pct(Number(el.dataset.pct));
        });
        root.querySelectorAll('.chart-delta[data-change]').forEach((el) => {
            const change = parseInt(el.dataset.change, 10);
            const dir = el.dataset.direction;
            const arrow = dir === 'up' ? '▲' : dir === 'down' ? '▼' : '=';
            const text = el.querySelector('.chart-delta__text');
            if (text) text.textContent = `${arrow} ${theme.pct(Math.abs(change))}`;
            const key = dir === 'up' ? 'an.delta_up' : dir === 'down' ? 'an.delta_down' : 'an.delta_same';
            const sr = el.querySelector('.visually-hidden');
            if (sr) sr.textContent = t(key).replace('{p}', theme.pct(Math.abs(change)));
        });
        root.querySelectorAll('[data-prev]').forEach((el) => {
            const decimals = el.dataset.decimals === undefined ? 0 : parseInt(el.dataset.decimals, 10);
            el.setAttribute('title', t('an.prev_value').replace('{n}', theme.num(el.dataset.prev, decimals)));
        });
    }

    const instances = [];

    function draw() {
        document.querySelectorAll('.chart-card[data-chart]').forEach((card) => {
            const name = card.dataset.chart;
            const chart = payload.charts[name];
            if (!chart) return;
            buildTable(card, name, chart);
            if (isEmpty(chart) || card.dataset.empty === '1') return;
            const canvas = card.querySelector('canvas');
            if (!canvas || typeof window.Chart !== 'function') return;
            const display = card.dataset.display || 'line';
            const c = config(name, chart, display);
            instances.push({ name, chart, display, instance: new window.Chart(canvas, { type: c.type, data: c.data, options: c.options }) });
        });
    }

    function restyle() {
        instances.forEach((item) => {
            const c = config(item.name, item.chart, item.display);
            const inst = item.instance;
            inst.data.labels = c.data.labels;
            inst.data.datasets.forEach((ds, i) => Object.assign(ds, c.data.datasets[i]));
            inst.options = c.options;
            inst.update('none');
        });
        document.querySelectorAll('.chart-card[data-chart]').forEach((card) => {
            const chart = payload.charts[card.dataset.chart];
            if (chart) buildTable(card, card.dataset.chart, chart);
        });
        relabelNumbers();
    }

    // CSV links carry the current UI language for the column / category names.
    function syncCsvLinks() {
        document.querySelectorAll('a[data-csv]').forEach((a) => {
            const url = new URL(a.href, window.location.href);
            url.searchParams.set('lang', theme.lang());
            a.href = url.pathname + url.search;
        });
    }

    let heat = null;

    function heatmap() {
        const node = document.getElementById('analyticsHeat');
        if (!node || typeof GCMap === 'undefined') return;
        heat = GCMap.createHeatmap(node, { colors: theme.heatColors() });
        if (!heat) return;
        fetch(node.dataset.url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then((r) => (r.ok ? r.json() : { points: [] }))
            .then((data) => {
                const points = data.points || [];
                heat.setPoints(points, theme.heatColors());
                const empty = node.parentElement.querySelector('.chart-empty');
                if (empty) empty.hidden = points.length > 0;
            })
            .catch(() => {});
    }

    function init() {
        theme.apply(window.Chart);
        relabelNumbers();
        draw();
        syncCsvLinks();
        heatmap();
        theme.onChange(() => {
            restyle();
            syncCsvLinks();
            if (heat && heat.setColors) heat.setColors(theme.heatColors());
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
