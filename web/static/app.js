'use strict';

// ── state ─────────────────────────────────────────────────────────────────────
const state = {
  snapshot:   {},
  rsi:        {},
  newsFilter: 'all',
  news:       [],
  alerts:     [],
};

// ── helpers ───────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt = (n, decimals = 2) => {
  if (!n && n !== 0) return '—';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
};
const relTime = ts => {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60)   return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};

// ── sentiment tag ─────────────────────────────────────────────────────────────
const BULL = ['surge','rally','gain','bull','record','profit','beat','strong','rise','jump','soar','growth','boom','buy','upgrade'];
const BEAR = ['crash','fall','drop','bear','loss','miss','weak','recession','inflation','risk','sell','downgrade','concern','fear','crisis','decline','plunge'];
function sentimentClass(text) {
  const t = (text || '').toLowerCase();
  const b = BULL.filter(w => t.includes(w)).length;
  const r = BEAR.filter(w => t.includes(w)).length;
  if (b > r) return 'sentiment-positive';
  if (r > b) return 'sentiment-negative';
  return 'sentiment-neutral';
}

// ── clock ──────────────────────────────────────────────────────────────────────
function startClock() {
  const update = () => {
    const now = new Date();
    $('clock').textContent = now.toLocaleTimeString('en-US', {
      timeZone: 'America/New_York',
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
    }) + ' ET';
  };
  update();
  setInterval(update, 1000);
}

// ── ticker bar ────────────────────────────────────────────────────────────────
function renderTicker(snap) {
  const items = Object.entries(snap).map(([sym, d]) => {
    const cls = d.change_pct >= 0 ? 'up' : 'dn';
    const arrow = d.change_pct >= 0 ? '▲' : '▼';
    return `<span class="tick-item">
      <span class="sym">${sym}</span>
      <span class="prc">${fmt(d.current)}</span>
      <span class="${cls}">${arrow} ${Math.abs(d.change_pct).toFixed(2)}%</span>
    </span>`;
  }).join('');
  // Duplicate for seamless loop
  $('ticker-inner').innerHTML = items + items;
}

// ── price cards ───────────────────────────────────────────────────────────────
function renderPriceCards(snap, rsi) {
  const container = $('price-cards');
  Object.entries(snap).forEach(([sym, d]) => {
    const existing = container.querySelector(`[data-sym="${sym}"]`);
    const cls  = d.change_pct >= 0 ? 'up' : 'dn';
    const arrow = d.change_pct >= 0 ? '▲' : '▼';
    const rsiInfo = rsi[sym] || {};
    const rsiVal  = rsiInfo.rsi;
    const rsiSig  = rsiInfo.signal || 'neutral';
    const rsiColor = rsiVal > 70 ? '#ff3d5a' : rsiVal < 30 ? '#00e676' : '#4fc3f7';
    const rsiPct   = rsiVal ? Math.min(rsiVal, 100) : 50;

    const priceStr = sym === 'BTC' ? fmt(d.current, 0) : fmt(d.current);
    const html = `
      <div class="pc-header">
        <span class="pc-sym">${sym}</span>
        <span class="pc-type">${d.type || ''}</span>
      </div>
      <div class="pc-price">${sym === 'BTC' ? '$' : ''}${priceStr}</div>
      <div class="pc-footer">
        <span class="pc-chg ${cls}">${arrow} ${Math.abs(d.change_pct).toFixed(2)}%</span>
        <div style="display:flex;align-items:center;gap:4px;">
          <div class="rsi-bar"><div class="rsi-fill" style="width:${rsiPct}%;background:${rsiColor}"></div></div>
          <span class="rsi-label">${rsiVal ? 'RSI ' + rsiVal : 'RSI —'}</span>
        </div>
      </div>`;

    if (existing) {
      const old = parseFloat(existing.dataset.price);
      if (d.current !== old) {
        existing.classList.remove('flash-up', 'flash-down');
        void existing.offsetWidth;
        existing.classList.add(d.current > old ? 'flash-up' : 'flash-down');
      }
      existing.innerHTML = html;
      existing.dataset.price = d.current;
    } else {
      const card = document.createElement('div');
      card.className = 'price-card';
      card.dataset.sym = sym;
      card.dataset.price = d.current;
      card.innerHTML = html;
      container.appendChild(card);
    }
  });
}

// ── news ──────────────────────────────────────────────────────────────────────
function renderNews() {
  const list = $('news-list');
  const filtered = state.newsFilter === 'all'
    ? state.news
    : state.news.filter(n => n.category === state.newsFilter);

  list.innerHTML = filtered.slice(0, 30).map(n => {
    const sent = sentimentClass(n.headline + ' ' + n.summary);
    const sentIcon = sent === 'sentiment-positive' ? '🟢' : sent === 'sentiment-negative' ? '🔴' : '⚪';
    return `<a class="news-item ${sent}" href="${n.url}" target="_blank" rel="noopener">
      <div class="ni-header">
        <span class="ni-source">${n.source || 'News'}</span>
        <span class="ni-cat ${n.category}">${n.category}</span>
        <span class="ni-time">${relTime(n.datetime)}</span>
      </div>
      <div class="ni-headline">${sentIcon} ${n.headline}</div>
      ${n.summary ? `<div class="ni-summary">${n.summary}</div>` : ''}
    </a>`;
  }).join('');

  if (!filtered.length) {
    list.innerHTML = '<div style="color:var(--muted);text-align:center;padding:20px">No news yet…</div>';
  }
}

