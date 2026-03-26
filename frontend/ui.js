(function () {
"use strict";
var API = "";
var qQueue = [], pendingQ = null, qAnswered = 0, qTotal = 0;
var currentMode = "feed", currentQFilter = "all";
var webSearchEnabled = false;

function fmt(n) { return (n || 0).toLocaleString(); }
function animateIn(el) {
  el.classList.add("fade-enter");
  requestAnimationFrame(function () { requestAnimationFrame(function () { el.classList.add("visible"); }); });
  return el;
}

document.addEventListener("DOMContentLoaded", function () {
  Brain.initGraph();
  loadBrainSelector();
  fetchBrainState();
  initCostWidget();
});

function fetchBrainState() {
  fetch(API + "/api/brain").then(function (r) { return r.json(); })
    .then(function (data) {
      var nodes = data.nodes || [], links = data.links || [];
      window._lastBrainState = { nodes: nodes, links: links };
      if (nodes.length > 0) { hideEmpty(); Brain.updateGraph(nodes, links); }
      refreshStats();
      var pq = data.pending_questions || [];
      pq.forEach(function (q) { qQueue.push(q); });
      qTotal = qQueue.length;
      updateActBadge();
      return fetch(API + "/api/brain/history");
    })
    .then(function (r) { return r.json(); })
    .then(function (data) { replayHistory(data.history || []); showNextQuestion(); })
    .catch(function (e) { console.error("[ui] init:", e); });
}

// ── MODE SWITCHING ──────────────────────────────────────────
window.switchMode = function (mode) {
  currentMode = mode;
  document.querySelectorAll(".mode-tab").forEach(function (t) { t.classList.toggle("active", t.dataset.mode === mode); });
  document.querySelectorAll(".mode-panel").forEach(function (p) { p.classList.toggle("active", p.id === "mode-" + mode); });
  if (mode === "observatory") loadObservatory();
  if (mode === "act") loadQuestionsDashboard();
  if (mode === "think") {
    var tlog = document.getElementById("think-log");
    if (tlog && tlog.children.length === 0) {
      tlog.innerHTML = '<div style="font-size:11px;color:var(--dim);padding:16px 12px;text-align:center;line-height:1.7">Run Brain Cleanup or click \u25B6 Evolve<br>to see the activity log here.</div>';
    }
    loadSummaryStatus();
  }
  if (mode === "chat") {
    var ch = document.getElementById("chat-history");
    if (ch && ch.children.length === 0) {
      fetch(API + "/api/brain/stats").then(function(r){return r.json();}).then(function(d){showSuggestedQueries(d);}).catch(function(){});
    }
  }
};

// ── BRAIN SELECTOR ──────────────────────────────────────────
function loadBrainSelector() {
  fetch(API + "/api/brains").then(function(r) { return r.json(); })
    .then(function(data) {
      var sel = document.getElementById("brain-select");
      if (!sel) return;
      sel.innerHTML = "";
      (data.brains || []).forEach(function(b) {
        var opt = document.createElement("option");
        opt.value = b.id;
        opt.textContent = b.label + " (" + (b.node_count || 0) + ")";
        if (b.is_active) opt.selected = true;
        sel.appendChild(opt);
      });
    }).catch(function(e) { console.error("[ui] brain selector:", e); });
}

window.switchBrain = function(brainId) {
  fetch(API + "/api/brains/" + brainId + "/activate", { method: "POST" })
    .then(function(r) { return r.json(); })
    .then(function() { location.reload(); })
    .catch(function(e) { console.error("[ui] switch brain:", e); });
};

window.createNewBrain = function() {
  var label = prompt("Brain name (e.g. 'My Project'):");
  if (!label) return;
  var id = label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").substring(0, 40);
  if (!id || id.length < 3) { alert("Name too short (min 3 chars)"); return; }
  fetch(API + "/api/brains", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ id: id, label: label })
  })
  .then(function(r) { if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail); }); return r.json(); })
  .then(function() { switchBrain(id); })
  .catch(function(e) { alert("Error: " + e.message); });
};

// ── OBSERVATORY ─────────────────────────────────────────────
function _formatRelativeTime(isoStr) {
  try {
    var d = new Date(isoStr), now = new Date();
    var diffMs = now - d, diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return "just now";
    if (diffMins < 60) return diffMins + "m ago";
    var diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return diffHours + "h ago";
    var diffDays = Math.floor(diffHours / 24);
    if (diffDays < 30) return diffDays + "d ago";
    return d.toLocaleDateString();
  } catch (e) { return "unknown"; }
}

function loadObservatory() {
  var grid = document.getElementById("observatory-grid");
  if (!grid) return;
  grid.innerHTML = '<div style="padding:20px;color:var(--dim);font-size:11px">Loading brains...</div>';
  fetch(API + "/api/brains").then(function (r) { return r.json(); })
    .then(function (data) {
      var brains = data.brains || [];
      grid.innerHTML = "";
      brains.forEach(function (b) {
        var card = document.createElement("div");
        card.className = "obs-card" + (b.is_active ? " obs-card-active" : "");
        var gradeColor = {"A+":"var(--outcome)","A":"var(--outcome)","B":"var(--blue)",
          "C":"var(--decision)","D":"var(--red)","F":"var(--red)"}[b.health_grade] || "var(--dim)";
        var lastUp = b.last_updated ? _formatRelativeTime(b.last_updated) : "never";
        var score = typeof b.health_score === "number" ? b.health_score.toFixed(1) : "0";
        card.innerHTML =
          '<div class="obs-card-color" style="background:' + esc(b.color || "#4da6ff") + '"></div>' +
          '<div class="obs-card-body">' +
            '<div class="obs-card-name">' + esc(b.label) + '</div>' +
            '<div class="obs-card-desc">' + esc(b.description || "No description") + '</div>' +
            '<div class="obs-card-stats">' +
              '<span>' + (b.node_count || 0) + ' nodes</span>' +
              '<span>' + (b.edge_count || 0) + ' edges</span>' +
            '</div>' +
            '<div class="obs-card-footer">' +
              '<span class="obs-card-grade" style="color:' + gradeColor + '">' + score + ' ' + esc(b.health_grade || "\u2014") + '</span>' +
              '<span class="obs-card-updated">' + lastUp + '</span>' +
            '</div>' +
          '</div>';
        card.onclick = function () {
          if (b.is_active) { switchMode("feed"); return; }
          switchBrain(b.id);
        };
        grid.appendChild(card);
      });
      // "New brain" card
      var newCard = document.createElement("div");
      newCard.className = "obs-card obs-card-new";
      newCard.innerHTML = '<div class="obs-card-body" style="display:flex;align-items:center;justify-content:center;flex-direction:column;gap:8px;min-height:60px">' +
        '<span style="font-size:24px;color:var(--dim)">+</span>' +
        '<span style="font-size:11px;color:var(--dim)">New brain</span></div>';
      newCard.onclick = createNewBrain;
      grid.appendChild(newCard);
    })
    .catch(function (e) {
      grid.innerHTML = '<div style="padding:20px;color:var(--red);font-size:11px">Failed to load brains</div>';
      console.error("[observatory]", e);
    });
}

// ── STATS + HEALTH SCORE ────────────────────────────────────
function refreshStats() {
  fetch(API + "/api/brain/stats").then(function (r) { return r.json(); })
    .then(function (d) {
      el("stat-n", fmt(d.total_nodes));
      el("stat-e", fmt(d.total_links));
      if (d.health_score) renderScore(d.health_score);
      showSuggestedQueries(d);
    }).catch(function () {});
}

function renderScore(hs) {
  var s = hs.score || 0, g = hs.grade || "--", delta = hs.delta || 0;
  var col = s >= 70 ? "var(--outcome)" : s >= 50 ? "var(--decision)" : "var(--red)";
  var numEl = document.getElementById("hs-number");
  var gradeEl = document.getElementById("hs-grade");
  var deltaEl = document.getElementById("hs-delta");
  if (numEl) { numEl.textContent = s; numEl.style.color = col; }
  if (gradeEl) { gradeEl.textContent = g; gradeEl.style.color = col; }
  if (deltaEl) {
    if (delta !== 0) {
      deltaEl.textContent = delta > 0 ? "\u25B2+" + delta : "\u25BC" + delta;
      deltaEl.style.color = delta > 0 ? "var(--outcome)" : "var(--red)";
    } else {
      deltaEl.textContent = "";
    }
  }
}

function el(id, val) { var e = document.getElementById(id); if (e) e.textContent = val; }

// ── FILE HANDLING ────────────────────────────────────────────
window.handleDrop = function (ev) {
  ev.preventDefault(); document.getElementById("upload-area").classList.remove("drag");
  var files = ev.dataTransfer.files;
  if (!files || !files.length) return;
  if (files.length === 1) uploadFile(files[0]); else uploadBatch(files);
};
window.handleFileInput = function (input) {
  if (!input.files || !input.files.length) return;
  if (input.files.length === 1) uploadFile(input.files[0]); else uploadBatch(input.files);
  input.value = "";
};

function uploadFile(file) {
  setProcessing(true, "Reading " + file.name + "...");
  hideEmpty();
  var fd = new FormData(); fd.append("file", file);
  fetch(API + "/api/upload", { method: "POST", body: fd })
    .then(function (r) { if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || r.statusText); }); return r.json(); })
    .then(function (data) {
      setProcessing(false);
      if (data.duplicate) { showDuplicateCard(data); refreshStats(); return; }
      addDocChip(file.name, data.nodes_added || 0);
      showExtractBlock(data.summary, data);
      (data.questions || []).forEach(function (q) { qQueue.push(q); });
      qTotal += (data.questions || []).length;
      refreshStats(); updateActBadge();
      fetch(API + "/api/brain").then(function (r) { return r.json(); }).then(function (b) { Brain.updateGraph(b.nodes || [], b.links || []); });
      if (data.doc_record && data.doc_record.summary) {
        var ch = document.getElementById("feed-qa");
        if (ch) {
          var docCard = document.createElement("div");
          docCard.style.cssText = "border:1px solid rgba(77,166,255,.15);border-radius:9px;padding:11px 13px;background:rgba(77,166,255,.04);cursor:pointer;";
          docCard.onclick = function() { openDocPanel(data.doc_record.filename || file.name); };
          var facts = (data.doc_record.key_facts || []).slice(0, 3);
          var factsHtml = facts.map(function(f) { return '<div style="font-size:10px;color:var(--dim);padding:1px 0">&middot; ' + esc(f) + '</div>'; }).join("");
          docCard.innerHTML = '<div style="font-family:monospace;font-size:8px;letter-spacing:.12em;color:var(--blue);text-transform:uppercase;margin-bottom:5px">' + ((data.doc_type || "document").toUpperCase()) + ' SUMMARY</div>' +
            '<div style="font-size:11px;color:var(--muted);line-height:1.65;margin-bottom:6px">' + esc(data.doc_record.summary) + '</div>' + factsHtml;
          ch.appendChild(docCard); ch.scrollTop = ch.scrollHeight;
        }
      }
      showNextQuestion();
    })
    .catch(function (err) { setProcessing(false); showBrainMsg("Error: " + err.message); });
}

function uploadBatch(fileList) {
  var files = Array.from(fileList);
  var totalBytes = files.reduce(function(s, f) { return s + f.size; }, 0);
  var totalMB = (totalBytes / 1024 / 1024).toFixed(1);
  if (totalBytes > 50 * 1024 * 1024) { showBrainMsg("Combined size is " + totalMB + "MB — exceeds 50MB limit."); return; }
  setProcessing(true, "Uploading " + files.length + " files (" + totalMB + "MB)...");
  hideEmpty();
  var fd = new FormData();
  files.forEach(function(f) { fd.append("files", f); });
  fetch(API + "/api/upload-batch", { method: "POST", body: fd })
    .then(function(r) { if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || r.statusText); }); return r.json(); })
    .then(function(data) {
      setProcessing(false);
      var results = data.results || [], summary = data.summary || {};
      results.forEach(function(result) {
        if (result.error) { showBrainMsg("Failed: " + (result.filename || "") + " — " + result.error); return; }
        if (result.duplicate) { showDuplicateCard(result); } else {
          addDocChip(result.filename, result.nodes_added || 0);
          showExtractBlock(result.summary || "", result);
        }
        (result.questions || []).forEach(function(q) { qQueue.push(q); });
        qTotal += (result.questions || []).length;
      });
      var parts = [];
      if (summary.new_docs > 0) parts.push(summary.new_docs + " new");
      if (summary.duplicates > 0) parts.push(summary.duplicates + " already known");
      if (summary.failed > 0) parts.push(summary.failed + " failed");
      if (summary.nodes_added > 0) parts.push("+" + summary.nodes_added + " nodes");
      if (parts.length) showBrainMsg(files.length + " files: " + parts.join(" · "));
      refreshStats(); updateActBadge();
      fetch(API + "/api/brain").then(function(r) { return r.json(); }).then(function(brain) { Brain.updateGraph(brain.nodes || [], brain.links || []); });
      showNextQuestion();
    })
    .catch(function(err) { setProcessing(false); showBrainMsg("Batch failed: " + err.message); });
}

function showDuplicateCard(data) {
  var ch = document.getElementById("feed-qa");
  if (!ch) return;
  var card = document.createElement("div");
  card.style.cssText = "border:1px solid rgba(240,160,64,.2);border-radius:9px;padding:11px 13px;background:rgba(240,160,64,.04);";
  var statusIcon = data.status === "duplicate_clean" ? "&#10003;" : "&#8635;";
  var statusColor = data.status === "duplicate_clean" ? "var(--outcome)" : "var(--amber)";
  var fixedHtml = (data.fixed || []).length ? '<div style="font-size:10px;color:var(--outcome);margin-top:5px">&#10003; ' + esc(data.fixed.join(" · ")) + '</div>' : "";
  var gapsHtml = (data.gaps_found || []).length ? '<div style="font-size:10px;color:var(--amber);margin-top:3px">! Still: ' + esc(data.gaps_found.join(", ")) + '</div>' : "";
  card.innerHTML = '<div style="font-family:monospace;font-size:8px;letter-spacing:.12em;color:' + statusColor + ';text-transform:uppercase;margin-bottom:5px">' + statusIcon + ' Already in brain</div>' +
    '<div style="font-size:11px;color:var(--muted)">' + esc(data.filename || "") + '</div>' +
    '<div style="font-size:11px;color:var(--muted);margin-top:4px;line-height:1.55">' + esc(data.message || "") + '</div>' + fixedHtml + gapsHtml;
  ch.appendChild(card); ch.scrollTop = ch.scrollHeight;
}

// ── Q&A FEED MODE ────────────────────────────────────────────
function showNextQuestion() { if (pendingQ || qQueue.length === 0) return; pendingQ = qQueue.shift(); renderQuestion(pendingQ); }

