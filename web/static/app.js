'use strict';

// ── state ────────────────────────────────────────────────────────────────────
const S = {
  snapshot:    {},
  rsi:         {},
  technicals:  {},
  analysis:    {},
  analystRecs: {},
  news:        [],
  alerts:      [],
  newsFilter:  'all',
  assetFilter: 'all',
  viewMode:    'simple',  // 'simple' | 'expert'
};

// ── helpers ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt = (n, d = 2) => n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const relTime = ts => {
  const s = Date.now() / 1000 - ts;
  if (s < 60)    return `${Math.floor(s)}s ago`;
  if (s < 3600)  return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};
const sent = text => {
  const t = (text||'').toLowerCase();
  const b = ['surge','rally','gain','bull','record','profit','beat','strong','jump','soar','rise','growth'].filter(w => t.includes(w)).length;
  const r = ['crash','fall','drop','bear','loss','miss','weak','recession','risk','sell','decline','plunge','fear','crisis'].filter(w => t.includes(w)).length;
  return b > r ? 'pos' : r > b ? 'neg' : 'neu';
};

// ── clock ────────────────────────────────────────────────────────────────────
setInterval(() => {
  $('clock').textContent = new Date().toLocaleTimeString('en-US', {
    timeZone: 'America/New_York', hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  }) + ' ET';
}, 1000);

// ── ticker ───────────────────────────────────────────────────────────────────
function renderTicker(snap) {
  const items = Object.entries(snap).map(([sym, d]) => {
    const cls = d.change_pct >= 0 ? 'up' : 'dn';
    const arr = d.change_pct >= 0 ? '▲' : '▼';
    const price = sym === 'BTC' || sym === 'ETH' ? fmt(d.current, 0) : fmt(d.current);
    return `<span class="tick-item">
      <span class="tsym">${sym}</span>
      <span class="tprc">${price}</span>
      <span class="${cls}">${arr} ${Math.abs(d.change_pct).toFixed(2)}%</span>
    </span>`;
  }).join('');
  $('ticker-inner').innerHTML = items + items;
}

// ── hero ─────────────────────────────────────────────────────────────────────
function renderHero(analysis) {
  if (!analysis || !analysis.mood) return;
  const mb = $('mood-badge');
  mb.textContent = analysis.mood;
  mb.className   = 'mood-badge ' + (analysis.mood_color || 'yellow');
  $('mood-desc').textContent    = analysis.mood_desc;
  $('mood-ts').textContent      = 'Updated ' + relTime(analysis.timestamp);
  $('summary-text').textContent = analysis.simple_summary;

  const st = analysis.stats || {};
  $('stat-up').textContent   = st.up_count   ?? '—';
  $('stat-down').textContent = st.down_count  ?? '—';
  $('stat-vix').textContent  = st.vix != null ? st.vix.toFixed(1) : '—';

  const fgEl = $('stat-fg');
  fgEl.textContent = st.fear_greed ?? '—';
  fgEl.style.color = st.fear_greed < 30 ? 'var(--dn)' : st.fear_greed > 70 ? 'var(--gold)' : 'var(--up)';
}

// ── intelligence signals ──────────────────────────────────────────────────────
const SIG_KIND_LABEL = {
  vol:'Volatility', sent:'Sentiment', fx:'Currency', energy:'Energy',
  crypto:'Crypto', tech:'Tech / AI', banks:'Banks', geo:'Geopolitical',
};

function renderSignals(analysis) {
  if (!analysis || !analysis.signals) return;

  // Signals
  $('signal-grid').innerHTML = (analysis.signals || []).map(sig => {
    const kindLabel = SIG_KIND_LABEL[sig.kind] || (sig.type || 'Signal').toUpperCase();
    return `
    <div class="signal-card ${sig.type || 'info'}">
      <span class="sig-kind ${sig.type || ''}">${kindLabel}</span>
      <div class="sig-title">${sig.title}</div>
      <div class="sig-text">${S.viewMode === 'expert' ? sig.expert : sig.plain}</div>
    </div>`;
  }).join('') || '<p style="color:var(--muted)">Loading intelligence signals…</p>';

  // Top movers
  $('movers-row').innerHTML = (analysis.top_movers || []).map(m => {
    const cls = m.change_pct >= 0 ? 'up' : 'dn';
    const arr = m.change_pct >= 0 ? '▲' : '▼';
    return `<div class="mover-chip">
      <span class="mover-sym">${m.symbol}</span>
      <span class="mover-name">${m.name}</span>
      <span class="mover-chg ${cls}">${arr} ${Math.abs(m.change_pct).toFixed(2)}%</span>
    </div>`;
  }).join('');
}

