/* "Обновить моё местоположение" on the volunteer dashboard: one tap reads the
   device position (browser permission) and saves it as the profile point that
   matching uses (POST profile_location). Live tracking never changes it. */
(function () {
  'use strict';
  const card = document.getElementById('profileLocationCard');
  const btn = document.getElementById('profileLocationBtn');
  const status = document.getElementById('profileLocationStatus');
  if (!card || !btn || !status) return;

  function tr(key, fallback) {
    const table = (window.T && (window.T[document.documentElement.lang] || window.T.ru)) || {};
    return table[key] || fallback;
  }

  function say(key, fallback, isError) {
    status.textContent = tr(key, fallback);
    status.classList.toggle('is-error', !!isError);
  }

  function pad(n) { return String(n).padStart(2, '0'); }

  btn.addEventListener('click', () => {
    if (!('geolocation' in navigator)) {
      say('ploc.unavailable', 'Could not determine your location.', true);
      return;
    }
    btn.disabled = true;
    say('ploc.locating', 'Determining your location…');
    navigator.geolocation.getCurrentPosition((pos) => {
      const body = new FormData();
      body.append('lat', pos.coords.latitude.toFixed(6));
      body.append('lng', pos.coords.longitude.toFixed(6));
      body.append('accuracy', Math.round(pos.coords.accuracy || 0));
      const csrf = card.querySelector('[name=csrfmiddlewaretoken]');
      fetch(card.dataset.url, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrf ? csrf.value : '' },
        body,
      })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
          btn.disabled = false;
          if (ok && data.success) {
            const d = new Date(data.updated_at);
            card.classList.remove('is-stale');
            status.textContent = `${tr('ploc.saved', 'Saved')} · ${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
            status.classList.remove('is-error');
          } else if (data && data.error === 'inaccurate') {
            say('ploc.inaccurate', 'The position is too imprecise — try near a window or outdoors.', true);
          } else {
            say('ploc.error', 'Could not save your location. Please try again.', true);
          }
        })
        .catch(() => {
          btn.disabled = false;
          say('ploc.error', 'Could not save your location. Please try again.', true);
        });
    }, (err) => {
      btn.disabled = false;
      if (err && err.code === 1) say('ploc.denied', 'Allow location access for this site in your browser, then try again.', true);
      else say('ploc.unavailable', 'Could not determine your location.', true);
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
})();