function renderQuestion(q) {
  var qa = document.getElementById("feed-qa"), card = document.createElement("div");
  card.className = "q-card"; card.id = "q-active";
  var tl = q.type === "yesno" ? "YES / NO" : q.type === "choice" ? "CHOOSE ONE" : "YOUR ANSWER";
  var html = '<div class="q-meta">' + tl + '<span class="q-num">' + (qAnswered + 1) + ' of ' + (qAnswered + 1 + qQueue.length) + '</span></div>';
  html += '<div class="q-text">' + esc(q.question) + '</div>';
  if (q.why) html += '<div class="q-why">Why: ' + esc(q.why) + '</div>';
  if (q.type === "yesno") { html += '<div class="q-options">' + qb("Yes") + qb("No") + qb("Not sure") + '</div><div class="qi-or-divider">or write your own</div>'; }
  else if (q.type === "choice" && q.options && q.options.length) { html += '<div class="q-options">'; q.options.forEach(function (o) { html += qb(o); }); html += qb("None of these") + '</div><div class="qi-or-divider">or write your own</div>'; }
  var feedPh = q.type === "yesno" ? "Or add context\u2026" : q.type === "choice" ? "Or type a different answer\u2026" : "Type your answer\u2026";
  html += '<div class="q-freetext-wrap"><textarea class="q-freetext" id="q-freetext" placeholder="' + feedPh + '"></textarea><button class="q-submit" onclick="submitFreetext()">&#8594;</button></div>';
  html += '<button class="q-skip" onclick="answerQ(\'skip\')">Skip</button>';
  card.innerHTML = html; qa.appendChild(card);
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  setTimeout(function () { var ta = document.getElementById("q-freetext"); if (ta) ta.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); window.submitFreetext(); } }); }, 50);
}

