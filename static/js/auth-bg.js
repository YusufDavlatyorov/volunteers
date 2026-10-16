/* ===== auth-bg.js — the calm background behind the login / register card =====
   One <canvas data-auth-bg> inside .auth-scene (accounts/login.html, register.html).
   Light theme: a few autumn leaves drift down, swaying and turning.
   Dark theme:  still, twinkling stars + a rare shooting star (every 3–6 s).

   - Follows html[data-theme] live (MutationObserver, like map.js / charts-theme.js),
     so the theme toggle switches the scene at once, no reload.
   - prefers-reduced-motion: one still frame, no animation loop.
   - Pauses while the tab is hidden; re-sizes with the scene (devicePixelRatio aware).
   Plain JS, no libraries. Purely decorative: aria-hidden, pointer-events: none. */
(function () {
    'use strict';
    var canvas = document.querySelector('[data-auth-bg]');
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    var root = document.documentElement;
    var host = canvas.parentElement;
    var motion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

    var LEAF_COLORS = ['#D9922E', '#C8892E', '#B5523B', '#D97A3E', '#A8683A'];
    var W = 0, H = 0, mode = '', raf = 0, last = 0, clock = 0, nextMeteor = 0;
    var leaves = [], stars = [], meteors = [];

    function rand(a, b) { return a + Math.random() * (b - a); }
    function theme() { return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light'; }
    function still() { return !!(motion && motion.matches); }

    function resize() {
        var rect = host.getBoundingClientRect();
        var dpr = Math.min(window.devicePixelRatio || 1, 2);
        W = Math.max(1, rect.width);
        H = Math.max(1, rect.height);
        canvas.width = Math.round(W * dpr);
        canvas.height = Math.round(H * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        fill();
    }

    /* ---- leaves ---- */
    function makeLeaf(anywhere) {
        var size = rand(7, 16);
        var depth = (size - 7) / 9;            // 0 = far (small, faint), 1 = near
        return {
            x: rand(0, W),
            y: anywhere ? rand(-20, H) : rand(-60, -20),
            size: size,
            vy: 10 + depth * 16 + rand(0, 6),  // px/s — slow
            sway: rand(14, 34),
            freq: rand(0.25, 0.6),
            phase: rand(0, Math.PI * 2),
            rot: rand(0, Math.PI * 2),
            vrot: rand(-0.7, 0.7),
            flip: rand(0.6, 1.4),
            alpha: 0.32 + depth * 0.4,
            color: LEAF_COLORS[Math.floor(Math.random() * LEAF_COLORS.length)]
        };
    }
    function drawLeaf(l) {
        var x = l.x + Math.sin(clock * l.freq + l.phase) * l.sway;
        ctx.save();
        ctx.translate(x, l.y);
        ctx.rotate(l.rot);
        // A cosine squash on one axis reads as the leaf tumbling in 3D.
        ctx.scale(Math.max(0.2, Math.abs(Math.cos(clock * l.flip + l.phase))), 1);
        ctx.globalAlpha = l.alpha;
        var s = l.size;
        ctx.beginPath();
        ctx.moveTo(0, -s);
        ctx.bezierCurveTo(s * 0.75, -s * 0.55, s * 0.7, s * 0.45, 0, s);
        ctx.bezierCurveTo(-s * 0.7, s * 0.45, -s * 0.75, -s * 0.55, 0, -s);
        ctx.fillStyle = l.color;
        ctx.fill();
        ctx.beginPath();                      // midrib + a short stem
        ctx.moveTo(0, -s * 0.8);
        ctx.lineTo(0, s * 1.3);
        ctx.strokeStyle = 'rgba(80, 40, 15, 0.35)';
        ctx.lineWidth = 0.8;
        ctx.stroke();
        ctx.restore();
    }

    /* ---- stars ---- */
    function makeStar() {
        return {
            x: Math.random(), y: Math.random(),   // relative: survives a resize
            r: rand(0.4, 1.3),
            base: rand(0.25, 0.8),
            freq: rand(0.4, 1.4),
            phase: rand(0, Math.PI * 2),
            tint: Math.random() < 0.25 ? '201, 184, 255' : '236, 236, 255'
        };
    }
    function makeMeteor() {
        var angle = rand(25, 40) * Math.PI / 180;   // down and to the left
        var speed = rand(520, 780);
        return {
            x: rand(W * 0.35, W * 1.05), y: rand(-H * 0.05, H * 0.35),
            vx: -Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
            len: rand(90, 170), life: 0, ttl: rand(0.8, 1.3)
        };
    }
    function drawStars() {
        for (var i = 0; i < stars.length; i++) {
            var st = stars[i];
            var a = still() ? st.base : st.base * (0.6 + 0.4 * Math.sin(clock * st.freq + st.phase));
            ctx.beginPath();
            ctx.arc(st.x * W, st.y * H, st.r, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(' + st.tint + ', ' + a.toFixed(3) + ')';
            ctx.fill();
        }
    }
    function drawMeteor(m) {
        var t = m.life / m.ttl;
        var a = t < 0.2 ? t / 0.2 : 1 - (t - 0.2) / 0.8;   // fade in, then out
        var speed = Math.sqrt(m.vx * m.vx + m.vy * m.vy);
        var tx = m.x - (m.vx / speed) * m.len, ty = m.y - (m.vy / speed) * m.len;
        var g = ctx.createLinearGradient(m.x, m.y, tx, ty);
        g.addColorStop(0, 'rgba(240, 236, 255, ' + (0.9 * a).toFixed(3) + ')');
        g.addColorStop(0.3, 'rgba(201, 184, 255, ' + (0.45 * a).toFixed(3) + ')');
        g.addColorStop(1, 'rgba(201, 184, 255, 0)');
        ctx.save();
        ctx.lineCap = 'round';
        ctx.lineWidth = 1.3;
        ctx.strokeStyle = g;
        ctx.shadowColor = 'rgba(201, 184, 255, ' + (0.8 * a).toFixed(3) + ')';
        ctx.shadowBlur = 8;
        ctx.beginPath();
        ctx.moveTo(m.x, m.y);
        ctx.lineTo(tx, ty);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(m.x, m.y, 1.4, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(255, 255, 255, ' + a.toFixed(3) + ')';
        ctx.fill();
        ctx.restore();
    }

    /* ---- scene ---- */
    // Keep the population matched to the area (15–25 leaves, 50–170 stars).
    function fill() {
        var area = W * H;
        var wantLeaves = Math.max(15, Math.min(25, Math.round(area / 60000)));
        var wantStars = Math.max(50, Math.min(170, Math.round(area / 7000)));
        while (leaves.length < wantLeaves) leaves.push(makeLeaf(true));
        leaves.length = wantLeaves;
        while (stars.length < wantStars) stars.push(makeStar());
        stars.length = wantStars;
    }
    function setMode(next) {
        if (next === mode) return;
        mode = next;
        meteors = [];
        nextMeteor = clock + rand(1.5, 3);
        leaves = [];
        if (W) fill();   // before the first resize() there is no size to spread them over
    }

    function step(dt) {
        clock += dt;
        if (mode === 'light') {
            for (var i = 0; i < leaves.length; i++) {
                var l = leaves[i];
                l.y += l.vy * dt;
                l.rot += l.vrot * dt;
                if (l.y - l.size * 2 > H) leaves[i] = makeLeaf(false);
            }
        } else {
            if (clock >= nextMeteor) {
                meteors.push(makeMeteor());
                nextMeteor = clock + rand(3, 6);
            }
            for (var j = meteors.length - 1; j >= 0; j--) {
                var m = meteors[j];
                m.life += dt;
                m.x += m.vx * dt;
                m.y += m.vy * dt;
                if (m.life >= m.ttl) meteors.splice(j, 1);
            }
        }
    }
    function draw() {
        ctx.clearRect(0, 0, W, H);
        if (mode === 'light') {
            for (var i = 0; i < leaves.length; i++) drawLeaf(leaves[i]);
        } else {
            drawStars();
            for (var j = 0; j < meteors.length; j++) drawMeteor(meteors[j]);
        }
    }

    function frame(now) {
        var dt = last ? Math.min((now - last) / 1000, 0.05) : 0;
        last = now;
        step(dt);
        draw();
        raf = window.requestAnimationFrame(frame);
    }
    function stop() {
        if (raf) window.cancelAnimationFrame(raf);
        raf = 0;
        last = 0;
    }
    function start() {
        stop();
        if (still()) { meteors = []; draw(); return; }   // one still frame
        if (document.hidden) return;
        raf = window.requestAnimationFrame(frame);
    }

    setMode(theme());
    resize();
    start();

    new MutationObserver(function () {
        setMode(theme());
        if (still() || !raf) draw();
    }).observe(root, { attributes: true, attributeFilter: ['data-theme'] });

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) stop(); else start();
    });

    if (motion) {
        if (motion.addEventListener) motion.addEventListener('change', start);
        else if (motion.addListener) motion.addListener(start);
    }

    var pending = 0;
    function onResize() {
        if (pending) return;
        pending = window.requestAnimationFrame(function () {
            pending = 0;
            resize();
            if (!raf) draw();
        });
    }
    if ('ResizeObserver' in window) new ResizeObserver(onResize).observe(host);
    else window.addEventListener('resize', onResize);
    // DPR changes (moving the window to another monitor, browser zoom).
    window.addEventListener('resize', onResize);
})();