// ── price cards ───────────────────────────────────────────────────────────────
// Map symbols to TradingView chart URLs with explicit exchange:ticker
// (the chart URL always resolves correctly, unlike /symbols/ redirects)
const TV_LINK = {
  ES:   'AMEX:SPY',
  NQ:   'NASDAQ:QQQ',
  DJI:  'AMEX:DIA',
  RUT:  'AMEX:IWM',
  VIX:  'TVC:VIX',
  CL:   'TVC:USOIL',
  GC:   'TVC:GOLD',
  DXY:  'TVC:DXY',
  BTC:  'BINANCE:BTCUSDT',
  ETH:  'BINANCE:ETHUSDT',
  SOL:  'BINANCE:SOLUSDT',
  NVDA: 'NASDAQ:NVDA',
  AAPL: 'NASDAQ:AAPL',
  TSLA: 'NASDAQ:TSLA',
  META: 'NASDAQ:META',
  AMZN: 'NASDAQ:AMZN',
  MSFT: 'NASDAQ:MSFT',
  JPM:  'NYSE:JPM',
  XOM:  'NYSE:XOM',
};
const tvUrl = sym => `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(TV_LINK[sym] || sym)}`;

function renderPrices(snap, rsi) {
  const grid = $('price-grid');
  const entries = Object.entries(snap).filter(([, d]) =>
    S.assetFilter === 'all' || d.type === S.assetFilter
  );

  // Hide/show existing cards
  grid.querySelectorAll('.price-card').forEach(el => {
    const d = snap[el.dataset.sym];
    el.style.display = (S.assetFilter === 'all' || (d && d.type === S.assetFilter)) ? '' : 'none';
  });

  entries.forEach(([sym, d]) => {
    const isUp   = d.change_pct >= 0;
    const cls    = isUp ? 'up' : 'dn';
    const arr    = isUp ? '▲' : '▼';
    const rsiInfo = rsi[sym] || {};
    const rsiVal  = rsiInfo.rsi;
    const rsiColor = rsiVal > 70 ? 'var(--dn)' : rsiVal < 30 ? 'var(--up)' : 'var(--blue)';
    const rsiPct   = rsiVal ? Math.min(rsiVal, 100) : 50;
    const isCrypto = sym === 'BTC' || sym === 'ETH';
    const price    = isCrypto ? '$' + fmt(d.current, 0) : '$' + fmt(d.current);

    // Day range bar
    const h = d.high, l = d.low, c = d.current;
    const rangePos = (h > l) ? Math.round(((c - l) / (h - l)) * 100) : 50;

    const inner = `
      <div class="pc-top">
        <span class="pc-sym">${sym}</span>
        <span class="pc-type ${d.type || ''}">${d.type || ''}</span>
      </div>
      <div class="pc-price">${price}</div>
      <div class="pc-chg ${cls}">${arr} ${Math.abs(d.change_pct).toFixed(2)}%</div>
      <div class="pc-name">${d.name}</div>
      ${h && l ? `
      <div class="pc-range">
        <div class="pc-range-bar"><div class="pc-range-fill" style="width:${rangePos}%"></div></div>
        <div class="pc-range-labels"><span>L $${fmt(l)}</span><span>H $${fmt(h)}</span></div>
      </div>` : ''}
      ${d.type !== 'crypto' ? `<div class="pc-rsi">
        <div class="rsi-bar"><div class="rsi-fill" style="width:${rsiPct}%;background:${rsiColor}"></div></div>
        <span class="rsi-txt">${rsiVal ? 'RSI ' + rsiVal : 'RSI —'}</span>
      </div>` : ''}`;

    let card = grid.querySelector(`[data-sym="${sym}"]`);
    if (card) {
      const oldPrice = parseFloat(card.dataset.price);
      if (d.current !== oldPrice) {
        card.classList.remove('flash-up', 'flash-dn');
        void card.offsetWidth;
        card.classList.add(d.current > oldPrice ? 'flash-up' : 'flash-dn');
      }
      card.innerHTML = inner;
      card.dataset.price = d.current;
    } else {
      card = document.createElement('a');
      card.className    = 'price-card';
      card.dataset.sym  = sym;
      card.dataset.price = d.current;
      card.href         = tvUrl(sym);
      card.target       = '_blank';
      card.rel          = 'noopener';
      card.title        = `Open ${sym} chart on TradingView`;
      card.innerHTML    = inner;
      grid.appendChild(card);
    }
  });
}