function qb(label) { return '<button class="q-opt" onclick="answerQ(\'' + esc(label).replace(/'/g, "\\'") + '\')">' + esc(label) + '</button>'; }
window.submitFreetext = function () { var ta = document.getElementById("q-freetext"); if (ta && ta.value.trim()) window.answerQ(ta.value.trim()); };

window.answerQ = function (answer) {
  if (!pendingQ) return;
  var q = pendingQ; pendingQ = null;
  var active = document.getElementById("q-active"); if (active) active.remove();
  var qa = document.getElementById("feed-qa"), card = document.createElement("div");
  card.className = "answered-card";
  card.innerHTML = '<div class="answered-check">&#10003;</div><div class="answered-body">' + esc(q.question) + '<br><span class="answered-ans">' + esc(answer) + '</span></div>';
  qa.appendChild(card); card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  qAnswered++;
  if (answer !== "skip" && answer !== "Not sure") {
    fetch(API + "/api/answer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question_id: q.id || "", question: q, answer: answer, source_doc: q.from_doc || "" }) })
      .then(function (r) { return r.json(); })
      .then(function () { return fetch(API + "/api/brain"); })
      .then(function (r) { return r.json(); })
      .then(function (brain) { Brain.updateGraph(brain.nodes || [], brain.links || []); refreshStats(); })
      .catch(function () {});
  }
  setTimeout(showNextQuestion, 300);
};

function replayHistory(historyArr) {
  if (!historyArr || !historyArr.length) return;
  hideEmpty();
  var qa = document.getElementById("feed-qa");
  var banner = document.createElement("div"); banner.className = "brain-msg";
  banner.style.position = "relative";
  banner.innerHTML = '<div class="msg-label">Session resumed</div><div class="msg-text">' + historyArr.length + ' previous answers</div><button onclick="this.parentNode.remove()" style="position:absolute;right:10px;top:50%;transform:translateY(-50%);background:transparent;border:none;color:var(--dim);cursor:pointer;font-size:12px">\u00D7</button>';
  qa.insertBefore(banner, qa.firstChild);
  var recent = historyArr.slice(-3);
  recent.forEach(function (item) {
    var card = document.createElement("div"); card.className = "answered-card";
    card.innerHTML = '<div class="answered-check">&#10003;</div><div class="answered-body">' + esc(item.question) + '<br><span class="answered-ans">' + esc(item.answer) + '</span></div>';
    qa.appendChild(card);
  });
  if (historyArr.length > 3) {
    var more = document.createElement("div");
    more.style.cssText = "font-size:10px;color:var(--dim);padding:4px 0;cursor:pointer";
    more.textContent = "+ " + (historyArr.length - 3) + " earlier answers";
    more.onclick = function () {
      more.remove();
      historyArr.slice(0, -3).forEach(function (item) {
        var card = document.createElement("div"); card.className = "answered-card";
        card.innerHTML = '<div class="answered-check">&#10003;</div><div class="answered-body">' + esc(item.question) + '<br><span class="answered-ans">' + esc(item.answer) + '</span></div>';
        qa.insertBefore(card, qa.children[1]);
      });
    };
    qa.appendChild(more);
  }
  qAnswered = historyArr.length;
}

// ── THINK MODE — SSE STREAMING ───────────────────────────────
var AGENT_ORDER = ["cartographer","skeptic","synthesizer","detective","archivist","questioner","compressor","conceptualizer"];
var AGENT_STATUS_TEXT = {
  cartographer: "Mapping your brain's topic clusters...",
  skeptic: "Looking for duplicate nodes...",
  synthesizer: "Merging confirmed duplicates...",
  detective: "Checking for contradictions...",
  archivist: "Scoring node confidence levels...",
  questioner: "Identifying what the brain needs to learn..."
};

window.runBrainHealth = function () {
  if (_evolutionRunning) return; // can't run both
  var btn = document.getElementById("health-btn");
  var statusEl = document.getElementById("health-status");
  if (!btn || btn.disabled) return;
  btn.textContent = "Running\u2026"; btn.disabled = true; btn.style.opacity = "0.5";
  if (statusEl) statusEl.textContent = "";
  // Disable evolve while cleanup runs
  var evoBtn = document.getElementById("think-evo-start"); if (evoBtn) { evoBtn.disabled = true; evoBtn.style.opacity = "0.4"; }
  // Expand cleanup section
  var cleanupSection = document.getElementById("ts-cleanup");
  if (cleanupSection) cleanupSection.classList.remove("collapsed");
  var thinkRunning = document.getElementById("think-running");
  var thinkResult = document.getElementById("think-result");
  if (thinkRunning) thinkRunning.style.display = "block";
  if (thinkResult) thinkResult.style.display = "none";
  var pb = document.getElementById("think-progress-bar"); if (pb) pb.style.width = "0%";
  var logEl = document.getElementById("think-log");
  if (logEl) logEl.innerHTML = "";
  var startTime = Date.now();
  var agentStartTimes = {};

  function _addLogEntry(agent, text, eventType) {
    var log = document.getElementById("think-log");
    if (!log) return;
    var totalEl = ((Date.now() - startTime) / 1000).toFixed(1);
    var agentEl = agentStartTimes[agent] ? ((Date.now() - agentStartTimes[agent]) / 1000).toFixed(1) : null;
    var timeStr = "+" + totalEl + "s" + (agentEl ? " (" + agentEl + "s)" : "");
    var COLORS = {cartographer:"#4da6ff",skeptic:"#f0a040",synthesizer:"#4ecb8d",detective:"#ff6b6b",archivist:"#a87fff",questioner:"#ff8fab",compressor:"#a87fff"};
    var col = COLORS[agent] || "var(--dim)";
    var entry = document.createElement("div");
    entry.className = "tl-entry type-" + (eventType || "info");
    entry.innerHTML = '<span class="tl-time">' + timeStr + '</span>' +
      '<span class="tl-agent" style="color:' + col + '">' + (agent || "").slice(0, 8).toUpperCase() + '</span>' +
      '<span class="tl-text">' + esc(text) + '</span>';
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
  }

  function _addAgentHeader(agent, desc) {
    var log = document.getElementById("think-log");
    if (!log) return;
    // Mark previous headers as done
    log.querySelectorAll(".tl-agent-spinner").forEach(function (s) {
      s.outerHTML = '<span style="color:var(--outcome);font-size:10px">\u2713</span>';
    });
    var COLORS = {cartographer:"#4da6ff",skeptic:"#f0a040",synthesizer:"#4ecb8d",detective:"#ff6b6b",archivist:"#a87fff",questioner:"#ff8fab"};
    var col = COLORS[agent] || "var(--dim)";
    var h = document.createElement("div");
    h.className = "tl-agent-header"; h.id = "tl-header-" + agent; h.style.color = col;
    h.innerHTML = '<div class="tl-agent-spinner" style="border-top-color:' + col + '"></div> ' +
      esc(agent) + '<span style="color:var(--dim);font-weight:400;font-size:9px;margin-left:4px">' + esc(desc || "") + '</span>';
    log.appendChild(h);
    log.scrollTop = log.scrollHeight;
  }

  var agentMemo = {};
  var es = new EventSource(API + "/api/brain/health/stream");
  es.onmessage = function (ev) {
    var data = JSON.parse(ev.data);

    if (data.type === "agent_start") {
      agentStartTimes[data.agent] = Date.now();
      _addAgentHeader(data.agent, data.description || "");
      var step = AGENT_ORDER.indexOf(data.agent) + 1;
      document.getElementById("think-progress-bar").style.width = Math.round((step - 1) / AGENT_ORDER.length * 100) + "%";
      var lbl = document.getElementById("think-agent-label");
      if (lbl) lbl.textContent = step + " of " + AGENT_ORDER.length + " \u2014 " + data.agent;

    } else if (data.type === "event") {
      _addLogEntry(data.agent, data.text, data.event_type);

    } else if (data.type === "agent_progress") {
      agentMemo[data.agent] = data.message || "";

    } else if (data.type === "agent_done") {
      var header = document.getElementById("tl-header-" + data.agent);
      if (header) {
        var sp = header.querySelector(".tl-agent-spinner");
        if (sp) sp.outerHTML = '<span style="color:var(--outcome);font-size:10px">\u2713</span>';
      }
      var step2 = AGENT_ORDER.indexOf(data.agent) + 1;
      document.getElementById("think-progress-bar").style.width = Math.round(step2 / AGENT_ORDER.length * 100) + "%";

    } else if (data.type === "complete") {
      es.close();
      document.getElementById("think-progress-bar").style.width = "100%";
      // Mark all spinners as done
      document.querySelectorAll(".tl-agent-spinner").forEach(function (s) {
        s.outerHTML = '<span style="color:var(--outcome);font-size:10px">\u2713</span>';
      });

      setTimeout(function () {
        var tr2 = document.getElementById("think-running"); if (tr2) tr2.style.display = "none";

        var r = data.result || {};
        var el2 = document.getElementById("think-result");
        el2.style.display = "block";
        document.getElementById("tr-fixed").style.display = "none";
        document.getElementById("tr-attention").style.display = "none";
        document.getElementById("tr-act-cta").style.display = "none";
        document.getElementById("tr-agent-details").style.display = "none";
        document.getElementById("tr-time").textContent = (r.elapsed_seconds || 0) + "s";

        var fixedItems = [];
        if (r.duplicates_merged > 0) fixedItems.push({label:"Duplicates merged", val:r.duplicates_merged});
        if (r.orphan_links_removed > 0) fixedItems.push({label:"Broken links removed", val:r.orphan_links_removed});
        if (r.nodes_enriched > 0) fixedItems.push({label:"Nodes enriched", val:r.nodes_enriched});
        if (r.synthesized > 0) fixedItems.push({label:"Descriptions rewritten", val:r.synthesized});
        if (fixedItems.length) {
          document.getElementById("tr-fixed").style.display = "block";
          document.getElementById("tr-fixed-items").innerHTML = fixedItems.map(function(it) {
            return '<div class="tr-item">' + esc(it.label) + ' <span class="tr-item-value">' + it.val + '</span></div>';
          }).join("");
        }

        var attItems = [];
        var cons = r.contradictions || [];
        if (cons.length) attItems.push({label:"Contradictions found", val:cons.length, detail:cons.slice(0,2).map(function(c){return c.node;}).join(", ")});
        var gaps = r.gaps || {};
        if ((gaps.unowned_features||[]).length) attItems.push({label:"Features with no owner", val:(gaps.unowned_features||[]).length});
        if ((gaps.unclaimed_outcomes||[]).length) attItems.push({label:"Outcomes with no source", val:(gaps.unclaimed_outcomes||[]).length});
        if ((gaps.isolated_nodes||[]).length) attItems.push({label:"Isolated nodes", val:(gaps.isolated_nodes||[]).length});
        if (attItems.length) {
          document.getElementById("tr-attention").style.display = "block";
          document.getElementById("tr-attention-items").innerHTML = attItems.map(function(it) {
            var d = it.detail ? ' <span style="color:var(--dim);font-size:10px">(' + esc(it.detail) + ')</span>' : '';
            return '<div class="tr-item">' + esc(it.label) + ' <span class="tr-item-value">' + it.val + '</span>' + d + '</div>';
          }).join("");
        }

        var newQs = r.gap_questions_added || 0;
        if (newQs > 0) { document.getElementById("tr-act-cta").style.display = "block"; document.getElementById("tr-act-count").textContent = newQs; }
        buildAgentStories(r, agentMemo);
        refreshStats(); updateActBadge();
        fetch(API + "/api/brain").then(function(rv){return rv.json();}).then(function(brain){Brain.updateGraph(brain.nodes||[],brain.links||[]);});
        btn.textContent = "Run Brain Cleanup"; btn.disabled = false; btn.style.opacity = "";
        var evoBtn2 = document.getElementById("think-evo-start"); if (evoBtn2) { evoBtn2.disabled = false; evoBtn2.style.opacity = ""; }
        var parts = [];
        if (r.duplicates_merged > 0) parts.push(r.duplicates_merged + " merged");
        if (attItems.length) parts.push(attItems.length + " issues");
        statusEl.textContent = parts.join(" \u00B7 ") || "Brain looks clean";
      }, 400);
    }
  };
  es.onerror = function () {
    es.close();
    btn.textContent = "Run Brain Cleanup"; btn.disabled = false; btn.style.opacity = "";
    var evoBtn3 = document.getElementById("think-evo-start"); if (evoBtn3) { evoBtn3.disabled = false; evoBtn3.style.opacity = ""; }
    if (statusEl) statusEl.textContent = "Stream error - try again";
    var tr = document.getElementById("think-running"); if (tr) tr.style.display = "none";
  };
};

function buildAgentStories(result, memo) {
  var container = document.getElementById("tr-agent-details");
  if (!container) return;
  var cons = result.contradictions || [];
  var gaps = result.gaps || {};
  var stories = [
    {agent:"Cartographer", story: memo.cartographer ? memo.cartographer.replace(/\u00B7/g,"<br>\u00B7") : "Mapped brain topology."},
    {agent:"Skeptic", story: (result.duplicates_merged||0) === 0 ? "No clear duplicates found." : "Found " + result.duplicates_merged + " duplicate pair(s) with high confidence."},
    {agent:"Synthesizer", story: function(){
      var m=result.duplicates_merged||0, s=result.synthesized||0;
      if(!m&&!s) return "Nothing to merge.";
      var p=[]; if(m) p.push("Merged "+m+" duplicate(s)"); if(s) p.push("Rewrote "+s+" description(s)");
      return p.join(". ")+".";
    }()},
    {agent:"Detective", story: cons.length===0 ? "No contradictions found." : "Found "+cons.length+" contradiction(s). Most notable: "+esc((cons[0]||{}).node||"")+" — "+esc((cons[0]||{}).issue||"")},
    {agent:"Archivist", story: memo.archivist || ("Enriched "+(result.nodes_enriched||0)+" nodes.")},
    {agent:"Questioner", story: (result.gap_questions_added||0)===0 ? "No critical gaps found." : "Added "+result.gap_questions_added+" targeted questions."}
  ];
  container.innerHTML = stories.map(function(s){
    return '<div><div class="tr-agent-name">'+esc(s.agent)+'</div><div class="tr-agent-story">'+s.story+'</div></div>';
  }).join("");
}

window.toggleAgentDetails = function () {
  var det = document.getElementById("tr-agent-details");
  var tog = document.getElementById("tr-details-toggle");
  if (!det) return;
  var open = det.style.display !== "none";
  det.style.display = open ? "none" : "block";
  if (tog) tog.textContent = open ? "See what each agent found" : "Hide agent details";
};

// ── ACT MODE — QUESTION TRIAGE ───────────────────────────────
function _qInterest(q) {
  var s = 0;
  var cw = {contradiction:10,concept_proposal:9,web_conflict:8,no_source:6,no_owner:5,gap:4,evolution:3,compress:2,plan:3};
  s += cw[q.category] || 3;
  if (q.priority === "high") s += 5; else if (q.priority === "medium") s += 2;
  var m = (q.id || "").match(/_(\d{10,13})(?:_|$)/);
  if (m) { var ts = parseInt(m[1]); if (ts < 1e12) ts *= 1000; var h = (Date.now() - ts) / 3600000; if (h < 24) s += 3; if (h > 168) s -= 2; }
  var t = q.question || ""; if (t.includes("%") || t.includes("$")) s += 2; if (t.length > 200) s -= 1;
  return s;
}
var _catColors = {contradiction:"#ff6b6b",concept_proposal:"#e0e4f0",web_conflict:"#f0a040",no_source:"#4da6ff",no_owner:"#a87fff",evolution:"#4ecb8d",compress:"#7ec8e3",gap:"#4da6ff"};

function loadQuestionsDashboard() {
  fetch(API + "/api/brain/questions").then(function (r) { return r.json(); })
    .then(function (data) {
      var qs = data.questions || [];
      var inbox = document.getElementById("questions-inbox");
      var counts = document.getElementById("act-counts");
      if (counts) {
        var catCounts = {};
        qs.forEach(function (q) { var cat = q.category || "other"; catCounts[cat] = (catCounts[cat] || 0) + 1; });
        var parts = [];
        if (catCounts.evolution) parts.push(catCounts.evolution + " evolved");
        var webTotal = (catCounts.web_enrich || 0) + (catCounts.web_conflict || 0) + (catCounts.web_new || 0);
        if (webTotal) parts.push(webTotal + " web");
        if (catCounts.contradiction) parts.push(catCounts.contradiction + " contradictions");
        if (catCounts.concept_proposal) parts.push(catCounts.concept_proposal + " concepts");
        if (catCounts.no_owner) parts.push(catCounts.no_owner + " ownership");
        var main = fmt(data.total) + " question" + (data.total !== 1 ? "s" : "") + " (" + fmt(data.high) + " high)";
        counts.innerHTML = parts.length ? main + '<br><span style="font-size:9px;color:var(--dim)">' + parts.join(" \u00B7 ") + '</span>' : main;
      }
      inbox.innerHTML = "";
      if (!qs.length) { inbox.innerHTML = '<div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">No pending questions. Upload a document or run Brain Cleanup.</div>'; return; }
      window._actQuestions = qs;
      // Score and tier
      var scored = qs.map(function (q, i) { return {q: q, score: _qInterest(q), idx: i}; }).sort(function (a, b) { return b.score - a.score; });
      var hot = scored.filter(function (x) { return x.score >= 12; });
      var normal = scored.filter(function (x) { return x.score >= 6 && x.score < 12; });
      var low = scored.filter(function (x) { return x.score < 6; });
      var html = "";
      if (hot.length) {
        html += '<div class="qi-section-header qi-section-hot">\u25C6 Needs your attention (' + hot.length + ')</div>';
        hot.forEach(function (x) { html += buildQuestionCard(x.q, x.idx, "hot"); });
      }
      if (normal.length) {
        html += '<div class="qi-section-header qi-section-normal">Worth reviewing (' + normal.length + ')</div>';
        normal.forEach(function (x) { html += buildQuestionCard(x.q, x.idx, "normal"); });
      }
      if (low.length) {
        html += '<div class="qi-section-header qi-section-low" onclick="window._toggleLowSection()" style="cursor:pointer"><span id="low-toggle-icon">\u25B8</span> Quick reviews (' + low.length + ')</div>';
        html += '<div id="qi-low-section" style="display:none">';
        low.forEach(function (x) {
          var q = x.q, idx = x.idx;
          var micro = (q.question || "").length > 65 ? (q.question || "").slice(0, 63) + "\u2026" : (q.question || "");
          var col = _catColors[q.category] || "#888";
          html += '<div class="qi-row-micro" data-id="' + esc(q.id) + '" data-category="' + esc(q.category) + '" data-idx="' + idx + '">' +
            '<span class="qi-dot" style="background:' + col + '"></span>' +
            '<span class="qi-micro-text">' + esc(micro) + '</span>' +
            '<div class="qi-micro-actions"><button onclick="event.stopPropagation();inboxAnswerSmart(' + idx + ',\'Yes\')">Y</button><button onclick="event.stopPropagation();inboxAnswerSmart(' + idx + ',\'No\')">N</button><button onclick="event.stopPropagation();inboxSkip(' + idx + ')">\u2014</button></div></div>';
        });
        html += '</div>';
      }
      html += '<div class="qi-keyboard-hint">Y yes \u00B7 N no \u00B7 S skip</div>';
      inbox.innerHTML = html;
    }).catch(function () {});
}
window._toggleLowSection = function () {
  var s = document.getElementById("qi-low-section");
  var i = document.getElementById("low-toggle-icon");
  if (!s) return;
  var h = s.style.display === "none";
  s.style.display = h ? "block" : "none";
  if (i) i.textContent = h ? "\u25BE" : "\u25B8";
};

function _qAge(q) {
  var m = (q.id || "").match(/_(\d{10,13})(?:_|$)/);
  if (!m) return "";
  var ts = parseInt(m[1]); if (ts < 1e12) ts *= 1000;
  var d = Math.floor((Date.now() - ts) / 86400000);
  var h = Math.floor((Date.now() - ts) / 3600000);
  return d > 1 ? d + "d ago" : h > 1 ? h + "h ago" : "today";
}

var _clarifyCategories = ["contradiction","gap","no_source","no_owner","no_surface","web_conflict","plan","web_enrich","web_new","isolated"];

function buildQuestionCard(q, idx, mode) {
  var cl = q.category_label || (q.category || "").replace(/_/g, " ").toUpperCase();
  var text = q.question || "";
  var startExpanded = (mode === "hot");
  var showClarify = _clarifyCategories.includes(q.category);

  var html = '<div class="qi-card qi-card-' + esc(mode) + (startExpanded ? "" : " qi-card-collapsed") + '" data-id="' + esc(q.id) + '" data-category="' + esc(q.category) + '" data-priority="' + esc(q.priority || '') + '" data-idx="' + idx + '">';

  // Header row — clickable to toggle
  html += '<div class="qi-card-header" onclick="window.expandCard(\'' + esc(q.id) + '\')">';
  html += '<span class="qi-category qi-cat-' + esc(q.category) + '">' + esc(cl) + '</span>';
  if (mode === "hot") html += '<span class="qi-score-dot qi-score-hot"></span><span style="font-size:9px;color:var(--dim)">' + _qAge(q) + '</span>';
  html += '<span class="qi-question-text' + (startExpanded ? "" : " qi-truncated") + '" id="qt-' + esc(q.id) + '">' + esc(text) + '</span>';
  if (mode !== "hot") html += '<span class="qi-expand-icon" id="qi-icon-' + esc(q.id) + '">' + (startExpanded ? "\u2191" : "\u2193") + '</span>';
  html += '</div>';

  // Why line (hot only)
  if (mode === "hot" && q.why) html += '<div class="qi-why">' + esc(q.why) + '</div>';

  // Answer area — hidden when collapsed
  html += '<div class="qi-answer-area' + (startExpanded ? "" : " qi-hidden") + '" id="qa-' + esc(q.id) + '">';
  html += buildAnswerUI(q, idx);
  html += '<div class="qi-answer-footer"><button class="qi-skip-btn" onclick="event.stopPropagation();inboxSkip(' + idx + ')">Skip</button>';
  if (showClarify) html += '<button class="qi-clarify-btn" onclick="event.stopPropagation();clarifyQuestion(' + idx + ')">Clarify this question</button>';
  html += '</div></div>';

  html += '</div>';
  return html;
}

function buildAnswerUI(q, idx) {
  var html = '<div class="qi-answer-inline">';
  if (q.type === "yesno") {
    html += '<div class="qi-opts-row">' + qiOpt(idx, "Yes") + qiOpt(idx, "No") + qiOpt(idx, "Not sure") + '</div>';
    html += '<div class="qi-or-divider">or write your own</div>';
  } else if (q.type === "choice" && q.options && q.options.length) {
    html += '<div class="qi-opts-col">';
    (q.options || []).forEach(function (o) { html += qiOpt(idx, o); });
    html += qiOpt(idx, "None of these", "qi-opt qi-opt-none");
    html += '</div>';
    html += '<div class="qi-or-divider">or write your own</div>';
  }
  var ph = q.type === "yesno" ? "Or add context to your answer\u2026" : q.type === "choice" ? "Or type a different answer\u2026" : "Type your answer\u2026";
  html += '<div class="qi-freetext-row"><textarea class="qi-freetext" id="qi-text-' + idx + '" placeholder="' + ph + '" rows="2"></textarea><button class="qi-submit" onclick="inboxAnswer(' + idx + ', null)">&#8594;</button></div>';
  html += '</div>';
  return html;
}

window.expandCard = function (qid) {
  var card = document.querySelector('[data-id="' + qid + '"]');
  var area = document.getElementById("qa-" + qid);
  var qtxt = document.getElementById("qt-" + qid);
  var icon = document.getElementById("qi-icon-" + qid);
  if (!card || !area) return;
  var isCollapsed = area.classList.contains("qi-hidden");
  if (isCollapsed) {
    area.classList.remove("qi-hidden");
    card.classList.remove("qi-card-collapsed");
    if (qtxt) qtxt.classList.remove("qi-truncated");
    if (icon) icon.textContent = "\u2191";
  } else {
    area.classList.add("qi-hidden");
    card.classList.add("qi-card-collapsed");
    if (qtxt) qtxt.classList.add("qi-truncated");
    if (icon) icon.textContent = "\u2193";
  }
};

function qiOpt(idx, label, cls) {
  cls = cls || "qi-opt";
  return '<button class="' + cls + '" onclick="inboxAnswerSmart(' + idx + ', \'' + esc(label).replace(/'/g, "\\'") + '\')">' + esc(label) + '</button>';
}

window.inboxAnswerSmart = function (idx, optionLabel) {
  var ta = document.getElementById("qi-text-" + idx);
  var typed = ta ? ta.value.trim() : "";
  inboxAnswer(idx, typed.length > 3 ? typed : optionLabel);
};

window.inboxAnswer = function (idx, answer) {
  var q = (window._actQuestions || [])[idx];
  if (!q) return;
  if (!answer) {
    var ta = document.getElementById("qi-text-" + idx);
    if (ta && ta.value.trim()) answer = ta.value.trim();
    else return;
  }
  var item = document.querySelector('[data-idx="' + idx + '"]');
  if (item) { item.style.opacity = "0.3"; item.style.pointerEvents = "none"; }
  fetch(API + "/api/answer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question_id: q.id || "", question: q, answer: answer, source_doc: q.from_doc || "" }) })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (item) {
        var result = document.createElement("div");
        result.className = "qi-result-card";
        var summary = data.update_summary || "Answer recorded.";
        var affected = data.affected_nodes || [];
        var wasSkipped = data.updates && data.updates[0] && data.updates[0].action === "skipped";
        var nodesHtml = "";
        if (affected.length) {
          nodesHtml = '<div class="qi-result-nodes">';
          var TC = {Feature:"#4da6ff",Surface:"#a87fff",Outcome:"#4ecb8d",Decision:"#f0a040",Person:"#ff8fab",Company:"#7ec8e3"};
          affected.forEach(function (n) {
            var col = TC[n.type] || "#888";
            nodesHtml += '<span class="qi-result-node" style="border-color:' + col + '40;color:' + col + '" onclick="focusNode(\'' + esc(n.id) + '\')" title="Click to inspect">' + esc(n.label) + '</span>';
          });
          nodesHtml += '</div>';
        }
        result.innerHTML = '<div class="qi-result-check">' + (wasSkipped ? '&mdash;' : '&#10003;') + '</div>' +
          '<div class="qi-result-body"><div class="qi-result-summary">' + esc(summary) + '</div>' + nodesHtml + '</div>';
        item.replaceWith(result);
        setTimeout(function () {
          if (result.parentNode) { result.style.opacity = "0"; result.style.transition = "opacity 0.4s"; setTimeout(function () { if (result.parentNode) result.remove(); }, 400); }
        }, 4000);
      }
      if (affected.length && Brain.highlightNodes) {
        Brain.highlightNodes(affected.map(function (n) { return n.id; }));
        setTimeout(function () { Brain.clearSelection(); }, 3000);
      }
      updateActBadge(); refreshStats();
      fetch(API + "/api/brain").then(function (r) { return r.json(); }).then(function (brain) { Brain.updateGraph(brain.nodes || [], brain.links || []); });
    });
};

window.inboxSkip = function (idx) {
  var q = (window._actQuestions || [])[idx];
  if (!q) return;
  var item = document.querySelector('[data-idx="' + idx + '"]');
  if (item) { item.style.opacity = "0.3"; item.style.pointerEvents = "none"; }
  fetch(API + "/api/answer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question_id: q.id || "", question: q, answer: "skip", source_doc: q.from_doc || "" }) })
    .then(function () {
      if (item) item.remove();
      updateActBadge(); refreshStats();
    });
};

window.filterQuestions = function (f) {
  currentQFilter = f;
  document.querySelectorAll(".act-filter-btn").forEach(function (b) { b.classList.toggle("active", b.dataset.f === f); });
  var webCats = ["web_enrich", "web_conflict", "web_new", "web"];
  var compressCats = ["compress", "isolated"];
  document.querySelectorAll("[data-idx]").forEach(function (item) {
    if (f === "all") { item.classList.remove("hidden"); return; }
    var cat = item.dataset.category || "";
    var pri = item.dataset.priority || "";
    var show = false;
    if (f === "high") show = (pri === "high");
    else if (f === "med") show = (pri === "medium");
    else if (f === "web") show = webCats.indexOf(cat) >= 0;
    else if (f === "compress") show = compressCats.indexOf(cat) >= 0;
    else show = (cat === f);
    item.classList.toggle("hidden", !show);
  });
};

function updateActBadge() {
  fetch(API + "/api/brain/questions").then(function (r) { return r.json(); })
    .then(function (data) {
      var badge = document.getElementById("act-badge");
      var counts = document.getElementById("act-counts");
      var total = data.total || 0;
      var high = data.high || 0;
      if (badge) {
        if (total > 0) { badge.textContent = total; badge.style.display = "flex"; badge.style.background = high > 0 ? "var(--red)" : "var(--dim)"; }
        else { badge.style.display = "none"; }
      }
      if (counts) counts.textContent = total + " question" + (total !== 1 ? "s" : "") + " (" + high + " high)";
    }).catch(function () {});
}

// ── CLARIFY QUESTION ─────────────────────────────────────────
window.clarifyQuestion = function (idx) {
  var q = (window._actQuestions || [])[idx];
  if (!q) return;
  var item = document.querySelector('.q-inbox-item[data-idx="' + idx + '"]');
  if (!item) return;
  // Toggle off if already open
  var existing = item.querySelector(".qi-clarify-panel");
  if (existing) { existing.remove(); return; }
  var btn = item.querySelector(".qi-clarify-btn");
  if (btn) { btn.textContent = "Thinking..."; btn.disabled = true; }
  fetch(API + "/api/brain/clarify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q, followup: "" }) })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (btn) { btn.textContent = "Clarify this question"; btn.disabled = false; }
      _showClarifyPanel(idx, q, data, item);
    })
    .catch(function () { if (btn) { btn.textContent = "Clarify this question"; btn.disabled = false; } });
};

function _showClarifyPanel(idx, q, data, item) {
  var old = item.querySelector(".qi-clarify-panel"); if (old) old.remove();
  var panel = document.createElement("div"); panel.className = "qi-clarify-panel";
  var optsHtml = "";
  if (data.suggested_options && data.suggested_options.length) {
    optsHtml = '<div class="qi-clarify-opts">';
    data.suggested_options.forEach(function (o) { optsHtml += '<button class="qi-opt" onclick="inboxAnswer(' + idx + ',\'' + esc(o).replace(/'/g, "\\'") + '\')">' + esc(o) + '</button>'; });
    optsHtml += '</div>';
  }
  panel.innerHTML =
    '<div class="qi-clarify-node">' + (data.node_label ? '&#9678; ' + esc(data.node_label) : '') + (data.node_context_summary ? ' &mdash; <em>' + esc(data.node_context_summary) + '</em>' : '') + '</div>' +
    '<div class="qi-clarify-explanation">' + esc(data.explanation || "") + '</div>' +
    (data.rewritten_question && data.rewritten_question !== q.question ? '<div class="qi-clarify-rewritten">&#8635; ' + esc(data.rewritten_question) + '</div>' : '') +
    optsHtml +
    '<div class="qi-clarify-followup"><input type="text" class="qi-clarify-input" id="qi-clar-' + idx + '" placeholder="Ask for more clarification..."><button class="qi-clarify-ask" onclick="_askClarification(' + idx + ')">&#8594;</button></div>';
  var answerInline = item.querySelector(".qi-answer-inline");
  if (answerInline) answerInline.parentNode.insertBefore(panel, answerInline);
  else item.querySelector(".qi-body").appendChild(panel);
  var inp = document.getElementById("qi-clar-" + idx);
  if (inp) inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); _askClarification(idx); } });
}

window._askClarification = function (idx) {
  var inp = document.getElementById("qi-clar-" + idx);
  var followup = inp ? inp.value.trim() : "";
  if (!followup) return;
  var q = (window._actQuestions || [])[idx]; if (!q) return;
  inp.value = ""; inp.placeholder = "Thinking..."; inp.disabled = true;
  fetch(API + "/api/brain/clarify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q, followup: followup }) })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      inp.placeholder = "Ask for more clarification..."; inp.disabled = false;
      var item = document.querySelector('.q-inbox-item[data-idx="' + idx + '"]');
      if (item) _showClarifyPanel(idx, q, data, item);
    })
    .catch(function () { inp.placeholder = "Ask for more clarification..."; inp.disabled = false; });
};

