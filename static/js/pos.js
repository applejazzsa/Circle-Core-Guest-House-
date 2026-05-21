/* Circle Core Guest House — POS Cart Logic
   Handles all cart state, rendering, and sale completion. */

// ─── STATE ────────────────────────────────────────────────

const cart = {
  items: [],          // [{id, name, price, qty, discount_pct, line_total, pos_item_id, emoji}]
  booking: null,      // {id, reference, guest_name, room_name, balance_due}
  discount_pct: 0,
  discount_amount: 0,
  payment_method: null,
  cash_tendered: 0,
  cash_amount: 0,
  card_amount: 0,
};

// Restore cart from sessionStorage on load
function loadCartState() {
  try {
    const saved = sessionStorage.getItem('pos_cart_state');
    if (saved) {
      const parsed = JSON.parse(saved);
      cart.items = parsed.items || [];
      cart.booking = parsed.booking || null;
      cart.discount_pct = parsed.discount_pct || 0;
      cart.discount_amount = parsed.discount_amount || 0;
    }
  } catch (e) { /* ignore */ }
}

function saveCartState() {
  try {
    sessionStorage.setItem('pos_cart_state', JSON.stringify({
      items: cart.items,
      booking: cart.booking,
      discount_pct: cart.discount_pct,
      discount_amount: cart.discount_amount,
    }));
  } catch (e) { /* ignore */ }
}

// ─── CALCULATIONS ─────────────────────────────────────────

function calculateTotals() {
  const subtotal = cart.items.reduce((sum, item) => sum + item.line_total, 0);
  let discountAmount = cart.discount_amount;
  if (discountAmount === 0 && cart.discount_pct > 0) {
    discountAmount = +(subtotal * cart.discount_pct / 100).toFixed(2);
  }
  const afterDiscount = Math.max(subtotal - discountAmount, 0);
  const taxAmount = window.POS_VAT_REGISTERED
    ? +(afterDiscount * window.POS_VAT_RATE / (100 + window.POS_VAT_RATE)).toFixed(2)
    : 0;
  const total = afterDiscount;
  return { subtotal, discountAmount, taxAmount, total };
}

function calcLineTotal(price, qty, discountPct) {
  const discount = price * (discountPct / 100);
  return +((price - discount) * qty).toFixed(2);
}

// ─── CART OPERATIONS ──────────────────────────────────────

function addItem(posItemId, name, price, emoji, color) {
  price = parseFloat(price);
  const existing = cart.items.findIndex(i => i.pos_item_id === posItemId && posItemId !== null);
  if (existing >= 0) {
    cart.items[existing].qty++;
    cart.items[existing].line_total = calcLineTotal(
      cart.items[existing].price,
      cart.items[existing].qty,
      cart.items[existing].discount_pct
    );
  } else {
    cart.items.push({
      id: Date.now() + Math.random(),
      pos_item_id: posItemId,
      name,
      price,
      qty: 1,
      discount_pct: 0,
      line_total: +price.toFixed(2),
      emoji: emoji || '',
    });
  }
  saveCartState();
  renderCart();
  flashCartAdd();
}

function removeItem(itemId) {
  cart.items = cart.items.filter(i => i.id !== itemId);
  saveCartState();
  renderCart();
}

function updateQty(itemId, delta) {
  const item = cart.items.find(i => i.id === itemId);
  if (!item) return;
  item.qty = Math.max(0.5, item.qty + delta);
  item.line_total = calcLineTotal(item.price, item.qty, item.discount_pct);
  saveCartState();
  renderCart();
}

function setQty(itemId, newQty) {
  newQty = parseFloat(newQty) || 1;
  if (newQty <= 0) { removeItem(itemId); return; }
  const item = cart.items.find(i => i.id === itemId);
  if (!item) return;
  item.qty = newQty;
  item.line_total = calcLineTotal(item.price, item.qty, item.discount_pct);
  saveCartState();
  renderCart();
}

function setLineDiscount(itemId, pct) {
  const item = cart.items.find(i => i.id === itemId);
  if (!item) return;
  item.discount_pct = Math.max(0, Math.min(100, parseFloat(pct) || 0));
  item.line_total = calcLineTotal(item.price, item.qty, item.discount_pct);
  saveCartState();
  renderCart();
}