// ── technicals table ──────────────────────────────────────────────────────────
function pill(val, opts) {
  const cls = opts[val] || 'na';
  const label = val || '—';
  return `<span class="sig-pill ${cls}">${label}</span>`;
}
function renderTechnicals(tech) {
  const tbody = $('tech-tbody');
  const rows = Object.entries(tech).map(([sym, d]) => {
    if (!d.available) {
      return `<tr>
        <td><strong>${sym}</strong></td>
        <td>${d.type}</td>
        <td colspan="7"><span class="sig-pill na">— ${d.type === 'crypto' ? 'Crypto — candles not supported' : 'Insufficient data'}</span></td>
      </tr>`;
    }
    const rsiPill  = d.rsi ? `<span class="sig-pill ${d.rsi_signal === 'overbought' ? 'bear' : d.rsi_signal === 'oversold' ? 'bull' : 'neut'}">${d.rsi}</span>` : '<span class="sig-pill na">—</span>';
    const vs50Pill = d.vs_sma50 == null ? '<span class="sig-pill na">—</span>'
                   : `<span class="sig-pill ${d.vs_sma50 === 'above' ? 'bull' : 'bear'}">${d.vs_sma50 === 'above' ? '↑ Above' : '↓ Below'}</span>`;
    const vs200Pill= d.vs_sma200 == null ? '<span class="sig-pill na">—</span>'
                   : `<span class="sig-pill ${d.vs_sma200 === 'above' ? 'bull' : 'bear'}">${d.vs_sma200 === 'above' ? '↑ Above' : '↓ Below'}</span>`;
    const gcPill   = d.golden_cross == null ? '<span class="sig-pill na">—</span>'
                   : d.golden_cross ? '<span class="sig-pill bull">Golden ✓</span>'
                                    : '<span class="sig-pill bear">Death ✗</span>';
    const macdPill = pill(d.macd_trend, { bullish: 'bull', bearish: 'bear' });
    const bbPill   = pill(d.bb_signal,  { neutral: 'neut', overbought: 'bear', oversold: 'bull' });
    const ovPill   = pill(d.overall,    { bullish: 'bull', bearish: 'bear', neutral: 'neut' });
    return `<tr>
      <td><a href="${tvUrl(sym)}" target="_blank" rel="noopener" class="tech-link"><strong style="color:#fff">${sym}</strong></a> <span style="color:var(--muted);font-size:10px">${d.name}</span></td>
      <td><span class="pc-type ${d.type}" style="display:inline-block">${d.type}</span></td>
      <td>${rsiPill}</td>
      <td>${vs50Pill}</td>
      <td>${vs200Pill}</td>
      <td>${gcPill}</td>
      <td>${macdPill}</td>
      <td>${bbPill}</td>
      <td>${ovPill}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.join('') || '<tr><td colspan="9" style="color:var(--muted);text-align:center;padding:20px">Loading…</td></tr>';
}

// ── Fear & Greed gauge ────────────────────────────────────────────────────────
function drawGauge(v) {
  const canvas = $('fg-canvas'), ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height, cx = W/2, cy = H-8, r = W*0.42;
  ctx.clearRect(0,0,W,H);
  ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,0); ctx.lineWidth=14; ctx.strokeStyle='#1c2235'; ctx.stroke();
  [
    [0,25,'#ff3d5a'], [25,45,'#ff7043'], [45,55,'#ffc107'], [55,75,'#aed581'], [75,100,'#00e676']
  ].forEach(([a,b,col]) => {
    ctx.beginPath(); ctx.arc(cx,cy,r, Math.PI+(a/100)*Math.PI, Math.PI+(b/100)*Math.PI);
    ctx.lineWidth=14; ctx.strokeStyle=col+'66'; ctx.stroke();
  });
  const col = v<25?'#ff3d5a':v<45?'#ff7043':v<55?'#ffc107':v<75?'#aed581':'#00e676';
  ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,Math.PI+(v/100)*Math.PI); ctx.lineWidth=14; ctx.strokeStyle=col; ctx.stroke();
  const ang = Math.PI+(v/100)*Math.PI;
  ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+(r-7)*Math.cos(ang),cy+(r-7)*Math.sin(ang));
  ctx.lineWidth=2; ctx.strokeStyle='#fff'; ctx.stroke();
  ctx.beginPath(); ctx.arc(cx,cy,4,0,Math.PI*2); ctx.fillStyle='#fff'; ctx.fill();
}

function fgColor(v) {
  return v<25?'#ff3d5a':v<45?'#ff7043':v<55?'#ffc107':v<75?'#aed581':'#00e676';
}

function renderFearGreed(fg) {
  const v = fg.value || 50;
  drawGauge(v);
  const col = fgColor(v);
  $('fg-value').textContent = v; $('fg-value').style.color = col;
  $('fg-label').textContent = fg.label||'Neutral'; $('fg-label').style.color = col;

  // Change vs yesterday
  const ch = fg.change_1d || 0;
  const chEl = $('fg-change');
  if (chEl) {
    chEl.textContent = ch === 0 ? 'Unchanged from yesterday'
      : `${ch > 0 ? '▲' : '▼'} ${Math.abs(ch)} pts vs yesterday (was ${fg.prev_value})`;
    chEl.style.color = ch > 0 ? 'var(--up)' : ch < 0 ? 'var(--dn)' : 'var(--muted)';
  }

  // Advice
  const advEl = $('fg-advice');
  if (advEl) advEl.textContent = fg.advice || '';

  // 7-day history bars
  const histEl = $('fg-history');
  if (histEl && fg.history && fg.history.length) {
    const maxV = 100;
    histEl.innerHTML = [...fg.history].reverse().map(h => {
      const pct  = Math.round(h.value / maxV * 100);
      const col2 = fgColor(h.value);
      const date = new Date(h.timestamp * 1000).toLocaleDateString('en-US', {month:'short', day:'numeric'});
      return `<div class="fg-bar" style="height:${pct}%;background:${col2}">
        <div class="fg-bar-tip">${date}: ${h.value} (${h.label})</div>
      </div>`;
    }).join('');
  }
}

// ── breadth bar ───────────────────────────────────────────────────────────────
function renderBreadth(snap) {
  const entries = Object.entries(snap);
  const up    = entries.filter(([,d]) => d.change_pct >= 0);
  const down  = entries.filter(([,d]) => d.change_pct < 0);
  const total = entries.length;
  const pct   = total ? Math.round(up.length/total*100) : 50;
  $('breadth-bar-up').style.width = pct + '%';
  $('breadth-labels').innerHTML =
    `<span class="up">${up.length} up (${pct}%)</span><span class="dn">${down.length} down (${100-pct}%)</span>`;
  $('breadth-note').textContent = pct >= 70 ? 'Strong broad advance — healthy bullish breadth'
    : pct >= 55 ? 'More assets rising than falling — mild bullish breadth'
    : pct <= 30 ? 'Broad market decline — widespread selling pressure'
    : pct <= 45 ? 'More assets falling — mild bearish breadth'
    : 'Mixed market — no clear directional bias';

  // Breakdown by type
  const byType = {};
  entries.forEach(([sym, d]) => {
    const t = d.type || 'other';
    if (!byType[t]) byType[t] = {up:0,dn:0};
    d.change_pct >= 0 ? byType[t].up++ : byType[t].dn++;
  });
  const bdEl = $('breadth-breakdown');
  if (bdEl) {
    bdEl.innerHTML = Object.entries(byType).map(([t, v]) => {
      const tot = v.up + v.dn;
      const upPct = Math.round(v.up/tot*100);
      return `<div class="bd-row">
        <span class="bd-sym">${t.charAt(0).toUpperCase()+t.slice(1)}</span>
        <span class="bd-val ${v.up >= v.dn ? 'up' : 'dn'}">${v.up}/${tot} up (${upPct}%)</span>
      </div>`;
    }).join('');
  }
}

// ── social (multi-stock) ──────────────────────────────────────────────────────
function renderSocial(social) {
  const list = $('social-list');
  if (!list) return;
  if (!social || !Object.keys(social).length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px">Loading social data…</div>';
    return;
  }
  const rows = Object.entries(social).map(([sym, d]) => {
    const rPct  = Math.max(0, Math.min(((d.reddit_score||0)+1)/2*100, 100));
    const tPct  = Math.max(0, Math.min(((d.twitter_score||0)+1)/2*100, 100));
    const ocls  = d.overall || 'neutral';
    const oLbl  = ocls === 'bullish' ? '🟢 Bullish' : ocls === 'bearish' ? '🔴 Bearish' : '⚪ Neutral';
    const rColor = rPct > 60 ? 'var(--up)' : rPct < 40 ? 'var(--dn)' : 'var(--accent)';
    const tColor = tPct > 60 ? 'var(--up)' : tPct < 40 ? 'var(--dn)' : 'var(--accent)';
    return `<div class="social-sym-row">
      <span class="social-sym">${sym}</span>
      <div class="social-bars-col">
        <div>
          <div class="social-bar-label">Reddit: ${d.reddit_mentions||0} mentions</div>
          <div class="soc-mini-bar-outer"><div class="soc-mini-bar-inner" style="width:${rPct}%;background:${rColor}"></div></div>
        </div>
        <div>
          <div class="social-bar-label">Twitter: ${d.twitter_mentions||0} mentions</div>
          <div class="soc-mini-bar-outer"><div class="soc-mini-bar-inner" style="width:${tPct}%;background:${tColor}"></div></div>
        </div>
      </div>
      <span class="social-overall ${ocls}">${oLbl}</span>
    </div>`;
  });
  list.innerHTML = rows.join('');
}

// ── insider trades ────────────────────────────────────────────────────────────
function renderInsider(trades) {
  const list = $('insider-list');
  if (!list) return;
  if (!trades || !trades.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px">Loading insider data…</div>';
    return;
  }
  list.innerHTML = trades.map(t => {
    const sig  = t.signal;
    const sigColor = sig === 'bullish' ? 'var(--up)' : sig === 'bearish' ? 'var(--dn)' : 'var(--muted)';
    const sigIcon  = sig === 'bullish' ? '🟢' : sig === 'bearish' ? '🔴' : '⚪';
    const recentHtml = (t.recent || []).map(x =>
      `<div style="font-size:10px;color:var(--muted);margin-top:2px">
        ${x.action === 'BUY' ? '▲' : '▼'} <strong style="color:${x.action==='BUY'?'var(--up)':'var(--dn)'}">${x.action}</strong>
        ${x.shares.toLocaleString()} shares @ $${fmt(x.price)} — ${x.name.split(' ').slice(-1)[0]} · ${x.date}
      </div>`
    ).join('');
    return `<div class="social-sym-row" style="flex-direction:column;align-items:flex-start;gap:4px">
      <div style="display:flex;justify-content:space-between;width:100%;align-items:center">
        <span style="font-weight:700;color:#fff;font-size:13px">${t.symbol}</span>
        <span style="font-size:11px;font-weight:700;color:${sigColor}">${sigIcon} ${sig.toUpperCase()}</span>
      </div>
      <div style="display:flex;gap:14px;font-size:11px;color:var(--muted)">
        <span class="up">▲ ${t.buy_count} buys ($${t.buy_value}M)</span>
        <span class="dn">▼ ${t.sell_count} sells ($${t.sell_value}M)</span>
      </div>
      ${recentHtml}
    </div>`;
  }).join('');
}

// ── analyst recs ──────────────────────────────────────────────────────────────
function renderAnalyst(recs) {
  const list = $('analyst-list');
  if (!recs || !Object.keys(recs).length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px">Loading analyst data…</div>';
    return;
  }
  list.innerHTML = Object.entries(recs).map(([sym, r]) => {
    const t = r.total || 1;
    const sb = r.strongBuy||0, b = r.buy||0, h = r.hold||0, s = r.sell||0, ss = r.strongSell||0;
    const seg = (n) => `${(n/t)*100}%`;

    let trendHtml = '';
    if (r.trend !== null && r.trend !== undefined) {
      const t3 = r.trend;
      if (Math.abs(t3) < 0.05) {
        trendHtml = `<div class="analyst-trend">Stable vs 3 months ago</div>`;
      } else if (t3 > 0) {
        trendHtml = `<div class="analyst-trend up">▲ Sentiment improved (+${t3}) vs 3 months ago</div>`;
      } else {
        trendHtml = `<div class="analyst-trend down">▼ Sentiment cooled (${t3}) vs 3 months ago</div>`;
      }
    }

    let targetHtml = '';
    if (r.target && r.target.median) {
      const tg = r.target;
      targetHtml = `<div class="analyst-trend">Median target: <strong style="color:var(--text)">$${tg.median}</strong>` +
        (tg.high && tg.low ? ` <span style="color:var(--muted)">(range $${tg.low}–$${tg.high})</span>` : '') +
        `</div>`;
    }

    return `<div class="analyst-row">
      <div class="analyst-top">
        <span class="analyst-sym">${sym}</span>
        <span class="analyst-consensus ${r.consensus}">${r.consensus?.toUpperCase()} (score ${r.score})</span>
      </div>
      <div class="analyst-bar" title="Strong Buy / Buy / Hold / Sell / Strong Sell">
        <div class="analyst-seg sb" style="width:${seg(sb)}"></div>
        <div class="analyst-seg b"  style="width:${seg(b)}"></div>
        <div class="analyst-seg h"  style="width:${seg(h)}"></div>
        <div class="analyst-seg s"  style="width:${seg(s)}"></div>
        <div class="analyst-seg ss" style="width:${seg(ss)}"></div>
      </div>
      <div class="analyst-counts">
        <span><strong>${sb}</strong> Strong Buy</span>
        <span><strong>${b}</strong> Buy</span>
        <span><strong>${h}</strong> Hold</span>
        <span><strong>${s}</strong> Sell</span>
        <span><strong>${ss}</strong> Strong Sell</span>
        <span style="margin-left:auto;color:var(--muted)">${t} analysts</span>
      </div>
      ${targetHtml}
      ${trendHtml}
    </div>`;
  }).join('');
}

// ── correlations ─────────────────────────────────────────────────────────────
function renderCorrelations(corrs) {
  const list = $('correlations-list');
  if (!list) return;
  if (!corrs || !corrs.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px 0">Loading correlations…</div>';
    return;
  }
  list.innerHTML = corrs.map(c => {
    return `<div class="corr-row">
      <div>
        <div class="corr-pair">${c.pair}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">
          A: ${c.a_pct >= 0 ? '+' : ''}${c.a_pct}% · B: ${c.b_pct >= 0 ? '+' : ''}${c.b_pct}%
        </div>
      </div>
      <div class="corr-desc">${c.why}</div>
      <span class="corr-state ${c.state}">${c.label}</span>
    </div>`;
  }).join('');
}

async function loadCorrelations() {
  try { const r = await api('/api/correlations'); renderCorrelations(r.data || []); }
  catch {}
}

// ── news ─────────────────────────────────────────────────────────────────────
const BULL_W = ['surge','rally','gain','bull','record','profit','beat','strong','jump','soar'];
const BEAR_W = ['crash','fall','drop','bear','loss','miss','weak','recession','risk','decline','plunge'];

function renderNews() {
  const filtered = S.newsFilter === 'all' ? S.news : S.news.filter(n => n.category === S.newsFilter);
  $('news-grid').innerHTML = filtered.slice(0, 40).map(n => {
    const cls = sent(n.headline + ' ' + n.summary);
    return `<a class="news-card ${cls}" href="${n.url}" target="_blank" rel="noopener">
      <div class="nc-top">
        <span class="nc-source">${n.source||'News'}</span>
        <span class="nc-cat ${n.category}">${n.category}</span>
        <span class="nc-time">${relTime(n.datetime)}</span>
      </div>
      <div class="nc-headline"><span class="nc-tone ${cls}"></span>${n.headline}</div>
      ${n.summary ? `<div class="nc-summary">${n.summary}</div>` : ''}
    </a>`;
  }).join('') || '<div style="color:var(--muted);padding:20px">No news yet…</div>';
}

// ── alerts ────────────────────────────────────────────────────────────────────
function renderAlerts(alerts) {
  const grid = $('alerts-grid');
  if (!alerts || !alerts.length) {
    grid.innerHTML = `<div class="no-alerts">
      <div style="font-weight:700;color:var(--text);margin-bottom:6px;font-size:15px">Quiet markets — no events have triggered yet today</div>
      <div style="color:var(--muted);font-size:13px;line-height:1.55">
        This panel records the <em>moments</em> a tracked asset broke its threshold —
        the kind of move worth your attention. The Live Prices section above tells you
        the current state. This tells you when something <em>happened</em>.
      </div>
      <div style="margin-top:14px;font-size:10px;color:var(--muted)">
        Thresholds: VIX 5% · BTC 2% · ETH/SOL 2.5–3% · Stocks 1–2% · Indices 0.4–0.6% · Oil 0.8%
      </div>
    </div>`;
    return;
  }
  grid.innerHTML = alerts.slice(0, 24).map(a => {
    const isUp = a.direction === 'UP';
    const typeBadge = a.type ? `<span class="pc-type ${a.type}" style="display:inline-block;margin-left:6px">${a.type}</span>` : '';
    return `<div class="alert-card ${isUp ? 'up' : 'dn'}">
      <div class="ac-top">
        <span class="ac-sym">${a.symbol}${typeBadge}</span>
        <span class="ac-dir ${isUp ? 'up' : 'dn'}">${isUp ? '▲ UP' : '▼ DOWN'}</span>
      </div>
      <div class="ac-price">$${fmt(a.current)}</div>
      <div class="ac-pct ${isUp ? 'up' : 'dn'}">Moved ${a.pct}% · threshold was ${a.threshold}%</div>
      ${a.context ? `<div class="ac-context">${a.context}</div>` : ''}
      <div class="ac-time">${relTime(a.timestamp)}</div>
    </div>`;
  }).join('');
}

// ── toast + push notification ─────────────────────────────────────────────────
function toast(title, msg, type='up') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<div class="t-icon">${type==='up'?'📈':'📉'}</div>
    <div><div class="t-title">${title}</div><div class="t-msg">${msg}</div></div>`;
  $('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

function pushNotify(a) {
  const up = a.direction === 'UP';
  toast(`${a.symbol} ${up?'▲':'▼'} ${a.pct}%`, `${a.name} now $${fmt(a.current)}`, up?'up':'dn');
  if (Notification.permission === 'granted') {
    new Notification(`${a.symbol} ${up?'UP':'DOWN'} ${a.pct}%`, {
      body: `${a.name} moved to $${fmt(a.current)}`, tag: a.symbol
    });
  }
}

// ── websocket ──────────────────────────────────────────────────────────────────
let ws, wsRetries = 0;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { $('ws-dot').classList.add('live'); $('ws-status-text').textContent='LIVE'; wsRetries=0; };
  ws.onmessage = ({data}) => {
    const msg = JSON.parse(data);
    if (msg.type === 'update') {
      S.snapshot = msg.snapshot;
      renderTicker(S.snapshot);
      renderPrices(S.snapshot, S.rsi);
      renderBreadth(S.snapshot);
      (msg.alerts||[]).forEach(a => { pushNotify(a); S.alerts.unshift(a); });
      renderAlerts(S.alerts);
      $('last-updated').textContent = 'Live · Updated ' + new Date().toLocaleTimeString();
    }
  };
  ws.onclose = () => {
    $('ws-dot').classList.remove('live'); $('ws-status-text').textContent='RECONNECTING';
    setTimeout(connectWS, Math.min(1000*2**wsRetries++, 30000));
  };
  ws.onerror = () => ws.close();
}

// ── fetch helpers ──────────────────────────────────────────────────────────────
const api = url => fetch(url).then(r => r.json());

// ── Lazy section loader ─────────────────────────────────────────────────────
// Sections register themselves; their fetch only fires the first time the
// section scrolls within ~600px of the viewport. Massive perceived-speed win.
const _lazyOnce = new Set();
function lazyLoad(sectionId, fn) {
  const el = document.getElementById(sectionId);
  if (!el) { fn(); return; }
  if ('IntersectionObserver' in window) {
    const obs = new IntersectionObserver((entries, o) => {
      entries.forEach(e => {
        if (e.isIntersecting && !_lazyOnce.has(sectionId)) {
          _lazyOnce.add(sectionId);
          fn();
          o.unobserve(e.target);
        }
      });
    }, { rootMargin: '600px 0px' });
    obs.observe(el);
  } else {
    fn();  // fallback
  }
}

async function loadAll() {
  try {
    // CRITICAL — above-the-fold (always loads immediately)
    const [snap, analysisR] = await Promise.all([
      api('/api/snapshot'), api('/api/analysis'),
    ]);
    S.snapshot = snap.data;
    S.analysis = analysisR.data || {};
    renderTicker(S.snapshot);
    renderPrices(S.snapshot, S.rsi);
    renderBreadth(S.snapshot);
    renderHero(S.analysis);
    renderSignals(S.analysis);

    // BELOW-THE-FOLD — lazy-loaded on scroll
    lazyLoad('news', async () => {
      const r = await api('/api/news').catch(()=>({}));
      S.news = r.data || [];
      renderNews();
    });

    lazyLoad('sentiment', async () => {
      const r = await api('/api/sentiment').catch(()=>({}));
      renderFearGreed(r.fear_greed || {value:50});
      renderSocial(r.social || {});
      // Also kick off the slow structural + correlations once Sentiment is in view
      loadCorrelations();
      loadStructural();
      loadAnalystRecs();
    });

    lazyLoad('technicals', async () => {
      const [rsiR, techR] = await Promise.all([
        api('/api/rsi').catch(()=>({})),
        api('/api/technicals').catch(()=>({})),
      ]);
      S.rsi = rsiR.data || {};
      S.technicals = techR.data || {};
      renderPrices(S.snapshot, S.rsi);   // re-render Prices RSI badges now that we have them
      renderTechnicals(S.technicals);
    });

    lazyLoad('alerts', async () => {
      const r = await api('/api/alerts').catch(()=>({}));
      S.alerts = r.data || [];
      renderAlerts(S.alerts);
    });
  } catch(e) { console.error('Initial load error', e); }
}

async function loadTechnicals() {
  try { const r = await api('/api/technicals'); S.technicals = r.data||{}; renderTechnicals(S.technicals); }
  catch {}
}
async function loadAnalystRecs() {
  try { const r = await api('/api/analyst-recs'); S.analystRecs = r.data||{}; renderAnalyst(S.analystRecs); }
  catch {}
}

// ── earnings priced-in ────────────────────────────────────────────────────────
let earningsMode = 'simple';

function renderEarnings(events) {
  const list = $('earnings-list');
  if (!list) return;
  if (!events || !events.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:12px 0">Loading earnings data…</div>';
    return;
  }
  list.innerHTML = events.map(e => {
    const p      = e.priced_in || {};
    const col    = p.color || 'neutral';
    const isUp   = e.change_pct >= 0;
    const timing = e.hour === 'bmo' ? 'Before Open' : e.hour === 'amc' ? 'After Close' : e.hour || '?';
    const revStr = e.revenueEstimate ? `$${(e.revenueEstimate/1e9).toFixed(1)}B` : '—';
    const epsStr = e.epsEstimate != null ? `$${e.epsEstimate.toFixed(2)}` : '—';
    const text   = earningsMode === 'expert' ? p.expert : p.plain;

    return `<div class="earn-row ${col}">
      <div class="earn-left">
        <div class="earn-sym">${e.symbol}</div>
        <div class="earn-date">${e.date}</div>
        <div class="earn-hour">${timing}</div>
        ${e.price ? `<div class="earn-price ${isUp?'up':'dn'}">$${fmt(e.price)} ${isUp?'▲':'▼'}${Math.abs(e.change_pct)}%</div>` : ''}
      </div>
      <div class="earn-mid">
        <span class="earn-verdict ${col}">${p.verdict || '—'}</span>
        <div class="earn-plain">${text || ''}</div>
        <div class="earn-stats">
          ${p.move_5d != null ? `<span class="earn-stat">5-Day: <span class="${p.move_5d>=0?'up':'dn'}">${p.move_5d>=0?'+':''}${p.move_5d}%</span></span>` : ''}
          ${p.move_1m != null ? `<span class="earn-stat">1-Month: <span class="${p.move_1m>=0?'up':'dn'}">${p.move_1m>=0?'+':''}${p.move_1m}%</span></span>` : ''}
          ${p.rsi    != null ? `<span class="earn-stat">RSI: <span style="color:${p.rsi>70?'var(--dn)':p.rsi<30?'var(--up)':'var(--blue)'}">${p.rsi}</span></span>` : ''}
        </div>
      </div>
      <div class="earn-est">
        <strong>${epsStr}</strong>EPS est
        <br><strong>${revStr}</strong>Rev est
      </div>
    </div>`;
  }).join('');
}

// Earnings mode toggle
document.querySelectorAll('[data-emode]').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('[data-emode]').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  earningsMode = btn.dataset.emode;
  if (S.structural) renderEarnings(S.structural.earnings_calendar || []);
}));