// ── NODE DETAIL PANEL ────────────────────────────────────────
window.openNodePanel = function (d, allNodes, allLinks) {
  window._openPanelNode = d;
  var panel = document.getElementById("node-panel"); panel.classList.add("open");
  // Close cost panel if open (avoids overlap with node panel)
  if (_costPanelOpen) { toggleCostPanel(); }
  // Scroll-aware sticky header border
  panel.onscroll = function () {
    var hdr = document.getElementById("np-sticky-header");
    if (hdr) hdr.style.borderColor = panel.scrollTop > 10 ? "var(--border)" : "transparent";
  };
  var col = Brain.getTypeColor(d.type);
  var isWeb = d.confidence === "web" || d.source_doc === "web_search";

  // ── Phase 1: Instant render with static data ──
  var badge = document.getElementById("np-badge");
  badge.textContent = d.type.toUpperCase() + (isWeb ? " \u00B7 WEB" : "");
  badge.style.cssText = isWeb ? "background:rgba(78,203,141,.12);color:var(--outcome);border:1px solid rgba(78,203,141,.3)" : "background:" + col + "18;color:" + col + ";border:1px solid " + col + "40";

  // Memory type badge
  var memBadge = document.getElementById("np-memory-badge");
  if (!memBadge) {
    memBadge = document.createElement("span");
    memBadge.id = "np-memory-badge";
    badge.parentNode.insertBefore(memBadge, badge.nextSibling);
  }
  var isSemantic = d.memory_type === "semantic";
  memBadge.textContent = isSemantic ? "SEMANTIC" : "EPISODIC";
  memBadge.style.cssText = isSemantic
    ? "display:inline-block;font-size:8px;font-family:monospace;letter-spacing:.08em;padding:2px 6px;border-radius:3px;margin-left:6px;background:rgba(78,203,141,.12);color:var(--outcome);border:1px solid rgba(78,203,141,.25)"
    : "display:inline-block;font-size:8px;font-family:monospace;letter-spacing:.08em;padding:2px 6px;border-radius:3px;margin-left:6px;background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--border)";

  document.getElementById("np-name").textContent = d.label;
  // Show current company for Person nodes with career
  var displayCompany = d.company || "";
  if (d.type === "Person" && d.career && d.career.length) {
    var currentRole = d.career.find(function (c) { return c.is_current; });
    if (currentRole) displayCompany = currentRole.company + (currentRole.role ? " \u00B7 " + currentRole.role : "");
  }
  document.getElementById("np-where").textContent = displayCompany || d.where || "";

  // Description — career timeline (Person) + skeleton for AI summary
  var descEl = document.getElementById("np-desc");
  var careerHtml = "";
  if (d.type === "Person" && d.career && d.career.length > 0) {
    careerHtml = _buildCareerTimeline(d.career);
  }
  descEl.innerHTML = careerHtml + '<div id="np-summary-block"><div class="skeleton" style="height:13px;width:95%;margin-bottom:6px"></div><div class="skeleton" style="height:13px;width:80%;margin-bottom:6px"></div><div class="skeleton" style="height:13px;width:65%"></div></div><div id="np-role-block" style="margin-top:8px"><div class="skeleton" style="height:11px;width:70%"></div></div>';

  // Metrics section
  var mw = document.getElementById("np-metrics"); mw.innerHTML = "";
  if (d.type === "Person" && d.profile) {
    var p = d.profile;
    mw.innerHTML = '<div class="np-role">' + esc(p.role || "") + '</div><div class="np-summary">' + esc(p.summary || "") + '</div>';
    if (p.key_features && p.key_features.length) { mw.innerHTML += '<div class="np-section-hd">Owns / leads</div>'; p.key_features.forEach(function (f) { mw.innerHTML += '<span class="np-feature-pill">' + esc(f) + '</span>'; }); }
  } else if (d.metrics && Object.keys(d.metrics).length) {
    mw.innerHTML = '<div class="np-section-hd">Metrics</div>';
    Object.entries(d.metrics).forEach(function (kv) { mw.innerHTML += '<div class="np-metric"><span class="np-mkey">' + esc(kv[0]) + '</span><span class="np-mval">' + esc(String(kv[1])) + '</span></div>'; });
  }
  if (d.web_updates && d.web_updates.length) {
    mw.innerHTML += '<div class="np-section-hd">Web updates</div>';
    d.web_updates.forEach(function (wu) { mw.innerHTML += '<div class="np-metric"><span class="np-mkey">&#127760; ' + esc(wu.field || "") + '</span><span class="np-mval" style="font-size:10px">' + esc(wu.new || "") + '</span></div>'; });
  }
  // Depth score
  if (d.depth_score !== undefined) {
    var ds = Math.round(d.depth_score * 100);
    var dsCol = ds >= 70 ? "var(--outcome)" : ds >= 40 ? "var(--decision)" : "var(--red)";
    mw.innerHTML += '<div class="np-section-hd">Node depth</div><div style="display:flex;align-items:center;gap:8px;padding:4px 0"><div style="flex:1;height:4px;background:var(--border2);border-radius:2px"><div style="width:' + ds + '%;height:100%;background:' + dsCol + ';border-radius:2px;transition:width .4s"></div></div><span style="font-family:monospace;font-size:10px;color:' + dsCol + '">' + ds + '%</span></div>';
    if (ds < 40) mw.innerHTML += '<div style="font-size:10px;color:var(--dim);padding:2px 0 6px">Shallow \u2014 add documents or answer questions</div>';
  }

  // Connections — render immediately from static data
  var lw = document.getElementById("np-connections"); lw.innerHTML = "";
  var nodeMap = {}; (allNodes || []).forEach(function (n) { nodeMap[n.id] = n; });
  var conns = [];
  (allLinks || []).forEach(function (l) {
    var sid = typeof l.source === "object" ? l.source.id : l.source;
    var tid = typeof l.target === "object" ? l.target.id : l.target;
    if (sid === d.id && nodeMap[tid]) conns.push({ node: nodeMap[tid], rel: l.rel || "related", dir: "\u2192", causal: l.causal });
    if (tid === d.id && nodeMap[sid]) conns.push({ node: nodeMap[sid], rel: l.rel || "related", dir: "\u2190", causal: l.causal });
  });
  if (conns.length) {
    // Group: causal first, then outgoing, then incoming
    var causalConns = conns.filter(function(c){return c.causal;});
    var otherConns = conns.filter(function(c){return !c.causal;});
    lw.innerHTML = '<div class="np-section-hd">Connections (' + conns.length + ')</div>';
    if (causalConns.length) lw.innerHTML += '<div style="font-size:9px;color:var(--red);font-family:monospace;letter-spacing:.08em;padding:4px 0 2px">CAUSAL</div>';
    causalConns.slice(0,6).forEach(function(c) { lw.innerHTML += _renderConn(c); });
    otherConns.slice(0,10).forEach(function(c) { lw.innerHTML += _renderConn(c); });
    if (conns.length > 16) lw.innerHTML += '<div style="font-size:10px;color:var(--dim);padding:4px 0">+ ' + (conns.length - 16) + ' more</div>';
  }

  // Open questions placeholder
  lw.innerHTML += '<div id="np-questions-block"></div>';

  // Sources
  fetch(API + "/api/nodes/" + encodeURIComponent(d.id) + "/sources")
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var sources = data.sources || [];
      if (!sources.length) return;
      var srcHtml = '<div class="np-section-hd">Sources (' + sources.length + ')</div>';
      sources.forEach(function(doc) {
        var sn = (doc.filename || "").replace(/[_\-]/g, " ").split(".")[0].trim().slice(0, 28);
        if ((doc.filename || "").length > 28) sn += "\u2026";
        var tc = doc.contribution === "primary" ? "tag-primary" : "tag-enriched";
        srcHtml += '<div class="np-source-chip" onclick="openDocPanel(\'' + esc(doc.filename).replace(/'/g, "\\'") + '\')" title="' + esc(doc.filename) + '"><span class="np-source-icon">&#128196;</span><span class="np-source-name">' + esc(sn) + '</span><span class="' + tc + '">' + (doc.contribution || "source") + '</span></div>';
      });
      document.getElementById("np-connections").innerHTML += srcHtml;
    }).catch(function(){});

  // ── Phase 2: Structured AI summary ──
  // Check for saved summary first
  if (d._summary) {
    _renderStructuredSummary(d._summary, false, d.id);
  } else {
    fetch(API + "/api/nodes/" + encodeURIComponent(d.id) + "/summary/generate")
      .then(function(r) { return r.json(); })
      .then(function(summary) { _renderStructuredSummary(summary, true, d.id); window._pendingSummary = summary; })
      .catch(function() {
        var sb = document.getElementById("np-summary-block");
        if (sb) sb.innerHTML = '<div style="font-size:11px;color:var(--muted);line-height:1.7">' + esc(d.desc || "No description.") + '</div>';
        var rb = document.getElementById("np-role-block"); if (rb) rb.innerHTML = "";
      });
  }
};

function _buildCareerTimeline(career) {
  if (!career || !career.length) return "";
  var sorted = career.slice().sort(function (a, b) {
    if (a.is_current && !b.is_current) return -1;
    if (!a.is_current && b.is_current) return 1;
    return (b.from || "").localeCompare(a.from || "");
  });
  var html = '<div class="np-career-timeline"><div class="np-section-hd">Career Timeline</div>';
  sorted.forEach(function (e, i) {
    var cur = e.is_current;
    var dateStr = e.from ? (e.from + (e.to ? " \u2192 " + e.to : "")) : "";
    var dotCol = cur ? "var(--outcome)" : "var(--dim)";
    var lineCol = cur ? "rgba(78,203,141,.3)" : "var(--border)";
    var isLast = i === sorted.length - 1;
    html += '<div class="np-career-row"><div class="np-career-spine"><div class="np-career-dot" style="background:' + dotCol + (cur ? ";box-shadow:0 0 6px rgba(78,203,141,.4)" : "") + '"></div>' + (!isLast ? '<div class="np-career-line" style="background:' + lineCol + '"></div>' : '') + '</div><div class="np-career-content"><div class="np-career-company" style="color:' + (cur ? "var(--text)" : "var(--muted)") + '">' + esc(e.company || "") + (cur ? ' <span style="font-size:9px;color:var(--outcome);font-family:monospace;margin-left:4px">current</span>' : '') + '</div>' + (e.role ? '<div class="np-career-role">' + esc(e.role) + '</div>' : '') + (dateStr ? '<div class="np-career-date">' + esc(dateStr) + '</div>' : '') + '</div></div>';
  });
  html += '</div>';
  return html;
}

function _renderConn(c) {
  var cc = Brain.getTypeColor(c.node.type);
  var lbl = c.node.label.length > 24 ? c.node.label.slice(0, 22) + "\u2026" : c.node.label;
  return '<div class="np-conn" data-nid="' + esc(c.node.id) + '" onclick="focusNode(\'' + esc(c.node.id) + '\')" title="' + esc(c.node.label) + '"><span style="color:' + cc + ';font-size:8px;flex-shrink:0">\u25CF</span><span>' + (c.causal ? "<b>" : "") + esc(lbl) + (c.causal ? "</b>" : "") + '</span><span class="np-conn-rel">' + c.dir + " " + esc(c.rel) + '</span></div>';
}

window.sendQuestionToAct = function(question, nodeId) {
  fetch(API + "/api/brain/queue-question", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: "panel_q_" + Date.now(), type: "freetext", question: question,
      why: "From node panel inspection", context: nodeId, priority: "medium",
      category: "plan", category_label: "PLAN", from_doc: "node_panel"})
  }).then(function() { updateActBadge(); }).catch(function(){});
};

