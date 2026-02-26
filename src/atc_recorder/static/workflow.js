/* ATC Recorder – Workflow pipeline editor (Litegraph.js) */

(function () {
  'use strict';

  if (typeof LiteGraph === 'undefined') {
    console.error('Litegraph.js not loaded – workflow tab disabled');
    return;
  }

  var AUDIO_TYPE = 'audio';
  var TRANSCRIPT_TYPE = 'transcript';
  var RESULT_TYPE = 'result';

  // ───────────── Custom node types ──────────────────────────

  function AudioSourceNode() {
    this.addOutput('audio', AUDIO_TYPE);
    this.title = 'Audio Source';
    this.color = '#2a4';
    this.size = [180, 30];
  }
  AudioSourceNode.title = 'Audio Source';
  AudioSourceNode.desc = 'Input audio file (selected above)';
  LiteGraph.registerNodeType('atc/AudioSource', AudioSourceNode);

  function MaxineNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.addOutput('audio', AUDIO_TYPE);
    this.properties = {
      effect: 'denoiser',
      effect_version: 2,
      intensity_ratio: 1.0,
      enable_vad: false,
    };
    this.addWidget('combo', 'Effect', 'denoiser', function (v) { this.properties.effect = v; }.bind(this),
      { values: ['denoiser', 'dereverb_denoiser', 'superres', 'studio_voice_high_quality'] });
    this.addWidget('combo', 'Version', 2, function (v) { this.properties.effect_version = Number(v); }.bind(this),
      { values: [1, 2] });
    this.addWidget('slider', 'Intensity', 1.0, function (v) { this.properties.intensity_ratio = v; }.bind(this),
      { min: 0, max: 1, step: 0.05 });
    this.addWidget('toggle', 'VAD', false, function (v) { this.properties.enable_vad = v; }.bind(this));
    this.title = 'Maxine Denoise';
    this.color = '#547';
    this.size = [220, 130];
  }
  MaxineNode.title = 'Maxine Denoise';
  MaxineNode.desc = 'NVIDIA Maxine Audio Effects denoiser';
  LiteGraph.registerNodeType('atc/Maxine', MaxineNode);

  function BandpassNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.addOutput('audio', AUDIO_TYPE);
    this.properties = { highpass_freq: 300, lowpass_freq: 3400 };
    this.addWidget('slider', 'Highpass (Hz)', 300, function (v) { this.properties.highpass_freq = Math.round(v); }.bind(this),
      { min: 50, max: 1000, step: 10 });
    this.addWidget('slider', 'Lowpass (Hz)', 3400, function (v) { this.properties.lowpass_freq = Math.round(v); }.bind(this),
      { min: 1000, max: 8000, step: 100 });
    this.title = 'Bandpass Filter';
    this.color = '#456';
    this.size = [220, 80];
  }
  BandpassNode.title = 'Bandpass Filter';
  BandpassNode.desc = 'FFmpeg highpass + lowpass';
  LiteGraph.registerNodeType('atc/Bandpass', BandpassNode);

  function DenoiseNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.addOutput('audio', AUDIO_TYPE);
    this.properties = { noise_floor_db: -25 };
    this.addWidget('slider', 'Noise Floor (dB)', -25, function (v) { this.properties.noise_floor_db = Math.round(v); }.bind(this),
      { min: -60, max: 0, step: 1 });
    this.title = 'FFT Denoise';
    this.color = '#456';
    this.size = [220, 50];
  }
  DenoiseNode.title = 'FFT Denoise';
  DenoiseNode.desc = 'FFmpeg afftdn noise reduction';
  LiteGraph.registerNodeType('atc/Denoise', DenoiseNode);

  function SilenceRemoveNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.addOutput('audio', AUDIO_TYPE);
    this.properties = { stop_duration: 0.3, threshold_db: -30, leave_silence: 0.1 };
    this.addWidget('slider', 'Min Silence (s)', 0.3, function (v) { this.properties.stop_duration = v; }.bind(this),
      { min: 0.05, max: 5.0, step: 0.05 });
    this.addWidget('slider', 'Threshold (dB)', -30, function (v) { this.properties.threshold_db = Math.round(v); }.bind(this),
      { min: -60, max: 0, step: 1 });
    this.addWidget('slider', 'Leave Silence (s)', 0.1, function (v) { this.properties.leave_silence = v; }.bind(this),
      { min: 0, max: 2, step: 0.05 });
    this.title = 'Silence Remove';
    this.color = '#654';
    this.size = [220, 100];
  }
  SilenceRemoveNode.title = 'Silence Remove';
  SilenceRemoveNode.desc = 'FFmpeg silenceremove filter (WARNING: changes audio timeline)';
  LiteGraph.registerNodeType('atc/SilenceRemove', SilenceRemoveNode);

  function NormalizeNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.addOutput('audio', AUDIO_TYPE);
    this.properties = { peak: 0.9, smoothing: 5 };
    this.addWidget('slider', 'Peak', 0.9, function (v) { this.properties.peak = v; }.bind(this),
      { min: 0.1, max: 1.0, step: 0.05 });
    this.addWidget('slider', 'Smoothing', 5, function (v) { this.properties.smoothing = Math.round(v); }.bind(this),
      { min: 1, max: 30, step: 1 });
    this.title = 'Normalize';
    this.color = '#456';
    this.size = [220, 80];
  }
  NormalizeNode.title = 'Normalize';
  NormalizeNode.desc = 'FFmpeg dynaudnorm dynamic normalization';
  LiteGraph.registerNodeType('atc/Normalize', NormalizeNode);

  function SoxNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.addOutput('audio', AUDIO_TYPE);
    this.properties = {
      noise_sample_duration: 0.5,
      noise_reduction: 0.21,
      highpass_freq: 300,
      lowpass_freq: 3400,
    };
    this.addWidget('slider', 'Sample (s)', 0.5, function (v) { this.properties.noise_sample_duration = v; }.bind(this),
      { min: 0.1, max: 5.0, step: 0.1 });
    this.addWidget('slider', 'Reduction', 0.21, function (v) { this.properties.noise_reduction = v; }.bind(this),
      { min: 0, max: 1, step: 0.01 });
    this.addWidget('slider', 'Highpass (Hz)', 300, function (v) { this.properties.highpass_freq = Math.round(v); }.bind(this),
      { min: 50, max: 1000, step: 10 });
    this.addWidget('slider', 'Lowpass (Hz)', 3400, function (v) { this.properties.lowpass_freq = Math.round(v); }.bind(this),
      { min: 1000, max: 8000, step: 100 });
    this.title = 'Sox Noisered';
    this.color = '#564';
    this.size = [220, 130];
  }
  SoxNode.title = 'Sox Noisered';
  SoxNode.desc = 'Sox noise reduction with auto noise profile';
  LiteGraph.registerNodeType('atc/Sox', SoxNode);

  // ── ASR Node ──
  function ASRNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.addOutput('transcript', TRANSCRIPT_TYPE);
    this.properties = { model: 'whisper', segment_by_pauses: true };
    this.addWidget('combo', 'Model', 'whisper', function (v) { this.properties.model = v; }.bind(this),
      { values: ['whisper', 'parakeet'] });
    this.addWidget('toggle', 'Segment by pauses', true, function (v) { this.properties.segment_by_pauses = v; }.bind(this));
    this.title = 'ASR Transcribe';
    this.color = '#d82';
    this.size = [220, 80];
  }
  ASRNode.title = 'ASR Transcribe';
  ASRNode.desc = 'Whisper / Parakeet speech-to-text';
  LiteGraph.registerNodeType('atc/ASR', ASRNode);

  // ── Embedding / RAG Ingest Node ──
  function EmbeddingNode() {
    this.addInput('transcript', TRANSCRIPT_TYPE);
    this.addOutput('result', RESULT_TYPE);
    this.properties = { enabled: true };
    this.addWidget('toggle', 'Ingest to RAG', true, function (v) { this.properties.enabled = v; }.bind(this));
    this.title = 'Embed & Ingest';
    this.color = '#28a';
    this.size = [220, 50];
  }
  EmbeddingNode.title = 'Embed & Ingest';
  EmbeddingNode.desc = 'Embed transcript and ingest into vector store';
  LiteGraph.registerNodeType('atc/Embedding', EmbeddingNode);

  // ── End Node (accepts any type) ──
  function EndNode() {
    this.addInput('in', 0);
    this.title = 'End';
    this.color = '#a42';
    this.size = [180, 30];
  }
  EndNode.title = 'End';
  EndNode.desc = 'Pipeline terminus';
  LiteGraph.registerNodeType('atc/End', EndNode);

  // Keep legacy Output for backward compat with saved presets
  function OutputNode() {
    this.addInput('audio', AUDIO_TYPE);
    this.title = 'Output';
    this.color = '#a42';
    this.size = [180, 30];
  }
  OutputNode.title = 'Output';
  OutputNode.desc = 'Pipeline output (legacy)';
  LiteGraph.registerNodeType('atc/Output', OutputNode);

  // Preprocessing step mapping
  var NODE_TO_STEP = {
    'atc/Maxine': 'maxine',
    'atc/Bandpass': 'bandpass',
    'atc/Denoise': 'denoise',
    'atc/SilenceRemove': 'silence_remove',
    'atc/Normalize': 'normalize',
    'atc/Sox': 'sox_noisered',
  };

  // ───────────── Graph setup ────────────────────────────────

  var graph = new LGraph();
  var canvas = null;

  function initCanvas() {
    var el = document.getElementById('wf-canvas');
    if (!el) return;
    var wrap = document.getElementById('wf-canvas-wrap');
    el.width = wrap.clientWidth;
    el.height = wrap.clientHeight;

    canvas = new LGraphCanvas(el, graph);
    canvas.background_image = null;
    canvas.clear_background = true;
    canvas.render_shadows = false;
    canvas.default_link_color = '#8af';
    canvas.highquality_render = true;

    seedDefaultGraph();
    graph.start();
  }

  function seedDefaultGraph() {
    graph.clear();
    var src = LiteGraph.createNode('atc/AudioSource');
    src.pos = [50, 200];
    graph.add(src);

    var maxine = LiteGraph.createNode('atc/Maxine');
    maxine.pos = [280, 160];
    graph.add(maxine);

    var asr = LiteGraph.createNode('atc/ASR');
    asr.pos = [550, 190];
    graph.add(asr);

    var embed = LiteGraph.createNode('atc/Embedding');
    embed.pos = [820, 200];
    graph.add(embed);

    var endN = LiteGraph.createNode('atc/End');
    endN.pos = [1090, 210];
    graph.add(endN);

    src.connect(0, maxine, 0);
    maxine.connect(0, asr, 0);
    asr.connect(0, embed, 0);
    embed.connect(0, endN, 0);
  }

  // ───────────── Graph-to-pipeline serialization ────────────

  function graphToFullPipeline() {
    var terminalNodes = graph._nodes.filter(function (n) {
      return n.type === 'atc/End' || n.type === 'atc/Output';
    });
    if (terminalNodes.length === 0) return { steps: [], asr: null, embed: false };

    var termNode = terminalNodes[0];
    var allNodes = [];
    collectChain(termNode, allNodes);
    allNodes.reverse();

    var steps = [];
    var asrConfig = null;
    var embedEnabled = false;

    allNodes.forEach(function (n) {
      var stepType = NODE_TO_STEP[n.type];
      if (stepType) {
        var params = {};
        for (var k in n.properties) {
          if (n.properties.hasOwnProperty(k)) params[k] = n.properties[k];
        }
        steps.push({ step: stepType, params: params });
      } else if (n.type === 'atc/ASR') {
        asrConfig = {
          model: n.properties.model || 'whisper',
          segment_by_pauses: !!n.properties.segment_by_pauses,
        };
      } else if (n.type === 'atc/Embedding') {
        embedEnabled = !!n.properties.enabled;
      }
    });

    return { steps: steps, asr: asrConfig, embed: embedEnabled };
  }

  function collectChain(node, collected) {
    if (!node || !node.inputs) return;
    for (var i = 0; i < node.inputs.length; i++) {
      var input = node.inputs[i];
      if (!input || input.link == null) continue;
      var linkInfo = graph.links[input.link];
      if (!linkInfo) continue;
      var srcNode = graph.getNodeById(linkInfo.origin_id);
      if (!srcNode) continue;
      if (srcNode.type !== 'atc/AudioSource') {
        collected.push(srcNode);
      }
      collectChain(srcNode, collected);
    }
  }

  // Legacy compat: returns just preprocessing steps (for old save-preset flow)
  function graphToPipeline() {
    return graphToFullPipeline().steps;
  }

  // ───────────── Feed/date/file selectors ───────────────────

  var wfFeed = document.getElementById('wf-feed');
  var wfDate = document.getElementById('wf-date');
  var wfFile = document.getElementById('wf-file');
  var wfPreset = document.getElementById('wf-preset');
  var wfStatus = document.getElementById('wf-status');

  async function loadWfFeeds() {
    try {
      var data = await api('/api/feeds');
      var all = [].concat(data.configured || [], data.discovered || []);
      all = all.filter(function (v, i, a) { return a.indexOf(v) === i; }).sort();
      setOptions(wfFeed, all, '— select feed —');
    } catch (e) { console.error(e); }
  }

  wfFeed.addEventListener('change', async function () {
    var feed = wfFeed.value;
    if (!feed) return;
    try {
      var data = await api('/api/recordings?feed_id=' + encodeURIComponent(feed));
      setOptions(wfDate, data.dates || [], '— select date —');
      wfFile.innerHTML = '';
    } catch (e) { console.error(e); }
  });

  wfDate.addEventListener('change', async function () {
    var feed = wfFeed.value, date = wfDate.value;
    if (!feed || !date) return;
    try {
      var data = await api('/api/recordings?feed_id=' + encodeURIComponent(feed) + '&date=' + encodeURIComponent(date));
      var recs = data.recordings || [];
      wfFile.innerHTML = '<option value="">— select recording —</option>';
      recs.forEach(function (r) {
        var o = document.createElement('option');
        o.value = r.filename;
        o.textContent = r.filename;
        wfFile.appendChild(o);
      });
    } catch (e) { console.error(e); }
  });

  // ───────────── Preset management ──────────────────────────

  async function loadPresetList() {
    try {
      var data = await api('/api/pipeline/presets');
      var presets = data.presets || [];
      wfPreset.innerHTML = '<option value="">— select preset —</option>';
      presets.forEach(function (p) {
        var o = document.createElement('option');
        o.value = p.name;
        o.textContent = p.name + (p.is_builtin ? ' (built-in)' : '');
        wfPreset.appendChild(o);
      });
    } catch (e) { console.error('Failed to load presets', e); }
  }

  document.getElementById('wf-load-preset').addEventListener('click', async function () {
    var name = wfPreset.value;
    if (!name) { wfStatus.textContent = 'Select a preset first.'; return; }
    try {
      var data = await api('/api/pipeline/presets/' + encodeURIComponent(name));
      var defn = data.definition || {};
      if (defn.graph_json) {
        graph.configure(defn.graph_json);
      } else {
        pipelineToGraph(defn.steps || []);
      }
      wfStatus.textContent = 'Loaded preset: ' + name;
    } catch (e) {
      wfStatus.textContent = 'Error loading preset: ' + e.message;
    }
  });

  document.getElementById('wf-save-preset').addEventListener('click', async function () {
    var name = prompt('Preset name:');
    if (!name || !name.trim()) return;
    name = name.trim();

    var pipeline = graphToFullPipeline();
    if (pipeline.steps.length === 0 && !pipeline.asr) {
      wfStatus.textContent = 'Cannot save empty pipeline. Add processing nodes.';
      return;
    }

    try {
      await api('/api/pipeline/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          steps: pipeline.steps,
          graph_json: graph.serialize(),
        }),
      });
      wfStatus.textContent = 'Saved preset: ' + name;
      await loadPresetList();
      wfPreset.value = name;
    } catch (e) {
      wfStatus.textContent = 'Error saving: ' + e.message;
    }
  });

  document.getElementById('wf-delete-preset').addEventListener('click', async function () {
    var name = wfPreset.value;
    if (!name) { wfStatus.textContent = 'Select a preset first.'; return; }
    if (!confirm('Delete preset "' + name + '"?')) return;
    try {
      await api('/api/pipeline/presets/' + encodeURIComponent(name), { method: 'DELETE' });
      wfStatus.textContent = 'Deleted preset: ' + name;
      await loadPresetList();
    } catch (e) {
      wfStatus.textContent = 'Error: ' + e.message;
    }
  });

  // ───────────── Pipeline-to-graph (load preset) ────────────

  var STEP_TO_NODE = {
    'maxine': 'atc/Maxine',
    'bandpass': 'atc/Bandpass',
    'denoise': 'atc/Denoise',
    'silence_remove': 'atc/SilenceRemove',
    'normalize': 'atc/Normalize',
    'sox_noisered': 'atc/Sox',
  };

  function pipelineToGraph(steps) {
    graph.clear();
    var src = LiteGraph.createNode('atc/AudioSource');
    src.pos = [50, 200];
    graph.add(src);

    var xOffset = 300;
    var prevNode = src;

    steps.forEach(function (s, i) {
      var nodeType = STEP_TO_NODE[s.step];
      if (!nodeType) return;
      var node = LiteGraph.createNode(nodeType);
      node.pos = [xOffset + i * 260, 170];
      if (s.params) {
        for (var k in s.params) {
          if (s.params.hasOwnProperty(k)) {
            node.properties[k] = s.params[k];
          }
        }
        syncWidgetsFromProperties(node);
      }
      graph.add(node);
      prevNode.connect(0, node, 0);
      prevNode = node;
    });

    var endN = LiteGraph.createNode('atc/End');
    endN.pos = [xOffset + steps.length * 260, 200];
    graph.add(endN);
    prevNode.connect(0, endN, 0);
  }

  function syncWidgetsFromProperties(node) {
    if (!node.widgets) return;
    var WIDGET_MAP = {
      'Effect': 'effect', 'Version': 'effect_version', 'Intensity': 'intensity_ratio',
      'VAD': 'enable_vad', 'Highpass (Hz)': 'highpass_freq', 'Lowpass (Hz)': 'lowpass_freq',
      'Noise Floor (dB)': 'noise_floor_db', 'Min Silence (s)': 'stop_duration',
      'Threshold (dB)': 'threshold_db', 'Leave Silence (s)': 'leave_silence',
      'Peak': 'peak', 'Smoothing': 'smoothing', 'Sample (s)': 'noise_sample_duration',
      'Reduction': 'noise_reduction', 'Model': 'model',
      'Segment by pauses': 'segment_by_pauses', 'Ingest to RAG': 'enabled',
    };
    node.widgets.forEach(function (w) {
      var prop = WIDGET_MAP[w.name];
      if (prop && node.properties.hasOwnProperty(prop)) {
        w.value = node.properties[prop];
      }
    });
  }

  // ───────────── Run pipeline (single file) ─────────────────

  document.getElementById('wf-run').addEventListener('click', async function () {
    var feed = wfFeed.value, date = wfDate.value, file = wfFile.value;
    if (!feed || !date || !file) {
      wfStatus.textContent = 'Select feed, date, and recording first.';
      wfStatus.className = 'text-sm text-red-400 mt-3';
      return;
    }

    var pipeline = graphToFullPipeline();
    if (pipeline.steps.length === 0 && !pipeline.asr) {
      wfStatus.textContent = 'Pipeline is empty. Add processing nodes between Source and End.';
      wfStatus.className = 'text-sm text-red-400 mt-3';
      return;
    }

    var btn = document.getElementById('wf-run');
    btn.disabled = true;

    var desc = pipeline.steps.map(function (s) { return s.step; }).join(' → ');
    if (pipeline.asr) desc += (desc ? ' → ' : '') + 'ASR(' + pipeline.asr.model + ')';
    if (pipeline.embed) desc += ' → Embed';
    wfStatus.textContent = 'Running: ' + desc + '...';
    wfStatus.className = 'text-sm text-gray-300 mt-3 animate-pulse';

    var resultDiv = document.getElementById('wf-result');
    var resultContent = document.getElementById('wf-result-content');

    try {
      if (pipeline.asr) {
        var result = await api('/api/pipeline/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            feed_id: feed, date: date, filename: file,
            steps: pipeline.steps,
            asr: pipeline.asr,
            embed: pipeline.embed,
          }),
        });

        var msg = 'Complete in ' + result.elapsed_seconds + 's — ' + result.segment_count + ' segments';
        if (result.ingest) msg += ' | Ingested ' + result.ingest.docs_upserted + ' docs';
        wfStatus.textContent = msg;
        wfStatus.className = 'text-sm text-green-400 mt-3';

        renderWorkflowTranscript(result, feed, date, file);
        resultDiv.classList.remove('hidden');
      } else {
        var result = await api('/api/pipeline/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ feed_id: feed, date: date, filename: file, steps: pipeline.steps }),
        });

        wfStatus.textContent = 'Preprocessing complete — ' + fmtBytes(result.size_bytes) + ' in ' + result.elapsed_seconds + 's';
        wfStatus.className = 'text-sm text-green-400 mt-3';

        var html = '<div class="card"><h3 class="card-title">Preprocessed Audio</h3>';
        html += '<div class="text-xs text-gray-500 mt-1">' + esc(result.filename) + ' (' + fmtBytes(result.size_bytes) + ')</div>';
        html += '<audio controls class="w-full rounded-lg mt-2"><source src="/api/preprocessed/'
          + encodeURIComponent(feed) + '/' + encodeURIComponent(date) + '/' + encodeURIComponent(result.filename)
          + '" type="audio/wav" /></audio></div>';
        resultContent.innerHTML = html;
        resultDiv.classList.remove('hidden');
      }
    } catch (e) {
      wfStatus.textContent = 'Error: ' + e.message;
      wfStatus.className = 'text-sm text-red-400 mt-3';
    } finally {
      btn.disabled = false;
    }
  });

  // ───────────── Interactive transcript display ──────────────

  var wfAudio = null;

  function renderWorkflowTranscript(result, feed, date, filename) {
    var resultContent = document.getElementById('wf-result-content');
    var segments = result.segments || [];
    var visibleSegs = segments.filter(function (seg) {
      var t = (seg.text || '').trim();
      return t && t !== '...';
    });

    var html = '<div class="card">';
    html += '<div class="flex items-center justify-between">';
    html += '<h3 class="card-title">Transcript</h3>';
    html += '<div class="text-xs text-gray-500">' + esc(result.audio_file) + ' &middot; ' + result.segment_count + ' segments</div>';
    html += '</div>';
    if (result.ingest) {
      html += '<div class="text-xs text-blue-400 mt-1">RAG: ' + result.ingest.docs_upserted + ' docs ingested';
      if (result.ingest.errors > 0) html += ' (' + result.ingest.errors + ' errors)';
      html += '</div>';
    }
    var audioLabel = result.preprocessed_file
      ? '<span class="text-xs text-green-400">Playing: preprocessed audio (' + esc(result.preprocessed_file) + ')</span>'
      : '<span class="text-xs text-gray-500">Playing: original audio</span>';
    html += '<div class="mt-2">' + audioLabel + '</div>';
    html += '<audio id="wf-transcript-audio" controls class="w-full rounded-lg mt-2"></audio>';
    html += '<div id="wf-transcript-timeline" class="mt-3 space-y-1">';

    if (visibleSegs.length === 0) {
      var fullText = (result.text_preview || '').trim();
      if (fullText) {
        html += '<p class="text-gray-300 text-sm whitespace-pre-wrap">' + esc(fullText) + '</p>';
      } else {
        html += '<p class="text-gray-500">No displayable segments.</p>';
      }
    } else {
      visibleSegs.forEach(function (seg, i) {
        var role = (seg.speaker_role || 'UNKNOWN').toLowerCase();
        var text = seg.text || '';
        var startVal = parseTimeValue(seg.start_time);
        var endVal = parseTimeValue(seg.end_time);
        var startSec = Number.isFinite(startVal) ? startVal : 0;
        var endSec = Number.isFinite(endVal) ? endVal : 0;
        html += '<div class="seg-row" data-start="' + startSec + '" data-end="' + endSec + '" data-idx="' + i + '">';
        html += '<span class="seg-time">' + fmtTime(startSec) + ' – ' + fmtTime(endSec) + '</span>';
        html += '<span class="seg-role ' + role + '">' + esc(role) + '</span>';
        html += '<span class="seg-text">' + esc(text) + '</span>';
        html += '</div>';
      });
    }
    html += '</div></div>';
    resultContent.innerHTML = html;

    wfAudio = document.getElementById('wf-transcript-audio');
    if (wfAudio) {
      if (result.preprocessed_file) {
        wfAudio.src = '/api/preprocessed/' + encodeURIComponent(feed) + '/' + encodeURIComponent(date) + '/' + encodeURIComponent(result.preprocessed_file);
      } else {
        if (!filename.endsWith('.mp3')) filename = filename.replace(/\.[^.]+$/, '.mp3');
        wfAudio.src = '/api/audio/' + encodeURIComponent(feed) + '/' + encodeURIComponent(date) + '/' + encodeURIComponent(filename);
      }

      var timeline = document.getElementById('wf-transcript-timeline');

      timeline.querySelectorAll('.seg-row').forEach(function (row) {
        row.addEventListener('click', function () {
          var start = parseFloat(row.dataset.start);
          wfAudio.currentTime = Number.isFinite(start) ? start : 0;
          wfAudio.play().catch(function () {});
          timeline.querySelectorAll('.seg-row').forEach(function (r) { r.classList.remove('playing'); });
          row.classList.add('playing');
        });
      });

      wfAudio.ontimeupdate = function () {
        var t = wfAudio.currentTime;
        timeline.querySelectorAll('.seg-row').forEach(function (row) {
          var s = parseFloat(row.dataset.start);
          var e = parseFloat(row.dataset.end);
          row.classList.toggle('playing', t >= s && t <= e);
        });
      };
    }
  }

  // ───────────── Batch execution ────────────────────────────

  var currentBatchId = null;
  var batchPollTimer = null;

  async function loadBatchFeeds() {
    var batchFeed = document.getElementById('wf-batch-feed');
    if (!batchFeed) return;
    try {
      var data = await api('/api/feeds');
      var all = [].concat(data.configured || [], data.discovered || []);
      all = all.filter(function (v, i, a) { return a.indexOf(v) === i; }).sort();
      batchFeed.innerHTML = '<option value="">All feeds</option>';
      all.forEach(function (f) {
        var o = document.createElement('option');
        o.value = f;
        o.textContent = f;
        batchFeed.appendChild(o);
      });
    } catch (e) { console.error(e); }
  }

  var batchFeedEl = document.getElementById('wf-batch-feed');
  if (batchFeedEl) {
    batchFeedEl.addEventListener('change', async function () {
      var batchDate = document.getElementById('wf-batch-date');
      var feed = batchFeedEl.value;
      batchDate.innerHTML = '<option value="">All dates</option>';
      if (!feed) return;
      try {
        var data = await api('/api/recordings?feed_id=' + encodeURIComponent(feed));
        (data.dates || []).forEach(function (d) {
          var o = document.createElement('option');
          o.value = d;
          o.textContent = d;
          batchDate.appendChild(o);
        });
      } catch (e) { console.error(e); }
    });
  }

  var batchRunBtn = document.getElementById('wf-batch-run');
  if (batchRunBtn) {
    batchRunBtn.addEventListener('click', startBatch);
  }
  var batchCancelBtn = document.getElementById('wf-batch-cancel');
  if (batchCancelBtn) {
    batchCancelBtn.addEventListener('click', cancelBatch);
  }

  async function startBatch() {
    var pipeline = graphToFullPipeline();
    if (!pipeline.asr) {
      setBatchStatus('Pipeline must include an ASR node for batch processing.', 'red');
      return;
    }

    var scope = {};
    var feedVal = (document.getElementById('wf-batch-feed') || {}).value;
    var dateVal = (document.getElementById('wf-batch-date') || {}).value;
    if (feedVal) scope.feed_id = feedVal;
    if (dateVal) scope.date = dateVal;
    var force = !!(document.getElementById('wf-batch-force') || {}).checked;

    batchRunBtn.disabled = true;
    setBatchStatus('Scanning files...', 'gray');

    try {
      var result = await api('/api/pipeline/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          steps: pipeline.steps,
          asr: pipeline.asr,
          embed: pipeline.embed,
          scope: scope,
          force: force,
        }),
      });

      if (!result.batch_id) {
        setBatchStatus('No files to process.', 'yellow');
        batchRunBtn.disabled = false;
        return;
      }

      currentBatchId = result.batch_id;
      setBatchStatus('Started — 0 / ' + result.total + ' files', 'blue');
      updateProgressBar(0, result.total);
      document.getElementById('wf-batch-cancel').classList.remove('hidden');
      document.getElementById('wf-batch-progress-wrap').classList.remove('hidden');

      batchPollTimer = setInterval(function () { pollBatch(currentBatchId); }, 2000);
    } catch (e) {
      setBatchStatus('Error: ' + e.message, 'red');
      batchRunBtn.disabled = false;
    }
  }

  async function pollBatch(batchId) {
    try {
      var data = await api('/api/pipeline/batch/' + encodeURIComponent(batchId));

      var pct = data.total > 0 ? Math.round((data.completed + data.failed) / data.total * 100) : 0;
      updateProgressBar(data.completed + data.failed, data.total);

      var msg = data.completed + ' / ' + data.total + ' done';
      if (data.failed > 0) msg += ' (' + data.failed + ' failed)';
      if (data.current_file) msg += ' — ' + data.current_file;
      setBatchStatus(msg, data.status === 'running' ? 'blue' : 'green');

      var errLog = document.getElementById('wf-batch-errors');
      if (errLog && data.errors && data.errors.length > 0) {
        errLog.innerHTML = data.errors.map(function (e) {
          return '<div class="text-xs text-red-400">' + esc(e.file) + ': ' + esc(e.error) + '</div>';
        }).join('');
        errLog.classList.remove('hidden');
      }

      if (data.status !== 'running') {
        clearInterval(batchPollTimer);
        batchPollTimer = null;
        currentBatchId = null;
        batchRunBtn.disabled = false;
        document.getElementById('wf-batch-cancel').classList.add('hidden');
        var finalMsg = 'Batch ' + data.status + ': ' + data.completed + ' succeeded, ' + data.failed + ' failed out of ' + data.total;
        setBatchStatus(finalMsg, data.failed > 0 ? 'yellow' : 'green');
      }
    } catch (e) {
      console.error('Poll error', e);
    }
  }

  async function cancelBatch() {
    if (!currentBatchId) return;
    try {
      await api('/api/pipeline/batch/' + encodeURIComponent(currentBatchId) + '/cancel', { method: 'POST' });
      setBatchStatus('Cancelling...', 'yellow');
    } catch (e) {
      console.error('Cancel error', e);
    }
  }

  function setBatchStatus(msg, color) {
    var el = document.getElementById('wf-batch-status');
    if (!el) return;
    el.textContent = msg;
    el.className = 'text-sm mt-2 text-' + color + '-400';
  }

  function updateProgressBar(done, total) {
    var bar = document.getElementById('wf-batch-bar');
    var label = document.getElementById('wf-batch-bar-label');
    if (!bar) return;
    var pct = total > 0 ? Math.round(done / total * 100) : 0;
    bar.style.width = pct + '%';
    if (label) label.textContent = done + ' / ' + total;
  }

  // ───────────── Resize handling ────────────────────────────

  function resizeCanvas() {
    var wrap = document.getElementById('wf-canvas-wrap');
    var el = document.getElementById('wf-canvas');
    if (!wrap || !el || !canvas) return;
    el.width = wrap.clientWidth;
    el.height = wrap.clientHeight;
    canvas.resize();
  }

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resizeCanvas, 100);
  });

  // ───────────── Tab activation hook ────────────────────────

  var tabInitialized = false;

  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (btn.dataset.tab === 'workflow' && !tabInitialized) {
        tabInitialized = true;
        setTimeout(function () {
          initCanvas();
          loadWfFeeds();
          loadPresetList();
          loadBatchFeeds();
        }, 50);
      }
      if (btn.dataset.tab === 'workflow' && tabInitialized) {
        setTimeout(resizeCanvas, 50);
      }
    });
  });

})();
