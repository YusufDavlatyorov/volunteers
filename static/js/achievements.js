/* Volunteer achievements — badge dialog (Feature Pack v2, feature 6).
 *
 * Each .badge-tile[data-ach-open] carries its own data (code, tier, earned,
 * date, value/threshold, rule key, average). Clicking fills the page's one
 * <dialog class="badge-dialog"> in the current language (window.T from
 * i18n.js) and opens it as a modal; Esc / the Close button / a click on the
 * backdrop close it and focus returns to the badge. Without <dialog> support
 * nothing happens — the tile already shows the title and progress.
 */
(function () {
    'use strict';

    function lang() {
        const l = document.documentElement.getAttribute('lang') || 'ru';
        return window.T && window.T[l] ? l : 'ru';
    }

    function t(key, fallback, params) {
        const table = (window.T && window.T[lang()]) || {};
        const en = (window.T && window.T.en) || {};
        let text = table[key] || en[key] || fallback || key;
        if (params) text = text.replace(/\{(\w+)\}/g, (m, n) => (n in params ? String(params[n]) : m));
        return text;
    }

    function setKeyed(node, key, fallback, params) {
        node.textContent = t(key, fallback, params);
        if (params) {
            node.setAttribute('data-i18n-args', JSON.stringify(params));
        } else {
            node.removeAttribute('data-i18n-args');
        }
        node.setAttribute('data-i18n', key);
    }

    function open(tile, dialog) {
        const d = tile.dataset;
        const q = (sel) => dialog.querySelector(sel);
        const img = q('[data-ach-img]');
        img.src = d.icon;
        const art = q('[data-ach-art]');
        art.className = `badge-art badge-art--lg badge-art--${d.tier}${d.earned === '1' ? '' : ' is-locked'}`;

        setKeyed(q('[data-ach-tier]'), `ach.tier.${d.tier}`, d.tier);
        q('[data-ach-tier]').className = `tag tier-tag tier-tag--${d.tier}`;
        setKeyed(q('[data-ach-title]'), `ach.${d.code}.title`, d.code);
        setKeyed(q('[data-ach-desc]'), `ach.${d.code}.desc`, '');

        const status = q('[data-ach-status]');
        const progress = q('[data-ach-progress]');
        if (d.earned === '1') {
            status.textContent = `${t('ach.earned_on', 'Earned on')} ${d.date}`;
            status.removeAttribute('data-i18n');
            progress.hidden = true;
        } else if (d.soon === '1') {
            setKeyed(status, 'ach.coming_soon', 'Coming soon');
            progress.hidden = true;
        } else if (d.manual === '1') {
            setKeyed(status, 'ach.manual', 'Given by a coordinator');
            progress.hidden = true;
        } else {
            status.textContent = `${t('ach.progress', 'Progress')}: ${d.value} / ${d.threshold}`;
            status.removeAttribute('data-i18n');
            const value = Number(d.value) || 0;
            const threshold = Math.max(Number(d.threshold) || 1, 1);
            progress.hidden = false;
            progress.firstElementChild.style.width = `${Math.min(100, Math.round((value * 100) / threshold))}%`;
            progress.setAttribute('role', 'progressbar');
            progress.setAttribute('aria-valuemin', '0');
            progress.setAttribute('aria-valuemax', String(threshold));
            progress.setAttribute('aria-valuenow', String(Math.min(value, threshold)));
        }

        const avg = q('[data-ach-avg]');
        if (d.avg) {
            setKeyed(avg, 'ach.review_avg', 'Average: {avg}', { avg: d.avg });
            avg.hidden = false;
        } else {
            avg.hidden = true;
        }
        const rule = q('[data-ach-rule]');
        if (d.ruleKey) {
            setKeyed(rule, d.ruleKey, '');
            rule.hidden = false;
        } else {
            rule.hidden = true;
        }

        dialog._opener = tile;
        dialog.showModal();
    }

    function init() {
        document.querySelectorAll('.achievements-card').forEach((card) => {
            const dialog = document.getElementById(`${card.id}Dialog`);
            if (!dialog || typeof dialog.showModal !== 'function') return;
            card.querySelectorAll('[data-ach-open]').forEach((tile) => {
                tile.addEventListener('click', () => open(tile, dialog));
            });
            // A click on the backdrop (the dialog element itself) closes it.
            dialog.addEventListener('click', (event) => {
                if (event.target === dialog) dialog.close();
            });
            dialog.addEventListener('close', () => {
                if (dialog._opener) dialog._opener.focus();
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