window.openDocPanel = function(filename) {
  fetch(API + "/api/documents/" + encodeURIComponent(filename))
    .then(function(r) { return r.json(); })
    .then(function(doc) {
      var panel = document.getElementById("node-panel");
      panel.classList.add("open");
      var badge = document.getElementById("np-badge");
      badge.textContent = (doc.doc_type || "DOCUMENT").toUpperCase();
      badge.style.cssText = "background:rgba(77,166,255,.1);color:var(--blue);border:1px solid rgba(77,166,255,.3)";
      document.getElementById("np-name").textContent = doc.filename || "";
      document.getElementById("np-where").textContent = (doc.doc_type || "") + " · " + (doc.nodes_created || []).length + " nodes · " + new Date(doc.uploaded_at || "").toLocaleDateString();
      document.getElementById("np-desc").textContent = doc.summary || "";
      var mw = document.getElementById("np-metrics");
      mw.innerHTML = "";
      if (doc.key_facts && doc.key_facts.length) {
        mw.innerHTML += '<div class="np-section-hd">Key facts</div>';
        doc.key_facts.forEach(function(fact) {
          mw.innerHTML += '<div style="font-size:11px;color:var(--muted);padding:3px 0;border-bottom:1px solid var(--border);line-height:1.5">&middot; ' + esc(fact) + '</div>';
        });
      }
      var conn = document.getElementById("np-connections");
      conn.innerHTML = "";
      if (doc.nodes_created && doc.nodes_created.length) {
        conn.innerHTML = '<div class="np-section-hd">Nodes from this document (' + doc.nodes_created.length + ')</div>';
        doc.nodes_created.slice(0, 10).forEach(function(nodeId) {
          conn.innerHTML += '<div class="np-conn"><div class="np-conn-dot" style="background:var(--dim)"></div><span>' + esc(nodeId.replace(/_/g, " ")) + '</span></div>';
        });
      }
    })
    .catch(function() {});
};

window.closeNodePanel = function () { document.getElementById("node-panel").classList.remove("open"); Brain.clearSelection(); };

// ── HELPERS ──────────────────────────────────────────────────
function hideEmpty() { var es = document.getElementById("empty-state"); if (es) es.style.display = "none"; }
function setProcessing(show, text) { var p = document.getElementById("processing"); if (show) { p.classList.add("show"); document.getElementById("proc-text").textContent = text || "Working..."; } else { p.classList.remove("show"); } }
function addDocChip(name, count) { var dl = document.getElementById("docs-list"), chip = document.createElement("div"); chip.className = "doc-chip"; chip.innerHTML = '<div class="doc-chip-dot"></div><div class="doc-chip-name">' + esc(name) + '</div><div class="doc-chip-count">+' + count + ' nodes</div>'; dl.appendChild(chip); }
function showBrainMsg(text) { var qa = document.getElementById("feed-qa"), d = document.createElement("div"); d.className = "brain-msg"; d.innerHTML = '<div class="msg-label">Brain</div><div class="msg-text">' + esc(text) + '</div>'; qa.appendChild(d); }
function showExtractBlock(summary, data) {
  var qa = document.getElementById("feed-qa"), el2 = document.createElement("div"); el2.className = "extract-block";
  var nodes = (data.extracted && data.extracted.nodes) || [], links = (data.extracted && data.extracted.links) || [];
  var TC = { Feature:"#4da6ff", Surface:"#a87fff", Outcome:"#4ecb8d", Decision:"#f0a040", Person:"#ff8fab", Company:"#7ec8e3" };
  var byType = {}; nodes.forEach(function (n) { byType[n.type] = (byType[n.type] || 0) + 1; });
  var rows = ""; Object.keys(byType).forEach(function (t) { var c = TC[t] || "#888"; rows += '<div class="ex-row"><div class="ex-dot" style="background:' + c + '"></div>' + byType[t] + " " + t + (byType[t] > 1 ? "s" : "") + '<span class="ex-count">+' + byType[t] + '</span></div>'; });
  if (links.length) rows += '<div class="ex-row"><div class="ex-dot" style="background:#555"></div>' + links.length + " connection" + (links.length > 1 ? "s" : "") + '</div>';
  el2.innerHTML = '<div class="ex-label">Extracted</div><div class="ex-summary">' + esc(summary || "") + '</div>' + rows;
  qa.appendChild(el2);
}
function esc(str) { if (!str) return ""; return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

// ── BRAIN CHAT ───────────────────────────────────────────────
window.toggleWebSearch = function () {
  webSearchEnabled = !webSearchEnabled;
  var btn = document.getElementById("web-toggle");
  var icon = document.getElementById("web-toggle-icon");
  if (btn) btn.classList.toggle("active", webSearchEnabled);
  if (icon) icon.innerHTML = webSearchEnabled ? "&#10022;" : "&#8853;";
};

window.sendBrainQuery = function () {
  var input = document.getElementById("chat-input");
  var query = input ? input.value.trim() : "";
  if (!query) return;
  if (webSearchEnabled) { sendWebEnrichQuery(query); return; }
  sendBrainOnlyQuery(query);
};

function sendBrainOnlyQuery(query) {
  var sendBtn = document.getElementById("chat-send");
  var history = document.getElementById("chat-history");
  var input = document.getElementById("chat-input");
  var sug = document.getElementById("chat-suggestions"); if (sug) sug.remove();
  var qBubble = document.createElement("div"); qBubble.className = "chat-query";
  qBubble.innerHTML = '<div class="chat-query-text">' + esc(query) + '</div>';
  history.appendChild(qBubble);
  var thinkEl = document.createElement("div"); thinkEl.className = "chat-answer"; thinkEl.id = "chat-thinking";
  thinkEl.innerHTML = '<div class="chat-thinking"><div class="spin" style="width:10px;height:10px;border-width:1.5px"></div><span class="chat-thinking-text">Traversing graph...</span></div>';
  history.appendChild(thinkEl);
  var skel = document.createElement("div"); skel.className = "chat-answer"; skel.id = "chat-skeleton";
  skel.innerHTML = '<div class="chat-answer-text" style="padding:12px 14px"><div class="skeleton" style="height:12px;width:90%;margin-bottom:8px"></div><div class="skeleton" style="height:12px;width:75%;margin-bottom:8px"></div><div class="skeleton" style="height:12px;width:60%;margin-bottom:8px"></div><div class="skeleton" style="height:12px;width:45%;margin-bottom:12px"></div><div style="display:flex;gap:5px"><div class="skeleton" style="height:10px;width:60px"></div><div class="skeleton" style="height:10px;width:50px"></div><div class="skeleton" style="height:10px;width:40px"></div></div></div>';
  history.appendChild(skel);
  history.scrollTop = history.scrollHeight;
  input.value = ""; if (sendBtn) sendBtn.classList.add("loading");

  fetch(API + "/api/brain/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query: query }) })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var th = document.getElementById("chat-thinking"); if (th) th.remove();
      var sk = document.getElementById("chat-skeleton"); if (sk) sk.remove();
      if (sendBtn) sendBtn.classList.remove("loading");
      if (data.response_type === "command_preview") {
        renderCommandPreview(data, history);
      } else if (data.response_type === "plan_summary") {
        renderPlanSummary(data, history);
      } else {
        renderChatAnswer(data, history);
        if (data.cited_node_ids && data.cited_node_ids.length && Brain.highlightNodes) {
          Brain.highlightNodes(data.cited_node_ids);
          setTimeout(function () { Brain.clearSelection(); }, 4000);
        }
      }
      history.scrollTop = history.scrollHeight;
    })
    .catch(function (err) {
      var th = document.getElementById("chat-thinking"); if (th) th.remove();
      var sk2 = document.getElementById("chat-skeleton"); if (sk2) sk2.remove();
      if (sendBtn) sendBtn.classList.remove("loading");
      var eEl = document.createElement("div"); eEl.className = "chat-answer";
      eEl.innerHTML = '<div class="chat-answer-text confidence-low" style="color:var(--red)">Error: ' + esc(err.message) + '</div>';
      history.appendChild(eEl); history.scrollTop = history.scrollHeight;
    });
}

function sendWebEnrichQuery(query) {
  var sendBtn = document.getElementById("chat-send");
  var history = document.getElementById("chat-history");
  var input = document.getElementById("chat-input");
  var sug = document.getElementById("chat-suggestions"); if (sug) sug.remove();
  var qBubble = document.createElement("div"); qBubble.className = "chat-query";
  qBubble.innerHTML = '<div class="chat-query-text">' + esc(query) + ' <span style="font-size:9px;color:var(--outcome);margin-left:5px">&#10022; web</span></div>';
  history.appendChild(qBubble);
  var thinkEl = document.createElement("div"); thinkEl.id = "chat-thinking"; thinkEl.className = "chat-answer";
  thinkEl.innerHTML = '<div class="chat-thinking"><div class="spin" style="width:10px;height:10px;border-width:1.5px;border-top-color:var(--outcome)"></div><span class="chat-thinking-text" style="color:var(--outcome)">Searching web for graph enrichment...</span></div>';
  history.appendChild(thinkEl); history.scrollTop = history.scrollHeight;
  input.value = ""; if (sendBtn) sendBtn.classList.add("loading");

  fetch(API + "/api/brain/web-enrich", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query: query }) })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var th = document.getElementById("chat-thinking"); if (th) th.remove();
      if (sendBtn) sendBtn.classList.remove("loading");
      renderWebEnrichResult(data, history); history.scrollTop = history.scrollHeight;
      updateActBadge();
      if (currentMode === "act") loadQuestionsDashboard();
    })
    .catch(function (err) {
      var th = document.getElementById("chat-thinking"); if (th) th.remove();
      if (sendBtn) sendBtn.classList.remove("loading");
      var eEl = document.createElement("div"); eEl.className = "chat-answer";
      eEl.innerHTML = '<div class="chat-answer-text confidence-low" style="color:var(--red)">Web search failed: ' + esc(err.message) + '</div>';
      history.appendChild(eEl); history.scrollTop = history.scrollHeight;
    });
}

function renderWebEnrichResult(data, container) {
  var el = document.createElement("div"); el.className = "chat-answer";
  if (data.status !== "ok" || !data.questions || !data.questions.length) {
    el.innerHTML = '<div class="chat-answer-text confidence-low">' + esc(data.message || "No web findings.") + '</div>';
    container.appendChild(el); return;
  }
  var counts = data.counts || {}; var total = data.questions.length;
  var html = '<div style="background:rgba(78,203,141,.06);border:1px solid rgba(78,203,141,.2);border-radius:9px;padding:11px 13px;">';
  html += '<div style="font-family:monospace;font-size:8px;letter-spacing:.12em;color:var(--outcome);text-transform:uppercase;margin-bottom:7px">&#10022; Web Enrichment Found</div>';
  html += '<div style="font-size:11px;color:var(--muted);margin-bottom:9px;line-height:1.6">Searched ' + (data.searches_run || 0) + ' queries about: ' + (data.target_nodes || []).slice(0, 3).map(esc).join(", ") + '</div>';
  if (counts.conflict > 0) html += '<div style="font-size:11px;color:var(--red);margin-bottom:3px">&#9889; ' + counts.conflict + ' conflict' + (counts.conflict > 1 ? "s" : "") + ' — requires your review</div>';
  if (counts.enrich > 0) html += '<div style="font-size:11px;color:var(--blue);margin-bottom:3px">&uarr; ' + counts.enrich + ' enrichment' + (counts.enrich > 1 ? "s" : "") + ' proposed</div>';
  if (counts.new > 0) html += '<div style="font-size:11px;color:var(--muted);margin-bottom:3px">+ ' + counts.new + ' new fact' + (counts.new > 1 ? "s" : "") + ' found</div>';
  html += '<button onclick="switchMode(\'act\')" style="margin-top:9px;width:100%;padding:7px 12px;border-radius:7px;border:1px solid rgba(78,203,141,.3);background:rgba(78,203,141,.08);color:var(--outcome);font-size:11px;cursor:pointer;text-align:left">&rarr; Review ' + total + ' findings in Act</button>';
  html += '</div>';
  el.innerHTML = html; container.appendChild(el);
}

function renderChatAnswer(data, container) {
  var conf = data.confidence || "low";
  var answer = data.answer || "No answer generated.";
  var citations = data.cited_node_ids || [];
  var citLabels = data.cited_node_labels || [];
  var missing = data.missing_info;
  var entryNodes = data.entry_nodes || [];
  var subSize = data.subgraph_size || 0;

  var el2 = document.createElement("div"); el2.className = "chat-answer";
  var html = '<div class="chat-answer-text confidence-' + conf + '">' + esc(answer) + '</div>';
  if (citations.length) {
    html += '<div class="chat-citations">';
    citations.forEach(function (id, i) {
      var label = citLabels[i] || id;
      html += '<span class="chat-cite-pill" onclick="focusNode(\'' + esc(id).replace(/'/g, "\\'") + '\')">' + esc(label) + '</span>';
    });
    html += '</div>';
  }
  html += '<div class="chat-meta">' + conf + ' confidence &middot; ' + subSize + ' nodes searched' + (entryNodes.length ? ' &middot; via ' + entryNodes.slice(0, 2).map(esc).join(', ') : '') + '</div>';
  if (missing && missing !== "null" && missing !== null && conf === "low") {
    html += '<div class="chat-missing">' + esc(String(missing)) + '</div>';
  }
  el2.innerHTML = html;
  container.appendChild(el2);
}

window.focusNode = function (nodeId) {
  fetch(API + "/api/brain").then(function (r) { return r.json(); })
    .then(function (brain) {
      var n = (brain.nodes || []).find(function (nd) { return nd.id === nodeId; });
      if (n) window.openNodePanel(n, brain.nodes, brain.links);
    });
};

function renderCommandPreview(data, container) {
  var el = document.createElement("div"); el.className = "chat-answer"; animateIn(el);
  var preview = data.preview || {}; var lines = preview.preview_lines || [];
  var isDestructive = preview.is_destructive; var ops = data.operations || [];
  var linesHtml = lines.map(function (l) {
    var color = l.charAt(0) === "\u2715" ? "var(--red)" : l.charAt(0) === "\u2192" ? "var(--blue)" : l.charAt(0) === "\u270E" ? "var(--amber)" : "var(--muted)";
    return '<div style="padding:3px 0;font-size:11px;color:' + color + '">' + esc(l) + '</div>';
  }).join("");
  var opsStr = JSON.stringify(ops);
  var acCol = isDestructive ? "var(--red)" : "var(--blue)";
  var acBg = isDestructive ? "rgba(255,107,107,.1)" : "rgba(77,166,255,.1)";
  el.innerHTML =
    '<div style="font-size:10px;font-family:monospace;letter-spacing:.08em;color:var(--dim);text-transform:uppercase;margin-bottom:10px">Command Preview</div>' +
    '<div style="background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px">' + linesHtml + '</div>' +
    '<div class="chat-cmd-actions"><button class="chat-cmd-confirm" style="border-color:' + acCol + ';background:' + acBg + ';color:' + acCol + '">Confirm</button>' +
    '<button class="chat-cmd-cancel">Cancel</button></div>';
  container.appendChild(el);
  // Store ops on element to survive any DOM manipulation
  var confirmBtn = el.querySelector(".chat-cmd-confirm");
  confirmBtn._storedOps = ops;
  confirmBtn.onclick = function () {
    var btn = this; var storedOps = btn._storedOps;
    btn.textContent = "Executing\u2026"; btn.style.opacity = "0.6"; btn.style.pointerEvents = "none";
    fetch(API + "/api/brain/chat/confirm", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({operations: storedOps, confirmed: true}) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var wrap = btn.parentNode;
        var resultHtml = '<div style="font-size:10px;font-family:monospace;letter-spacing:.08em;color:var(--outcome);text-transform:uppercase;margin-bottom:8px">\u2713 Done</div>';
        resultHtml += (d.changes || []).map(function (c) {
          var color = c.indexOf("Skipped") >= 0 ? "var(--amber)" : "var(--muted)";
          return '<div style="font-size:12px;color:' + color + ';padding:3px 0;line-height:1.5">' + esc(c) + '</div>';
        }).join("");
        if ((d.affected_nodes || []).length) {
          var TC = {Feature:"#4da6ff",Surface:"#a87fff",Outcome:"#4ecb8d",Decision:"#f0a040",Person:"#ff8fab",Company:"#7ec8e3"};
          resultHtml += '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:7px">' +
            (d.affected_nodes || []).map(function (n) {
              return '<span class="qi-result-node" style="border-color:' + (TC[n.type]||"#888") + '40;color:' + (TC[n.type]||"#888") + '" onclick="focusNode(\'' + esc(n.id) + '\')">' + esc(n.label) + '</span>';
            }).join("") + '</div>';
        }
        wrap.innerHTML = resultHtml;
        fetch(API + "/api/brain").then(function (r) { return r.json(); }).then(function (b) {
          window._lastBrainState = { nodes: b.nodes || [], links: b.links || [] };
          Brain.updateGraph(b.nodes || [], b.links || []);
          var openNode = window._openPanelNode;
          if (openNode) {
            var fresh = (b.nodes || []).find(function (n) { return n.id === openNode.id; });
            if (fresh) setTimeout(function () { openNodePanel(fresh, b.nodes, b.links); }, 300);
          }
        });
        refreshStats(); updateActBadge();
      }).catch(function (e) { btn.textContent = "Failed \u2014 " + (e.message || ""); });
  };
  el.querySelector(".chat-cmd-cancel").onclick = function () { el.remove(); };
}