async function loadStructural() {
  try {
    const r = await api('/api/structural');
    S.structural = r;
    renderEarnings(r.earnings_calendar || []);
    renderInsider(r.insider_trades    || []);
  } catch(e) { console.error('structural load failed', e); }
}

setInterval(async () => {
  const r = await api('/api/news').catch(()=>({}));
  if (r.data) { S.news = r.data; renderNews(); }
}, 5*60*1000);

setInterval(async () => {
  const r = await api('/api/analysis').catch(()=>({}));
  if (r.data) { S.analysis = r.data; renderHero(S.analysis); renderSignals(S.analysis); }
}, 5*60*1000);

setInterval(async () => {
  const r = await api('/api/sentiment').catch(()=>({}));
  if (r.fear_greed) renderFearGreed(r.fear_greed);
  if (r.social)     renderSocial(r.social);
}, 60*60*1000);

setInterval(async () => {
  const r = await api('/api/alerts').catch(()=>({}));
  if (r.data) { S.alerts = r.data; renderAlerts(S.alerts); }
}, 2*60*1000);

// ── event listeners ───────────────────────────────────────────────────────────
// Asset filter
document.querySelectorAll('.atab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.atab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  S.assetFilter = btn.dataset.type;
  renderPrices(S.snapshot, S.rsi);
}));

// News filter
document.querySelectorAll('.ntab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.ntab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  S.newsFilter = btn.dataset.cat;
  renderNews();
}));

// Plain/Expert toggle
document.querySelectorAll('.vtog').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.vtog').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  S.viewMode = btn.dataset.mode;
  renderSignals(S.analysis);
}));

// Notifications
$('notif-btn').addEventListener('click', async () => {
  if (Notification.permission === 'granted') { toast('Already enabled','You will receive alerts'); return; }
  const p = await Notification.requestPermission();
  if (p === 'granted') { $('notif-btn').textContent = '🔔 ON'; toast('Alerts enabled!','You will get OS notifications for price alerts.'); }
});
if (Notification.permission === 'granted') $('notif-btn').textContent = '🔔 ON';

// ── boot ──────────────────────────────────────────────────────────────────────
loadAll();
connectWS();