// ── Fear & Greed gauge ────────────────────────────────────────────────────────
function drawGauge(value) {
  const canvas = $('fg-canvas');
  const ctx    = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const cx = W / 2, cy = H - 10, r = W * 0.42;
  const startAngle = Math.PI, endAngle = 0;

  // Background arc
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 0);
  ctx.lineWidth = 14;
  ctx.strokeStyle = '#1d2333';
  ctx.stroke();

  // Colour zones
  const zones = [
    { from: 0,   to: 25,  color: '#ff3d5a' },
    { from: 25,  to: 45,  color: '#ff8a65' },
    { from: 45,  to: 55,  color: '#ffc107' },
    { from: 55,  to: 75,  color: '#aed581' },
    { from: 75,  to: 100, color: '#00e676' },
  ];
  zones.forEach(z => {
    const a1 = Math.PI + (z.from / 100) * Math.PI;
    const a2 = Math.PI + (z.to   / 100) * Math.PI;
    ctx.beginPath();
    ctx.arc(cx, cy, r, a1, a2);
    ctx.lineWidth = 14;
    ctx.strokeStyle = z.color + '88';
    ctx.stroke();
  });

  // Filled arc up to value
  const fillColor = value < 25 ? '#ff3d5a' : value < 45 ? '#ff8a65' : value < 55 ? '#ffc107' : value < 75 ? '#aed581' : '#00e676';
  const fillAngle = Math.PI + (Math.min(value, 100) / 100) * Math.PI;
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, fillAngle);
  ctx.lineWidth = 14;
  ctx.strokeStyle = fillColor;
  ctx.stroke();

  // Needle
  const needleAngle = Math.PI + (value / 100) * Math.PI;
  const nx = cx + (r - 7) * Math.cos(needleAngle);
  const ny = cy + (r - 7) * Math.sin(needleAngle);
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(nx, ny);
  ctx.lineWidth = 2;
  ctx.strokeStyle = '#fff';
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(cx, cy, 4, 0, Math.PI * 2);
  ctx.fillStyle = '#fff';
  ctx.fill();

  // Labels
  ctx.font = '9px sans-serif';
  ctx.fillStyle = '#5a6070';
  ctx.textAlign = 'left';  ctx.fillText('Fear', 14, cy - 4);
  ctx.textAlign = 'right'; ctx.fillText('Greed', W - 14, cy - 4);
}

function updateFearGreed(fg) {
  const val = fg.value || 50;
  drawGauge(val);
  $('fg-value').textContent = val;
  $('fg-value').style.color = val < 25 ? '#ff3d5a' : val < 45 ? '#ff8a65' : val < 55 ? '#ffc107' : val < 75 ? '#aed581' : '#00e676';
  $('fg-label').textContent = fg.label || 'Neutral';
  $('fg-label').style.color = $('fg-value').style.color;
}

// ── social sentiment ──────────────────────────────────────────────────────────
function updateSocial(social) {
  const rScore  = Math.max(0, Math.min((social.reddit_score  + 1) / 2 * 100, 100));
  const tScore  = Math.max(0, Math.min((social.twitter_score + 1) / 2 * 100, 100));
  $('social-reddit-bar').style.width  = rScore + '%';
  $('social-twitter-bar').style.width = tScore + '%';
  $('social-reddit-val').textContent  = social.reddit_mentions  + ' mentions';
  $('social-twitter-val').textContent = social.twitter_mentions + ' mentions';
}

// ── alerts ────────────────────────────────────────────────────────────────────
function renderAlerts(alerts) {
  const list = $('alerts-list');
  if (!alerts || !alerts.length) {
    list.innerHTML = '<div id="no-alerts">No alerts yet</div>';
    return;
  }
  list.innerHTML = alerts.slice(0, 20).map(a => {
    const cls   = a.direction === 'UP' ? 'up' : 'dn';
    const arrow = a.direction === 'UP' ? '▲' : '▼';
    return `<div class="alert-item">
      <span class="al-sym">${a.symbol}</span>
      <span class="al-dir ${cls}">${arrow} ${a.pct}%</span>
      <span class="al-pct">$${fmt(a.current)}</span>
      <span class="al-time">${relTime(a.timestamp)}</span>
    </div>`;
  }).join('');
}