function renderPlanSummary(data, container) {
  var el = document.createElement("div"); el.className = "chat-answer"; animateIn(el);
  var count = data.questions_added || 0; var previews = data.question_previews || [];
  if (count === 0) {
    el.innerHTML = '<div class="chat-answer-text confidence-low">' + esc(data.message || "No questions generated.") + '</div>';
    container.appendChild(el); return;
  }
  var previewHtml = previews.map(function (q) {
    return '<div style="padding:4px 0;font-size:11px;color:var(--muted);border-bottom:1px solid var(--border);line-height:1.5">' + esc(q) + '</div>';
  }).join("");
  el.innerHTML =
    '<div style="font-size:10px;font-family:monospace;letter-spacing:.08em;color:var(--purple);text-transform:uppercase;margin-bottom:8px">\u25CE Plan Generated</div>' +
    '<div style="background:rgba(168,127,255,.05);border:1px solid rgba(168,127,255,.2);border-radius:8px;padding:10px 12px;margin-bottom:10px">' +
    '<div style="font-size:11px;color:var(--muted);margin-bottom:8px">Added ' + count + ' targeted questions for: <em>' + esc(data.topic || "") + '</em></div>' +
    previewHtml +
    (count > previews.length ? '<div style="font-size:10px;color:var(--dim);padding-top:4px">+ ' + (count - previews.length) + ' more</div>' : '') +
    '</div>' +
    '<button onclick="switchMode(\'act\')" style="width:100%;padding:7px 12px;border-radius:7px;border:1px solid rgba(168,127,255,.3);background:rgba(168,127,255,.08);color:var(--purple);font-size:11px;cursor:pointer;text-align:left">\u2192 Review ' + count + ' questions in Act</button>';
  container.appendChild(el);
  updateActBadge();
  if (currentMode === "act") loadQuestionsDashboard();
}

function showSuggestedQueries(stats) {
  var history = document.getElementById("chat-history");
  if (!history || history.children.length > 0) return;
  if (!stats || (stats.total_nodes || 0) < 5) return;
  var suggestions = ["What caused the churn reduction?", "Which features did Suneet build?", "What are the main product outcomes?", "What decisions shaped the MVP?"];
  var el2 = document.createElement("div"); el2.id = "chat-suggestions";
  el2.style.cssText = "display:flex;flex-direction:column;gap:5px;padding:4px 0";
  var label = document.createElement("div");
  label.style.cssText = "font-size:9px;color:var(--dim);font-family:monospace;letter-spacing:.1em;text-transform:uppercase;margin-bottom:3px";
  label.textContent = "Try asking";
  el2.appendChild(label);
  suggestions.forEach(function (s) {
    var pill = document.createElement("button");
    pill.style.cssText = "padding:6px 10px;border:1px solid var(--border2);border-radius:7px;background:transparent;color:var(--muted);font-size:10px;cursor:pointer;text-align:left;transition:all .15s";
    pill.textContent = s;
    pill.onmouseenter = function () { this.style.borderColor = "rgba(77,166,255,.4)"; this.style.color = "var(--blue)"; };
    pill.onmouseleave = function () { this.style.borderColor = "var(--border2)"; this.style.color = "var(--muted)"; };
    pill.onclick = function () { document.getElementById("chat-input").value = s; el2.remove(); sendBrainQuery(); };
    el2.appendChild(pill);
  });
  history.appendChild(el2);
}

// ── PANEL RESIZE ─────────────────────────────────────────────
(function initResize() {
  var handle = document.getElementById("resize-handle");
  var left = document.getElementById("left");
  if (!handle || !left) return;
  var dragging = false, startX = 0, startW = 0;
  handle.addEventListener("mousedown", function (e) {
    dragging = true; startX = e.clientX; startW = left.getBoundingClientRect().width;
    handle.classList.add("dragging"); document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
  });
  document.addEventListener("mousemove", function (e) {
    if (!dragging) return;
    left.style.width = Math.min(Math.max(startW + (e.clientX - startX), 280), 660) + "px";
  });
  document.addEventListener("mouseup", function () {
    if (!dragging) return;
    dragging = false; handle.classList.remove("dragging"); document.body.style.cursor = ""; document.body.style.userSelect = "";
    if (Brain && Brain.zoomFit) Brain.zoomFit();
  });
}());

// ── CHAT RESIZE ──────────────────────────────────────────────
(function initChatResize() {
  var bar = document.getElementById("chat-resize-bar");
  var chat = document.getElementById("brain-chat");
  if (!bar || !chat) return;
  var dragging = false, startY = 0, startH = 0;
  bar.addEventListener("mousedown", function (e) {
    dragging = true; startY = e.clientY; startH = chat.getBoundingClientRect().height;
    bar.classList.add("dragging"); document.body.style.cursor = "row-resize"; document.body.style.userSelect = "none"; e.preventDefault();
  });
  document.addEventListener("mousemove", function (e) {
    if (!dragging) return;
    chat.style.height = Math.min(Math.max(startH + (startY - e.clientY), 120), window.innerHeight * 0.7) + "px";
  });
  document.addEventListener("mouseup", function () {
    if (!dragging) return;
    dragging = false; bar.classList.remove("dragging"); document.body.style.cursor = ""; document.body.style.userSelect = "";
  });
}());

// ── STRUCTURED NODE SUMMARY ──────────────────────────────
function _renderStructuredSummary(summary, isPending, nodeId) {
  var sb = document.getElementById("np-summary-block");
  var rb = document.getElementById("np-role-block");
  var qb = document.getElementById("np-questions-block");
  if (!sb) return;

  var bCol = isPending ? "rgba(77,166,255,.2)" : "rgba(255,255,255,.06)";
  var badge = isPending ? "PREVIEW" : "v" + (summary.version || 1) + " \u00B7 saved";
  var bColor = isPending ? "var(--blue)" : "var(--dim)";
  var fields = [
    {key:"role",icon:"\u25C8",label:"Role",col:"var(--text)"},
    {key:"context",icon:"\u25C9",label:"Context",col:"var(--muted)"},
    {key:"ownership",icon:"\u25CE",label:"Ownership",col:"var(--person)"},
    {key:"impact",icon:"\u2191",label:"Impact",col:"var(--outcome)"}
  ];
  var html = '<div style="background:rgba(255,255,255,.02);border:1px solid ' + bCol + ';border-radius:9px;padding:11px 13px">';
  html += '<div style="font-size:9px;color:' + bColor + ';font-family:monospace;margin-bottom:10px;letter-spacing:.06em">' + badge + '</div>';
  fields.forEach(function(f) {
    var val = summary[f.key] || "";
    if (!val || val === "Unknown" || val === "Not documented") return;
    html += '<div style="display:flex;gap:6px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04);align-items:flex-start">' +
      '<span style="color:' + f.col + ';font-size:10px;flex-shrink:0;margin-top:2px;width:12px">' + f.icon + '</span>' +
      '<div><div style="font-size:9px;color:var(--dim);font-family:monospace;letter-spacing:.07em;text-transform:uppercase;margin-bottom:2px">' + f.label + '</div>' +
      '<div style="font-size:11px;color:var(--muted);line-height:1.6">' + esc(val) + '</div></div></div>';
  });
  if (summary.connections_narrative) {
    html += '<div style="padding:8px 0 4px"><div style="font-size:9px;color:var(--dim);font-family:monospace;letter-spacing:.07em;text-transform:uppercase;margin-bottom:4px">Graph role</div>' +
      '<div style="font-size:11px;color:var(--dim);line-height:1.7;font-style:italic">' + esc(summary.connections_narrative) + '</div></div>';
  }
  if (summary.open_gaps && summary.open_gaps.length) {
    html += '<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,.04)"><div style="font-size:9px;color:var(--blue);font-family:monospace;letter-spacing:.07em;margin-bottom:5px">OPEN GAPS</div>';
    summary.open_gaps.forEach(function(g) {
      html += '<div style="display:flex;gap:5px;padding:2px 0;font-size:10px;color:var(--dim)"><span style="color:var(--blue)">\u25E6</span><span>' + esc(g) + '</span></div>';
    });
    html += '</div>';
  }
  html += '</div>';
  if (isPending) {
    html += '<div style="display:flex;gap:6px;margin-top:10px">' +
      '<button onclick="saveSummary(\'' + esc(nodeId) + '\')" style="padding:5px 12px;border-radius:6px;border:1px solid rgba(78,203,141,.4);background:rgba(78,203,141,.08);color:var(--outcome);font-size:10px;cursor:pointer">\u2713 Save</button>' +
      '<button onclick="discardSummary()" style="padding:5px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--dim);font-size:10px;cursor:pointer">Discard</button></div>';
  } else {
    html += '<button onclick="regenNodeSummary(\'' + esc(nodeId) + '\')" style="margin-top:8px;font-size:9px;color:var(--dim);background:transparent;border:none;cursor:pointer">\u21BB Regenerate</button>';
  }
  sb.innerHTML = html;
  if (rb) rb.innerHTML = "";
  if (qb && summary.open_gaps && summary.open_gaps.length && nodeId) {
    var qh = '<div class="np-section-hd">Open questions</div>';
    summary.open_gaps.forEach(function(g) {
      qh += '<div style="display:flex;gap:7px;padding:5px 0;border-bottom:1px solid var(--border);align-items:flex-start"><span style="color:var(--amber);font-size:10px;flex-shrink:0;margin-top:1px">?</span><span style="font-size:11px;color:var(--muted);line-height:1.5;flex:1">' + esc(g) + '</span><button onclick="sendQuestionToAct(\'' + esc(g).replace(/'/g, "\\'") + '\',\'' + esc(nodeId) + '\')" style="flex-shrink:0;padding:2px 7px;border-radius:5px;font-size:9px;border:1px solid var(--border2);background:transparent;color:var(--dim);cursor:pointer">\u2192 Act</button></div>';
    });
    qb.innerHTML = qh;
  }
}

window.saveSummary = function(nodeId) {
  var s = window._pendingSummary; if (!s) return;
  // Disable save button immediately
  var saveBtn = document.querySelector('#np-summary-block button[onclick*="saveSummary"]');
  if (saveBtn) { saveBtn.textContent = "Saving\u2026"; saveBtn.disabled = true; saveBtn.style.opacity = "0.5"; }
  fetch(API + "/api/nodes/" + nodeId + "/summary/save", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(s)})
    .then(function() {
      if (window._openPanelNode) { window._openPanelNode._summary = s; window._openPanelNode._summary_saved_at = new Date().toISOString(); }
      _renderStructuredSummary(s, false, nodeId); window._pendingSummary = null;
    })
    .catch(function() { if (saveBtn) { saveBtn.textContent = "\u2713 Save"; saveBtn.disabled = false; saveBtn.style.opacity = "1"; } });
};
window.discardSummary = function() {
  window._pendingSummary = null;
  var sb = document.getElementById("np-summary-block");
  if (sb && window._openPanelNode) {
    sb.innerHTML = '<div style="font-size:11px;color:var(--muted)">' + esc(window._openPanelNode.desc || "No description.") + '</div>' +
      '<button onclick="regenNodeSummary(\'' + esc(window._openPanelNode.id) + '\')" style="margin-top:8px;font-size:9px;color:var(--dim);background:transparent;border:none;cursor:pointer">\u21BB Generate summary</button>';
  }
};
window.regenNodeSummary = function(nodeId) {
  var sb = document.getElementById("np-summary-block");
  if (sb) sb.innerHTML = '<div class="skeleton" style="height:13px;width:95%;margin-bottom:5px"></div><div class="skeleton" style="height:13px;width:80%;margin-bottom:5px"></div><div class="skeleton" style="height:13px;width:65%"></div>';
  fetch(API + "/api/nodes/" + nodeId + "/summary/generate").then(function(r){return r.json();})
    .then(function(s) { _renderStructuredSummary(s, true, nodeId); window._pendingSummary = s; });
};