function setDiscount(value, type) {
  if (type === 'pct') {
    cart.discount_pct = Math.max(0, Math.min(100, parseFloat(value) || 0));
    cart.discount_amount = 0;
  } else {
    cart.discount_amount = Math.max(0, parseFloat(value) || 0);
    cart.discount_pct = 0;
  }
  saveCartState();
  renderCart();
}

function setBooking(bookingData) {
  cart.booking = bookingData;
  renderBookingChip();
  saveCartState();
}

function clearBooking() {
  cart.booking = null;
  renderBookingChip();
  saveCartState();
}

function clearCart() {
  cart.items = [];
  cart.booking = null;
  cart.discount_pct = 0;
  cart.discount_amount = 0;
  cart.payment_method = null;
  cart.cash_tendered = 0;
  cart.cash_amount = 0;
  cart.card_amount = 0;
  sessionStorage.removeItem('pos_cart_state');
  renderCart();
  renderBookingChip();
  resetPaymentUI();
}

// ─── RENDERING ────────────────────────────────────────────

function renderCart() {
  const container = document.getElementById('cart-items');
  const emptyState = document.getElementById('cart-empty');
  if (!container) return;

  const totals = calculateTotals();

  if (cart.items.length === 0) {
    container.innerHTML = '';
    if (emptyState) emptyState.style.display = 'flex';
    updateTotalsDisplay(totals);
    updatePayButtonTotal(totals.total);
    return;
  }
  if (emptyState) emptyState.style.display = 'none';

  container.innerHTML = cart.items.map(item => `
    <div class="cart-row" data-item-id="${item.id}">
      <div style="display:flex; align-items:flex-start; gap:8px;">
        ${item.emoji ? `<span style="font-size:18px; flex-shrink:0; margin-top:1px;">${item.emoji}</span>` : ''}
        <div style="flex:1; min-width:0;">
          <div style="font-size:13px; font-weight:500; color:var(--color-text-primary); line-height:1.3; word-break:break-word;">${escHtml(item.name)}</div>
          <div style="font-size:11.5px; color:var(--color-text-tertiary); margin-top:2px;">R${item.price.toFixed(2)} each</div>
        </div>
        <button onclick="removeItem(${item.id})" style="background:none;border:none;cursor:pointer;color:var(--color-text-tertiary);padding:2px 4px;border-radius:4px;flex-shrink:0;font-size:14px;line-height:1;transition:color 0.1s;" onmouseover="this.style.color='#f87171'" onmouseout="this.style.color='var(--color-text-tertiary)'">✕</button>
      </div>
      <div style="display:flex; align-items:center; justify-content:space-between; margin-top:8px;">
        <div style="display:flex; align-items:center; gap:0;">
          <button onclick="updateQty(${item.id}, -1)" class="qty-btn">−</button>
          <input type="number" value="${item.qty}" min="0.5" step="0.5"
            onchange="setQty(${item.id}, this.value)"
            style="width:42px; text-align:center; font-size:13px; font-weight:600; padding:5px 4px; background:var(--color-bg-subtle); border:1px solid var(--color-border); border-radius:0; color:var(--color-text-primary);">
          <button onclick="updateQty(${item.id}, 1)" class="qty-btn">+</button>
        </div>
        <div style="font-family:var(--font-mono); font-size:13.5px; font-weight:600; color:var(--color-green-400);">R${item.line_total.toFixed(2)}</div>
      </div>
      ${item.discount_pct > 0 ? `<div style="font-size:11px; color:#fbbf24; margin-top:4px;">Discount: ${item.discount_pct}% applied</div>` : ''}
    </div>
  `).join('');

  updateTotalsDisplay(totals);
  updatePayButtonTotal(totals.total);
}

function updateTotalsDisplay(totals) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set('cart-subtotal', `R${totals.subtotal.toFixed(2)}`);
  set('cart-discount', totals.discountAmount > 0 ? `-R${totals.discountAmount.toFixed(2)}` : 'R0.00');
  if (document.getElementById('cart-tax')) {
    set('cart-tax', `R${totals.taxAmount.toFixed(2)}`);
    document.getElementById('cart-tax-row').style.display = totals.taxAmount > 0 ? 'flex' : 'none';
  }
  set('cart-total', `R${totals.total.toFixed(2)}`);
  const discountRow = document.getElementById('cart-discount-row');
  if (discountRow) discountRow.style.display = totals.discountAmount > 0 ? 'flex' : 'none';
}

function updatePayButtonTotal(total) {
  document.querySelectorAll('.pay-total-label').forEach(el => {
    el.textContent = `R${total.toFixed(2)}`;
  });
}

