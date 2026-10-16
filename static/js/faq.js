/* ===== faq.js — the FAQ page (templates/myapp/faq.html) =====
   1. Accordion: the questions are native <details>/<summary> (keyboard, screen
      readers and no-JS all work); this only animates the height. Under
      prefers-reduced-motion the native instant toggle is left alone.
   2. Search: filters the questions as you type, in the language on screen
      (html[lang]), hides categories left empty and says "Nothing found". */
(function () {
    'use strict';
    var root = document.documentElement;
    var motion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    var items = Array.prototype.slice.call(document.querySelectorAll('[data-faq-item]'));
    var cats = Array.prototype.slice.call(document.querySelectorAll('[data-faq-cat]'));
    var search = document.querySelector('[data-faq-search]');
    var empty = document.querySelector('[data-faq-empty] > span');
    if (!items.length) return;

    /* ---- accordion ---- */
    var DURATION = 220;
    items.forEach(function (details) {
        var summary = details.querySelector('summary');
        var answer = details.querySelector('[data-faq-answer]');
        var running = null;
        summary.addEventListener('click', function (event) {
            if ((motion && motion.matches) || !answer.animate) return;   // native toggle
            event.preventDefault();
            var wasClosing = details.classList.contains('is-closing');
            var opening = !details.open || wasClosing;
            // A closed <details> may still report its content's full height
            // (Chrome's ::details-content), so a fresh open starts from 0.
            var from = (!opening || wasClosing) ? answer.offsetHeight : 0;
            if (running) running.cancel();
            details.classList.remove('is-closing');
            if (opening) details.open = true;
            var to = opening ? answer.scrollHeight : 0;
            if (!opening) details.classList.add('is-closing');
            running = answer.animate(
                [{ height: from + 'px', opacity: opening ? 0.2 : 1 },
                 { height: to + 'px', opacity: opening ? 1 : 0.2 }],
                // Closing holds the last frame until <details> is shut, so the full
                // answer never flashes back for a frame.
                { duration: DURATION, easing: 'ease', fill: opening ? 'none' : 'forwards' }
            );
            var anim = running;
            anim.onfinish = function () {
                if (running === anim) running = null;
                if (!opening) {
                    details.open = false;
                    details.classList.remove('is-closing');
                    anim.cancel();
                }
            };
        });
    });

    // A shared link to one question (/faq/#q-12) opens it.
    if (location.hash && /^#q-\d+$/.test(location.hash)) {
        var target = document.getElementById(location.hash.slice(1));
        if (target && target.matches('[data-faq-item]')) target.open = true;
    }

    /* ---- search ---- */
    if (!search) return;
    function lang() {
        var l = root.getAttribute('lang');
        return l === 'en' || l === 'tj' ? l : 'ru';
    }
    function norm(text) {
        return (text || '').toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();
    }
    function textOf(item, l) {
        var parts = item.querySelectorAll('[data-faq-lang="' + l + '"]');
        var out = '';
        for (var i = 0; i < parts.length; i++) out += ' ' + parts[i].textContent;
        return norm(out);
    }
    function filter() {
        var words = norm(search.value).split(' ').filter(Boolean);
        var l = lang();
        var shown = 0;
        items.forEach(function (item) {
            var hay = textOf(item, l);
            var hit = words.every(function (w) { return hay.indexOf(w) !== -1; });
            item.hidden = !hit;
            if (hit) shown++;
        });
        cats.forEach(function (cat) {
            cat.hidden = !cat.querySelector('[data-faq-item]:not([hidden])');
        });
        if (empty) empty.hidden = shown > 0;
    }
    search.addEventListener('input', filter);
    search.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && search.value) { search.value = ''; filter(); }
    });
    // Language switch (i18n.js sets html[lang]): search the newly shown texts.
    new MutationObserver(function () { if (search.value) filter(); })
        .observe(root, { attributes: true, attributeFilter: ['lang'] });
})();
