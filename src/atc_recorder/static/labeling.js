/* Labeling and Training tab logic */

(function () {
  "use strict";

  let currentReviewChunkId = null;

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

  function enableStep3() {
    const btn = document.getElementById("lb-start-review");
    if (btn) btn.disabled = false;
  }

  function populateFeedSelector() {
    const sel = document.getElementById("lb-chunk-feed");
    if (!sel) return;
    fetch("/api/feeds")
      .then((r) => r.json())
      .then((feeds) => {
        sel.innerHTML = '<option value="">All feeds</option>';
        (feeds || []).forEach((f) => {
          const name = typeof f === "string" ? f : f.feed_id || f.name || "";
          if (name) sel.innerHTML += `<option value="${name}">${name}</option>`;
        });
      })
      .catch(() => {});
  }

  async function checkStepStates() {
    try {
      const s = await api("/api/labeling/summary");
      if (s.total > 0) {
        enableStep2();
        enableStep3();
        return;
      }
    } catch (_) {}

    try {
      const chunks = await fetch("/api/labeling/chunks?limit=1");
      const data = await chunks.json();
      if (data && data.length > 0) {
        enableStep2();
        enableStep3();
      }
    } catch (_) {}
  }

  // --- Labeling tab ---
  async function loadLabelingSummary() {
    try {
      const s = await api("/api/labeling/summary");

      if (s.total === 0) {
        showEmptyGuide();
        return;
      }
      showActiveUI();

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

      document.getElementById("lb-review").classList.remove("hidden");
      document.getElementById("lb-audio").src = `/api/labeling/audio/${chunkId}`;
      document.getElementById("lb-whisper-text").textContent = c.whisper_text;
      document.getElementById("lb-parakeet-text").textContent = c.parakeet_text;
      document.getElementById("lb-verified-text").value =
        c.verified_text || c.consensus_text || c.whisper_text;
      document.getElementById("lb-review-id").textContent =
        `${chunkId} | CER: ${(c.cer * 100).toFixed(1)}% | ${c.status}`;
    } catch (e) {
      console.warn("open review:", e);
    }
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

      btn.disabled = true;
      btn.textContent = "Chunking...";
      prog.classList.remove("hidden");
      done.classList.add("hidden");
      bar.style.width = "0%";

      try {
        const r = await api("/api/labeling/start-chunking", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ feed_id: feed }),
        });

        if (!r.batch_id) {
          status.textContent = "No audio files found in recordings directory";
          btn.disabled = false;
          btn.textContent = "Start Chunking";
          return;
        }

        pollJob(r.batch_id, bar, status, (j) => {
          const chunks = j.chunks_created || 0;
          done.textContent = `Done: ${chunks} chunks from ${j.completed} files` +
            (j.failed > 0 ? ` (${j.failed} failed)` : "");
          done.classList.remove("hidden");
          prog.classList.add("hidden");
          btn.textContent = "Re-run Chunking";
          btn.disabled = false;
          enableStep2();
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

      btn.disabled = true;
      btn.textContent = "Labeling...";
      prog.classList.remove("hidden");
      done.classList.add("hidden");
      bar.style.width = "0%";

      try {
        const r = await api("/api/labeling/start-labeling", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ max_cer: maxCer, auto_filter: autoFilter }),
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
          enableStep3();
          loadLabelingSummary();
        });
      } catch (e) {
        status.textContent = "Error: " + e.message;
        btn.disabled = false;
        btn.textContent = "Start Labeling";
      }
    });

    // Step 3: Start Reviewing (switch to active UI)
    document.getElementById("lb-start-review")?.addEventListener("click", () => {
      showActiveUI();
      loadLabelingSummary();
      loadLabelingChunks();
    });

    // Populate feed selector and check which steps are ready
    populateFeedSelector();
    checkStepStates();

    // Tab activation hooks
    const observer = new MutationObserver(() => {
      const labelPanel = document.getElementById("panel-labeling");
      if (labelPanel && !labelPanel.classList.contains("hidden")) {
        loadLabelingSummary();
        loadLabelingChunks();
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