function renderBookingChip() {
  const chip = document.getElementById('booking-chip');
  const roomSection = document.getElementById('room-charge-method');
  if (!chip) return;
  if (cart.booking) {
    chip.innerHTML = `
      <div style="display:inline-flex; align-items:center; gap:6px; background:var(--color-green-glow); border:1px solid var(--color-green-border); border-radius:var(--radius-full); padding:4px 10px 4px 8px; font-size:12px; color:var(--color-green-400);">
        <span>🏨</span>
        <span style="font-weight:500;">${escHtml(cart.booking.room_name)} — ${escHtml(cart.booking.guest_name)}</span>
        <button onclick="clearBooking()" style="background:none;border:none;cursor:pointer;color:var(--color-green-400);font-size:14px;line-height:1;padding:0 0 0 2px;">✕</button>
      </div>`;
    chip.style.display = 'block';
    if (roomSection) {
      roomSection.innerHTML = `<div style="background:var(--color-green-glow);border:1px solid var(--color-green-border);border-radius:var(--radius-md);padding:12px 14px;font-size:13px;color:var(--color-green-400);">Charging to Room <strong>${escHtml(cart.booking.room_name)}</strong> — ${escHtml(cart.booking.guest_name)}<br><span style="font-size:11.5px;color:var(--color-text-tertiary);">Balance due: R${parseFloat(cart.booking.balance_due).toFixed(2)}</span></div>`;
    }
  } else {
    chip.innerHTML = '';
    chip.style.display = 'none';
  }
}

// ─── ITEMS GRID ───────────────────────────────────────────

function renderItems(filterCategoryId) {
  const grid = document.getElementById('items-grid');
  if (!grid || !window.POS_ITEMS) return;
  let items = window.POS_ITEMS;
  if (filterCategoryId && filterCategoryId !== 'all') {
    items = items.filter(i => i.category_id == filterCategoryId);
  }
  if (items.length === 0) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--color-text-tertiary);font-size:13.5px;">No items in this category.</div>';
    return;
  }
  grid.innerHTML = items.map(item => `
    <button onclick="${item.is_out_of_stock ? '' : `addItem(${item.id}, '${escJs(item.name)}', ${item.price}, '${escJs(item.emoji)}', '${item.color}')`}"
      class="pos-item-card ${item.is_out_of_stock ? 'out-of-stock' : ''}"
      style="background:${item.color}; ${item.is_out_of_stock ? 'opacity:0.45;cursor:not-allowed;' : ''}"
      ${item.is_out_of_stock ? 'disabled' : ''}>
      ${item.emoji ? `<div style="font-size:28px;margin-bottom:4px;line-height:1;">${item.emoji}</div>` : ''}
      <div style="font-size:12px;font-weight:600;color:var(--color-text-primary);line-height:1.2;word-break:break-word;">${escHtml(item.name)}</div>
      <div style="font-family:var(--font-mono);font-size:12px;color:var(--color-green-400);margin-top:3px;font-weight:500;">R${item.price.toFixed(2)}</div>
      ${item.is_out_of_stock ? '<div style="font-size:9px;color:#f87171;font-weight:600;margin-top:2px;text-transform:uppercase;">Out of Stock</div>' : ''}
      ${item.is_low_stock && !item.is_out_of_stock ? '<div style="width:6px;height:6px;border-radius:50%;background:#f59e0b;position:absolute;top:6px;right:6px;"></div>' : ''}
    </button>
  `).join('');
}

function setActiveCategory(categoryId, btn) {
  cart._activeCategory = categoryId;
  document.querySelectorAll('.cat-tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  renderItems(categoryId);
}

// ─── BARCODE HANDLING ─────────────────────────────────────

let barcodeBuffer = '';
let barcodeTimer = null;

function handleBarcodeInput(value) {
  value = value.trim();
  if (!value) return;
  const searchEl = document.getElementById('barcode-input');
  if (searchEl) searchEl.value = '';

  // Numeric 8+ chars = barcode scan
  if (/^\d{8,}$/.test(value) || value.length >= 6) {
    lookupBarcode(value);
  } else {
    // Text search — filter items grid
    filterItemsByText(value);
  }
}

function filterItemsByText(query) {
  const grid = document.getElementById('items-grid');
  if (!grid || !window.POS_ITEMS) return;
  query = query.toLowerCase();
  const filtered = window.POS_ITEMS.filter(i => i.name.toLowerCase().includes(query));
  if (filtered.length === 0) {
    grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--color-text-tertiary);">No items match "${escHtml(query)}"</div>`;
    return;
  }
  // Temporarily re-render with filtered list
  const savedItems = window.POS_ITEMS;
  window.POS_ITEMS = filtered;
  renderItems('all');
  window.POS_ITEMS = savedItems;
}

