/* ATC Recorder Dashboard – SPA logic */

const API = '';  // same origin

// ───────────────────────── Tabs ─────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
    document.getElementById('panel-' + btn.dataset.tab).classList.remove('hidden');
  });
});

// ───────────────────────── Helpers ──────────────────────
async function api(path, opts) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

let ragEnabled = false;

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function fmtBytes(b) {
  if (b > 1e9) return (b / 1e9).toFixed(1) + ' GB';
  if (b > 1e6) return (b / 1e6).toFixed(1) + ' MB';
  return (b / 1e3).toFixed(0) + ' KB';
}

function fmtTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function setOptions(sel, items, placeholder) {
  sel.innerHTML = '';
  if (placeholder) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = placeholder;
    sel.appendChild(o);
  }
  items.forEach(v => {
    const o = document.createElement('option');
    o.value = v;
    o.textContent = v;
    sel.appendChild(o);
  });
}

// ─────────────────── OVERVIEW TAB ──────────────────────
async function loadOverview() {
  try {
    const data = await api('/api/status');
    ragEnabled = !!data.rag_enabled;
    renderStatCards(data);
    renderServiceHealth(data.services || {});
    renderRecent(data.recent_transcriptions || []);
    updateRagBanners();
  } catch (e) {
    console.error('Failed to load status', e);
  }
}

function updateRagBanners() {
  const searchBanner = document.getElementById('se-rag-banner');
  const embBanner = document.getElementById('em-rag-banner');
  if (ragEnabled) {
    searchBanner.classList.add('hidden');
    embBanner.classList.add('hidden');
  } else {
    searchBanner.classList.remove('hidden');
    embBanner.classList.remove('hidden');
  }
}

function renderStatCards(d) {
  const cards = [
    { value: d.feed_count, label: 'Feeds' },
    { value: d.recording_count, label: 'Recordings' },
    { value: d.transcript_count, label: 'Transcripts' },
    { value: d.total_audio_hours, label: 'Audio hours' },
  ];
  if (d.ingested_doc_count != null) {
    cards.push({ value: d.ingested_doc_count, label: 'Indexed docs' });
  }
  const range = d.date_range || {};
  if (range.earliest) {
    cards.push({ value: `${range.earliest} – ${range.latest}`, label: 'Date range', small: true });
  }
  document.getElementById('stats-cards').innerHTML = cards.map(c => `
    <div class="stat-card">
      <span class="${c.small ? 'text-lg font-bold text-white' : 'stat-value'}">${esc(String(c.value))}</span>
      <span class="stat-label">${esc(c.label)}</span>
    </div>
  `).join('');
}

function renderServiceHealth(svc) {
  const names = { whisper_asr: 'Whisper ASR', embedding_nim: 'Embedding NIM', milvus: 'Milvus' };
  const el = document.getElementById('service-health');
  const dots = document.getElementById('service-dots');
  let html = '';
  let dotHtml = '';
  for (const [key, val] of Object.entries(svc)) {
    const cls = val === true ? 'up' : val === false ? 'down' : 'na';
    const label = val === true ? 'Online' : val === false ? 'Offline' : 'N/A';
    html += `<div class="flex items-center gap-2"><span class="health-dot ${cls}"></span>
      <span class="text-sm">${esc(names[key] || key)}</span>
      <span class="text-xs text-gray-500 ml-auto">${label}</span></div>`;
    dotHtml += `<span class="flex items-center gap-1"><span class="health-dot ${cls}"></span>${esc(names[key] || key)}</span>`;
  }
  el.innerHTML = html;
  dots.innerHTML = dotHtml;
}

function renderRecent(items) {
  const el = document.getElementById('recent-list');
  if (!items.length) { el.innerHTML = '<p class="text-gray-500">No transcriptions yet.</p>'; return; }
  el.innerHTML = items.map(r => `
    <div class="flex items-center gap-2 text-gray-400">
      <span class="text-brand-400 font-mono text-xs">${esc(r.feed_id)}</span>
      <span class="text-gray-600">·</span>
      <span class="truncate flex-1">${esc(r.file)}</span>
      <span class="text-xs text-gray-600 shrink-0">${esc(r.modified?.slice(0, 16).replace('T', ' '))}</span>
    </div>
  `).join('');
}

// ──────────────── TRANSCRIPT BROWSER ───────────────────
const brFeed = document.getElementById('br-feed');
const brDate = document.getElementById('br-date');
const brFile = document.getElementById('br-file');
const brAudio = document.getElementById('br-audio');

async function loadBrowserFeeds() {
  try {
    const data = await api('/api/feeds');
    const all = [...new Set([...(data.configured || []), ...(data.discovered || [])])].sort();
    setOptions(brFeed, all, '— select feed —');
    setOptions(document.getElementById('em-feed'), all, '— select feed —');
  } catch (e) { console.error(e); }
}

