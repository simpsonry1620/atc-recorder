/* Labeling and Training tab logic */

(function () {
  "use strict";

  let currentReviewChunkId = null;
  let perfRuns = [];
  let browseOffset = 0;

  // --- helpers ---
  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  function cerColor(cer) {
    if (cer < 0.02) return "text-green-400";
    if (cer < 0.05) return "text-yellow-400";
    return "text-red-400";
  }

  function statusBadge(status) {
    const colors = {
      pending: "bg-gray-700 text-gray-300",
      accepted: "bg-green-900/50 text-green-400",
      rejected: "bg-red-900/50 text-red-400",
      verified: "bg-cyan-900/50 text-cyan-400",
    };
    const cls = colors[status] || "bg-gray-700 text-gray-300";
    return `<span class="px-2 py-0.5 rounded text-xs ${cls}">${status}</span>`;
  }

  function truncate(s, n) {
    return s && s.length > n ? s.slice(0, n) + "..." : s || "";
  }

  // --- empty vs active state toggling ---
  function showEmptyGuide() {
    const guide = document.getElementById("lb-empty-guide");
    const active = document.getElementById("lb-active-ui");
    if (guide) guide.classList.remove("hidden");
    if (active) active.classList.add("hidden");
  }

  function showActiveUI() {
    const guide = document.getElementById("lb-empty-guide");
    const active = document.getElementById("lb-active-ui");
    if (guide) guide.classList.add("hidden");
    if (active) active.classList.remove("hidden");
  }

  // --- background job polling ---
  function pollJob(batchId, barEl, statusEl, onComplete) {
    const interval = setInterval(async () => {
      try {
        const j = await api(`/api/pipeline/batch/${batchId}`);
        const pct = j.total > 0 ? Math.round((j.completed / j.total) * 100) : 0;
        if (barEl) barEl.style.width = pct + "%";
        if (statusEl) {
          statusEl.textContent = j.current_file
            ? `${j.completed + j.failed} / ${j.total} — ${j.current_file}`
            : `${j.completed + j.failed} / ${j.total}`;
        }
        if (j.status === "completed" || j.status === "cancelled") {
          clearInterval(interval);
          if (barEl) barEl.style.width = "100%";
          if (onComplete) onComplete(j);
        }
      } catch (e) {
        clearInterval(interval);
        if (statusEl) statusEl.textContent = "Polling error: " + e.message;
      }
    }, 2000);
    return interval;
  }

  // --- step state helpers ---
  function enableStep2() {
    const btn = document.getElementById("lb-start-label");
    if (btn) btn.disabled = false;
  }

  function enableStepTrim() {
    const btn = document.getElementById("lb-start-trim");
    if (btn) btn.disabled = false;
  }

  function enableStep3() {
    const btn = document.getElementById("lb-start-review");
    if (btn) btn.disabled = false;
  }

  const EXCLUDE_FEEDS = new Set(["chunks", "preprocessed"]);

  function populateFeedSelector() {
    fetch("/api/feeds")
      .then((r) => r.json())
      .then((data) => {
        const list = (data.discovered || data.configured || [])
          .filter((f) => !EXCLUDE_FEEDS.has(f));
        const selectors = [
          document.getElementById("lb-chunk-feed"),
          document.getElementById("lb-label-feed"),
          document.getElementById("lb-trim-feed"),
          document.getElementById("lb-browse-feed"),
        ];
        selectors.forEach((sel) => {
          if (!sel) return;
          const prev = sel.value;
          sel.innerHTML = '<option value="">All feeds</option>';
          list.forEach((name) => {
            sel.innerHTML += `<option value="${name}">${name}</option>`;
          });
          if (prev) sel.value = prev;
        });
      })
      .catch(() => {});
  }

  async function checkStepStates() {
    try {
      const s = await api("/api/labeling/summary");
      if (s.total > 0) {
        enableStep2();
        enableStepTrim();
        enableStep3();
        return;
      }
    } catch (_) {}

    try {
      const r = await api("/api/labeling/chunk-count");
      if (r.count > 0) {
        enableStep2();
      }
    } catch (_) {}
  }

  // --- Dataset overview banner ---
  async function loadDatasetStats() {
    try {
      const [status, cc, summary] = await Promise.all([
        api("/api/status"),
        api("/api/labeling/chunk-count"),
        api("/api/labeling/summary"),
      ]);
      const el = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
      el("lb-ds-feeds", status.feed_count || 0);
      el("lb-ds-recordings", (status.recording_count || 0).toLocaleString());
      el("lb-ds-hours", (status.total_audio_hours || 0).toFixed(1));
      el("lb-ds-chunks", (cc.count || 0).toLocaleString());
      el("lb-ds-labeled", `${(summary.total || 0).toLocaleString()}`);
      el("lb-explorer-count", (cc.count || 0).toLocaleString());
    } catch (_) {}
  }

  // --- Chunk explorer ---
  async function loadChunkStats() {
    try {
      const data = await api("/api/labeling/chunk-stats");
      const tbody = document.getElementById("lb-explorer-stats");
      if (!tbody) return;
      if (!data.feeds || data.feeds.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-gray-500 py-3 text-center">No chunks yet</td></tr>';
        return;
      }
      tbody.innerHTML = data.feeds.map((f) => `
        <tr class="hover:bg-gray-800/50">
          <td class="py-1 text-gray-300">${f.feed_id}</td>
          <td class="py-1 text-right">${f.count.toLocaleString()}</td>
          <td class="py-1 text-right">${(f.total_dur / 3600).toFixed(1)}h</td>
          <td class="py-1 text-right">${f.avg_dur.toFixed(1)}s</td>
          <td class="py-1 pl-3 text-gray-500">${f.earliest} &mdash; ${f.latest}</td>
        </tr>`).join("");
    } catch (e) { console.warn("chunk stats:", e); }
  }

  async function loadChunkBrowse(append) {
    const feed = document.getElementById("lb-browse-feed")?.value || "";
    if (!append) browseOffset = 0;
    const params = new URLSearchParams({ limit: "20", offset: String(browseOffset) });
    if (feed) params.set("feed_id", feed);
    try {
      const data = await api(`/api/labeling/chunk-browse?${params}`);
      const tbody = document.getElementById("lb-browse-tbody");
      if (!tbody) return;
      if (!append) tbody.innerHTML = "";
      if (data.chunks.length === 0 && !append) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-gray-500 py-3 text-center">No chunks found</td></tr>';
        return;
      }
      tbody.innerHTML += data.chunks.map((c) => `
        <tr class="hover:bg-gray-800/50">
          <td class="py-1 font-mono text-gray-400">${c.chunk_id.slice(0, 10)}</td>
          <td class="py-1">${c.feed_id}</td>
          <td class="py-1 text-gray-400">${c.source_file}</td>
          <td class="py-1 text-right">${c.offset_seconds}s</td>
          <td class="py-1 text-right">${c.duration_seconds}s</td>
          <td class="py-1 text-center"><audio src="${c.audio_url}" preload="none" controls class="h-7 w-36"></audio></td>
        </tr>`).join("");
      browseOffset += data.chunks.length;
    } catch (e) { console.warn("chunk browse:", e); }
  }

  function toggleChunkExplorer() {
    const body = document.getElementById("lb-explorer-body");
    const arrow = document.getElementById("lb-explorer-arrow");
    if (!body) return;
    const show = body.classList.toggle("hidden");
    if (arrow) arrow.style.transform = show ? "" : "rotate(90deg)";
    if (!show) {
      loadChunkStats();
      loadChunkBrowse(false);
    }
  }

  // --- Performance comparison panel ---
  function showPerfMetrics(diagnostics, vadBackend) {
    const d = diagnostics;
    if (!d || d.total_wall_time <= 0) return;

    const run = {
      backend: (d.vad_backend || vadBackend || "?").toUpperCase(),
      files: d.energy_sample_count || 0,
      segments: d.total_segments || 0,
      vad: d.total_vad_time.toFixed(2),
      extract: d.total_extract_time.toFixed(2),
      wall: d.total_wall_time.toFixed(2),
    };
    perfRuns.push(run);
    if (perfRuns.length > 2) perfRuns = perfRuns.slice(-2);

    const panel = document.getElementById("lb-perf-panel");
    const tbody = document.getElementById("lb-perf-tbody");
    const col1 = document.getElementById("lb-perf-col1");
    const col2 = document.getElementById("lb-perf-col2");
    if (!panel || !tbody) return;

    const r1 = perfRuns[0];
    const r2 = perfRuns.length > 1 ? perfRuns[1] : null;

    if (col1) col1.textContent = r1.backend;
    if (col2) {
      if (r2) { col2.textContent = r2.backend; col2.classList.remove("hidden"); }
      else col2.classList.add("hidden");
    }

    const rows = [
      ["Segments detected", r1.segments, r2?.segments],
      ["VAD time", r1.vad + "s", r2 ? r2.vad + "s" : null],
      ["Extraction time", r1.extract + "s", r2 ? r2.extract + "s" : null],
      ["Total wall time", r1.wall + "s", r2 ? r2.wall + "s" : null],
    ];
    tbody.innerHTML = rows.map(([label, v1, v2]) => `
      <tr>
        <td class="py-1 pr-4 text-gray-400">${label}</td>
        <td class="py-1 px-3 text-right font-mono">${v1}</td>
        ${r2 ? `<td class="py-1 px-3 text-right font-mono">${v2}</td>` : ""}
      </tr>`).join("");
    panel.classList.remove("hidden");
  }

  // --- Populate pipeline presets ---
  async function loadPipelinePresets() {
    try {
      const data = await api("/api/pipeline/presets");
      const sel = document.getElementById("lb-chunk-preset");
      if (!sel || !data.presets) return;
      sel.innerHTML = '<option value="">None</option>';
      data.presets.forEach((p) => {
        sel.innerHTML += `<option value="${p.name}">${p.name}${p.is_builtin ? " (built-in)" : ""}</option>`;
      });
    } catch (_) {}
  }

  // --- Labeling tab ---
  async function loadLabelingSummary() {
    try {
      const s = await api("/api/labeling/summary");

      if (s.total > 0) {
        enableStepTrim();
        enableStep3();
      }

      const cards = document.getElementById("lb-summary");
      if (!cards) return;
      cards.innerHTML = [
        { label: "Total", value: s.total, cls: "" },
        { label: "Pending", value: s.pending, cls: "text-gray-400" },
        { label: "Accepted", value: s.accepted, cls: "text-green-400" },
        { label: "Rejected", value: s.rejected, cls: "text-red-400" },
        { label: "Verified", value: s.verified, cls: "text-cyan-400" },
      ]
        .map(
          (c) => `
        <div class="stat-card">
          <div class="stat-label">${c.label}</div>
          <div class="stat-value ${c.cls}">${c.value}</div>
        </div>`
        )
        .join("");
    } catch (e) {
      console.warn("labeling summary:", e);
    }
  }

  async function loadLabelingChunks() {
    const status = document.getElementById("lb-status-filter")?.value || "";
    const maxCer = document.getElementById("lb-cer-max")?.value || "";
    const feed = document.getElementById("lb-feed-filter")?.value || "";

    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (maxCer) params.set("max_cer", maxCer);
    if (feed) params.set("feed_id", feed);
    params.set("limit", "200");

    try {
      const chunks = await api(`/api/labeling/chunks?${params}`);
      const tbody = document.getElementById("lb-tbody");
      if (!tbody) return;

      if (chunks.length === 0) {
        tbody.innerHTML =
          '<tr><td colspan="8" class="text-center text-gray-500 py-8">No chunks found</td></tr>';
        return;
      }

      tbody.innerHTML = chunks
        .map(
          (c) => `
        <tr class="hover:bg-gray-800/50 cursor-pointer" data-chunk-id="${c.chunk_id}">
          <td class="px-3 py-2 font-mono text-xs">${c.chunk_id.slice(0, 10)}</td>
          <td class="px-3 py-2">${c.feed_id}</td>
          <td class="px-3 py-2">${c.duration.toFixed(1)}s</td>
          <td class="px-3 py-2 ${cerColor(c.cer)}">${(c.cer * 100).toFixed(1)}%</td>
          <td class="px-3 py-2">${statusBadge(c.status)}</td>
          <td class="px-3 py-2 text-xs text-gray-400">${truncate(c.whisper_text, 60)}</td>
          <td class="px-3 py-2 text-xs text-gray-400">${truncate(c.parakeet_text, 60)}</td>
          <td class="px-3 py-2">
            <button class="text-xs text-brand-400 hover:underline lb-review-btn" data-id="${c.chunk_id}">Review</button>
          </td>
        </tr>`
        )
        .join("");

      tbody.querySelectorAll(".lb-review-btn").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          openReview(btn.dataset.id);
        });
      });
    } catch (e) {
      console.warn("labeling chunks:", e);
    }
  }

  async function openReview(chunkId) {
    try {
      const c = await api(`/api/labeling/chunk/${chunkId}`);
      currentReviewChunkId = chunkId;

      const reviewEl = document.getElementById("lb-review");
      reviewEl.classList.remove("hidden");
      reviewEl.scrollIntoView({ behavior: "smooth", block: "start" });
      document.getElementById("lb-audio").src = `/api/labeling/audio/${chunkId}`;
      document.getElementById("lb-whisper-text").textContent = c.whisper_text;
      document.getElementById("lb-parakeet-text").textContent = c.parakeet_text;
      document.getElementById("lb-verified-text").value =
        c.verified_text || c.consensus_text || c.whisper_text;
      document.getElementById("lb-review-id").textContent =
        `${chunkId} | CER: ${(c.cer * 100).toFixed(1)}% | ${c.status}`;

      const toggle = document.getElementById("lb-denoise-toggle");
      const statusEl = document.getElementById("lb-denoise-status");
      if (toggle) { toggle.checked = false; }
      if (statusEl) statusEl.textContent = "";
    } catch (e) {
      console.warn("open review:", e);
    }
  }

  async function toggleDenoise() {
    if (!currentReviewChunkId) return;
    const toggle = document.getElementById("lb-denoise-toggle");
    const audio = document.getElementById("lb-audio");
    const statusEl = document.getElementById("lb-denoise-status");
    if (!toggle || !audio) return;

    const wasPlaying = !audio.paused;
    const pos = audio.currentTime;

    if (toggle.checked) {
      if (statusEl) statusEl.textContent = "(loading...)";
      audio.src = `/api/labeling/audio/${currentReviewChunkId}?denoise=true`;
      audio.addEventListener("canplay", function onReady() {
        audio.removeEventListener("canplay", onReady);
        if (statusEl) statusEl.textContent = "";
        audio.currentTime = pos;
        if (wasPlaying) audio.play();
      }, { once: true });
      audio.addEventListener("error", function onErr() {
        audio.removeEventListener("error", onErr);
        if (statusEl) statusEl.textContent = "(denoise unavailable)";
        toggle.checked = false;
        audio.src = `/api/labeling/audio/${currentReviewChunkId}`;
      }, { once: true });
    } else {
      audio.src = `/api/labeling/audio/${currentReviewChunkId}`;
      if (statusEl) statusEl.textContent = "";
      audio.addEventListener("canplay", function onReady() {
        audio.removeEventListener("canplay", onReady);
        audio.currentTime = pos;
        if (wasPlaying) audio.play();
      }, { once: true });
    }
    audio.load();
  }

  async function updateChunkStatus(status) {
    if (!currentReviewChunkId) return;
    const verifiedText = document.getElementById("lb-verified-text")?.value || "";
    try {
      await api(`/api/labeling/chunk/${currentReviewChunkId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, verified_text: verifiedText }),
      });
      loadLabelingSummary();
      loadLabelingChunks();
    } catch (e) {
      console.warn("update status:", e);
    }
  }

  // --- Training tab ---
  async function loadBenchmarks() {
    try {
      const runs = await api("/api/training/benchmarks?limit=20");
      const tbody = document.getElementById("tr-bench-tbody");
      const empty = document.getElementById("tr-bench-empty");
      if (!tbody) return;

      if (runs.length === 0) {
        tbody.innerHTML = "";
        if (empty) empty.classList.remove("hidden");
        return;
      }
      if (empty) empty.classList.add("hidden");

      tbody.innerHTML = runs
        .map(
          (r) => `
        <tr class="hover:bg-gray-800/50">
          <td class="px-3 py-2">#${r.id}</td>
          <td class="px-3 py-2">${r.model}</td>
          <td class="px-3 py-2">${r.total_files}</td>
          <td class="px-3 py-2 font-semibold">${(r.aggregate_wer * 100).toFixed(1)}%</td>
          <td class="px-3 py-2">${(r.aggregate_cer * 100).toFixed(1)}%</td>
          <td class="px-3 py-2 text-xs text-gray-500">${r.created_at?.slice(0, 16) || ""}</td>
        </tr>`
        )
        .join("");
    } catch (e) {
      console.warn("benchmarks:", e);
    }
  }

  // --- event wiring ---
  function init() {
    document.getElementById("lb-search")?.addEventListener("click", () => {
      loadLabelingChunks();
    });

    document.getElementById("lb-verify-btn")?.addEventListener("click", () => updateChunkStatus("verified"));
    document.getElementById("lb-accept-btn")?.addEventListener("click", () => updateChunkStatus("accepted"));
    document.getElementById("lb-reject-btn")?.addEventListener("click", () => updateChunkStatus("rejected"));
    document.getElementById("lb-denoise-toggle")?.addEventListener("change", toggleDenoise);

    document.getElementById("lb-accept-all")?.addEventListener("click", async () => {
      try {
        const r = await api("/api/labeling/batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "accept_by_cer", max_cer: 0.05 }),
        });
        alert(`Accepted ${r.updated} chunks`);
        loadLabelingSummary();
        loadLabelingChunks();
      } catch (e) {
        console.warn(e);
      }
    });

    document.getElementById("lb-reject-high")?.addEventListener("click", async () => {
      try {
        const r = await api("/api/labeling/batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "reject_by_cer", min_cer: 0.10 }),
        });
        alert(`Rejected ${r.updated} chunks`);
        loadLabelingSummary();
        loadLabelingChunks();
      } catch (e) {
        console.warn(e);
      }
    });

    document.getElementById("lb-normalize")?.addEventListener("click", async () => {
      try {
        const r = await api("/api/labeling/normalize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        alert(`Normalized ${r.normalized} chunks`);
      } catch (e) {
        console.warn(e);
      }
    });

    document.getElementById("lb-export")?.addEventListener("click", async () => {
      try {
        const r = await api("/api/training/manifest", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (r.error) {
          alert(r.error);
        } else {
          alert(
            `Exported: ${r.train} train, ${r.val} val\n${r.total_duration_hours}h total`
          );
        }
      } catch (e) {
        console.warn(e);
      }
    });

    // Keyboard shortcuts for rapid review
    document.addEventListener("keydown", (e) => {
      const panel = document.getElementById("panel-labeling");
      if (!panel || panel.classList.contains("hidden")) return;
      const review = document.getElementById("lb-review");
      if (!review || review.classList.contains("hidden")) return;
      if (document.activeElement?.tagName === "TEXTAREA") return;

      if (e.key === "a") updateChunkStatus("accepted");
      else if (e.key === "r") updateChunkStatus("rejected");
      else if (e.key === "v") updateChunkStatus("verified");
      else if (e.key === " ") {
        e.preventDefault();
        const audio = document.getElementById("lb-audio");
        if (audio) audio.paused ? audio.play() : audio.pause();
      }
    });

    // "Training tab" link inside the empty-state guide
    document.querySelectorAll(".lb-go-training").forEach((btn) => {
      btn.addEventListener("click", () => {
        const trainBtn = document.querySelector('[data-tab="training"]');
        if (trainBtn) trainBtn.click();
      });
    });

    // Step 1: Start Chunking
    document.getElementById("lb-start-chunk")?.addEventListener("click", async () => {
      const btn = document.getElementById("lb-start-chunk");
      const prog = document.getElementById("lb-chunk-progress");
      const bar = document.getElementById("lb-chunk-bar");
      const status = document.getElementById("lb-chunk-status");
      const done = document.getElementById("lb-chunk-done");
      const feed = document.getElementById("lb-chunk-feed")?.value || "";
      const vadBackend = document.getElementById("lb-vad-backend")?.value || "energy";
      const chunkBody = { feed_id: feed, vad_backend: vadBackend };

      const adv = document.getElementById("lb-chunk-advanced");
      if (adv && !adv.classList.contains("hidden")) {
        const v = (id) => document.getElementById(id)?.value;
        if (v("lb-chunk-min-dur")) chunkBody.min_duration = parseFloat(v("lb-chunk-min-dur"));
        if (v("lb-chunk-max-dur")) chunkBody.max_duration = parseFloat(v("lb-chunk-max-dur"));
        if (v("lb-chunk-pad")) chunkBody.pad_seconds = parseFloat(v("lb-chunk-pad"));
        if (v("lb-chunk-energy")) chunkBody.energy_threshold = parseFloat(v("lb-chunk-energy"));
        if (v("lb-chunk-preset")) chunkBody.preprocess = v("lb-chunk-preset");
        if (v("lb-chunk-pattern")) chunkBody.pattern = v("lb-chunk-pattern");
      }

      btn.disabled = true;
      btn.textContent = "Chunking...";
      prog.classList.remove("hidden");
      done.classList.add("hidden");
      bar.style.width = "0%";

      try {
        const r = await api("/api/labeling/start-chunking", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(chunkBody),
        });

        if (!r.batch_id) {
          status.textContent = "No audio files found in recordings directory";
          btn.disabled = false;
          btn.textContent = "Start Chunking";
          return;
        }

        pollJob(r.batch_id, bar, status, (j) => {
          const chunks = j.chunks_created || 0;
          let msg = `Done: ${chunks} chunks from ${j.completed} files`;
          if (j.failed > 0) msg += ` (${j.failed} failed)`;
          done.innerHTML = msg;

          if (j.diagnostics) {
            const d = j.diagnostics;
            showPerfMetrics(d, vadBackend);

            if (chunks === 0) {
              const n = d.energy_sample_count || 1;
              const avgMean = Math.round(d.energy_mean_sum / n);
              const avgP95 = Math.round(d.energy_p95_sum / n);
              const lines = [`<br><strong>Why 0 chunks?</strong>`];
              if (d.conversion_failures > 0)
                lines.push(`Conversion failures: ${d.conversion_failures}`);
              if (d.no_speech_files > 0)
                lines.push(`No speech detected: ${d.no_speech_files} files`);
              if (d.segments_too_short > 0)
                lines.push(`Segments too short: ${d.segments_too_short}`);
              if (d.segments_too_long > 0)
                lines.push(`Segments too long: ${d.segments_too_long}`);
              if (d.extract_failures > 0)
                lines.push(`Extract failures: ${d.extract_failures}`);
              lines.push(`Total segments found: ${d.total_segments}`);
              lines.push(`Avg energy: mean=${avgMean}, p95=${avgP95}`);
              if (avgP95 < 500)
                lines.push(`<em>Hint: energy p95 (${avgP95}) is below threshold (500). Lower energy_threshold in config.</em>`);
              else if (d.no_speech_files === 0 && d.segments_too_long > 0)
                lines.push(`<em>Hint: all segments too long — constant noise above threshold. Raise energy_threshold.</em>`);
              done.innerHTML += lines.join("<br>");
            }
          }

          done.classList.remove("hidden");
          prog.classList.add("hidden");
          btn.textContent = "Re-run Chunking";
          btn.disabled = false;
          enableStep2();
          loadDatasetStats();
        });
      } catch (e) {
        status.textContent = "Error: " + e.message;
        btn.disabled = false;
        btn.textContent = "Start Chunking";
      }
    });

    // Step 2: Start Labeling
    document.getElementById("lb-start-label")?.addEventListener("click", async () => {
      const btn = document.getElementById("lb-start-label");
      const prog = document.getElementById("lb-label-progress");
      const bar = document.getElementById("lb-label-bar");
      const status = document.getElementById("lb-label-status");
      const done = document.getElementById("lb-label-done");
      const maxCer = parseFloat(document.getElementById("lb-label-cer")?.value || "0.05");
      const autoFilter = document.getElementById("lb-auto-filter")?.checked ?? true;
      const labelFeed = document.getElementById("lb-label-feed")?.value || "";
      const labelMax = document.getElementById("lb-label-max")?.value || "";
      const skipLabeled = document.getElementById("lb-skip-labeled")?.checked ?? true;

      btn.disabled = true;
      btn.textContent = "Labeling...";
      prog.classList.remove("hidden");
      done.classList.add("hidden");
      bar.style.width = "0%";

      const labelBody = { max_cer: maxCer, auto_filter: autoFilter, skip_labeled: skipLabeled };
      if (labelFeed) labelBody.feed_id = labelFeed;
      if (labelMax) labelBody.max_chunks = parseInt(labelMax, 10);

      try {
        const r = await api("/api/labeling/start-labeling", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(labelBody),
        });

        if (!r.batch_id) {
          status.textContent = "No chunks found to label";
          btn.disabled = false;
          btn.textContent = "Start Labeling";
          return;
        }

        pollJob(r.batch_id, bar, status, (j) => {
          const parts = [`${j.labeled || j.completed} labeled`];
          if (j.accepted) parts.push(`${j.accepted} accepted`);
          if (j.rejected) parts.push(`${j.rejected} rejected`);
          if (j.failed > 0) parts.push(`${j.failed} failed`);
          done.textContent = "Done: " + parts.join(", ");
          done.classList.remove("hidden");
          prog.classList.add("hidden");
          btn.textContent = "Re-run Labeling";
          btn.disabled = false;
          enableStepTrim();
          enableStep3();
          loadLabelingSummary();
        });
      } catch (e) {
        status.textContent = "Error: " + e.message;
        btn.disabled = false;
        btn.textContent = "Start Labeling";
      }
    });

    // Step 3: Start Trimming
    document.getElementById("lb-start-trim")?.addEventListener("click", async () => {
      const btn = document.getElementById("lb-start-trim");
      const prog = document.getElementById("lb-trim-progress");
      const bar = document.getElementById("lb-trim-bar");
      const status = document.getElementById("lb-trim-status");
      const done = document.getElementById("lb-trim-done");
      const trimFeed = document.getElementById("lb-trim-feed")?.value || "";
      const onsetPad = parseFloat(document.getElementById("lb-trim-onset")?.value || "0.1");
      const offsetPad = parseFloat(document.getElementById("lb-trim-offset")?.value || "0.1");
      const trimMax = document.getElementById("lb-trim-max")?.value || "";

      btn.disabled = true;
      btn.textContent = "Trimming...";
      prog.classList.remove("hidden");
      done.classList.add("hidden");
      bar.style.width = "0%";

      const trimBody = { onset_pad: onsetPad, offset_pad: offsetPad };
      if (trimFeed) trimBody.feed_id = trimFeed;
      if (trimMax) trimBody.max_chunks = parseInt(trimMax, 10);

      try {
        const r = await api("/api/labeling/start-trimming", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(trimBody),
        });

        if (!r.batch_id) {
          status.textContent = "No untrimmed chunks found";
          btn.disabled = false;
          btn.textContent = "Start Trimming";
          return;
        }

        pollJob(r.batch_id, bar, status, (j) => {
          const parts = [];
          if (j.trimmed) parts.push(`${j.trimmed} trimmed`);
          if (j.skipped) parts.push(`${j.skipped} skipped`);
          if (j.failed > 0) parts.push(`${j.failed} failed`);
          const saved = (j.total_saved_sec || 0).toFixed(1);
          if (parseFloat(saved) > 0) parts.push(`${saved}s audio removed`);
          done.textContent = "Done: " + parts.join(", ");
          done.classList.remove("hidden");
          prog.classList.add("hidden");
          btn.textContent = "Re-run Trimming";
          btn.disabled = false;
          enableStep3();
        });
      } catch (e) {
        status.textContent = "Error: " + e.message;
        btn.disabled = false;
        btn.textContent = "Start Trimming";
      }
    });

    // Step 4: Start Reviewing (switch to active UI)
    document.getElementById("lb-start-review")?.addEventListener("click", () => {
      showActiveUI();
      loadLabelingSummary();
      loadLabelingChunks();
    });

    // Back to pipeline step cards from review UI
    document.getElementById("lb-back-pipeline")?.addEventListener("click", () => {
      showEmptyGuide();
      loadLabelingSummary();
    });

    // Advanced chunking options toggle
    document.getElementById("lb-chunk-adv-toggle")?.addEventListener("click", () => {
      const adv = document.getElementById("lb-chunk-advanced");
      const btn = document.getElementById("lb-chunk-adv-toggle");
      if (adv) {
        const hidden = adv.classList.toggle("hidden");
        if (btn) btn.textContent = hidden ? "Show Advanced Options" : "Hide Advanced Options";
      }
    });

    // Show/hide energy threshold based on VAD backend
    document.getElementById("lb-vad-backend")?.addEventListener("change", (e) => {
      const wrap = document.getElementById("lb-energy-thresh-wrap");
      if (wrap) wrap.style.display = e.target.value === "energy" ? "" : "none";
    });

    // Chunk explorer toggle
    document.getElementById("lb-explorer-toggle")?.addEventListener("click", toggleChunkExplorer);
    document.getElementById("lb-browse-refresh")?.addEventListener("click", () => loadChunkBrowse(false));
    document.getElementById("lb-browse-more")?.addEventListener("click", () => loadChunkBrowse(true));

    // Populate feed selector, pipeline presets, and check which steps are ready
    populateFeedSelector();
    loadPipelinePresets();
    checkStepStates();
    loadDatasetStats();

    // Tab activation hooks
    const observer = new MutationObserver(() => {
      const labelPanel = document.getElementById("panel-labeling");
      if (labelPanel && !labelPanel.classList.contains("hidden")) {
        loadLabelingSummary();
        loadDatasetStats();
        const activeUI = document.getElementById("lb-active-ui");
        if (activeUI && !activeUI.classList.contains("hidden")) {
          loadLabelingChunks();
        }
      }
      const trainPanel = document.getElementById("panel-training");
      if (trainPanel && !trainPanel.classList.contains("hidden")) {
        loadBenchmarks();
      }
    });

    const panels = document.querySelectorAll(".tab-panel");
    panels.forEach((p) => observer.observe(p, { attributes: true, attributeFilter: ["class"] }));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