async function lookupBarcode(code) {
  try {
    const resp = await fetch(`/pos/api/items/barcode/${encodeURIComponent(code)}/`);
    const data = await resp.json();
    if (data.found) {
      addItem(data.item.id, data.item.name, data.item.price, data.item.emoji, null);
      showToast(`${data.item.emoji || '✓'} ${data.item.name} added`, 'success');
      flashBarcodeInput(true);
    } else {
      showToast(`Barcode ${code} not found — add manually below`, 'error');
      flashBarcodeInput(false);
      // Focus manual item input
      const manualName = document.getElementById('manual-item-name');
      if (manualName) manualName.focus();
    }
  } catch (e) {
    showToast('Barcode lookup failed', 'error');
  }
}

function flashBarcodeInput(success) {
  const el = document.getElementById('barcode-input');
  if (!el) return;
  el.style.borderColor = success ? 'var(--color-green-500)' : '#ef4444';
  setTimeout(() => { el.style.borderColor = ''; }, 1200);
}

// Barcode scanner input handler (scanners type fast and press Enter)
function initBarcodeInput() {
  const el = document.getElementById('barcode-input');
  if (!el) return;

  el.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const val = el.value.trim();
      if (val) {
        handleBarcodeInput(val);
        el.value = '';
      }
    }
  });

  el.addEventListener('input', () => {
    clearTimeout(barcodeTimer);
    barcodeTimer = setTimeout(() => {
      const val = el.value.trim();
      if (val.length > 2) {
        if (/^\d{8,}$/.test(val)) {
          handleBarcodeInput(val);
          el.value = '';
        } else {
          filterItemsByText(val);
        }
      } else if (val.length === 0) {
        renderItems(cart._activeCategory || 'all');
      }
    }, 300);
  });
}

// ─── MANUAL ITEM ──────────────────────────────────────────

function addManualItem() {
  const nameEl = document.getElementById('manual-item-name');
  const priceEl = document.getElementById('manual-item-price');
  if (!nameEl || !priceEl) return;
  const name = nameEl.value.trim();
  const price = parseFloat(priceEl.value);
  if (!name) { nameEl.focus(); return; }
  if (!price || price <= 0) { priceEl.focus(); return; }
  addItem(null, name, price, '', '#1a1e26');
  nameEl.value = '';
  priceEl.value = '';
  nameEl.focus();
}

// ─── PAYMENT METHODS ──────────────────────────────────────

function selectPaymentMethod(method) {
  cart.payment_method = method;
  document.querySelectorAll('.pay-method-btn').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector(`[data-method="${method}"]`);
  if (btn) btn.classList.add('active');

  document.querySelectorAll('.pay-panel').forEach(p => p.style.display = 'none');
  const panel = document.getElementById(`pay-panel-${method}`);
  if (panel) panel.style.display = 'block';

  // If room_charge and no booking, show room selector
  if (method === 'room_charge') {
    if (!cart.booking) {
      loadActiveBookings();
    }
  }
}

function resetPaymentUI() {
  document.querySelectorAll('.pay-method-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.pay-panel').forEach(p => p.style.display = 'none');
  cart.payment_method = null;
}

function calculateChange() {
  const tendered = parseFloat(document.getElementById('cash-tendered')?.value || 0);
  const totals = calculateTotals();
  cart.cash_tendered = tendered;
  const change = Math.max(tendered - totals.total, 0);
  const changeEl = document.getElementById('cash-change');
  if (changeEl) {
    changeEl.textContent = `R${change.toFixed(2)}`;
    changeEl.style.color = change > 0 ? 'var(--color-green-400)' : 'var(--color-text-secondary)';
  }
}