// ── SUMMARY REGEN ALL ────────────────────────────────────
window.startSummaryRegen = function() {
  var btn = document.getElementById("summary-regen-btn");
  var status = document.getElementById("summary-regen-status");
  if (btn) { btn.disabled = true; btn.textContent = "\u25CE Generating\u2026"; }
  var allSummaries = {};
  var es = new EventSource(API + "/api/brain/summaries/generate-all");
  es.onmessage = function(e) {
    try {
      var ev = JSON.parse(e.data);
      if (ev.type === "start" && status) status.textContent = "0/" + ev.total;
      else if (ev.type === "summary") {
        allSummaries[ev.node_id] = ev.summary;
        if (status) status.textContent = "[" + (ev.index+1) + "/" + ev.total + "] " + ev.node_label;
        if (btn) btn.textContent = "\u25CE " + Math.round((ev.index+1)/ev.total*100) + "%";
      } else if (ev.type === "complete") {
        es.close();
        if (btn) { btn.disabled = false; btn.textContent = "\u25CE Regenerate All Summaries"; }
        if (status) status.textContent = Object.keys(allSummaries).length + " summaries ready";
        var sect = document.getElementById("summary-regen-section");
        if (sect) {
          var acts = document.createElement("div");
          acts.style.cssText = "display:flex;gap:8px;margin-top:10px";
          acts.innerHTML = '<button onclick="saveAllSummaries()" style="flex:1;padding:8px 12px;border-radius:7px;border:1px solid rgba(78,203,141,.4);background:rgba(78,203,141,.08);color:var(--outcome);font-size:11px;cursor:pointer">\u2713 Save all ' + Object.keys(allSummaries).length + '</button>' +
            '<button onclick="discardAllSummaries()" style="padding:8px 12px;border-radius:7px;border:1px solid var(--border);background:transparent;color:var(--dim);font-size:11px;cursor:pointer">Discard</button>';
          sect.appendChild(acts);
        }
        window._allGenSummaries = allSummaries;
      }
    } catch(err) {}
  };
  es.onerror = function() { es.close(); if (btn) { btn.disabled = false; btn.textContent = "\u25CE Regenerate All Summaries"; } if (status) status.textContent = "Failed"; };
};
window.saveAllSummaries = function(summaries) {
  var s = summaries || window._allGenSummaries || window._allGeneratedSummaries; if (!s) return;
  fetch(API + "/api/brain/summaries/save-all", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({summaries:s})})
    .then(function(r){return r.json();}).then(function(d) {
      var status = document.getElementById("summary-regen-status");
      if (status) status.textContent = "\u2713 " + d.count + " summaries saved";
      window._allGenSummaries = null; window._allGeneratedSummaries = null;
      document.querySelectorAll("#summary-regen-section div[style*='gap:8px']").forEach(function(e){e.remove();});
      var actions = document.getElementById("summary-regen-actions"); if (actions) actions.style.display = "none";
      loadSummaryStatus();
    });
};
window.discardAllSummaries = function() {
  window._allGenSummaries = null; window._allGeneratedSummaries = null;
  var status = document.getElementById("summary-regen-status");
  if (status) status.textContent = "Discarded";
  document.querySelectorAll("#summary-regen-section div[style*='gap:8px']").forEach(function(e){e.remove();});
};

// ── EVOLUTION CONTROL ────────────────────────────────────
var _evolutionRunning = false;
var _evolutionEvents = [];

window.startEvolutionManual = function() {
  if (_evolutionRunning) return;
  var intensity = (document.getElementById("evo-intensity-select") || {}).value || "medium";
  _evolutionRunning = true; _evolutionEvents = [];
  var dot = document.getElementById("evo-state-dot");
  var label = document.getElementById("evo-state-label");
  var startBtn = document.getElementById("evo-start-btn");
  var stopBtn = document.getElementById("evo-stop-btn");
  if (dot) dot.classList.add("running");
  if (label) label.textContent = "Starting\u2026";
  if (startBtn) startBtn.style.display = "none";
  if (stopBtn) stopBtn.style.display = "";
  switchMode("think");
  var log = document.getElementById("think-log"); if (log) log.innerHTML = "";
  document.getElementById("evolution-diff-panel").style.display = "none";

  window._evoES = new EventSource(API + "/api/evolve/stream?intensity=" + intensity);
  var startTime = Date.now();
  window._evoES.onmessage = function(e) {
    try {
      var ev = JSON.parse(e.data);
      _evolutionEvents.push(ev);
      if (ev.type === "snapshot") { if (label) label.textContent = "Snapshotted"; }
      else if (ev.type === "step") {
        if (label) label.textContent = ev.label || ev.step;
        // Add to think log
        var logEl = document.getElementById("think-log");
        if (logEl) {
          var h = document.createElement("div"); h.className = "tl-agent-header"; h.style.color = "var(--outcome)";
          h.innerHTML = '<div class="tl-agent-spinner" style="border-top-color:var(--outcome)"></div> ' + esc(ev.step) + '<span style="color:var(--dim);font-weight:400;font-size:9px;margin-left:4px">' + esc(ev.label||"") + '</span>';
          logEl.appendChild(h); logEl.scrollTop = logEl.scrollHeight;
        }
      }
      else if (ev.type === "step_done") {
        var logEl2 = document.getElementById("think-log");
        if (logEl2) {
          var sp = logEl2.querySelector(".tl-agent-spinner:last-of-type");
          if (sp) sp.outerHTML = '<span style="color:var(--outcome)">\u2713</span>';
          var entry = document.createElement("div"); entry.className = "tl-entry type-action";
          var elapsed = ((Date.now()-startTime)/1000).toFixed(1);
          entry.innerHTML = '<span class="tl-time">+' + elapsed + 's</span><span class="tl-agent" style="color:var(--outcome)">' + esc(ev.step||"").slice(0,8).toUpperCase() + '</span><span class="tl-text">' + (ev.found||0) + ' found \u00B7 ' + (ev.auto_apply||0) + ' auto \u00B7 ' + (ev.needs_review||0) + ' queued</span>';
          logEl2.appendChild(entry); logEl2.scrollTop = logEl2.scrollHeight;
        }
      }
      else if (ev.type === "complete" || ev.type === "skipped") {
        window._evoES.close();
        _evolutionRunning = false;
        if (dot) dot.classList.remove("running");
        if (startBtn) startBtn.style.display = "";
        if (stopBtn) stopBtn.style.display = "none";
        if (ev.type === "skipped") { if (label) label.textContent = "Already running"; return; }
        var applied = ev.changes_applied||0, queued = ev.queued_for_review||0;
        if (label) label.textContent = applied + " applied \u00B7 " + queued + " queued";
        _showEvoDiff(ev, _evolutionEvents);
        if (applied > 0 || queued > 0) {
          fetch(API+"/api/brain").then(function(r){return r.json();}).then(function(b){
            Brain.updateGraph(b.nodes||[],b.links||[]);
            window._lastBrainState={nodes:b.nodes||[],links:b.links||[]};
          });
          refreshStats(); updateActBadge();
        }
      }
    } catch(err) {}
  };
  window._evoES.onerror = function() { window._evoES.close(); _evolutionRunning=false;
    if (dot) dot.classList.remove("running"); if (label) label.textContent="Error";
    if (startBtn) startBtn.style.display=""; if (stopBtn) stopBtn.style.display="none"; };
};

window.stopEvolution = function() {
  if (window._evoES) window._evoES.close();
  _evolutionRunning = false;
  var dot = document.getElementById("evo-state-dot"); if (dot) dot.classList.remove("running");
  var label = document.getElementById("evo-state-label"); if (label) label.textContent = "Stopped";
  var startBtn = document.getElementById("evo-start-btn"); if (startBtn) startBtn.style.display = "";
  var stopBtn = document.getElementById("evo-stop-btn"); if (stopBtn) stopBtn.style.display = "none";
};

function _showEvoDiff(final, allEvents) {
  var panel = document.getElementById("evolution-diff-panel");
  var body = document.getElementById("evolution-diff-body");
  var elapsed = document.getElementById("evo-diff-elapsed");
  if (!panel || !body) return;
  panel.style.display = "";
  if (elapsed) elapsed.textContent = (final.elapsed||0) + "s";
  var changes = final.changes||[], queued = final.queued_for_review||0, delta = final.health_delta||0, rolled = final.rolled_back||false;
  var html = '<div class="evo-diff-section"><div class="evo-diff-section-hd">Health impact</div>';
  var dc = delta > 0 ? "var(--outcome)" : delta < 0 ? "var(--red)" : "var(--dim)";
  var ds = delta > 0 ? "\u25B2" : delta < 0 ? "\u25BC" : "\u2014";
  html += '<div class="evo-diff-item"><span class="evo-diff-icon" style="color:' + dc + '">' + ds + '</span><span>' + (delta === 0 ? "No change" : Math.abs(delta).toFixed(1)+"% "+(delta>0?"improvement":"degradation")+(rolled?" \u2014 ROLLED BACK":"")) + '</span></div></div>';
  if (changes.length) {
    html += '<div class="evo-diff-section"><div class="evo-diff-section-hd">Applied (' + changes.length + ')</div>';
    changes.forEach(function(c) {
      var icon = c.indexOf("Enriched")>=0?"\u270E":c.indexOf("Added")>=0?"+":"\u2713";
      var col = c.indexOf("Enriched")>=0?"var(--blue)":c.indexOf("Added")>=0?"var(--outcome)":"var(--muted)";
      html += '<div class="evo-diff-item"><span class="evo-diff-icon" style="color:'+col+'">'+icon+'</span><span>'+esc(c)+'</span></div>';
    });
    html += '</div>';
  }
  if (queued > 0) {
    html += '<div class="evo-diff-section"><div class="evo-diff-section-hd">Needs review (' + queued + ')</div>';
    html += '<div class="evo-diff-item"><span class="evo-diff-icon" style="color:var(--amber)">\u26A1</span><span>'+queued+' proposals queued in Act</span></div>';
    html += '<button onclick="switchMode(\'act\')" style="margin-top:7px;width:100%;padding:6px 10px;border-radius:6px;border:1px solid rgba(240,160,64,.3);background:rgba(240,160,64,.06);color:var(--decision);font-size:10px;cursor:pointer">\u2192 Review in Act</button></div>';
  }
  if (!changes.length && !queued) html = '<div style="font-size:11px;color:var(--dim);padding:4px 0">No changes \u2014 brain is already well-connected.</div>';
  body.innerHTML = html;
}

// ── COST PANEL (floating right edge) ─────────────────────
var _costPanelOpen = false;
var _lastCostCount = 0;

function initCostWidget() {
  setInterval(function () {
    fetch(API + "/api/cost/live").then(function (r) { return r.json(); })
      .then(function (d) {
        var floatTotal = document.getElementById("cost-float-total");
        var floatDot = document.getElementById("cost-float-dot");
        if (floatTotal) floatTotal.textContent = "$" + (d.total_usd || 0).toFixed(4);
        if (d.call_count !== _lastCostCount) {
          _lastCostCount = d.call_count;
          if (floatDot) { floatDot.classList.add("active"); setTimeout(function () { floatDot.classList.remove("active"); }, 2000); }
          if (_costPanelOpen) loadCostSidePanel();
        }
      }).catch(function () {});
  }, 3000);
}

window.toggleCostPanel = function () {
  _costPanelOpen = !_costPanelOpen;
  var panel = document.getElementById("cost-side-panel");
  if (panel) panel.classList.toggle("open", _costPanelOpen);
  if (_costPanelOpen) loadCostSidePanel();
};

function loadCostSidePanel() {
  fetch(API + "/api/cost/live").then(function (r) { return r.json(); })
    .then(function (d) {
      var totalEl = document.getElementById("cost-side-total");
      var stats = document.getElementById("cost-side-stats");
      if (totalEl) totalEl.textContent = "$" + (d.total_usd || 0).toFixed(4);
      if (stats) {
        var totalTok = (d.total_input_tokens || 0) + (d.total_output_tokens || 0);
        stats.innerHTML = _csc("SESSION TOTAL", "$" + (d.total_usd || 0).toFixed(4), d.call_count + " calls") +
          _csc("TOKENS USED", fmt(totalTok), fmt(d.total_input_tokens || 0) + " in / " + fmt(d.total_output_tokens || 0) + " out") +
          _csc("LAST CALL", "$" + (d.last_call_usd || 0).toFixed(4), d.last_touchpoint || "\u2014") +
          _csc("LAST TOKENS", fmt(d.last_call_tokens || 0), "");
      }
    }).catch(function () {});
  fetch(API + "/api/cost/log?limit=100").then(function (r) { return r.json(); })
    .then(function (data) {
      var list = document.getElementById("cost-side-log-list");
      if (!list) return;
      var calls = data.calls || [];
      var byTouch = {};
      calls.forEach(function (c) { var t = c.touchpoint || "unknown"; if (!byTouch[t]) byTouch[t] = {count: 0, usd: 0}; byTouch[t].count++; byTouch[t].usd += c.usd || 0; });
      var totalUsd = calls.reduce(function (s, c) { return s + (c.usd || 0); }, 0) || 1;
      var sorted = Object.entries(byTouch).sort(function (a, b) { return b[1].usd - a[1].usd; });
      var html = '<div style="padding:10px 16px 4px;font-size:9px;color:var(--dim);font-family:monospace;letter-spacing:.08em;text-transform:uppercase">By touchpoint</div>';
      html += sorted.slice(0, 8).map(function (e) {
        var pct = Math.round((e[1].usd / totalUsd) * 100);
        return '<div style="display:flex;align-items:center;gap:8px;padding:5px 16px;font-size:10px"><div style="flex:1;min-width:0"><div style="color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(e[0]) + '">' + esc(e[0]) + '</div><div style="height:2px;background:var(--border2);border-radius:1px;margin-top:3px"><div style="height:100%;width:' + pct + '%;background:var(--blue);border-radius:1px"></div></div></div><span style="font-family:monospace;font-size:10px;color:var(--dim)">' + e[1].count + 'x</span><span style="font-family:monospace;font-size:10px;color:var(--outcome);width:52px;text-align:right">$' + e[1].usd.toFixed(4) + '</span></div>';
      }).join("");
      html += '<div style="padding:10px 16px 4px;font-size:9px;color:var(--dim);font-family:monospace;letter-spacing:.08em;text-transform:uppercase;border-top:1px solid var(--border)">Recent calls</div>';
      html += calls.slice(0, 50).map(function (c) {
        var ts = new Date(c.ts);
        var t = ts.getHours().toString().padStart(2, "0") + ":" + ts.getMinutes().toString().padStart(2, "0") + ":" + ts.getSeconds().toString().padStart(2, "0");
        var tok = c.total_tokens > 999 ? (c.total_tokens / 1000).toFixed(1) + "k" : c.total_tokens + "";
        var dur = c.duration_ms > 999 ? (c.duration_ms / 1000).toFixed(1) + "s" : c.duration_ms + "ms";
        return '<div class="cost-log-row"><span class="cl-time">' + t + '</span><span class="cl-touch" title="' + esc(c.touchpoint || "unknown") + '">' + esc(c.touchpoint || "unknown") + '</span><span class="cl-tok">' + tok + '</span><span class="cl-usd">$' + (c.usd || 0).toFixed(4) + '</span><span class="cl-dur">' + dur + '</span></div>';
      }).join("");
      list.innerHTML = html;
    }).catch(function () {});
}

function _csc(label, value, sub) {
  return '<div class="cost-stat-card"><div class="cost-stat-label">' + label + '</div><div class="cost-stat-value">' + value + '</div>' + (sub ? '<div class="cost-stat-sub">' + sub + '</div>' : '') + '</div>';
}

