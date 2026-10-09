/* One look for every chart on the site (Chart.js 4, static/vendor/chartjs-4.4.7).
 *
 * The palette, fonts, grid, legend, tooltips, number/date formatting and the
 * two small drawing plugins (doughnut centre total, value at the bar end) live
 * here; a page script only says *what* to draw (crm-analytics.js). Nothing on
 * a page sets its own colours.
 *
 * Palette — primary #2A3BA5, accent #F2A23A + three calm colours, stepped in
 * lightness too (L* 30 / 44 / 54 / 63 / 73 in light; lighter versions of the
 * same hues in dark), so series stay apart for colour-blind readers and in
 * greyscale. One entity keeps one colour on every chart (ENTITY: "completed"
 * is always teal, "emergency" always rose, …). HEAT is the activity calendar's
 * scale (static/css/activity.css --act-1..4) for the request heatmap.
 * Theme and language switches restyle charts in place (onChange), no reload.
 */
(function () {
    'use strict';

    const PALETTE = {
        light: { primary: '#2A3BA5', rose: '#A9466F', teal: '#24907F', slate: '#8E98AC', accent: '#F2A23A' },
        dark: { primary: '#8796F2', rose: '#D46C94', teal: '#4DC4B0', slate: '#7A859B', accent: '#F7BE68' },
    };
    // Series order when a chart has no entity colours: dark → light alternation.
    const ORDER = ['primary', 'accent', 'teal', 'rose', 'slate'];
    const HEAT = {
        light: ['#C7CDF0', '#8B96DE', '#5361C7', '#2A3BA5'],
        dark: ['#303f86', '#4658c2', '#7182e6', '#aab6ff'],
    };
    // Series keys and category codes → colour name. Same meaning, same colour.
    const ENTITY = {
        created: 'primary', requests: 'primary', volunteers: 'primary', offers: 'primary',
        accepted: 'slate', completed: 'teal', cancelled: 'rose',
        overdue: 'accent', stale: 'rose', sos: 'rose', messages: 'rose',
        reviews: 'accent', avg: 'accent',
        clients: 'teal', curators: 'accent', other: 'slate',
        normal: 'primary', high: 'accent', emergency: 'rose',
        available: 'teal', busy: 'accent', offline: 'slate',
        sms_code: 'rose', money: 'accent', off_platform: 'teal', phone: 'primary',
    };

    const root = document.documentElement;

    function mode() {
        return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }

    function palette() { return PALETTE[mode()]; }

    function color(key, index) {
        const p = palette();
        const name = ENTITY[key];
        return name ? p[name] : p[ORDER[(index || 0) % ORDER.length]];
    }

    function hasEntity(key) { return Object.prototype.hasOwnProperty.call(ENTITY, key); }

    function alpha(hex, a) {
        const h = hex.replace('#', '');
        const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
        return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
    }

    // Text/surface colours follow the site's own tokens (style.css), so a chart
    // sits on its card exactly like the text around it. --muted is ≥ 4.5:1 on
    // the card in both themes.
    function tokens() {
        const css = getComputedStyle(document.body);
        const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
        const dark = mode() === 'dark';
        return {
            text: v('--text', dark ? '#e7ebf3' : '#111827'),
            text2: v('--text-2', dark ? '#c7cede' : '#33404f'),
            muted: v('--muted', dark ? '#8b93a7' : '#5b6473'),
            surface: v('--card-bg', dark ? '#151b28' : '#ffffff'),
            line: v('--line', dark ? 'rgba(255,255,255,0.09)' : '#e3e7f4'),
            grid: dark ? 'rgba(255,255,255,0.06)' : 'rgba(17,24,39,0.06)',
            font: css.fontFamily || 'Inter, system-ui, sans-serif',
        };
    }

    // ---- language, numbers, dates ------------------------------------------------
    function lang() {
        const l = root.getAttribute('lang') || 'ru';
        return window.T && window.T[l] ? l : 'ru';
    }

    function t(key, fallback) {
        if (!key) return fallback;
        const table = (window.T && window.T[lang()]) || {};
        const en = (window.T && window.T.en) || {};
        return table[key] || en[key] || (fallback === undefined ? key : fallback);
    }

    const TG_OK = (() => {
        try { return Intl.NumberFormat.supportedLocalesOf(['tg']).length > 0; } catch (e) { return false; }
    })();

    // Tajik and Russian share the separators (1 234,5) — Russian rules stand in
    // where a browser has no Tajik data.
    function locale() {
        const l = lang();
        if (l === 'tj') return TG_OK ? 'tg' : 'ru';
        return l;
    }

    const numCache = {};
    function num(value, decimals) {
        if (value === null || value === undefined || value === '' || isNaN(value)) return '—';
        // decimals given → exactly that many; omitted → up to 2, as needed.
        const max = decimals === undefined ? 2 : decimals;
        const min = decimals === undefined ? 0 : decimals;
        const key = `${locale()}:${min}:${max}`;
        if (!numCache[key]) {
            numCache[key] = new Intl.NumberFormat(locale(), { maximumFractionDigits: max, minimumFractionDigits: min });
        }
        return numCache[key].format(Number(value));
    }

    function pct(value) {
        try {
            return new Intl.NumberFormat(locale(), { style: 'percent', maximumFractionDigits: 0 }).format(value / 100);
        } catch (e) {
            return `${value}%`;
        }
    }

    function parts(iso) {
        const p = String(iso || '').split('-').map(Number);
        return p.length === 3 && !p.some(isNaN) ? p : null;
    }

    function fill(template, values) {
        return Object.keys(values).reduce((s, k) => s.split(`{${k}}`).join(values[k]), template);
    }

    // Month names come from i18n.js (act.mN short / act.mgN genitive) — Intl's
    // Tajik dates are capitalised and Russian ones carry "г.".
    function shortDate(iso) {
        const p = parts(iso);
        if (!p) return iso;
        return fill(t('an.date_short', '{d} {m}'), { d: p[2], m: t(`act.m${p[1]}`, String(p[1])) });
    }

    function fullDate(iso) {
        const p = parts(iso);
        if (!p) return iso;
        return fill(t('act.date', '{d} {m} {y}'), { d: p[2], m: t(`act.mg${p[1]}`, String(p[1])), y: p[0] });
    }

    function addDays(iso, days) {
        const p = parts(iso);
        const d = new Date(Date.UTC(p[0], p[1] - 1, p[2] + days));
        return d.toISOString().slice(0, 10);
    }

    function weekRange(iso) {
        return `${shortDate(iso)} – ${shortDate(addDays(iso, 6))}`;
    }

    // ---- plugins ------------------------------------------------------------------
    // Doughnut: the total in the middle (number + caption).
    const centerTotal = {
        id: 'gcCenterTotal',
        afterDraw(chart, args, opts) {
            if (chart.config.type !== 'doughnut' || !opts || opts.display === false) return;
            const meta = chart.getDatasetMeta(0);
            if (!meta || !meta.data.length) return;
            const { x, y } = meta.data[0];
            const k = tokens();
            const ctx = chart.ctx;
            const total = chart.data.datasets[0].data.reduce((a, b) => a + (b || 0), 0);
            const big = Math.max(18, Math.min(28, meta.data[0].innerRadius * 0.42));
            ctx.save();
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillStyle = k.text;
            ctx.font = `700 ${big}px ${k.font}`;
            ctx.fillText(num(total, 0), x, y - 7);
            ctx.fillStyle = k.muted;
            ctx.font = `500 12px ${k.font}`;
            ctx.fillText(t('an.total', 'total'), x, y + big / 2 + 4);
            ctx.restore();
        },
    };

    // Horizontal bars: the value at the end of each bar (or stack) instead of
    // a value axis and vertical grid lines.
    const barValues = {
        id: 'gcBarValues',
        afterDatasetsDraw(chart, args, opts) {
            if (!opts || !opts.display || chart.options.indexAxis !== 'y') return;
            const k = tokens();
            const ctx = chart.ctx;
            const stacked = chart.options.scales.x && chart.options.scales.x.stacked;
            ctx.save();
            ctx.font = `600 12px ${k.font}`;
            ctx.fillStyle = k.text2;
            ctx.textBaseline = 'middle';
            ctx.textAlign = 'left';
            if (stacked) {
                const last = chart.data.datasets.length - 1;
                const metaLast = chart.getDatasetMeta(last);
                chart.data.labels.forEach((_, i) => {
                    let total = 0;
                    let end = chart.chartArea.left;
                    chart.data.datasets.forEach((ds, di) => {
                        const meta = chart.getDatasetMeta(di);
                        if (meta.hidden) return;
                        total += ds.data[i] || 0;
                        const bar = meta.data[i];
                        if (bar) end = Math.max(end, bar.x);
                    });
                    const bar = metaLast.data[i];
                    if (bar) ctx.fillText(num(total, 0), end + 6, bar.y);
                });
            } else {
                chart.data.datasets.forEach((ds, di) => {
                    const meta = chart.getDatasetMeta(di);
                    if (meta.hidden) return;
                    meta.data.forEach((bar, i) => {
                        const v = ds.data[i];
                        if (v === null || v === undefined) return;
                        ctx.fillText(num(v, opts.decimals), bar.x + 6, bar.y);
                    });
                });
            }
            ctx.restore();
        },
    };

    // Tooltip: an HTML card (site card background, shadow, 8px radius) instead
    // of the canvas box, so it can carry the theme's shadow and wrap text.
    function externalTooltip(context) {
        const { chart, tooltip } = context;
        const box = chart.canvas.parentNode;
        let el = box.querySelector('.chart-tip');
        if (!el) {
            el = document.createElement('div');
            el.className = 'chart-tip';
            el.setAttribute('role', 'presentation');
            box.appendChild(el);
        }
        if (tooltip.opacity === 0) {
            el.style.opacity = '0';
            return;
        }
        const fmt = (chart.options.plugins.gcFormat || {});
        el.replaceChildren();
        // A doughnut row already names its slice — no title line repeating it.
        (chart.config.type === 'doughnut' ? [] : (tooltip.title || [])).forEach((line) => {
            const title = document.createElement('div');
            title.className = 'chart-tip__title';
            title.textContent = line;
            el.appendChild(title);
        });
        tooltip.dataPoints.forEach((point) => {
            const row = document.createElement('div');
            row.className = 'chart-tip__row';
            const dot = document.createElement('i');
            const ds = point.dataset;
            const bg = Array.isArray(ds.borderColor) || chart.config.type === 'doughnut' || chart.config.type === 'bar'
                ? (Array.isArray(ds.backgroundColor) ? ds.backgroundColor[point.dataIndex] : ds.backgroundColor)
                : ds.borderColor;
            dot.style.background = bg;
            const label = document.createElement('span');
            label.textContent = chart.config.type === 'doughnut' ? point.label : ds.label;
            const value = document.createElement('b');
            value.textContent = num(point.raw, fmt.decimals);
            if (chart.config.type === 'doughnut') {
                const total = ds.data.reduce((a, b) => a + (b || 0), 0);
                if (total) value.textContent += ` · ${pct(Math.round((point.raw || 0) * 100 / total))}`;
            }
            row.append(dot, label, value);
            el.appendChild(row);
        });
        el.style.opacity = '1';
        // Keep it inside the card: flip to the left of the cursor near the right edge.
        const w = el.offsetWidth;
        const h = el.offsetHeight;
        let left = tooltip.caretX + 12;
        if (left + w > box.clientWidth) left = Math.max(0, tooltip.caretX - w - 12);
        let top = tooltip.caretY - h / 2;
        top = Math.max(-8, Math.min(top, box.clientHeight - h + 8));
        el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
    }

    // ---- building blocks ----------------------------------------------------------
    const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function tickLimit(width, kind) {
        if (kind === 'value') return width < 480 ? 4 : 5;
        if (width < 480) return 4;
        if (width < 800) return 6;
        return 10;
    }

    function apply(Chart) {
        if (!Chart || Chart.__gcThemed) return;
        Chart.__gcThemed = true;
        Chart.register(centerTotal, barValues);
        const k = tokens();
        Chart.defaults.font.family = k.font;
        Chart.defaults.font.size = 12;
        Chart.defaults.color = k.muted;
    }

    /* datasets(chart spec, display) → Chart.js datasets.
       spec = {kind, labels, series:[{key, label, values}]} with labels already
       in the visitor's language; display = line | bars | rank | stack | doughnut. */
    function datasets(spec, display) {
        const many = spec.series.length;
        return spec.series.map((s, i) => {
            const c = color(s.key, i);
            const base = { label: s.label, data: s.values };
            if (display === 'line') {
                return Object.assign(base, {
                    borderColor: c, backgroundColor: alpha(c, mode() === 'dark' ? 0.10 : 0.06),
                    borderWidth: 2.5, fill: 'origin', cubicInterpolationMode: 'monotone', spanGaps: true,
                    pointRadius: 0, pointHoverRadius: 4, pointHitRadius: 10,
                    pointBackgroundColor: c, pointHoverBorderColor: tokens().surface, pointHoverBorderWidth: 2,
                });
            }
            if (display === 'doughnut') {
                const colors = spec.codes.map((code, j) => color(code, j));
                return Object.assign(base, {
                    backgroundColor: colors, hoverBackgroundColor: colors,
                    borderColor: tokens().surface, borderWidth: 2, hoverOffset: 4,
                });
            }
            // Bars. One series whose categories all have an entity colour
            // (priorities, flag topics, funnel stages) is coloured per category.
            let fillColor = c;
            if (many === 1 && spec.codes && spec.codes.length && spec.codes.every(hasEntity)) {
                fillColor = spec.codes.map((code, j) => color(code, j));
            }
            const last = i === many - 1;
            return Object.assign(base, {
                backgroundColor: fillColor, hoverBackgroundColor: fillColor, borderWidth: 0,
                // Rounded only at the free end (top / right); a stack only on its last segment.
                borderRadius: display === 'stack' && !last ? 0 : 6, borderSkipped: 'start',
                categoryPercentage: 0.6, barPercentage: many > 1 && display !== 'stack' ? 0.9 : 1,
                maxBarThickness: display === 'rank' || display === 'stack' ? 18 : 40,
            });
        });
    }

    function options(spec, display, extra) {
        extra = extra || {};
        const k = tokens();
        const horizontal = display === 'rank' || display === 'stack';
        const legend = {
            display: spec.series.length > 1 || display === 'doughnut',
            position: 'top',
            align: 'end',
            labels: {
                color: k.text2, usePointStyle: true, pointStyle: 'circle', boxWidth: 7, boxHeight: 7,
                padding: 12, font: { size: 12 },
            },
        };
        const animation = reduced() ? false : { duration: 350, easing: 'easeOutQuart' };
        const plugins = {
            legend,
            tooltip: {
                enabled: false, external: externalTooltip,
                callbacks: { title: (items) => (items.length ? extra.title(items[0].dataIndex) : '') },
            },
            gcFormat: { decimals: extra.decimals },
            gcCenterTotal: { display: display === 'doughnut' },
            gcBarValues: { display: horizontal, decimals: extra.decimals },
        };
        if (display === 'doughnut') {
            return {
                responsive: true, maintainAspectRatio: false, animation, cutout: '70%',
                layout: { padding: 4 }, plugins,
            };
        }
        const valueTicks = {
            color: k.muted, padding: 8, font: { size: 12 }, precision: extra.decimals ? undefined : 0,
            maxTicksLimit: 5, callback: (v) => num(v, extra.decimals === undefined ? 0 : extra.decimals),
        };
        const valueAxis = {
            beginAtZero: true, border: { display: false },
            grid: { color: k.grid, lineWidth: 1, drawTicks: false },
            ticks: valueTicks,
        };
        if (extra.max) Object.assign(valueAxis, { max: extra.max });
        const categoryAxis = {
            border: { display: false }, grid: { display: false },
            ticks: {
                color: k.muted, font: { size: 12 }, maxRotation: 0, padding: 6,
                autoSkip: !spec.categorical, autoSkipPadding: 12,
            },
        };
        if (!spec.categorical) categoryAxis.ticks.maxTicksLimit = tickLimit(window.innerWidth);
        let scales;
        if (horizontal) {
            // No value axis at all: values are printed at the bar ends.
            scales = {
                x: { display: false, beginAtZero: true, stacked: display === 'stack', grace: '12%' },
                y: Object.assign(categoryAxis, { stacked: display === 'stack' }),
            };
        } else {
            scales = { x: categoryAxis, y: valueAxis };
        }
        return {
            responsive: true, maintainAspectRatio: false, animation,
            indexAxis: horizontal ? 'y' : 'x',
            interaction: display === 'line' ? { mode: 'index', intersect: false } : { mode: horizontal ? 'index' : 'nearest', intersect: !horizontal, axis: horizontal ? 'y' : 'x' },
            layout: { padding: { top: 2, right: horizontal ? 8 : 4, left: 0, bottom: 0 } },
            scales,
            plugins,
            onResize(chart, size) {
                const x = chart.options.scales && chart.options.scales.x;
                if (x && x.ticks && !spec.categorical && !horizontal) {
                    const limit = tickLimit(size.width);
                    if (x.ticks.maxTicksLimit !== limit) {
                        x.ticks.maxTicksLimit = limit;
                        chart.update('none');
                    }
                }
            },
        };
    }

    // Restyle hook: theme or language changed.
    function onChange(callback) {
        new MutationObserver(callback).observe(root, { attributes: true, attributeFilter: ['data-theme', 'lang'] });
    }

    window.GCChartTheme = {
        PALETTE, HEAT, ENTITY, mode, palette, color, alpha, tokens, lang, t, locale, num, pct,
        shortDate, fullDate, weekRange, apply, datasets, options, onChange,
        // The map's base layer stays light in both site themes, so the heat ramp
        // is the calendar's light scale (the dark one is for dark surfaces).
        heatColors: () => HEAT.light.slice(),
    };
})();
