(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const api = async (url, method = 'GET', body) => {
    const r = await fetch(url, { method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: body ? JSON.stringify(body) : undefined });
    if (r.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'Something went wrong.');
    return d;
  };
  let toastT;
  const toast = (m) => { const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 4000); };

  // ---- time helpers (server-time offset keeps countdowns right on skewed clocks)
  let skew = 0;
  const nowMs = () => Date.now() + skew;
  const ago = (iso) => { const s = Math.max(0, Math.round((nowMs() - Date.parse(iso)) / 1000));
    return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : `${Math.floor(s / 3600)} h ago`; };
  const left = (iso) => { const s = Math.round((Date.parse(iso) - nowMs()) / 1000);
    if (s <= 0) return 'expired'; const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${m} min left` : m ? `${m} min left` : `${s}s left`; };
  const stamp = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' });

  // ---- map
  const map = L.map('map').setView([20.59, 78.96], 5);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
  const pinIcon = (txt, me) => { const d = el('div', 'pin' + (me ? ' me' : '')); d.append(el('span', '', txt)); return L.divIcon({ className: '', html: d, iconSize: [34, 34], iconAnchor: [17, 34] }); };
  const markers = new Map();
  let fitted = false;
  function putMarker(key, lat, lng, acc, label, me, popup) {
    let m = markers.get(key);
    if (!m) { m = { marker: L.marker([lat, lng], { icon: pinIcon(label, me) }).addTo(map),
      circle: L.circle([lat, lng], { radius: acc || 0, weight: 1, opacity: .5, fillOpacity: .08 }).addTo(map) }; markers.set(key, m); }
    m.marker.setLatLng([lat, lng]); m.circle.setLatLng([lat, lng]).setRadius(acc || 0);
    m.marker.bindPopup(popup);
  }
  const dropMarker = (key) => { const m = markers.get(key); if (m) { m.marker.remove(); m.circle.remove(); markers.delete(key); } };

  // ---- sharing: GPS runs only after an explicit tap + browser permission
  let watchId = null, lastSent = 0, wake = null;
  const geoOpts = { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 };
  function onPos(p) {
    const { latitude: lat, longitude: lng, accuracy } = p.coords;
    putMarker('me', lat, lng, accuracy, 'Me', true, 'You are here');
    if (Date.now() - lastSent < 8000) return;
    lastSent = Date.now();
    api('/api/location', 'POST', { lat, lng, accuracy }).catch((e) => toast(e.message));
  }
  function onGeoErr(e) {
    toast(e.code === 1 ? 'Location permission is blocked. Allow it in your browser settings to share.' : 'Could not read your location. Check GPS and try again.');
    if (e.code === 1) stopSharing(false);
  }
  function startSharing() {
    if (!navigator.geolocation) return toast('This browser does not support location sharing.');
    $('#shareBtn').disabled = true;
    navigator.geolocation.getCurrentPosition(async (p) => {   // triggers the permission prompt
      try {
        await api('/api/sharing/start', 'POST');
        if (watchId === null) watchId = navigator.geolocation.watchPosition(onPos, onGeoErr, geoOpts);
        onPos(p);
        if ('wakeLock' in navigator) navigator.wakeLock.request('screen').then((w) => { wake = w; }).catch(() => {});
        toast('Sharing started.');
      } catch (e) { toast(e.message); }
      refresh();
    }, (e) => { onGeoErr(e); $('#shareBtn').disabled = false; }, geoOpts);
  }
  async function stopSharing(tell = true) {
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
    if (wake) { wake.release().catch(() => {}); wake = null; }
    dropMarker('me');
    try { await api('/api/sharing/stop', 'POST'); if (tell) toast('Sharing stopped. Your stored location was deleted.'); } catch (e) { toast(e.message); }
    refresh();
  }
  $('#shareBtn').addEventListener('click', () => (watchId !== null ? stopSharing() : startSharing()));
  $('#stopAll').addEventListener('click', async () => {
    if (!confirm('Stop sharing, delete your stored location, and revoke every invite and share?')) return;
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
    dropMarker('me');
    try { await api('/api/stop-all', 'POST'); toast('Everything stopped. All access was revoked.'); } catch (e) { toast(e.message); }
    refresh();
  });

  // ---- invites
  $('#inviteForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try { await api('/api/invite', 'POST', { email: $('#inviteEmail').value, minutes: Number($('#inviteMinutes').value) });
      $('#inviteEmail').value = ''; toast('Invite sent.'); refresh(); } catch (e) { toast(e.message); }
  });
  const act = (url, body, msg) => async () => { try { await api(url, 'POST', body); if (msg) toast(msg); refresh(); } catch (e) { toast(e.message); } };
  const btn = (label, fn, cls = '') => { const b = el('button', 'btn sm ' + cls, label); b.type = 'button'; b.addEventListener('click', fn); return b; };

  // ---- render
  const tickers = [];
  const tick = () => tickers.forEach((f) => f());
  setInterval(tick, 1000);
  function render(st) {
    skew = Date.parse(st.server_time) - Date.now();
    tickers.length = 0;
    const on = st.me.sharing_on, active = on && watchId !== null;
    $('#shareCard').classList.toggle('live', active);
    $('#beacon').classList.toggle('live', active);
    $('#shareTitle').textContent = active ? 'You are sharing live' : on ? 'Sharing is paused' : 'Sharing is off';
    $('#shareSub').textContent = active ? 'Contacts you approved can see your latest position.' : on ? 'This tab stopped sending. Resume to update your position.' : 'Nobody can see your location.';
    $('#shareBtn').textContent = active ? 'Stop sharing' : on ? 'Resume sharing' : 'Start sharing my location';
    $('#shareBtn').disabled = false;

    const out = $('#outgoing'); out.replaceChildren();
    st.outgoing.forEach((s) => {
      const li = el('li', 'item'), row = el('div', 'row');
      row.append(el('span', 'name', s.email), el('span', s.status === 'pending' ? 'badge pending' : 'badge', s.status === 'pending' ? 'Waiting' : 'Can see you'));
      const meta = el('div', 'meta', s.status === 'pending' ? `Timer starts when accepted (${s.duration} min)` : '');
      if (s.status === 'active') tickers.push(() => { meta.textContent = left(s.expires_at); });
      const acts = el('div', 'acts');
      if (s.status === 'active') acts.append(btn('Reset to 1 hour', act(`/api/share/${s.id}/extend`, { minutes: 60 }, 'Timer reset to 1 hour.')));
      acts.append(btn(s.status === 'active' ? 'Stop sharing with them' : 'Cancel invite', act(`/api/share/${s.id}/revoke`, null, 'Access removed.'), 'danger'));
      li.append(row, meta, acts); out.append(li);
    });

    const inc = $('#incoming'); inc.replaceChildren();
    const seen = new Set();
    st.incoming.forEach((s) => {
      const li = el('li', 'item'), row = el('div', 'row'), meta = el('div', 'meta'), acts = el('div', 'acts');
      row.append(el('span', 'name', s.owner));
      if (s.status === 'pending') {
        row.append(el('span', 'badge pending', 'Invite'));
        meta.textContent = `Wants to share their location with you for ${s.duration} min.`;
        acts.append(btn('Accept', act(`/api/share/${s.id}/respond`, { accept: true }, 'Accepted.'), 'primary'), btn('Decline', act(`/api/share/${s.id}/respond`, { accept: false })));
      } else if (s.location) {
        const loc = s.location, key = 's' + s.id;
        seen.add(key);
        putMarker(key, loc.lat, loc.lng, loc.accuracy, s.owner.charAt(0).toUpperCase(), false, `${s.owner} - updated ${stamp(loc.updated_at)}`);
        const badge = el('span', 'badge');
        row.append(badge);
        tickers.push(() => { const old = nowMs() - Date.parse(loc.updated_at) > 90000;
          badge.textContent = old ? 'Last seen ' + ago(loc.updated_at) : 'Live'; badge.className = old ? 'badge stale' : 'badge';
          meta.textContent = `Updated ${stamp(loc.updated_at)} (${ago(loc.updated_at)}). Access ${left(s.expires_at)}.`; });
        acts.append(btn('Show on map', () => { map.setView([loc.lat, loc.lng], 16); markers.get(key).marker.openPopup(); }));
      } else {
        row.append(el('span', 'badge stale', 'Not sharing'));
        meta.textContent = s.sharing ? 'Waiting for their first location update.' : 'They have paused sharing right now.';
      }
      if (s.status === 'active') acts.append(btn('Stop viewing', act(`/api/share/${s.id}/revoke`, null, 'Removed.'), 'danger'));
      li.append(row, meta, acts); inc.append(li);
    });
    [...markers.keys()].filter((k) => k !== 'me' && !seen.has(k)).forEach(dropMarker);
    if (!fitted && seen.size) { map.fitBounds(L.featureGroup([...markers.values()].map((m) => m.marker)).getBounds().pad(0.3), { maxZoom: 16 }); fitted = true; }
    tick();
  }

  async function refresh() { try { render(await api('/api/state')); } catch (e) { /* offline: retry on next poll */ } }
  refresh(); setInterval(refresh, 8000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