// ── HEALTH INFO ──────────────────────────────────────────
window.toggleHealthInfo = function () {
  var panel = document.getElementById("hs-info-panel");
  if (!panel) return;
  var vis = panel.style.display !== "none";
  panel.style.display = vis ? "none" : "block";
  if (!vis) {
    var dims = [
      {l:"C",n:"Connectivity",c:"var(--blue)",d:"Nodes with 2+ connections, avg degree, causal ratio"},
      {l:"K",n:"Completeness",c:"var(--outcome)",d:"Nodes with desc >40 chars, features with surface, outcomes sourced"},
      {l:"V",n:"Confidence",c:"var(--purple)",d:"QA-confirmed ratio, avg confidence score, QA density"},
      {l:"H",n:"Coherence",c:"var(--decision)",d:"1 \u2212 contradiction penalty \u2212 orphan penalty \u2212 duplicate penalty"},
      {l:"R",n:"Coverage",c:"var(--person)",d:"Node/doc ratio, edge density, type diversity"}
    ];
    panel.innerHTML = '<div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:10px">Brain Health Score</div>' +
      '<div style="font-size:10px;color:var(--dim);margin-bottom:10px;line-height:1.6">Geometric mean of 5 dimensions \u00D7 100. Any weak dimension drags the whole score.</div>' +
      dims.map(function(d){return '<div style="display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);align-items:flex-start"><span style="font-family:monospace;font-size:11px;font-weight:700;color:'+d.c+';flex-shrink:0;width:14px">'+d.l+'</span><div><div style="font-size:10px;color:var(--muted);font-weight:600">'+d.n+'</div><div style="font-size:9px;color:var(--dim);line-height:1.5;margin-top:2px">'+d.d+'</div></div></div>';}).join("") +
      '<div style="margin-top:10px;font-size:9px;color:var(--dim);line-height:1.6">Grades: A+(\u226590) A(\u226580) B(\u226570) C(\u226560) D(\u226550) F(&lt;50)</div>';
  }
};
document.addEventListener("click", function (e) {
  var p = document.getElementById("hs-info-panel");
  var b = document.getElementById("hs-info-btn");
  if (p && b && !p.contains(e.target) && !b.contains(e.target)) p.style.display = "none";
});

// ── NODE SEARCH (left panel) ─────────────────────────────
window.clearNodeSearch = function () {
  var input = document.getElementById("node-search-input");
  var clear = document.getElementById("node-search-clear");
  var count = document.getElementById("node-search-count");
  if (input) input.value = "";
  if (clear) clear.style.display = "none";
  if (count) count.textContent = "";
  Brain.searchNodes("");
};

// ── THINK SECTION TOGGLE ─────────────────────────────────
window.toggleThinkSection = function (id) {
  var s = document.getElementById(id);
  if (s) s.classList.toggle("collapsed");
};

// ── TIMED EVOLUTION ──────────────────────────────────────
var _evolveDurationMs = 2 * 60 * 1000;
var _evolveStartedAt = null;
var _evolveTimerInterval = null;
var _evolveCycleCount = 0;
var _evolveES = null;

window.selectEvolveDuration = function (min) {
  _evolveDurationMs = min * 60 * 1000;
  document.querySelectorAll(".evolve-preset").forEach(function (b) { b.classList.toggle("active", parseInt(b.dataset.min) === min); });
  var hint = document.getElementById("evolve-cycles-hint");
  if (hint) hint.textContent = "~" + Math.max(1, Math.floor(min * 60 / 35)) + " cycles";
};

var _evolveLogStartTime = null;

function addEvolveLogEntry(text, type) {
  var log = document.getElementById("evolve-log");
  if (!log) return;
  if (!_evolveLogStartTime) _evolveLogStartTime = Date.now();
  var elapsed = ((Date.now() - _evolveLogStartTime) / 1000).toFixed(1);
  var entry = document.createElement("div");
  entry.className = "evo-log-entry type-" + (type || "info");
  entry.innerHTML = '<span class="evo-log-time">+' + elapsed + 's</span><span class="evo-log-text">' + esc(text) + '</span>';
  log.appendChild(entry);
  log.scrollTop = log.scrollHeight;
}

function addEvolveCycleHeader(num) {
  var log = document.getElementById("evolve-log");
  if (!log) return;
  var h = document.createElement("div"); h.className = "evo-log-cycle";
  h.textContent = "Cycle " + num;
  log.appendChild(h);
}

window.startTimedEvolution = function () {
  if (_evolutionRunning) return;
  // Disable cleanup button while evolving
  var hBtn = document.getElementById("health-btn"); if (hBtn) { hBtn.disabled = true; hBtn.style.opacity = "0.4"; }
  _evolutionRunning = true;
  _evolveStartedAt = Date.now();
  _evolveCycleCount = 0;
  _evolveLogStartTime = Date.now();
  window._allEvolveChanges = [];
  window._allEvolveQueued = 0;
  // Expand evolve section
  var evoSection = document.getElementById("ts-evolve");
  if (evoSection) evoSection.classList.remove("collapsed");
  var startBtn = document.getElementById("think-evo-start");
  var stopBtn = document.getElementById("think-evo-stop");
  var progress = document.getElementById("think-evo-progress");
  var diff = document.getElementById("evolution-diff-panel");
  var evoLog = document.getElementById("evolve-log");
  if (startBtn) startBtn.style.display = "none";
  if (stopBtn) stopBtn.style.display = "";
  if (progress) progress.style.display = "";
  if (diff) diff.style.display = "none";
  if (evoLog) evoLog.innerHTML = "";
  var sub = document.getElementById("ts-evolve-sub");
  if (sub) { sub.textContent = "Starting\u2026"; sub.style.color = "var(--purple)"; }
  var totalMin = _evolveDurationMs / 60000;
  var timerEl = document.getElementById("think-evo-timer");
  var barEl = document.getElementById("think-evo-bar");
  _evolveTimerInterval = setInterval(function () {
    var elapsed = Date.now() - _evolveStartedAt;
    var rem = Math.max(0, _evolveDurationMs - elapsed);
    var pct = Math.min(100, (elapsed / _evolveDurationMs) * 100);
    var m = Math.floor(rem / 60000), s = Math.floor((rem % 60000) / 1000);
    if (timerEl) timerEl.textContent = m + ":" + s.toString().padStart(2, "0") + " / " + totalMin + ":00";
    if (barEl) barEl.style.width = pct + "%";
    if (rem === 0) { clearInterval(_evolveTimerInterval); stopEvolution(); }
  }, 500);
  _runEvolveCycle();
};

function _runEvolveCycle() {
  if (!_evolutionRunning) return;
  if (Date.now() - _evolveStartedAt >= _evolveDurationMs) { _finishTimedEvo(); return; }
  _evolveCycleCount++;
  addEvolveCycleHeader(_evolveCycleCount);
  var sub = document.getElementById("ts-evolve-sub");
  if (sub) sub.textContent = "Cycle " + _evolveCycleCount + " running\u2026";
  var cycleEl = document.getElementById("think-evo-cycle-count");
  if (cycleEl) cycleEl.textContent = "Cycle " + _evolveCycleCount;
  var intensity = (document.getElementById("think-evo-intensity") || {}).value || "medium";
  var stepEl = document.getElementById("think-evo-current-step");
  _evolveES = new EventSource(API + "/api/evolve/stream?intensity=" + intensity);
  _evolveES.onmessage = function (e) {
    try {
      var ev = JSON.parse(e.data);
      if (ev.type === "snapshot") {
        addEvolveLogEntry(ev.nodes + " nodes \u00B7 " + ev.edges + " edges", "info");
      } else if (ev.type === "priority_done") {
        addEvolveLogEntry((ev.high_priority || 0) + " high-priority nodes", "find");
        if (ev.top3 && ev.top3.length) addEvolveLogEntry("Top: " + ev.top3.slice(0, 3).join(", "), "info");
      } else if (ev.type === "step") {
        addEvolveLogEntry((ev.label || ev.step) + "\u2026", "info");
        if (stepEl) stepEl.textContent = ev.label || ev.step;
      } else if (ev.type === "step_done") {
        var found = ev.found || 0, applied = ev.auto_apply || 0, queued = ev.needs_review || 0;
        addEvolveLogEntry(ev.step + ": " + (found > 0 ? applied + " applied \u00B7 " + queued + " queued" : "nothing new"), found > 0 ? "action" : "info");
      } else if (ev.type === "rollback") {
        addEvolveLogEntry("Rolled back \u2014 health degraded " + ev.delta + "%", "warn");
      } else if (ev.type === "stale") {
        _evolveES.close(); _evolveES = null;
        addEvolveLogEntry("Brain well-covered \u2014 pausing. Upload new docs to resume.", "warn");
        _evolutionRunning = false;
        clearInterval(_evolveTimerInterval);
        var _sb = document.getElementById("think-evo-start"); if (_sb) _sb.style.display = "";
        var _stb = document.getElementById("think-evo-stop"); if (_stb) _stb.style.display = "none";
        var _pr = document.getElementById("think-evo-progress"); if (_pr) _pr.style.display = "none";
        var _sub = document.getElementById("ts-evolve-sub");
        if (_sub) { _sub.textContent = "Paused \u2014 brain well-covered"; _sub.style.color = "var(--amber)"; }
        _finishTimedEvo();
      } else if (ev.type === "complete" || ev.type === "skipped") {
        _evolveES.close(); _evolveES = null;
        addEvolveLogEntry("Cycle done: " + (ev.changes_applied || 0) + " applied \u00B7 " + (ev.queued_for_review || 0) + " queued", (ev.changes_applied || 0) > 0 ? "action" : "info");
        if (ev.changes) window._allEvolveChanges = (window._allEvolveChanges || []).concat(ev.changes);
        window._allEvolveQueued = (window._allEvolveQueued || 0) + (ev.queued_for_review || 0);
        fetch(API + "/api/brain").then(function (r) { return r.json(); }).then(function (b) { Brain.updateGraph(b.nodes || [], b.links || []); });
        refreshStats(); updateActBadge();
        if (_evolutionRunning) setTimeout(_runEvolveCycle, 3000);
      }
    } catch (err) {}
  };
  _evolveES.onerror = function () { if (_evolveES) { _evolveES.close(); _evolveES = null; } if (_evolutionRunning) setTimeout(_runEvolveCycle, 5000); };
}

function _finishTimedEvo() {
  var changes = window._allEvolveChanges || [];
  var queued = window._allEvolveQueued || 0;
  window._allEvolveChanges = []; window._allEvolveQueued = 0;
  var sub = document.getElementById("ts-evolve-sub");
  if (sub) {
    sub.textContent = _evolveCycleCount + " cycles \u00B7 " + changes.length + " applied \u00B7 " + queued + " queued";
    sub.style.color = changes.length > 0 ? "var(--outcome)" : "var(--dim)";
  }
  _showEvoDiff({type: "complete", changes: changes, queued_for_review: queued, changes_applied: changes.length, health_delta: 0, elapsed: Math.round((Date.now() - (_evolveStartedAt || Date.now())) / 1000), rolled_back: false}, []);
  _evolveCycleCount = 0; _evolveStartedAt = null;
  // Re-enable cleanup button
  var hBtn = document.getElementById("health-btn"); if (hBtn) { hBtn.disabled = false; hBtn.style.opacity = ""; }
}

window.stopEvolution = function () {
  _evolutionRunning = false;
  clearInterval(_evolveTimerInterval);
  if (_evolveES) { _evolveES.close(); _evolveES = null; }
  var startBtn = document.getElementById("think-evo-start");
  var stopBtn = document.getElementById("think-evo-stop");
  var progress = document.getElementById("think-evo-progress");
  if (startBtn) startBtn.style.display = "";
  if (stopBtn) stopBtn.style.display = "none";
  if (progress) progress.style.display = "none";
  var sub = document.getElementById("ts-evolve-sub");
  if (sub) { sub.textContent = "Stopped"; sub.style.color = "var(--dim)"; }
  _finishTimedEvo();
};

// ── SUMMARY STATUS ───────────────────────────────────────
function loadSummaryStatus() {
  fetch(API + "/api/brain/summaries/status").then(function (r) { return r.json(); })
    .then(function (d) {
      var badge = document.getElementById("ts-summaries-badge");
      var btn = document.getElementById("summary-regen-btn");
      var rows = document.getElementById("summary-status-rows");
      var stale = d.stale || 0, missing = d.missing || 0, total = d.total || 0;
      var current = total - stale - missing;
      if (badge) {
        if (stale === 0 && missing === 0) { badge.textContent = "All current"; badge.style.color = "var(--outcome)"; }
        else { badge.textContent = (stale + missing) + " need update"; badge.style.color = "var(--amber)"; }
      }
      if (rows) {
        rows.innerHTML = _statusRow("\u25C9", "Current", current, "var(--outcome)") +
          _statusRow("\u25CB", "Stale", stale, "var(--amber)") +
          _statusRow("\u25CC", "Missing", missing, "var(--dim)");
      }
      if (btn) {
        var needs = stale + missing;
        if (needs > 0) { btn.style.display = ""; btn.textContent = "\u21BB Update " + needs + (needs === 1 ? " summary" : " summaries"); }
        else btn.style.display = "none";
      }
    }).catch(function () {});
}
function _statusRow(icon, label, count, color) {
  return '<div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:11px"><span style="color:' + color + ';font-size:10px;width:12px">' + icon + '</span><span style="color:var(--muted);flex:1">' + label + '</span><span style="font-family:monospace;font-size:10px;color:' + color + '">' + count + '</span></div>';
}

// ── STALE SUMMARY REGEN ──────────────────────────────────
window.startStaleSummaryRegen = function () {
  var btn = document.getElementById("summary-regen-btn");
  var status = document.getElementById("summary-regen-status");
  var actions = document.getElementById("summary-regen-actions");
  if (btn) { btn.disabled = true; btn.textContent = "\u21BB Generating\u2026"; }
  if (actions) actions.style.display = "none";
  var all = {};
  var es = new EventSource(API + "/api/brain/summaries/generate-stale");
  es.onmessage = function (e) {
    try {
      var ev = JSON.parse(e.data);
      if (ev.type === "start" && status) status.textContent = "Updating " + ev.total + " nodes\u2026";
      else if (ev.type === "summary") {
        all[ev.node_id] = ev.summary;
        if (status) status.textContent = "[" + (ev.index + 1) + "/" + ev.total + "] " + ev.node_label;
        if (btn) btn.textContent = "\u21BB " + Math.round((ev.index + 1) / ev.total * 100) + "%\u2026";
      } else if (ev.type === "complete") {
        es.close();
        if (btn) { btn.disabled = false; btn.textContent = "\u21BB Update stale summaries"; }
        var count = Object.keys(all).length;
        if (ev.total === 0) { if (status) status.textContent = "\u2713 All summaries current"; return; }
        if (status) status.textContent = count + " ready \u2014 save or discard";
        window._allGeneratedSummaries = all;
        if (actions) actions.style.display = "";
        var preview = document.getElementById("summary-regen-preview");
        if (preview) {
          var ids = Object.keys(all).slice(0, 3);
          preview.innerHTML = ids.map(function (nid) { var s = all[nid]; return '<div style="padding:8px 10px;border-bottom:1px solid var(--border);font-size:10px"><div style="color:var(--text);font-weight:600;margin-bottom:2px">' + esc(s.node_label || nid) + '</div><div style="color:var(--muted);line-height:1.5">' + esc(s.role || "") + '</div></div>'; }).join("") + (count > 3 ? '<div style="padding:6px 10px;font-size:9px;color:var(--dim)">\u2026 and ' + (count - 3) + ' more</div>' : '');
          preview.style.cssText = "border:1px solid var(--border);border-radius:8px;overflow:hidden;max-height:200px;overflow-y:auto";
        }
      }
    } catch (err) {}
  };
  es.onerror = function () { es.close(); if (btn) { btn.disabled = false; btn.textContent = "\u21BB Update stale summaries"; } if (status) status.textContent = "Failed"; };
};

}());

