(() => {
  const root = document.getElementById('offline-root');
  const userId = root.dataset.userId;
  const DB_NAME = `circle-core-offline:${location.host}:${userId}`;
  const DEVICE_KEY = `circle-core-device:${location.host}:${userId}`;
  const deviceId = localStorage.getItem(DEVICE_KEY) || crypto.randomUUID();
  localStorage.setItem(DEVICE_KEY, deviceId);

  let db;
  let snapshot = null;
  const $ = id => document.getElementById(id);
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
  const api = (url, options = {}) => fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf(), ...(options.headers || {})},
  });

  function openDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains('data')) database.createObjectStore('data');
        if (!database.objectStoreNames.contains('outbox')) database.createObjectStore('outbox', {keyPath: 'id'});
      };
      request.onsuccess = () => { db = request.result; resolve(db); };
      request.onerror = () => reject(request.error);
    });
  }

  function get(store, key) {
    return new Promise((resolve, reject) => {
      const request = db.transaction(store).objectStore(store).get(key);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  function put(store, value, key) {
    return new Promise((resolve, reject) => {
      const request = db.transaction(store, 'readwrite').objectStore(store).put(value, key);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  }

  function all(store) {
    return new Promise((resolve, reject) => {
      const request = db.transaction(store).objectStore(store).getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  function remove(store, key) {
    return new Promise((resolve, reject) => {
      const request = db.transaction(store, 'readwrite').objectStore(store).delete(key);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  }

  function clearStore(store) {
    return new Promise((resolve, reject) => {
      const request = db.transaction(store, 'readwrite').objectStore(store).clear();
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  }

  function notice(text) {
    $('notice').textContent = text;
    $('notice').style.display = text ? 'block' : 'none';
  }

  function showAlert(title, body, color = '#4ade80') {
    const stack = $('offline-alerts');
    if (!stack) return;
    const alert = document.createElement('div');
    alert.style.cssText = `padding:13px 15px;border:1px solid ${color}55;border-left:3px solid ${color};border-radius:10px;background:#15181e;box-shadow:0 14px 40px rgba(0,0,0,.45)`;
    const heading = document.createElement('div');
    heading.textContent = title;
    heading.style.cssText = 'font-size:13px;font-weight:800;color:#f8fafc';
    const copy = document.createElement('div');
    copy.textContent = body;
    copy.style.cssText = 'font-size:12px;color:#94a3b8;margin-top:3px;line-height:1.45';
    alert.append(heading, copy);
    stack.append(alert);
    setTimeout(() => alert.remove(), 12000);
  }

  function onlineState() {
    $('network-state').textContent = navigator.onLine ? 'Online' : 'Offline · changes queued';
    $('network-state').style.color = navigator.onLine ? '#4ade80' : '#fbbf24';
  }

  async function queueCount() {
    const rows = await all('outbox');
    $('queue-state').textContent = `${rows.length} waiting`;
    return rows;
  }

  function leaseValid() {
    return snapshot && Date.now() < new Date(snapshot.lease_expires).getTime();
  }

  async function enroll() {
    const response = await api('/api/offline/enroll/', {
      method: 'POST',
      body: JSON.stringify({client_id: deviceId, label: $('device-label').value}),
    });
    const data = await response.json();
    if (data.status === 'active') await bootstrap();
    else notice('Enrollment requested. Ask the owner to approve this device in Offline Management.');
  }

  async function bootstrap() {
    if (!navigator.onLine) return loadCached();
    const response = await api(`/api/offline/bootstrap/?device_id=${encodeURIComponent(deviceId)}`);
    if (!response.ok) {
      $('setup').style.display = 'block';
      $('workspace').style.display = 'none';
      const data = await response.json();
      notice(data.error || 'Device approval is required.');
      return;
    }
    snapshot = await response.json();
    await put('data', snapshot, 'snapshot');
    $('setup').style.display = 'none';
    $('workspace').style.display = 'block';
    notice('');
    render();
    await sync();
  }

  async function loadCached() {
    snapshot = await get('data', 'snapshot');
    if (!snapshot) {
      $('setup').style.display = 'block';
      notice('Connect once to enroll this device and download property data.');
      return;
    }
    $('workspace').style.display = 'block';
    if (!leaseValid()) notice('Offline access has expired. Reconnect before recording more changes.');
    else notice('Offline mode is active. Changes are stored securely on this device and will synchronize automatically.');
    render();
  }

  async function enqueue(type, payload) {
    if (!leaseValid()) {
      notice('Offline access expired. Reconnect before recording changes.');
      return;
    }
    const operation = {id: crypto.randomUUID(), type, payload, created_at: new Date().toISOString()};
    await put('outbox', operation);
    applyOptimistic(operation);
    await put('data', snapshot, 'snapshot');
    render();
    await queueCount();
    if (navigator.onLine) sync();
  }

  function applyOptimistic(operation) {
    if (operation.type === 'walk_in') {
      const room = snapshot.rooms.find(row => row.id === operation.payload.room_id);
      if (room) {
        room.status = 'Occupied';
        showAlert('Guest checked in', `${operation.payload.vehicle_registration || 'Walk-in Guest'} · ${room.name}`);
      }
    }
    if (operation.type === 'check_out') {
      const booking = snapshot.bookings.find(row => row.id === operation.payload.booking_id);
      if (booking) {
        booking.status = 'Checked Out';
        const room = snapshot.rooms.find(row => row.id === booking.room_id);
        if (room) {
          room.status = 'Cleaning';
          room.cleaning_status = 'Needs Cleaning';
        }
        showAlert('Room needs cleaning', `${booking.room} · guest checked out`, '#fb923c');
      }
    }
    if (operation.type === 'cash_payment') {
      const booking = snapshot.bookings.find(row => row.id === operation.payload.booking_id);
      if (booking) booking.balance = Math.max(0, Number(booking.balance) - Number(operation.payload.amount)).toFixed(2);
    }
    if (operation.type === 'cleaning') {
      const room = snapshot.rooms.find(row => row.id === operation.payload.room_id);
      if (room) {
        room.cleaning_status = operation.payload.status;
        if (operation.payload.status === 'Clean' && room.status === 'Cleaning') room.status = 'Available';
      }
    }
  }

  async function sync() {
    if (!navigator.onLine || !snapshot) return;
    const operations = await all('outbox');
    if (!operations.length) { await queueCount(); return; }
    const response = await api('/api/offline/sync/', {
      method: 'POST',
      body: JSON.stringify({device_id: deviceId, lease: snapshot.lease, operations}),
    });
    if (response.status === 403) {
      await clearStore('data');
      await clearStore('outbox');
      snapshot = null;
      $('workspace').style.display = 'none';
      $('setup').style.display = 'block';
      notice('This device was revoked. Local operational data was cleared. Contact the owner.');
      return;
    }
    const data = await response.json();
    if (!response.ok) { notice(data.error || 'Synchronization is temporarily unavailable.'); return; }
    for (const result of data.results || []) {
      if (result.status === 'applied') await remove('outbox', result.id);
      if (result.status === 'conflict') {
        await remove('outbox', result.id);
        notice(`Sync conflict: ${result.error} The owner can review it in Offline Management.`);
      }
      if (result.status === 'rejected') {
        await remove('outbox', result.id);
        notice(`Offline action rejected: ${result.error}`);
      }
    }
    snapshot = {...snapshot, ...data.state, server_time: data.server_time};
    await put('data', snapshot, 'snapshot');
    render();
    await queueCount();
    const appliedCount = (data.results || []).filter(result => result.status === 'applied').length;
    if (appliedCount) showAlert('Back online', `${appliedCount} queued change${appliedCount === 1 ? '' : 's'} synchronized successfully.`);
  }

  function button(text, onclick, color = '#26303b') {
    const element = document.createElement('button');
    element.textContent = text;
    element.style.cssText = `border:0;border-radius:7px;padding:8px 10px;background:${color};color:white;cursor:pointer;font-weight:700;font-size:11px`;
    element.onclick = onclick;
    return element;
  }

  function render() {
    if (!snapshot) return;
    $('property-name').textContent = snapshot.property.name;
    $('lease-state').textContent = `Offline access until ${new Date(snapshot.lease_expires).toLocaleString()}`;
    const rooms = $('rooms');
    rooms.innerHTML = '';
    snapshot.rooms.forEach(room => {
      const card = document.createElement('article');
      card.style.cssText = 'background:#15181e;border:1px solid #2a303b;border-radius:10px;padding:14px';
      card.innerHTML = `<div style="display:flex;justify-content:space-between"><strong>${escapeHtml(room.name)}</strong><span style="font-size:11px;color:${room.status === 'Available' ? '#4ade80' : '#fbbf24'}">${escapeHtml(room.status)}</span></div><div style="font-size:11px;color:#94a3b8;margin:5px 0 11px">${escapeHtml(room.type)} · ${escapeHtml(room.cleaning_status)}</div>`;
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap';
      if (room.status === 'Available' && room.cleaning_status === 'Clean') actions.append(button('Walk-in', () => walkIn(room), '#167a3a'));
      actions.append(button('Cleaning', () => cleaning(room)));
      actions.append(button('Maintenance', () => maintenance(room)));
      card.append(actions);
      rooms.append(card);
    });

    const bookings = $('bookings');
    bookings.innerHTML = '';
    snapshot.bookings.filter(booking => booking.status !== 'Checked Out').forEach(booking => {
      const row = document.createElement('article');
      row.style.cssText = 'background:#15181e;border:1px solid #2a303b;border-radius:9px;padding:12px;display:flex;justify-content:space-between;gap:10px;align-items:center';
      row.innerHTML = `<div><strong>${escapeHtml(booking.room)} · ${escapeHtml(booking.guest)}</strong><div style="font-size:11px;color:#94a3b8">${escapeHtml(booking.reference)} · ${escapeHtml(booking.status)} · Balance R ${escapeHtml(booking.balance)}</div></div>`;
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:6px';
      if (booking.status === 'Checked In') actions.append(button('Check out', () => enqueue('check_out', {booking_id: booking.id})));
      if (Number(booking.balance) > 0) actions.append(button('Cash', () => cash(booking), '#167a3a'));
      row.append(actions);
      bookings.append(row);
    });
    checkReminders();
  }

  function checkReminders() {
    if (!snapshot) return;
    const now = Date.now();
    snapshot.bookings.filter(booking => booking.status === 'Checked In' && booking.checkout_at).forEach(booking => {
      const checkout = new Date(booking.checkout_at).getTime();
      const key = `circle-core-offline-checkout:${booking.id}:${booking.checkout_at}`;
      if (checkout - now > 0 && checkout - now <= 300000 && !localStorage.getItem(key)) {
        localStorage.setItem(key, String(now));
        const time = new Date(booking.checkout_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
        showAlert('Checkout in 5 minutes', `${booking.guest} · ${booking.room} · checkout at ${time}`, '#fbbf24');
      }
    });
  }

  function walkIn(room) {
    const available = Object.entries(room.rates).filter(([, value]) => Number(value) > 0);
    const menu = available.map(([key, value], index) => `${index + 1}. ${key.replaceAll('_', ' ')} — R ${value}`).join('\n');
    const choice = Number(prompt(`Choose stay:\n${menu}`)) - 1;
    if (!available[choice]) return;
    const plate = (prompt('Vehicle number plate (optional):') || '').toUpperCase();
    const [duration, rate] = available[choice];
    enqueue('walk_in', {room_id: room.id, duration, rate, num_guests: 1, identity_mode: plate ? 'plate' : 'walk_in', vehicle_registration: plate});
  }

  function cleaning(room) {
    const value = prompt('Cleaning status: 1 = Needs Cleaning, 2 = In Progress, 3 = Clean');
    const status = {'1': 'Needs Cleaning', '2': 'In Progress', '3': 'Clean'}[value];
    if (status) enqueue('cleaning', {room_id: room.id, status});
  }

  function maintenance(room) {
    const title = prompt(`Maintenance issue for ${room.name}:`);
    if (title) enqueue('maintenance', {room_id: room.id, title, description: title, category: 'other', priority: 'medium', block_room: false});
  }

  function cash(booking) {
    const amount = prompt(`Cash amount received (balance R ${booking.balance}):`);
    if (amount && Number(amount) > 0) enqueue('cash_payment', {booking_id: booking.id, amount});
  }

  async function start() {
    onlineState();
    $('return-main').href = sessionStorage.getItem('circle-core-offline-return') || '/';
    await openDb();
    $('enroll').onclick = enroll;
    $('sync-now').onclick = sync;
    await queueCount();
    if (navigator.onLine) await bootstrap();
    else await loadCached();
    window.addEventListener('online', () => { onlineState(); bootstrap(); });
    window.addEventListener('offline', onlineState);
    setInterval(() => navigator.onLine && sync(), 30000);
    setInterval(checkReminders, 30000);
  }

  start().catch(error => notice(`Offline app error: ${error.message}`));
})();
