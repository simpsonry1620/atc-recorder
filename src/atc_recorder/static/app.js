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

function parseTimeValue(value) {
  if (Number.isFinite(value)) return Number(value);
  if (value == null) return NaN;

  if (typeof value === 'object') {
    const sec = Number(value.seconds);
    const nanos = Number(value.nanos);
    if (Number.isFinite(sec)) {
      const frac = Number.isFinite(nanos) ? nanos / 1e9 : 0;
      return sec + frac;
    }
  }

  const raw = String(value).trim();
  if (!raw) return NaN;

  const numeric = Number(raw);
  if (Number.isFinite(numeric)) return numeric;

  // Parse clock-like values such as HH:MM:SS(.sss) or MM:SS(.sss).
  const clock = raw.match(/^(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?$/);
  if (clock) {
    if (clock[3] != null) {
      const h = Number(clock[1]);
      const m = Number(clock[2]);
      const s = Number(clock[3]);
      if (Number.isFinite(h) && Number.isFinite(m) && Number.isFinite(s)) {
        return (h * 3600) + (m * 60) + s;
      }
    } else {
      const m = Number(clock[1]);
      const s = Number(clock[2]);
      if (Number.isFinite(m) && Number.isFinite(s)) {
        return (m * 60) + s;
      }
    }
  }

  return NaN;
}

function normalizeSegmentTimes(segments, audioDuration) {
  const cleaned = segments.map(seg => {
    const startRaw = parseTimeValue(seg.start_time);
    const endRaw = parseTimeValue(seg.end_time);
    const start = Number.isFinite(startRaw) ? startRaw : 0;
    const end = Number.isFinite(endRaw) ? endRaw : start;
    return { start, end: Math.max(start, end) };
  });

  if (!cleaned.length) return cleaned;

  const starts = cleaned.map(s => s.start);
  const ends = cleaned.map(s => s.end);
  const minStart = Math.min(...starts);
  const maxEnd = Math.max(...ends);
  const duration = Number.isFinite(audioDuration) && audioDuration > 0 ? audioDuration : null;

  let scale = 1;
  let offset = 0;

  const withinDuration = (value) => duration == null || value <= (duration * 1.25);

  if (duration != null && maxEnd > duration * 1.25) {
    if (withinDuration(maxEnd / 1000)) {
      scale = 1 / 1000;
    } else if (withinDuration(maxEnd / 100)) {
      scale = 1 / 100;
    } else if (withinDuration(maxEnd - minStart)) {
      offset = minStart;
    } else if (withinDuration((maxEnd - minStart) / 1000)) {
      scale = 1 / 1000;
      offset = minStart;
    } else if (withinDuration((maxEnd - minStart) / 100)) {
      scale = 1 / 100;
      offset = minStart;
    }
  } else if (duration != null && minStart > (duration * 0.5) && withinDuration(maxEnd - minStart)) {
    offset = minStart;
  }

  return cleaned.map(seg => {
    const start = Math.max(0, (seg.start - offset) * scale);
    const end = Math.max(start, (seg.end - offset) * scale);
    return { start, end };
  });
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
  const names = {
    whisper_asr: 'Whisper ASR',
    parakeet_asr: 'Parakeet ASR',
    embedding_nim: 'Embedding NIM',
    milvus: 'Milvus',
  };
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
const brAsrModel = document.getElementById('br-asr-model');
const brAsrPreprocess = document.getElementById('br-asr-preprocess');
const brAsrStatus = document.getElementById('br-transcribe-status');
const brTranscribeBtn = document.getElementById('br-transcribe');
let browserRecordings = [];

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
    browserRecordings = data.recordings || [];
    renderBrowserRecordingOptions(browserRecordings);
    brAsrStatus.textContent = 'Select a recording, then run ASR.';
    brAsrStatus.className = 'text-sm text-gray-400 mt-3';
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
brTranscribeBtn.addEventListener('click', runBrowserAsr);

function renderBrowserRecordingOptions(recs) {
  brFile.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '— select recording —';
  brFile.appendChild(placeholder);

  recs.forEach(r => {
    const option = document.createElement('option');
    option.value = r.filename;
    option.textContent = r.has_transcript ? r.filename : `${r.filename} (no transcript)`;
    brFile.appendChild(option);
  });
}

async function loadTranscript() {
  const feed = brFeed.value, date = brDate.value, file = brFile.value;
  if (!feed || !date || !file) return;

  const jsonName = file.replace('.mp3', '.json');
  try {
    const data = await api(`/api/transcript/${encodeURIComponent(feed)}/${encodeURIComponent(date)}/${encodeURIComponent(jsonName)}`);
    renderTranscript(data, feed, date, file);
  } catch (e) {
    console.error(e);
    if (String(e.message).toLowerCase().includes('transcript not found')) {
      document.getElementById('br-timeline').innerHTML =
        '<p class="text-yellow-300">No transcript exists for this file yet. Use "Run ASR" above.</p>';
      return;
    }
    document.getElementById('br-timeline').innerHTML =
      `<p class="text-red-400">Failed to load transcript: ${esc(e.message)}</p>`;
  }
}

async function runBrowserAsr() {
  const feed = brFeed.value;
  const date = brDate.value;
  const file = brFile.value;
  if (!feed || !date || !file) {
    brAsrStatus.textContent = 'Select feed, date, and recording first.';
    brAsrStatus.className = 'text-sm text-red-400 mt-3';
    return;
  }

  const model = brAsrModel.value;
  const preprocess = brAsrPreprocess.value;
  brTranscribeBtn.disabled = true;
  brAsrStatus.textContent = `Running ASR (${model}, ${preprocess})...`;
  brAsrStatus.className = 'text-sm text-gray-300 mt-3';

  try {
    const result = await api('/api/asr/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        feed_id: feed,
        date,
        filename: file,
        model,
        preprocess,
      }),
    });
    const segmentCount = result.segment_count ?? 0;
    brAsrStatus.textContent = `ASR complete: ${segmentCount} segments, model=${result.model}, preprocess=${result.preprocess}.`;
    brAsrStatus.className = 'text-sm text-green-400 mt-3';
    await loadTranscript();
  } catch (e) {
    brAsrStatus.textContent = `ASR failed: ${e.message}`;
    brAsrStatus.className = 'text-sm text-red-400 mt-3';
  } finally {
    brTranscribeBtn.disabled = false;
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
    const fullText = (data.text || '').trim();
    if (fullText) {
      timeline.innerHTML = `<div class="card"><p class="text-gray-300 text-sm whitespace-pre-wrap">${esc(fullText)}</p></div>`;
    } else {
      timeline.innerHTML = '<p class="text-gray-500">No segments in this transcript.</p>';
    }
    return;
  }
  const visibleSegments = segments.filter(seg => {
    const text = (seg.text || '').trim();
    return text && text !== '...';
  });
  if (!visibleSegments.length) {
    const fullText = (data.text || '').trim();
    if (fullText) {
      timeline.innerHTML = `<div class="card"><p class="text-gray-300 text-sm whitespace-pre-wrap">${esc(fullText)}</p></div>`;
    } else {
      timeline.innerHTML = '<p class="text-yellow-300">Transcript exists but segments have no displayable text.</p>';
    }
    return;
  }

  const drawTimeline = () => {
    const audioDuration = Number.isFinite(brAudio.duration) ? brAudio.duration : null;
    const normalizedTimes = normalizeSegmentTimes(visibleSegments, audioDuration);

    timeline.innerHTML = visibleSegments.map((seg, i) => {
      const role = (seg.speaker_role || 'UNKNOWN').toLowerCase();
      const text = seg.text || '';
      const times = normalizedTimes[i] || { start: 0, end: 0 };
      return `
        <div class="seg-row" data-start="${times.start}" data-end="${times.end}" data-idx="${i}">
          <span class="seg-time">${fmtTime(times.start)} – ${fmtTime(times.end)}</span>
          <span class="seg-role ${role}">${esc(role)}</span>
          <span class="seg-text">${esc(text)}</span>
        </div>`;
    }).join('');

    // Click-to-play segment
    timeline.querySelectorAll('.seg-row').forEach(row => {
      row.addEventListener('click', () => {
        const start = parseFloat(row.dataset.start);
        brAudio.currentTime = Number.isFinite(start) ? start : 0;
        brAudio.play().catch(() => {});
        timeline.querySelectorAll('.seg-row').forEach(r => r.classList.remove('playing'));
        row.classList.add('playing');
      });
    });
  };

  drawTimeline();
  if (!Number.isFinite(brAudio.duration) || brAudio.duration <= 0) {
    brAudio.addEventListener('loadedmetadata', drawTimeline, { once: true });
  }

  // Highlight playing segment during playback
  brAudio.ontimeupdate = () => {
    const t = brAudio.currentTime;
    timeline.querySelectorAll('.seg-row').forEach(row => {
      const s = parseFloat(row.dataset.start);
      const e = parseFloat(row.dataset.end);
      row.classList.toggle('playing', t >= s && t <= e);
    });
  };
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
    const m = h.audio_file.match(/^(.+?)_(\d{4}-\d{2}-\d{2})_\d{4}Z?\.mp3$/);
    const feedId = m ? m[1] : h.feed_id;
    const date = m ? m[2] : '';
    const startOffset = h.start_offset_seconds || 0;

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
        <audio controls preload="metadata" data-start="${startOffset}" class="w-full h-8 rounded">
          <source src="/api/audio/${encodeURIComponent(feedId)}/${encodeURIComponent(date)}/${encodeURIComponent(h.audio_file)}" type="audio/mpeg" />
        </audio>
      </div>
    </div>`;
  }).join('');

  el.querySelectorAll('audio[data-start]').forEach(audio => {
    const offset = parseFloat(audio.dataset.start);
    if (offset > 0) {
      audio.addEventListener('loadedmetadata', () => { audio.currentTime = offset; }, { once: true });
    }
  });
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

// ──────────────── FLIGHT TRACKER ───────────────────────
document.getElementById('ft-go').addEventListener('click', trackFlight);
document.getElementById('ft-callsign').addEventListener('keydown', e => {
  if (e.key === 'Enter') trackFlight();
});

async function trackFlight() {
  const cs = document.getElementById('ft-callsign').value.trim().toUpperCase();
  if (!cs) return;

  const trackEl = document.getElementById('ft-track');
  const recentEl = document.getElementById('ft-recent');
  trackEl.classList.add('hidden');
  recentEl.classList.add('hidden');

  try {
    const data = await api(`/api/flights/${encodeURIComponent(cs)}`);
    renderFlightTrack(data);
  } catch (e) {
    document.getElementById('ft-info').innerHTML =
      `<p class="text-red-400">Failed to load flight: ${esc(e.message)}</p>`;
    trackEl.classList.remove('hidden');
  }
}

function renderFlightTrack(data) {
  const trackEl = document.getElementById('ft-track');
  const infoEl = document.getElementById('ft-info');
  const timelineEl = document.getElementById('ft-timeline');

  let infoHtml = `<div class="flex items-center gap-4 flex-wrap">
    <span class="text-2xl font-bold text-white">${esc(data.callsign)}</span>
    <span class="text-sm text-gray-400">${data.feed_count} frequencies &middot; ${Math.round(data.total_duration_seconds / 60)} min tracked</span>`;
  infoHtml += `</div>`;
  infoEl.innerHTML = infoHtml;

  const legs = data.legs || [];
  if (!legs.length) {
    timelineEl.innerHTML = '<p class="text-gray-500">No frequency data available.</p>';
    trackEl.classList.remove('hidden');
    return;
  }

  timelineEl.innerHTML = legs.map((leg, i) => {
    const dur = ((new Date(leg.last_heard) - new Date(leg.first_heard)) / 1000 / 60).toFixed(1);
    const handoff = leg.handoff_to
      ? `<span class="text-xs text-yellow-400 ml-2">→ ${esc(leg.handoff_to)}${leg.handoff_frequency ? ' (' + esc(leg.handoff_frequency) + ')' : ''}</span>`
      : '';
    const segs = (leg.segments || []).map(s =>
      `<div class="seg-row text-xs py-1 px-2"><span class="seg-time">${esc((s.start_time_utc || '').slice(11, 19))}</span> <span class="seg-text">${esc(s.text || '')}</span></div>`
    ).join('');

    return `<div class="card">
      <div class="flex items-center gap-3 mb-2">
        <span class="inline-flex items-center justify-center w-6 h-6 rounded-full bg-brand-700 text-xs font-bold text-white">${i + 1}</span>
        <span class="text-brand-400 font-mono text-sm">${esc(leg.feed_id)}</span>
        ${leg.frequency ? `<span class="text-xs text-gray-500">${esc(leg.frequency)} MHz</span>` : ''}
        <span class="text-xs text-gray-500 ml-auto">${dur} min · ${leg.segment_count} segments</span>
        ${handoff}
      </div>
      <div class="space-y-0 max-h-40 overflow-y-auto">${segs}</div>
    </div>`;
  }).join('');

  trackEl.classList.remove('hidden');
}

async function loadRecentFlights() {
  try {
    const data = await api('/api/flights/recent?limit=50');
    const el = document.getElementById('ft-recent-list');
    const emptyEl = document.getElementById('ft-empty');
    const flights = data.flights || [];

    if (!flights.length) {
      emptyEl.textContent = 'No flight entity data yet. Run entity extraction on your transcripts.';
      return;
    }
    emptyEl.classList.add('hidden');

    el.innerHTML = flights.map(f => `
      <div class="flex items-center gap-2 text-gray-400 cursor-pointer hover:bg-gray-800/50 rounded px-2 py-1 ft-recent-row" data-cs="${esc(f.normalized)}">
        <span class="text-brand-400 font-mono text-sm font-bold w-20">${esc(f.normalized)}</span>
        <span class="text-xs text-gray-500">${f.mention_count} mentions</span>
        <span class="text-xs text-gray-600">${f.feed_count} feeds</span>
        <span class="text-xs text-gray-600 truncate flex-1">${esc(f.feeds || '')}</span>
        <span class="text-xs text-gray-600 shrink-0">${esc((f.last_seen || '').slice(0, 19).replace('T', ' '))}</span>
      </div>
    `).join('');

    el.querySelectorAll('.ft-recent-row').forEach(row => {
      row.addEventListener('click', () => {
        document.getElementById('ft-callsign').value = row.dataset.cs;
        trackFlight();
      });
    });
  } catch (e) {
    document.getElementById('ft-empty').textContent = 'Failed to load recent flights.';
  }
}

// ──────────────── CONTROLLER PROFILE ──────────────────
document.getElementById('cp-go').addEventListener('click', loadProfile);

async function loadProfileFeeds() {
  try {
    const data = await api('/api/feeds');
    const all = [...new Set([...(data.configured || []), ...(data.discovered || [])])].sort();
    setOptions(document.getElementById('cp-feed'), all, '— select feed —');
  } catch (e) { console.error(e); }
}

async function loadProfile() {
  const feed = document.getElementById('cp-feed').value;
  if (!feed) return;

  const startVal = document.getElementById('cp-start').value;
  const endVal = document.getElementById('cp-end').value;

  const params = new URLSearchParams();
  if (startVal) params.set('start_time', new Date(startVal).toISOString());
  if (endVal) params.set('end_time', new Date(endVal).toISOString());

  const profileEl = document.getElementById('cp-profile');
  const summaryEl = document.getElementById('cp-summary');
  profileEl.classList.add('hidden');
  summaryEl.classList.add('hidden');

  try {
    const data = await api(`/api/profile/${encodeURIComponent(feed)}?${params}`);
    renderProfile(data);
  } catch (e) {
    document.getElementById('cp-stats').innerHTML =
      `<p class="text-red-400 col-span-4">Failed to load profile: ${esc(e.message)}</p>`;
    profileEl.classList.remove('hidden');
  }
}

function renderProfile(d) {
  const profileEl = document.getElementById('cp-profile');

  // Stats cards
  const cards = [
    { value: d.total_segments, label: 'Total Segments' },
    { value: d.atc_segments, label: 'ATC Segments' },
    { value: d.unique_callsigns, label: 'Unique Callsigns' },
    { value: d.avg_segment_duration + 's', label: 'Avg Duration' },
    { value: Math.round(d.total_talk_time) + 's', label: 'Total Talk Time' },
    { value: d.pilot_segments, label: 'Pilot Segments' },
  ];

  document.getElementById('cp-stats').innerHTML = cards.map(c => `
    <div class="stat-card">
      <span class="stat-value">${esc(String(c.value))}</span>
      <span class="stat-label">${esc(c.label)}</span>
    </div>
  `).join('');

  // Phrases
  const phrases = Object.entries(d.phrases || {});
  const phrasesEl = document.getElementById('cp-phrases');
  if (phrases.length) {
    const maxCount = Math.max(...phrases.map(p => p[1]));
    phrasesEl.innerHTML = phrases.map(([name, count]) => {
      const pct = maxCount > 0 ? (count / maxCount * 100) : 0;
      return `<div class="flex items-center gap-2">
        <span class="w-44 truncate text-gray-300">${esc(name)}</span>
        <div class="flex-1 h-4 bg-gray-800 rounded overflow-hidden">
          <div class="h-full bg-brand-600 rounded" style="width:${pct}%"></div>
        </div>
        <span class="text-xs text-gray-500 w-10 text-right">${count}</span>
      </div>`;
    }).join('');
  } else {
    phrasesEl.innerHTML = '<p class="text-gray-500">No phraseology data.</p>';
  }

  // Busiest hours chart
  const hours = d.busiest_hours || [];
  if (hours.length && typeof Plotly !== 'undefined') {
    const sorted = [...hours].sort((a, b) => a.hour - b.hour);
    Plotly.newPlot('cp-hours', [{
      x: sorted.map(h => h.hour + ':00'),
      y: sorted.map(h => h.count),
      type: 'bar',
      marker: { color: '#0d84ff' },
    }], {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(3,7,18,1)',
      font: { color: '#9ca3af', size: 10 },
      margin: { l: 35, r: 10, t: 5, b: 30 },
      xaxis: { gridcolor: '#1f2937' },
      yaxis: { gridcolor: '#1f2937' },
    }, { responsive: true, displayModeBar: false });
  }

  // Callsigns
  const csEl = document.getElementById('cp-callsigns');
  const csList = d.callsign_list || [];
  if (csList.length) {
    csEl.innerHTML = csList.map(cs =>
      `<span class="inline-block px-2 py-0.5 bg-gray-800 rounded text-xs text-brand-400 font-mono cursor-pointer hover:bg-gray-700 cp-cs-tag" data-cs="${esc(cs)}">${esc(cs)}</span>`
    ).join('');
    csEl.querySelectorAll('.cp-cs-tag').forEach(tag => {
      tag.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelector('[data-tab="flights"]').classList.add('active');
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
        document.getElementById('panel-flights').classList.remove('hidden');
        document.getElementById('ft-callsign').value = tag.dataset.cs;
        trackFlight();
      });
    });
  } else {
    csEl.innerHTML = '<span class="text-gray-500 text-xs">No callsign data.</span>';
  }

  profileEl.classList.remove('hidden');
}

async function loadProfileSummary() {
  try {
    const data = await api('/api/profile/summary');
    const el = document.getElementById('cp-summary-list');
    const emptyEl = document.getElementById('cp-empty');
    const feeds = data.feeds || [];

    if (!feeds.length) {
      emptyEl.textContent = 'No position data yet. Ingest transcripts first.';
      return;
    }
    emptyEl.classList.add('hidden');

    el.innerHTML = feeds.map(f => `
      <div class="flex items-center gap-2 text-gray-400 cursor-pointer hover:bg-gray-800/50 rounded px-2 py-1 cp-sum-row" data-feed="${esc(f.feed_id)}">
        <span class="text-brand-400 font-mono text-sm w-36">${esc(f.feed_id)}</span>
        <span class="text-xs text-gray-500">${f.total_segments} segments</span>
        <span class="text-xs text-gray-600">${f.unique_callsigns} callsigns</span>
        <span class="text-xs text-gray-600 ml-auto shrink-0">${esc((f.first_seen || '').slice(0, 10))} – ${esc((f.last_seen || '').slice(0, 10))}</span>
      </div>
    `).join('');

    el.querySelectorAll('.cp-sum-row').forEach(row => {
      row.addEventListener('click', () => {
        document.getElementById('cp-feed').value = row.dataset.feed;
        loadProfile();
      });
    });
  } catch (e) {
    document.getElementById('cp-empty').textContent = 'Failed to load position summary.';
  }
}

// ──────────────── AUDIO LAB ────────────────────────────
const labFeed = document.getElementById('lab-feed');
const labDate = document.getElementById('lab-date');
const labFile = document.getElementById('lab-file');
const labRunBtn = document.getElementById('lab-run');
const labStatus = document.getElementById('lab-status');
const labMethodsEl = document.getElementById('lab-methods');
const labResultsEl = document.getElementById('lab-results');
let labAllMethods = [];
let labAvailableMethods = [];
let labParamSchema = {};

const PARAM_LABELS = {
  highpass_freq: 'Highpass (Hz)',
  lowpass_freq: 'Lowpass (Hz)',
  noise_floor_db: 'Noise Floor (dB)',
  dynaudnorm_peak: 'Norm Peak',
  dynaudnorm_smoothing: 'Norm Smoothing (s)',
  silence_stop_duration: 'Min Silence (s)',
  silence_threshold_db: 'Silence Threshold (dB)',
  leave_silence: 'Leave Silence (s)',
  noise_sample_duration: 'Noise Sample (s)',
  noise_reduction: 'Noise Reduction',
  intensity_ratio: 'Intensity',
  effect_version: 'Model Version',
  enable_vad: 'Built-in VAD',
  effect: 'Effect',
};

const PARAM_STEP = {
  highpass_freq: 10,
  lowpass_freq: 100,
  noise_floor_db: 1,
  dynaudnorm_peak: 0.05,
  dynaudnorm_smoothing: 1,
  silence_stop_duration: 0.05,
  silence_threshold_db: 1,
  leave_silence: 0.05,
  noise_sample_duration: 0.1,
  noise_reduction: 0.01,
  intensity_ratio: 0.05,
  effect_version: 1,
  enable_vad: 1,
};

async function loadLabStatus() {
  try {
    const [statusData, paramsData] = await Promise.all([
      api('/api/preprocess/status'),
      api('/api/preprocess/params'),
    ]);
    labAllMethods = statusData.all_methods || [];
    labAvailableMethods = statusData.available_methods || [];
    labParamSchema = paramsData.params || {};
    renderLabMethodCheckboxes();
    renderLabParamControls();
  } catch (e) { console.error('Lab status load failed', e); }
}

function renderLabMethodCheckboxes() {
  labMethodsEl.innerHTML = labAllMethods.map(m => {
    const available = labAvailableMethods.includes(m);
    const checked = available && m !== 'none' ? 'checked' : '';
    const disabled = available ? '' : 'disabled';
    const opacity = available ? '' : 'opacity-50';
    const badge = available ? '' : '<span class="text-xs text-yellow-500 ml-1">(unavailable)</span>';
    return `
      <label class="lab-method-card ${opacity}">
        <input type="checkbox" name="lab-method" value="${m}" ${checked} ${disabled} />
        <span class="text-sm font-medium text-gray-200">${esc(m)}</span>${badge}
      </label>`;
  }).join('');

  labMethodsEl.querySelectorAll('input[name="lab-method"]').forEach(cb => {
    cb.addEventListener('change', updateLabParamVisibility);
  });
}

function renderLabParamControls() {
  const container = document.getElementById('lab-params-controls');
  if (!Object.keys(labParamSchema).length) {
    container.innerHTML = '<p class="text-gray-500 text-sm">No tunable parameters available.</p>';
    return;
  }

  let html = '';
  for (const [method, params] of Object.entries(labParamSchema)) {
    const paramRows = Object.entries(params).map(([key, spec]) => {
      const label = PARAM_LABELS[key] || key;

      if (spec.options) {
        const opts = spec.options.map(o =>
          `<option value="${esc(o)}" ${o === spec.default ? 'selected' : ''}>${esc(o)}</option>`
        ).join('');
        return `
          <div class="lab-param-row lab-param-row-select">
            <label class="lab-param-label" for="lab-p-${method}-${key}">${esc(label)}</label>
            <select id="lab-p-${method}-${key}"
              class="lab-param-select" data-method="${method}" data-param="${key}">
              ${opts}
            </select>
            <span class="lab-param-default text-xs text-gray-600">(${esc(String(spec.default))})</span>
          </div>`;
      }

      if (spec.min === 0 && spec.max === 1 && spec.type === 'int') {
        const checked = spec.default ? 'checked' : '';
        return `
          <div class="lab-param-row lab-param-row-toggle">
            <label class="lab-param-label" for="lab-p-${method}-${key}">${esc(label)}</label>
            <label class="lab-toggle">
              <input type="checkbox" id="lab-p-${method}-${key}"
                class="lab-param-toggle" data-method="${method}" data-param="${key}" ${checked} />
              <span class="lab-toggle-track"><span class="lab-toggle-thumb"></span></span>
            </label>
            <span class="lab-param-default text-xs text-gray-600">(${spec.default ? 'on' : 'off'})</span>
          </div>`;
      }

      const step = PARAM_STEP[key] || (spec.type === 'float' ? 0.01 : 1);
      return `
        <div class="lab-param-row">
          <label class="lab-param-label" for="lab-p-${method}-${key}">${esc(label)}</label>
          <input type="range" id="lab-r-${method}-${key}"
            class="lab-param-range" data-method="${method}" data-param="${key}"
            min="${spec.min}" max="${spec.max}" step="${step}" value="${spec.default}" />
          <input type="number" id="lab-p-${method}-${key}"
            class="lab-param-input" data-method="${method}" data-param="${key}"
            min="${spec.min}" max="${spec.max}" step="${step}" value="${spec.default}" />
          <span class="lab-param-default text-xs text-gray-600">(${spec.default})</span>
        </div>`;
    }).join('');

    html += `
      <div class="lab-param-group" data-method="${method}">
        <h4 class="lab-param-method-title">${esc(method)}</h4>
        ${paramRows}
      </div>`;
  }
  container.innerHTML = html;

  container.querySelectorAll('.lab-param-range').forEach(range => {
    range.addEventListener('input', () => {
      const numInput = document.getElementById(`lab-p-${range.dataset.method}-${range.dataset.param}`);
      numInput.value = range.value;
      updateParamBadge();
    });
  });
  container.querySelectorAll('.lab-param-input').forEach(input => {
    input.addEventListener('input', () => {
      const rangeInput = document.getElementById(`lab-r-${input.dataset.method}-${input.dataset.param}`);
      rangeInput.value = input.value;
      updateParamBadge();
    });
  });
  container.querySelectorAll('.lab-param-select').forEach(sel => {
    sel.addEventListener('change', updateParamBadge);
  });
  container.querySelectorAll('.lab-param-toggle').forEach(tog => {
    tog.addEventListener('change', updateParamBadge);
  });

  updateLabParamVisibility();
}

function updateLabParamVisibility() {
  const checkedMethods = new Set(
    [...document.querySelectorAll('input[name="lab-method"]:checked')].map(el => el.value)
  );
  document.querySelectorAll('.lab-param-group').forEach(group => {
    const method = group.dataset.method;
    group.classList.toggle('hidden', !checkedMethods.has(method));
  });
}

function updateParamBadge() {
  const badge = document.getElementById('lab-params-badge');
  const hasCustom = hasCustomParams();
  badge.classList.toggle('hidden', !hasCustom);
}

function _getParamValue(method, key, spec) {
  const el = document.getElementById(`lab-p-${method}-${key}`);
  if (!el) return spec.default;
  if (spec.options) return el.value;
  if (el.type === 'checkbox') return el.checked ? 1 : 0;
  return spec.type === 'float' ? parseFloat(el.value) : parseInt(el.value, 10);
}

function hasCustomParams() {
  for (const [method, params] of Object.entries(labParamSchema)) {
    for (const [key, spec] of Object.entries(params)) {
      const val = _getParamValue(method, key, spec);
      if (val !== spec.default) return true;
    }
  }
  return false;
}

function collectLabParams() {
  const params = {};
  for (const [method, schema] of Object.entries(labParamSchema)) {
    const methodParams = {};
    let anyCustom = false;
    for (const [key, spec] of Object.entries(schema)) {
      const val = _getParamValue(method, key, spec);
      if (val !== spec.default) {
        methodParams[key] = val;
        anyCustom = true;
      }
    }
    if (anyCustom) params[method] = methodParams;
  }
  return params;
}

function resetLabParams() {
  for (const [method, params] of Object.entries(labParamSchema)) {
    for (const [key, spec] of Object.entries(params)) {
      const el = document.getElementById(`lab-p-${method}-${key}`);
      const rangeEl = document.getElementById(`lab-r-${method}-${key}`);
      if (!el) continue;
      if (spec.options) {
        el.value = spec.default;
      } else if (el.type === 'checkbox') {
        el.checked = !!spec.default;
      } else {
        el.value = spec.default;
      }
      if (rangeEl) rangeEl.value = spec.default;
    }
  }
  updateParamBadge();
}

// Toggle params panel
document.getElementById('lab-params-toggle').addEventListener('click', () => {
  const panel = document.getElementById('lab-params-panel');
  const chevron = document.querySelector('.lab-toggle-chevron');
  panel.classList.toggle('hidden');
  chevron.classList.toggle('lab-toggle-open');
});

document.getElementById('lab-params-reset').addEventListener('click', resetLabParams);

async function loadLabFeeds() {
  try {
    const data = await api('/api/feeds');
    const all = [...new Set([...(data.configured || []), ...(data.discovered || [])])].sort();
    setOptions(labFeed, all, '— select feed —');
  } catch (e) { console.error(e); }
}

labFeed.addEventListener('change', async () => {
  const feed = labFeed.value;
  if (!feed) return;
  try {
    const data = await api(`/api/recordings?feed_id=${encodeURIComponent(feed)}`);
    setOptions(labDate, data.dates || [], '— select date —');
    labFile.innerHTML = '';
    labResultsEl.innerHTML = '';
  } catch (e) { console.error(e); }
});

labDate.addEventListener('change', async () => {
  const feed = labFeed.value, date = labDate.value;
  if (!feed || !date) return;
  try {
    const data = await api(`/api/recordings?feed_id=${encodeURIComponent(feed)}&date=${encodeURIComponent(date)}`);
    const recs = data.recordings || [];
    labFile.innerHTML = '<option value="">— select recording —</option>';
    recs.forEach(r => {
      const o = document.createElement('option');
      o.value = r.filename;
      o.textContent = r.filename;
      labFile.appendChild(o);
    });
    labResultsEl.innerHTML = '';
  } catch (e) { console.error(e); }
});

labFile.addEventListener('change', async () => {
  const feed = labFeed.value, date = labDate.value, file = labFile.value;
  labResultsEl.innerHTML = '';
  if (!feed || !date || !file) return;

  const origPlayer = document.getElementById('lab-original-player');
  const origAudio = document.getElementById('lab-original-audio');
  origAudio.src = `/api/audio/${encodeURIComponent(feed)}/${encodeURIComponent(date)}/${encodeURIComponent(file)}`;
  origPlayer.classList.remove('hidden');

  await loadLabArtifacts();
});

async function loadLabArtifacts() {
  const feed = labFeed.value, date = labDate.value, file = labFile.value;
  if (!feed || !date || !file) return;

  try {
    const data = await api(`/api/preprocess/artifacts?feed_id=${encodeURIComponent(feed)}&date=${encodeURIComponent(date)}&filename=${encodeURIComponent(file)}`);
    renderLabResults(data.artifacts || [], feed, date);
  } catch (e) { console.error(e); }
}

labRunBtn.addEventListener('click', runLabPreprocess);

async function runLabPreprocess() {
  const feed = labFeed.value, date = labDate.value, file = labFile.value;
  if (!feed || !date || !file) {
    labStatus.textContent = 'Select feed, date, and recording first.';
    labStatus.className = 'text-sm text-red-400';
    return;
  }

  const checked = [...document.querySelectorAll('input[name="lab-method"]:checked')].map(el => el.value);
  if (!checked.length) {
    labStatus.textContent = 'Select at least one preprocessing method.';
    labStatus.className = 'text-sm text-red-400';
    return;
  }

  const params = collectLabParams();
  const paramInfo = Object.keys(params).length ? ' (custom params)' : '';

  labRunBtn.disabled = true;
  labStatus.textContent = `Running ${checked.join(', ')}${paramInfo}...`;
  labStatus.className = 'text-sm text-gray-300 animate-pulse';

  try {
    const payload = { feed_id: feed, date, filename: file, methods: checked };
    if (Object.keys(params).length) payload.params = params;

    const result = await api('/api/preprocess/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const ok = (result.results || []).filter(r => r.success).length;
    const fail = (result.results || []).filter(r => !r.success).length;
    labStatus.textContent = `Done: ${ok} succeeded, ${fail} failed.`;
    labStatus.className = fail > 0 ? 'text-sm text-yellow-400' : 'text-sm text-green-400';
    await loadLabArtifacts();
  } catch (e) {
    labStatus.textContent = `Error: ${e.message}`;
    labStatus.className = 'text-sm text-red-400';
  } finally {
    labRunBtn.disabled = false;
  }
}

function _formatParamTag(tag) {
  if (!tag) return '';
  return tag
    .replace(/([a-z_]+?)(\-?[\d.]+)/g, (_, k, v) => {
      const labels = { i: 'intensity', v: 'version', highpass_freq: 'hp', lowpass_freq: 'lp',
        noise_floor_db: 'nf', noise_reduction: 'nr', dynaudnorm_peak: 'peak',
        dynaudnorm_smoothing: 'smooth', silence_threshold_db: 'sil',
        silence_stop_duration: 'sildur', leave_silence: 'gap',
        noise_sample_duration: 'sample', intensity_ratio: 'intensity',
        effect_version: 'ver', enable_vad: 'vad' };
      return `${labels[k] || k} ${v}`;
    })
    .replace(/_/g, ', ')
    .replace(/derev/g, 'dereverb')
    .replace(/vad/g, 'vad on');
}

function renderLabResults(artifacts, feed, date) {
  const header = document.getElementById('lab-results-header');
  if (!artifacts.length) {
    header.classList.add('hidden');
    labResultsEl.innerHTML = '<p class="text-gray-500 text-center py-8">No preprocessed versions yet. Select methods and click Run.</p>';
    return;
  }

  header.classList.remove('hidden');

  labResultsEl.innerHTML = artifacts.map(a => {
    const url = `/api/preprocessed/${encodeURIComponent(feed)}/${encodeURIComponent(date)}/${encodeURIComponent(a.filename)}`;
    const size = fmtBytes(a.size_bytes);
    const customBadge = a.is_custom ? '<span class="lab-custom-badge">custom</span>' : '';
    const paramInfo = a.param_tag ? `<span class="text-xs text-yellow-400/70">${esc(_formatParamTag(a.param_tag))}</span>` : '';
    const timeStr = a.mtime_iso || '';
    return `
      <div class="card lab-result-card">
        <div class="flex items-center gap-2 mb-2">
          <span class="inline-block px-2 py-0.5 rounded text-xs font-bold uppercase
            ${a.method === 'maxine' ? 'bg-green-900/50 text-green-400' :
              a.method === 'none' ? 'bg-gray-800 text-gray-400' :
              'bg-brand-900/50 text-brand-400'}">${esc(a.method)}</span>
          ${customBadge}
          ${paramInfo}
          <span class="flex-1"></span>
          <span class="text-xs text-gray-600 shrink-0">${size}</span>
          <span class="text-xs text-gray-700 shrink-0">${esc(timeStr)}</span>
          <button type="button" class="lab-delete-btn" data-filename="${esc(a.filename)}" title="Delete this file">
            <svg class="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M8.75 1A2.75 2.75 0 006 3.75v.443c-.795.077-1.584.176-2.365.298a.75.75 0 10.23 1.482l.149-.022.841 10.518A2.75 2.75 0 007.596 19h4.807a2.75 2.75 0 002.742-2.53l.841-10.52.149.023a.75.75 0 00.23-1.482A41.03 41.03 0 0014 4.193V3.75A2.75 2.75 0 0011.25 1h-2.5zM10 4c.84 0 1.673.025 2.5.075V3.75c0-.69-.56-1.25-1.25-1.25h-2.5c-.69 0-1.25.56-1.25 1.25v.325C8.327 4.025 9.16 4 10 4zM8.58 7.72a.75.75 0 00-1.5.06l.3 7.5a.75.75 0 101.5-.06l-.3-7.5zm4.34.06a.75.75 0 10-1.5-.06l-.3 7.5a.75.75 0 101.5.06l.3-7.5z" clip-rule="evenodd"/></svg>
          </button>
        </div>
        <audio controls preload="metadata" class="w-full rounded-lg">
          <source src="${url}" type="${a.ext === '.mp3' ? 'audio/mpeg' : 'audio/wav'}" />
        </audio>
      </div>`;
  }).join('');

  labResultsEl.querySelectorAll('.lab-delete-btn').forEach(btn => {
    btn.addEventListener('click', () => deleteLabArtifact(btn.dataset.filename));
  });
}

async function deleteLabArtifact(targetFilename) {
  const feed = labFeed.value, date = labDate.value, file = labFile.value;
  if (!feed || !date || !file) return;
  try {
    await api('/api/preprocess/artifacts', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ feed_id: feed, date, filename: file, target: targetFilename }),
    });
    await loadLabArtifacts();
  } catch (e) {
    console.error('Delete failed', e);
  }
}

async function clearAllLabArtifacts() {
  const feed = labFeed.value, date = labDate.value, file = labFile.value;
  if (!feed || !date || !file) return;
  try {
    const result = await api('/api/preprocess/artifacts', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ feed_id: feed, date, filename: file, target: 'all' }),
    });
    labStatus.textContent = `Cleared ${result.deleted} file(s).`;
    labStatus.className = 'text-sm text-gray-400';
    await loadLabArtifacts();
  } catch (e) {
    console.error('Clear all failed', e);
  }
}

document.getElementById('lab-clear-all').addEventListener('click', clearAllLabArtifacts);

// ─────────────────── Initialization ────────────────────
loadOverview();
loadBrowserFeeds();
loadRecentFlights();
loadProfileFeeds();
loadProfileSummary();
loadLabStatus();
loadLabFeeds();