brFeed.addEventListener('change', async () => {
  const feed = brFeed.value;
  if (!feed) return;
  try {
    const data = await api(`/api/recordings?feed_id=${encodeURIComponent(feed)}`);
    setOptions(brDate, data.dates || [], '— select date —');
    brFile.innerHTML = '';
  } catch (e) { console.error(e); }
});

brDate.addEventListener('change', async () => {
  const feed = brFeed.value, date = brDate.value;
  if (!feed || !date) return;
  try {
    const data = await api(`/api/recordings?feed_id=${encodeURIComponent(feed)}&date=${encodeURIComponent(date)}`);
    const recs = (data.recordings || []).filter(r => r.has_transcript);
    setOptions(brFile, recs.map(r => r.filename), '— select recording —');
  } catch (e) { console.error(e); }
});

// Also update embedding date selector when em-feed changes
document.getElementById('em-feed').addEventListener('change', async () => {
  const feed = document.getElementById('em-feed').value;
  if (!feed) return;
  try {
    const data = await api(`/api/recordings?feed_id=${encodeURIComponent(feed)}`);
    setOptions(document.getElementById('em-date'), data.dates || [], '— all dates —');
  } catch (e) { console.error(e); }
});

document.getElementById('br-load').addEventListener('click', loadTranscript);

async function loadTranscript() {
  const feed = brFeed.value, date = brDate.value, file = brFile.value;
  if (!feed || !date || !file) return;

  const jsonName = file.replace('.mp3', '.json');
  try {
    const data = await api(`/api/transcript/${encodeURIComponent(feed)}/${encodeURIComponent(date)}/${encodeURIComponent(jsonName)}`);
    renderTranscript(data, feed, date, file);
  } catch (e) {
    console.error(e);
    document.getElementById('br-timeline').innerHTML =
      `<p class="text-red-400">Failed to load transcript: ${esc(e.message)}</p>`;
  }
}

function renderTranscript(data, feed, date, mp3Name) {
  // Audio player
  const playerEl = document.getElementById('br-player');
  playerEl.classList.remove('hidden');
  brAudio.src = `/api/audio/${encodeURIComponent(feed)}/${encodeURIComponent(date)}/${encodeURIComponent(mp3Name)}`;

  // Segments
  const segments = data.segments || [];
  const timeline = document.getElementById('br-timeline');
  if (!segments.length) {
    timeline.innerHTML = '<p class="text-gray-500">No segments in this transcript.</p>';
    return;
  }

  timeline.innerHTML = segments.map((seg, i) => {
    const role = (seg.speaker_role || 'UNKNOWN').toLowerCase();
    const text = seg.text || '';
    if (text === '...' || text.trim() === '') return '';
    return `
      <div class="seg-row" data-start="${seg.start_time}" data-end="${seg.end_time}" data-idx="${i}">
        <span class="seg-time">${fmtTime(seg.start_time)} – ${fmtTime(seg.end_time)}</span>
        <span class="seg-role ${role}">${esc(role)}</span>
        <span class="seg-text">${esc(text)}</span>
      </div>`;
  }).join('');

  // Click-to-play segment
  timeline.querySelectorAll('.seg-row').forEach(row => {
    row.addEventListener('click', () => {
      const start = parseFloat(row.dataset.start);
      brAudio.currentTime = start;
      brAudio.play();
      timeline.querySelectorAll('.seg-row').forEach(r => r.classList.remove('playing'));
      row.classList.add('playing');
    });
  });

  // Highlight playing segment during playback
  brAudio.addEventListener('timeupdate', () => {
    const t = brAudio.currentTime;
    timeline.querySelectorAll('.seg-row').forEach(row => {
      const s = parseFloat(row.dataset.start);
      const e = parseFloat(row.dataset.end);
      row.classList.toggle('playing', t >= s && t <= e);
    });
  });
}

// ──────────────── SEMANTIC SEARCH ──────────────────────
document.getElementById('se-go').addEventListener('click', doSearch);
document.getElementById('se-query').addEventListener('keydown', e => {
  if (e.key === 'Enter') doSearch();
});