function validateSplitPayment() {
  const totals = calculateTotals();
  const cashEl = document.getElementById('split-cash');
  const cardEl = document.getElementById('split-card');
  if (!cashEl || !cardEl) return false;
  const cashAmt = parseFloat(cashEl.value) || 0;
  const cardAmt = parseFloat(cardEl.value) || 0;
  cart.cash_amount = cashAmt;
  cart.card_amount = cardAmt;
  const diff = Math.abs((cashAmt + cardAmt) - totals.total);
  const balanceEl = document.getElementById('split-balance');
  if (balanceEl) {
    const remaining = totals.total - cashAmt - cardAmt;
    balanceEl.textContent = remaining === 0 ? '✓ Balanced' : `R${remaining.toFixed(2)} remaining`;
    balanceEl.style.color = Math.abs(remaining) < 0.01 ? 'var(--color-green-400)' : '#fbbf24';
  }
  return diff < 0.01;
}

// ─── ROOM BOOKING LOOKUP ──────────────────────────────────

async function loadActiveBookings() {
  const container = document.getElementById('active-bookings-list');
  if (!container) return;
  container.innerHTML = '<div style="padding:12px;color:var(--color-text-tertiary);font-size:13px;">Loading…</div>';
  try {
    const resp = await fetch('/pos/api/bookings/active/');
    const data = await resp.json();
    if (data.bookings.length === 0) {
      container.innerHTML = '<div style="padding:12px;color:var(--color-text-tertiary);font-size:13px;">No checked-in guests found.</div>';
      return;
    }
    container.innerHTML = data.bookings.map(b => `
      <div onclick="selectBookingForCharge(${JSON.stringify(b).replace(/"/g,'&quot;')})"
        style="padding:10px 12px;border-bottom:1px solid var(--color-border);cursor:pointer;transition:background 0.1s;"
        onmouseover="this.style.background='var(--color-bg-elevated)'" onmouseout="this.style.background='transparent'">
        <div style="font-size:13px;font-weight:500;color:var(--color-text-primary);">${escHtml(b.room_name)} — ${escHtml(b.guest_name)}</div>
        <div style="font-size:11.5px;color:var(--color-text-tertiary);margin-top:2px;font-family:var(--font-mono);">${b.reference} · Balance: R${parseFloat(b.balance_due).toFixed(2)}</div>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = '<div style="padding:12px;color:#f87171;font-size:13px;">Failed to load bookings.</div>';
  }
}

function selectBookingForCharge(bookingData) {
  setBooking(bookingData);
  const container = document.getElementById('active-bookings-list');
  if (container) container.innerHTML = '';
  showToast(`Room ${bookingData.room_name} selected`, 'success');
}

// ─── COMPLETE SALE ────────────────────────────────────────

let pendingApprovalPayload = null;

function buildSalePayload(managerCredentials) {
  const totals = calculateTotals();
  const cashTendered = parseFloat(document.getElementById('cash-tendered')?.value || 0);
  const changeGiven = Math.max(cashTendered - totals.total, 0);
  return {
    items: cart.items.map(i => ({
      pos_item_id: i.pos_item_id,
      name: i.name,
      quantity: i.qty,
      unit_price: i.price,
      discount_pct: i.discount_pct,
      line_total: i.line_total,
    })),
    booking_id: cart.booking ? cart.booking.id : null,
    discount_pct: cart.discount_pct,
    discount_amount: cart.discount_amount,
    payment_method: cart.payment_method,
    cash_tendered: cashTendered,
    change_given: changeGiven,
    cash_amount: cart.cash_amount,
    card_amount: cart.card_amount,
    notes: document.getElementById('sale-notes')?.value || '',
    manager_username: managerCredentials?.username || '',
    manager_password: managerCredentials?.password || '',
  };
}

function openManagerApproval(message, payload) {
  pendingApprovalPayload = payload;
  const modal = document.getElementById('manager-approval-modal');
  const msg = document.getElementById('manager-approval-message');
  if (msg) msg.textContent = message || 'Owner approval is required to complete this sale.';
  if (modal) modal.style.display = 'flex';
  setTimeout(() => document.getElementById('manager-approval-username')?.focus(), 50);
}

function closeManagerApproval() {
  pendingApprovalPayload = null;
  const modal = document.getElementById('manager-approval-modal');
  if (modal) modal.style.display = 'none';
  const btn = document.getElementById('complete-sale-btn');
  const totals = calculateTotals();
  if (btn) { btn.disabled = false; btn.textContent = `Complete Sale - R${totals.total.toFixed(2)}`; }
}

async function submitManagerApproval() {
  if (!pendingApprovalPayload) return;
  const username = document.getElementById('manager-approval-username')?.value || '';
  const password = document.getElementById('manager-approval-password')?.value || '';
  if (!username || !password) {
    showToast('Enter owner username and password', 'error');
    return;
  }
  const modal = document.getElementById('manager-approval-modal');
  if (modal) modal.style.display = 'none';
  await submitSale({...pendingApprovalPayload, manager_username: username, manager_password: password});
  const passwordInput = document.getElementById('manager-approval-password');
  if (passwordInput) passwordInput.value = '';
}

async function submitSale(payload) {
  const totals = calculateTotals();
  const btn = document.getElementById('complete-sale-btn');
  try {
    const resp = await fetch('/pos/terminal/complete/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrf(),
      },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.success) {
      pendingApprovalPayload = null;
      showReceipt(data);
    } else if (data.approval_required) {
      openManagerApproval(data.reason || data.error, payload);
    } else {
      showToast(data.error || 'Sale failed', 'error');
      if (btn) { btn.disabled = false; btn.textContent = `Complete Sale - R${totals.total.toFixed(2)}`; }
    }
  } catch (e) {
    showToast('Network error - sale not completed', 'error');
    if (btn) { btn.disabled = false; btn.textContent = `Complete Sale - R${totals.total.toFixed(2)}`; }
  }
}

async function completeSale() {
  if (cart.items.length === 0) {
    showToast('Cart is empty', 'error');
    return;
  }
  if (!cart.payment_method) {
    showToast('Please select a payment method', 'error');
    return;
  }
  if (cart.payment_method === 'room_charge' && !cart.booking) {
    showToast('Please select a room/guest for room charge', 'error');
    return;
  }
  if (cart.payment_method === 'split') {
    if (!validateSplitPayment()) {
      showToast('Split payment amounts must equal the total', 'error');
      return;
    }
  }

  const totals = calculateTotals();
  const btn = document.getElementById('complete-sale-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Processing...'; }
  await submitSale(buildSalePayload());
  return;
  if (btn) { btn.disabled = true; btn.textContent = 'Processing…'; }

  const cashTendered = parseFloat(document.getElementById('cash-tendered')?.value || 0);
  const changeGiven = Math.max(cashTendered - totals.total, 0);

  const payload = {
    items: cart.items.map(i => ({
      pos_item_id: i.pos_item_id,
      name: i.name,
      quantity: i.qty,
      unit_price: i.price,
      discount_pct: i.discount_pct,
      line_total: i.line_total,
    })),
    booking_id: cart.booking ? cart.booking.id : null,
    discount_pct: cart.discount_pct,
    discount_amount: cart.discount_amount,
    payment_method: cart.payment_method,
    cash_tendered: cashTendered,
    change_given: changeGiven,
    cash_amount: cart.cash_amount,
    card_amount: cart.card_amount,
    notes: document.getElementById('sale-notes')?.value || '',
  };

  try {
    const resp = await fetch('/pos/terminal/complete/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrf(),
      },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.success) {
      showReceipt(data);
    } else {
      showToast(data.error || 'Sale failed', 'error');
      if (btn) { btn.disabled = false; btn.textContent = `Complete Sale — R${totals.total.toFixed(2)}`; }
    }
  } catch (e) {
    showToast('Network error — sale not completed', 'error');
    if (btn) { btn.disabled = false; btn.textContent = `Complete Sale — R${totals.total.toFixed(2)}`; }
  }
}

// ─── RECEIPT MODAL ────────────────────────────────────────

function showReceipt(saleData) {
  const modal = document.getElementById('receipt-modal');
  if (!modal) return;
  const totals = calculateTotals();

  let itemRows = cart.items.map(i => `
    <tr>
      <td style="padding:4px 8px 4px 0;">${escHtml(i.emoji)} ${escHtml(i.name)}</td>
      <td style="text-align:right;padding:4px 8px;font-family:var(--font-mono);">${i.qty}</td>
      <td style="text-align:right;padding:4px 0;font-family:var(--font-mono);">R${i.price.toFixed(2)}</td>
      <td style="text-align:right;padding:4px 0 4px 8px;font-family:var(--font-mono);color:var(--color-green-400);">R${i.line_total.toFixed(2)}</td>
    </tr>
  `).join('');

  let paymentMethodLabel = (cart.payment_method || '').toUpperCase().replace('_', ' ');
  let changeRow = '';
  if (cart.payment_method === 'cash' && saleData.change_given > 0) {
    changeRow = `<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;">
      <span style="color:var(--color-text-secondary);">Change Given</span>
      <span style="font-family:var(--font-mono);color:#fbbf24;">R${parseFloat(saleData.change_given).toFixed(2)}</span>
    </div>`;
  }
  let bookingRow = '';
  if (cart.booking) {
    bookingRow = `<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;">
      <span style="color:var(--color-text-secondary);">Room Charge</span>
      <span style="font-family:var(--font-mono);">${escHtml(cart.booking.room_name)} — ${escHtml(cart.booking.reference)}</span>
    </div>`;
  }

  const now = new Date();
  const dateStr = now.toLocaleDateString('en-ZA', { day:'2-digit', month:'short', year:'numeric' });
  const timeStr = now.toLocaleTimeString('en-ZA', { hour:'2-digit', minute:'2-digit' });

  const content = document.getElementById('receipt-content');
  if (content) {
    content.innerHTML = `
      <div style="text-align:center;margin-bottom:18px;">
        <div style="font-family:var(--font-display);font-size:20px;font-weight:800;color:var(--color-text-primary);">Circle Core</div>
        <div style="font-size:10px;color:var(--color-text-tertiary);text-transform:uppercase;letter-spacing:0.1em;margin-top:2px;">Guest House</div>
        <div style="font-size:11px;color:var(--color-text-tertiary);margin-top:8px;">${dateStr} at ${timeStr}</div>
        <div style="font-family:var(--font-mono);font-size:15px;font-weight:700;color:var(--color-green-400);margin-top:6px;">${escHtml(saleData.sale_reference)}</div>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px;">
        <thead><tr style="border-bottom:1px solid var(--color-border);">
          <th style="text-align:left;padding:4px 0;font-size:11px;color:var(--color-text-tertiary);font-weight:600;">ITEM</th>
          <th style="text-align:right;padding:4px 8px;font-size:11px;color:var(--color-text-tertiary);">QTY</th>
          <th style="text-align:right;padding:4px 0;font-size:11px;color:var(--color-text-tertiary);">PRICE</th>
          <th style="text-align:right;padding:4px 0 4px 8px;font-size:11px;color:var(--color-text-tertiary);">TOTAL</th>
        </tr></thead>
        <tbody>${itemRows}</tbody>
      </table>
      <div style="border-top:1px solid var(--color-border);padding-top:10px;">
        ${totals.discountAmount > 0 ? `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px;"><span style="color:var(--color-text-secondary);">Subtotal</span><span style="font-family:var(--font-mono);">R${totals.subtotal.toFixed(2)}</span></div>
        <div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px;"><span style="color:#fbbf24;">Discount</span><span style="font-family:var(--font-mono);color:#fbbf24;">-R${totals.discountAmount.toFixed(2)}</span></div>` : ''}
        ${totals.taxAmount > 0 ? `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px;"><span style="color:var(--color-text-secondary);">VAT</span><span style="font-family:var(--font-mono);">R${totals.taxAmount.toFixed(2)}</span></div>` : ''}
        <div style="display:flex;justify-content:space-between;padding:8px 0 0;border-top:1px solid var(--color-border);margin-top:6px;">
          <span style="font-family:var(--font-display);font-weight:700;font-size:17px;color:var(--color-text-primary);">TOTAL</span>
          <span style="font-family:var(--font-display);font-weight:800;font-size:20px;color:var(--color-green-400);">R${totals.total.toFixed(2)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:13px;margin-top:4px;border-top:1px solid var(--color-border);">
          <span style="color:var(--color-text-secondary);">Payment</span>
          <span style="font-weight:500;">${paymentMethodLabel}</span>
        </div>
        ${changeRow}
        ${bookingRow}
      </div>
      <div style="text-align:center;margin-top:20px;padding-top:14px;border-top:1px dashed var(--color-border);font-size:12px;color:var(--color-text-tertiary);">Thank you for your purchase</div>
    `;
  }

  // Set PDF receipt link
  const pdfLink = document.getElementById('receipt-pdf-link');
  if (pdfLink) pdfLink.href = saleData.receipt_url;

  modal.style.display = 'flex';
  clearCart();
}

function closeReceipt() {
  const modal = document.getElementById('receipt-modal');
  if (modal) modal.style.display = 'none';
  // Re-focus barcode input
  const barcodeEl = document.getElementById('barcode-input');
  if (barcodeEl) barcodeEl.focus();
}

// ─── CAMERA SCANNING (ZXing) ─────────────────────────────

let codeReader = null;

function openCamera() {
  const modal = document.getElementById('camera-modal');
  if (!modal) return;
  modal.style.display = 'flex';

  if (typeof ZXing === 'undefined') {
    showToast('Camera scanner not available', 'error');
    closeCamera();
    return;
  }

  codeReader = new ZXing.BrowserMultiFormatReader();
  codeReader.decodeFromVideoDevice(null, 'camera-video', (result, err) => {
    if (result) {
      handleBarcodeInput(result.getText());
      closeCamera();
    }
  });
}

function closeCamera() {
  if (codeReader) {
    try { codeReader.reset(); } catch (e) { /* ignore */ }
    codeReader = null;
  }
  const modal = document.getElementById('camera-modal');
  if (modal) modal.style.display = 'none';
}

// ─── UTILITIES ────────────────────────────────────────────

function flashCartAdd() {
  const total = document.getElementById('cart-total');
  if (!total) return;
  total.style.transition = 'color 0.1s';
  total.style.color = '#4ade80';
  setTimeout(() => { total.style.color = ''; }, 400);
}

function showToast(message, type) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  const colors = {
    success: { bg: 'rgba(34,197,94,0.15)', border: 'rgba(34,197,94,0.3)', color: '#4ade80' },
    error: { bg: 'rgba(239,68,68,0.15)', border: 'rgba(239,68,68,0.3)', color: '#f87171' },
    info: { bg: 'rgba(59,130,246,0.15)', border: 'rgba(59,130,246,0.3)', color: '#93c5fd' },
  };
  const c = colors[type] || colors.info;
  toast.style.cssText = `background:${c.bg};border:1px solid ${c.border};color:${c.color};padding:10px 16px;border-radius:10px;font-size:13.5px;font-weight:500;box-shadow:0 4px 24px rgba(0,0,0,0.4);max-width:320px;animation:slideInRight 0.2s ease-out;`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; toast.style.transition = 'opacity 0.3s'; setTimeout(() => toast.remove(), 300); }, 2800);
}

function getCsrf() {
  const el = document.querySelector('[name=csrfmiddlewaretoken]');
  return el ? el.value : '';
}

function escHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escJs(str) {
  if (!str) return '';
  return String(str).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"');
}

// ─── INIT ────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  loadCartState();
  initBarcodeInput();
  renderCart();
  renderBookingChip();
  if (window.POS_ITEMS) {
    renderItems('all');
    const allTab = document.querySelector('.cat-tab[data-cat="all"]');
    if (allTab) allTab.classList.add('active');
  }
  // Auto-focus barcode input
  setTimeout(() => {
    const barcodeEl = document.getElementById('barcode-input');
    if (barcodeEl) barcodeEl.focus();
  }, 100);
});

// CSS for slide-in animation
const style = document.createElement('style');
style.textContent = `
@keyframes slideInRight {
  from { transform: translateX(40px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
.qty-btn {
  width: 32px; height: 32px; border: 1px solid var(--color-border-bright);
  background: var(--color-bg-elevated); color: var(--color-text-primary);
  font-size: 16px; cursor: pointer; border-radius: 0; display: flex;
  align-items: center; justify-content: center; transition: background 0.1s;
  font-weight: 600;
}
.qty-btn:first-child { border-radius: var(--radius-sm) 0 0 var(--radius-sm); }
.qty-btn:last-child { border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }
.qty-btn:hover { background: var(--color-bg-muted); }
.cart-row {
  padding: 12px 0; border-bottom: 1px solid var(--color-border);
  animation: fadeUp 0.15s ease-out;
}
.cart-row:last-child { border-bottom: none; }
.pos-item-card {
  position: relative; border: 1px solid rgba(255,255,255,0.06);
  border-radius: var(--radius-md); padding: 12px 10px;
  display: flex; flex-direction: column; align-items: center;
  text-align: center; cursor: pointer; transition: all 0.15s;
  min-height: 80px; justify-content: center;
}
.pos-item-card:hover:not([disabled]) {
  border-color: var(--color-green-500);
  transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(0,0,0,0.3);
}
.pos-item-card:active:not([disabled]) { transform: scale(0.96); }
.pay-method-btn.active {
  border-color: var(--color-green-500) !important;
  background: var(--color-green-glow) !important;
  color: var(--color-green-400) !important;
}
`;
document.head.appendChild(style);
