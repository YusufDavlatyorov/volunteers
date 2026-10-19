/* Activity calendar behaviour (templates/myapp/_activity_calendar.html).
   The grid is server-rendered (Russian labels); this file
   - re-labels every square, the total and the tooltip in the visitor's
     language (Intl.PluralRules picks act.count.* / act.total.*; Tajik only has
     "other"-like forms, so its strings are the same for every category),
   - shows the tooltip on hover, on tap (mobile) and with the arrow keys
     (the scroll box is focusable), announcing it to screen readers,
   - scrolls the grid to the latest weeks on load. */
(function () {
    'use strict';

    const card = document.getElementById('activityCard');
    if (!card) return;
    const grid = document.getElementById('actGrid');
    const scroller = document.getElementById('actScroll');
    const tip = document.getElementById('actTip');
    const live = document.getElementById('actLive');
    const totalEl = document.getElementById('actTotal');
    const all = Array.from(grid.children);            // every square is a day (no padding)
    const LOCALES = { ru: 'ru-RU', tj: 'tg-TJ', en: 'en-GB' };

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

    /* "act.count" + n → the key of the right plural form for the language. */
    function pluralKey(base, n) {
        let cat = 'other';
        try { cat = new Intl.PluralRules(LOCALES[lang()] || 'ru-RU').select(n); } catch (e) { /* old browser */ }
        const table = (window.T && window.T[lang()]) || {};
        return table[`${base}.${cat}`] ? `${base}.${cat}` : `${base}.other`;
    }

    function num(n) {
        try { return new Intl.NumberFormat(LOCALES[lang()] || 'ru-RU').format(n); } catch (e) { return String(n); }
    }

    function dateText(iso) {
        const [y, m, d] = iso.split('-').map(Number);
        return t('act.date', { d, m: t(`act.mg${m}`), y });
    }

    function info(cell) {
        const n = Number(cell.dataset.n || 0);
        const head = `${n ? t(pluralKey('act.count', n), { n: num(n) }) : t('act.none')} · ${dateText(cell.dataset.d)}`;
        let detail = '';
        if (n > 1 && cell.dataset.k) {
            detail = cell.dataset.k.split(',').map((part) => {
                const [kind, count] = part.split(':');
                return `${t(`act.k.${kind}`)}: ${num(Number(count))}`;
            }).join(' · ');
        }
        return { head, detail };
    }

    let active = null;
    let pinned = false;

    function isDay(el) {
        return el && el.classList && el.classList.contains('act-cell') && grid.contains(el);
    }

    function show(cell, announce) {
        const i = info(cell);
        tip.textContent = '';
        const head = document.createElement('strong');
        head.textContent = i.head;
        tip.appendChild(head);
        if (i.detail) {
            const detail = document.createElement('span');
            detail.textContent = i.detail;
            tip.appendChild(detail);
        }
        tip.hidden = false;
        const box = card.getBoundingClientRect();
        const r = cell.getBoundingClientRect();
        const half = tip.offsetWidth / 2;
        const x = Math.max(half + 8, Math.min(box.width - half - 8, r.left + r.width / 2 - box.left));
        tip.style.left = `${x}px`;
        tip.style.top = `${r.top - box.top}px`;
        if (active && active !== cell) active.classList.remove('is-active');
        active = cell;
        cell.classList.add('is-active');
        if (announce && live) live.textContent = cell.getAttribute('aria-label');
    }

    function hide() {
        tip.hidden = true;
        if (active) active.classList.remove('is-active');
        active = null;
        pinned = false;
    }

    function relabel() {
        all.forEach((cell) => {
            if (!isDay(cell)) return;
            const i = info(cell);
            cell.setAttribute('aria-label', i.detail ? `${i.head}. ${i.detail}` : i.head);
        });
        const n = Number(totalEl.dataset.n || 0);
        const key = pluralKey('act.total', n);
        totalEl.setAttribute('data-i18n', key);
        totalEl.setAttribute('data-i18n-args', JSON.stringify({ n: num(n) }));
        totalEl.textContent = t(key, { n: num(n) });
        // "N active days" / "current streak: N days" (data-act-plural = the key base).
        card.querySelectorAll('[data-act-plural]').forEach((el) => {
            const count = Number(el.dataset.n || 0);
            el.textContent = t(pluralKey(el.dataset.actPlural, count), { n: num(count) });
        });
        if (active) show(active, false);
    }

    /* Mouse: hover shows, leaving hides (unless a tap/keyboard pinned it). */
    grid.addEventListener('mouseover', (e) => { if (!pinned && isDay(e.target)) show(e.target, false); });
    grid.addEventListener('mouseleave', () => { if (!pinned) hide(); });
    /* Tap (mobile) / click: pin the tooltip; tap elsewhere closes it. */
    grid.addEventListener('click', (e) => {
        if (!isDay(e.target)) return;
        if (pinned && active === e.target) { hide(); return; }
        pinned = true;
        show(e.target, true);
    });
    document.addEventListener('click', (e) => { if (pinned && !grid.contains(e.target)) hide(); });
    scroller.addEventListener('scroll', () => { if (active) show(active, false); }, { passive: true });
    window.addEventListener('resize', () => { if (active) show(active, false); });

    /* Keyboard: arrows move day by day (←/→ a week, ↑/↓ a day), Esc closes. */
    const STEP = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 };
    scroller.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { hide(); return; }
        if (!(e.key in STEP) && e.key !== 'Home' && e.key !== 'End') return;
        e.preventDefault();
        const days = all.filter(isDay);
        let target;
        if (e.key === 'Home') target = days[0];
        else if (e.key === 'End' || !active) target = grid.querySelector('.is-today') || days[days.length - 1];
        else {
            let i = all.indexOf(active) + STEP[e.key];
            while (i >= 0 && i < all.length && !isDay(all[i])) i += STEP[e.key];
            target = isDay(all[i]) ? all[i] : active;
        }
        pinned = true;
        const r = target.getBoundingClientRect();
        const s = scroller.getBoundingClientRect();
        if (r.left < s.left + 24) scroller.scrollLeft -= s.left + 24 - r.left;
        else if (r.right > s.right - 24) scroller.scrollLeft += r.right - (s.right - 24);
        show(target, true);
    });
    scroller.addEventListener('blur', () => { if (pinned) hide(); });

    // Latest weeks first: start scrolled to the end.
    scroller.scrollLeft = scroller.scrollWidth;
    relabel();
    // A language switch (i18n.js sets <html lang>) re-labels everything.
    new MutationObserver(relabel).observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
})();