async function doSearch() {
  const query = document.getElementById('se-query').value.trim();
  if (!query) return;
  const feedRaw = document.getElementById('se-feed').value.trim();
  const topK = parseInt(document.getElementById('se-topk').value) || 10;

  const body = { query, top_k: topK };
  if (feedRaw) body.feed_ids = feedRaw.split(',').map(s => s.trim()).filter(Boolean);

  const resultsEl = document.getElementById('se-results');
  const emptyEl = document.getElementById('se-empty');
  resultsEl.innerHTML = '<p class="text-gray-400 animate-pulse">Searching...</p>';
  emptyEl.classList.add('hidden');

  try {
    const data = await api('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    renderSearchResults(data.hits || []);
  } catch (e) {
    resultsEl.innerHTML = `<p class="text-red-400">Search failed: ${esc(e.message)}</p>`;
  }
}

function renderSearchResults(hits) {
  const el = document.getElementById('se-results');
  const emptyEl = document.getElementById('se-empty');
  if (!hits.length) {
    el.innerHTML = '';
    emptyEl.textContent = 'No results found.';
    emptyEl.classList.remove('hidden');
    return;
  }
  emptyEl.classList.add('hidden');
  el.innerHTML = hits.map(h => {
    const parts = h.audio_file.replace('.mp3', '').split('_');
    const feedId = h.feed_id;
    const date = parts.length >= 3 ? parts[parts.length - 2] : '';
    return `
    <div class="hit-card" data-feed="${esc(feedId)}" data-date="${esc(date)}" data-file="${esc(h.audio_file)}">
      <div class="flex items-center gap-3 mb-2">
        <span class="hit-score">${h.score.toFixed(4)}</span>
        <span class="text-brand-400 text-sm font-mono">${esc(feedId)}</span>
        <span class="text-gray-600">·</span>
        <span class="text-xs text-gray-500">${esc(h.start_time_utc.slice(0, 19).replace('T', ' '))} UTC</span>
      </div>
      <p class="text-gray-300 text-sm">${esc(h.text)}</p>
      <div class="mt-2">
        <audio controls preload="none" class="w-full h-8 rounded">
          <source src="/api/audio/${encodeURIComponent(feedId)}/${encodeURIComponent(date)}/${encodeURIComponent(h.audio_file)}" type="audio/mpeg" />
        </audio>
      </div>
    </div>`;
  }).join('');
}

// ──────────────── EMBEDDING EXPLORER ───────────────────
document.getElementById('em-load').addEventListener('click', loadEmbeddings);

async function loadEmbeddings() {
  const feed = document.getElementById('em-feed').value;
  const date = document.getElementById('em-date').value;
  const colorBy = document.getElementById('em-color').value;
  const dims = parseInt(document.getElementById('em-dims').value);

  if (!feed) return;

  const emptyEl = document.getElementById('em-empty');
  emptyEl.textContent = 'Loading embeddings...';
  emptyEl.classList.remove('hidden');

  const params = new URLSearchParams({ feed_id: feed, dims: dims });
  if (date) params.set('date', date);
  params.set('color_by', colorBy);

  try {
    const data = await api(`/api/embeddings?${params}`);
    emptyEl.classList.add('hidden');
    renderEmbeddingPlot(data, dims, colorBy);
  } catch (e) {
    emptyEl.textContent = `Failed to load embeddings: ${e.message}`;
  }
}

function renderEmbeddingPlot(data, dims, colorBy) {
  const points = data.points || [];
  if (!points.length) {
    document.getElementById('em-empty').textContent = 'No embedding data available.';
    document.getElementById('em-empty').classList.remove('hidden');
    return;
  }

  // Group by color category
  const groups = {};
  points.forEach(p => {
    const key = p.color_label || 'unknown';
    if (!groups[key]) groups[key] = { x: [], y: [], z: [], text: [] };
    groups[key].x.push(p.x);
    groups[key].y.push(p.y);
    if (dims === 3) groups[key].z.push(p.z);
    groups[key].text.push(p.hover);
  });

  const palette = [
    '#28a3ff', '#34d399', '#f472b6', '#facc15', '#a78bfa',
    '#f87171', '#38bdf8', '#4ade80', '#fb923c', '#c084fc',
  ];

  const traces = Object.entries(groups).map(([key, g], i) => {
    const base = {
      x: g.x, y: g.y,
      mode: 'markers',
      type: dims === 3 ? 'scatter3d' : 'scatter',
      name: key,
      text: g.text,
      hoverinfo: 'text',
      marker: { size: dims === 3 ? 3 : 5, color: palette[i % palette.length], opacity: 0.8 },
    };
    if (dims === 3) base.z = g.z;
    return base;
  });

  const layout = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(3,7,18,1)',
    font: { color: '#9ca3af', size: 11 },
    margin: { l: 40, r: 20, t: 20, b: 40 },
    legend: { orientation: 'h', y: -0.12 },
    xaxis: { gridcolor: '#1f2937', zerolinecolor: '#374151' },
    yaxis: { gridcolor: '#1f2937', zerolinecolor: '#374151' },
  };

  Plotly.newPlot('em-plot', traces, layout, { responsive: true });
}

// ─────────────────── Initialization ────────────────────
loadOverview();
loadBrowserFeeds();
