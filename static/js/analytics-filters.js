/* CRM analytics filter panel (#anFilters).
 *
 * The server reads the filters in one place (analytics_v2.parse_filters) and
 * falls back to the defaults for anything invalid; this script only makes the
 * form pleasant: the dates are editable for "custom" and show the preset's
 * dates otherwise, the range is checked before leaving the page (error text
 * under the fields, never alert()), and the URL stays short
 * (?period=30d&region=…, from/to only for a custom range). "Today" is the
 * server's day in Asia/Dushanbe (data-today), not the browser's.
 * The period line under the panel (#anRange) is re-labelled in the visitor's
 * language with the activity calendar's date strings (act.date / act.mgN).
 */
(function () {
    'use strict';

    const form = document.getElementById('anFilters');
    if (!form) return;
    const period = form.querySelector('#anPeriod');
    const from = form.querySelector('#anFrom');
    const to = form.querySelector('#anTo');
    const region = form.querySelector('#anRegion');
    const errorBox = form.querySelector('#anFilterError');
    const rangeNode = document.getElementById('anRange');
    const today = form.dataset.today;
    const maxDays = parseInt(form.dataset.maxDays, 10) || 366;
    const PRESET_DAYS = { '7d': 7, '30d': 30, '3m': 90, '1y': 365 };

    function lang() {
        const l = document.documentElement.getAttribute('lang') || 'ru';
        return window.T && window.T[l] ? l : 'ru';
    }

    function t(key) {
        const table = (window.T && window.T[lang()]) || {};
        const en = (window.T && window.T.en) || {};
        return table[key] || en[key] || key;
    }

    // ISO date strings compare correctly as strings; arithmetic goes through UTC.
    function shift(iso, days) {
        const p = iso.split('-').map(Number);
        const d = new Date(Date.UTC(p[0], p[1] - 1, p[2] + days));
        return d.toISOString().slice(0, 10);
    }

    function dateText(iso) {
        const p = (iso || '').split('-').map(Number);
        if (p.length !== 3 || p.some(isNaN)) return iso || '';
        return t('act.date').replace('{d}', p[2]).replace('{m}', t('act.mg' + p[1])).replace('{y}', p[0]);
    }

    let errorKey = errorBox && !errorBox.hidden ? errorBox.getAttribute('data-i18n') : null;

    function showError(key) {
        errorKey = key;
        if (!errorBox) return;
        if (key) {
            errorBox.setAttribute('data-i18n', key);
            errorBox.textContent = t(key);
            errorBox.hidden = false;
        } else {
            errorBox.removeAttribute('data-i18n');
            errorBox.textContent = '';
            errorBox.hidden = true;
        }
    }

    function markInvalid(fields) {
        [from, to].forEach((input) => {
            if (fields.indexOf(input) >= 0) input.setAttribute('aria-invalid', 'true');
            else input.removeAttribute('aria-invalid');
        });
    }

    function syncPeriod() {
        const custom = period.value === 'custom';
        from.disabled = !custom;
        to.disabled = !custom;
        if (!custom) {
            const days = PRESET_DAYS[period.value] || 30;
            to.value = today;
            from.value = shift(today, -(days - 1));
            markInvalid([]);
            showError(null);
        }
    }

    // null when the custom range is fine, else the i18n key of the problem.
    function validate() {
        if (period.value !== 'custom') return null;
        const a = from.value;
        const b = to.value;
        if (!a || !b) {
            markInvalid([!a ? from : null, !b ? to : null].filter(Boolean));
            return 'an.err.required';
        }
        if (a > today || b > today) {
            markInvalid([a > today ? from : null, b > today ? to : null].filter(Boolean));
            return 'an.err.future';
        }
        if (a > b) {
            markInvalid([from, to]);
            return 'an.err.order';
        }
        if (shift(a, maxDays - 1) < b) {
            markInvalid([from, to]);
            return 'an.err.too_long';
        }
        markInvalid([]);
        return null;
    }

    period.addEventListener('change', () => {
        syncPeriod();
        if (period.value === 'custom') from.focus();
    });
    [from, to].forEach((input) => {
        // Keep each picker's own bounds in step with the other date.
        input.addEventListener('change', () => {
            to.min = from.value || '';
            from.max = to.value && to.value < today ? to.value : today;
            if (errorKey && errorKey !== 'an.err.url') showError(validate());
        });
    });

    form.addEventListener('submit', (event) => {
        event.preventDefault();
        const problem = validate();
        if (problem) {
            showError(problem);
            (from.getAttribute('aria-invalid') === 'true' ? from : to).focus();
            return;
        }
        const params = new URLSearchParams();
        params.set('period', period.value);
        if (period.value === 'custom') {
            params.set('from', from.value);
            params.set('to', to.value);
        }
        if (region.value) params.set('region', region.value);
        window.location.assign(form.getAttribute('action') + '?' + params.toString());
    });

    function relabel() {
        if (rangeNode) rangeNode.textContent = dateText(rangeNode.dataset.from) + ' – ' + dateText(rangeNode.dataset.to);
        if (errorKey && errorBox) errorBox.textContent = t(errorKey);
    }

    // Back/forward cache: restore the fields' enabled state for the shown period.
    window.addEventListener('pageshow', () => {
        from.disabled = to.disabled = period.value !== 'custom';
    });
    if (period.value === 'custom') {
        to.min = from.value || '';
        from.max = to.value || today;
    }
    relabel();
    new MutationObserver(relabel).observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
})();
