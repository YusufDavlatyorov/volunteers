/* Staff dashboard charts (templates/myapp/dashboard/_curator.html):
 *  - "open requests by region" doughnut;
 *  - the 14-day requests line (created / completed), filling its card.
 * Colours, fonts and tooltips come from charts-theme.js (GCChartTheme) —
 * nothing here sets a colour of its own; the HTML legend dots take the same
 * colours. Without Chart.js the server-rendered legends (numbers, %, totals)
 * still stand. Theme / language switches restyle in place.
 */
(function () {
    'use strict';

    function initRegions(canvas) {
        const theme = window.GCChartTheme;
        const card = canvas.closest('.dash-card');
        const items = Array.from(card.querySelectorAll('.dash-legend__item'));
        if (!items.length) return;

        function spec() {
            return {
                kind: 'regions',
                codes: items.map((li) => li.dataset.region || 'other'),
                labels: items.map((li) => li.querySelector('.dash-legend__name').textContent.trim()),
                series: [{ key: 'requests', label: theme.t('dash.open_requests', ''), values: items.map((li) => Number(li.dataset.count) || 0) }],
            };
        }

        function paintLegend(s) {
            const colors = theme.datasets(s, 'doughnut')[0].backgroundColor;
            items.forEach((li, i) => {
                li.querySelector('.dash-legend__dot').style.background = colors[i];
            });
        }

        function options(s) {
            const opts = theme.options(s, 'doughnut', {});
            // The legend and the total are HTML next to / inside the ring.
            opts.plugins.legend.display = false;
            opts.plugins.gcCenterTotal.display = false;
            return opts;
        }

        let s = spec();
        const chart = new window.Chart(canvas, {
            type: 'doughnut',
            data: { labels: s.labels, datasets: theme.datasets(s, 'doughnut') },
            options: options(s),
        });
        card.classList.add('has-chart');
        paintLegend(s);

        theme.onChange(() => {
            s = spec();
            chart.data.labels = s.labels;
            chart.data.datasets = theme.datasets(s, 'doughnut');
            chart.options = options(s);
            chart.update('none');
            paintLegend(s);
        });
    }

    function initTrend(canvas) {
        const theme = window.GCChartTheme;
        const source = document.getElementById('dashTrendData');
        if (!source) return;
        const trend = JSON.parse(source.textContent);

        function spec() {
            return {
                kind: 'trend',
                labels: trend.labels.map((iso) => theme.shortDate(iso)),
                series: [
                    { key: 'created', label: theme.t('an.s.created', 'Created'), values: trend.created },
                    { key: 'completed', label: theme.t('an.s.completed', 'Completed'), values: trend.completed },
                ],
            };
        }

        function options(s) {
            const opts = theme.options(s, 'line', {});
            // Totals legend is HTML under the chart; the chart fills the card.
            opts.plugins.legend.display = false;
            opts.maintainAspectRatio = false;
            return opts;
        }

        let s = spec();
        const chart = new window.Chart(canvas, {
            type: 'line',
            data: { labels: s.labels, datasets: theme.datasets(s, 'line') },
            options: options(s),
        });
        theme.onChange(() => {
            s = spec();
            chart.data.labels = s.labels;
            chart.data.datasets = theme.datasets(s, 'line');
            chart.options = options(s);
            chart.update('none');
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!window.Chart || !window.GCChartTheme) return;
        window.GCChartTheme.apply(window.Chart);
        document.querySelectorAll('[data-region-chart]').forEach(initRegions);
        document.querySelectorAll('[data-trend-chart]').forEach(initTrend);
    });
})();