function pushAlert(alert) {
  const isUp = alert.direction === 'UP';
  showToast(
    `${alert.symbol} ${isUp ? '▲' : '▼'} ${alert.pct}%`,
    `${alert.name} moved to $${fmt(alert.current)}`,
    isUp ? 'up' : 'down'
  );
  if (Notification.permission === 'granted') {
    new Notification(`${alert.symbol} ${isUp ? 'UP' : 'DOWN'} ${alert.pct}%`, {
      body: `${alert.name} is now $${fmt(alert.current)}`,
      icon: '/favicon.ico',
      tag:  alert.symbol,
    });
  }
}

// ── toast ──────────────────────────────────────────────────────────────────────
function showToast(title, msg, type = 'up') {
  const icon = type === 'up' ? '📈' : '📉';
  const el   = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<div class="toast-icon">${icon}</div>
    <div class="toast-body">
      <div class="toast-title">${title}</div>
      <div class="toast-msg">${msg}</div>
    </div>`;
  $('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

// ── WebSocket ──────────────────────────────────────────────────────────────────
let ws, wsRetries = 0;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    $('ws-dot').classList.add('live');
    $('ws-status-text').textContent = 'LIVE';
    wsRetries = 0;
  };

  ws.onmessage = ({ data }) => {
    const msg = JSON.parse(data);
    if (msg.type === 'update') {
      const prevSnap = {...state.snapshot};
      state.snapshot = msg.snapshot;
      renderTicker(state.snapshot);
      renderPriceCards(state.snapshot, state.rsi);
      (msg.alerts || []).forEach(a => {
        pushAlert(a);
        state.alerts.unshift(a);
      });
      renderAlerts(state.alerts);
      $('last-updated').textContent = 'Updated: ' + new Date().toLocaleTimeString();
    }
  };

  ws.onclose = () => {
    $('ws-dot').classList.remove('live');
    $('ws-status-text').textContent = 'RECONNECTING';
    const delay = Math.min(1000 * 2 ** wsRetries, 30000);
    wsRetries++;
    setTimeout(connectWS, delay);
  };

  ws.onerror = () => ws.close();
}

// ── fetch helpers ──────────────────────────────────────────────────────────────
async function fetchJSON(url) {
  const r = await fetch(url);
  return r.json();
}

async function loadInitialData() {
  try {
    const [snapRes, newsRes, sentRes, alertsRes, rsiRes] = await Promise.all([
      fetchJSON('/api/snapshot'),
      fetchJSON('/api/news'),
      fetchJSON('/api/sentiment'),
      fetchJSON('/api/alerts'),
      fetchJSON('/api/rsi'),
    ]);

    state.snapshot = snapRes.data;
    state.rsi      = rsiRes.data || {};
    state.news     = newsRes.data || [];
    state.alerts   = alertsRes.data || [];

    renderTicker(state.snapshot);
    renderPriceCards(state.snapshot, state.rsi);
    renderNews();
    renderAlerts(state.alerts);
    updateFearGreed(sentRes.fear_greed);
    updateSocial(sentRes.social);

    // Kick off periodic refreshes
    setInterval(refreshNews,      5 * 60 * 1000);  // 5 min
    setInterval(refreshSentiment, 60 * 60 * 1000); // 1 hr
    setInterval(refreshRSI,       60 * 60 * 1000); // 1 hr
    setInterval(refreshAlerts,    2  * 60 * 1000); // 2 min

  } catch (e) {
    console.error('Initial load failed', e);
  }
}

async function refreshNews() {
  try {
    const res = await fetchJSON('/api/news');
    state.news = res.data || [];
    renderNews();
  } catch {}
}

async function refreshSentiment() {
  try {
    const res = await fetchJSON('/api/sentiment');
    updateFearGreed(res.fear_greed);
    updateSocial(res.social);
  } catch {}
}

async function refreshRSI() {
  try {
    const res = await fetchJSON('/api/rsi');
    state.rsi = res.data || {};
    renderPriceCards(state.snapshot, state.rsi);
  } catch {}
}

async function refreshAlerts() {
  try {
    const res = await fetchJSON('/api/alerts');
    state.alerts = res.data || [];
    renderAlerts(state.alerts);
  } catch {}
}

// ── notifications ──────────────────────────────────────────────────────────────
$('notif-btn').addEventListener('click', async () => {
  if (Notification.permission === 'granted') {
    showToast('Notifications', 'Already enabled', 'up');
    return;
  }
  const perm = await Notification.requestPermission();
  if (perm === 'granted') {
    $('notif-btn').textContent = '🔔 ON';
    showToast('Notifications', 'Enabled! You\'ll get alerts here.', 'up');
  }
});

// ── tabs ───────────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    state.newsFilter = btn.dataset.cat;
    renderNews();
  });
});

// ── init ───────────────────────────────────────────────────────────────────────
startClock();
loadInitialData();
connectWS();

// Update notification button state
if (Notification.permission === 'granted') {
  $('notif-btn').textContent = '🔔 ON';
}
