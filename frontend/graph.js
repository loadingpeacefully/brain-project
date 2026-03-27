/* ── BRAIN GRAPH ENGINE ────────────────────────────────────────
   D3 v7 force-directed graph with Louvain community detection.
   Called by ui.js via updateGraph().
   ──────────────────────────────────────────────────────────── */

var Brain = Brain || {};

(function (B) {
  "use strict";

  var TC = {
    Feature:  "#4da6ff",
    Surface:  "#a87fff",
    Outcome:  "#4ecb8d",
    Decision: "#f0a040",
    Person:   "#ff8fab",
    Company:  "#7ec8e3",
    Concept:  "#e0e4f0",
  };

  var svgEl, gMain, sim, zoomBeh;
  var edgeSel, nodeSel;
  var hullLayer;
  var W, H;

  var brainNodes = [];
  var brainLinks = [];
  var degreeMap = {};
  var clusterPositions = {};
  var hiddenTypes = {};
  var NODE_TYPES_ORDER = ["Feature","Surface","Outcome","Decision","Person","Company","Concept"];
  var conceptExpanded = {};
  var memberToConceptMap = {};

  // ── INIT ─────────────────────────────────────────────────────
  B.initGraph = function () {
    var container = document.getElementById("right");
    W = container.clientWidth;
    H = container.clientHeight;

    svgEl = d3.select("#graph-svg");
    gMain = svgEl.append("g");

    // Hull layer BEHIND edges and nodes
    hullLayer = gMain.insert("g", ":first-child").attr("class", "hull-layer");

    // Arrow markers + hull blur filter
    var defs = svgEl.append("defs");
    [
      { id: "arr",  c: "rgba(255,255,255,0.3)" },
      { id: "arrc", c: "rgba(255,100,100,0.85)" },
      { id: "arrs", c: "rgba(168,127,255,0.4)" },
    ].forEach(function (m) {
      defs.append("marker")
        .attr("id", m.id)
        .attr("viewBox", "0 0 10 10")
        .attr("refX", 22).attr("refY", 5)
        .attr("markerWidth", 5).attr("markerHeight", 5)
        .attr("orient", "auto")
        .append("path")
        .attr("d", "M1 1L9 5L1 9")
        .attr("fill", "none").attr("stroke", m.c)
        .attr("stroke-width", 1.5)
        .attr("stroke-linecap", "round").attr("stroke-linejoin", "round");
    });
    var hullBlurDev = window.devicePixelRatio > 1 ? "10" : "15";
    defs.append("filter").attr("id", "hull-blur")
      .append("feGaussianBlur").attr("stdDeviation", hullBlurDev);

    // Zoom behaviour
    zoomBeh = d3.zoom()
      .scaleExtent([0.05, 4])
      .on("zoom", function (e) {
        gMain.attr("transform", e.transform);
        var k = e.transform.k;
        gMain.selectAll("text").style("display", function () {
          var pd = d3.select(this.parentNode).datum();
          if (!pd) return "block";
          var deg = degreeMap[pd.id] || 0;
          if (k < 0.3 && deg < 10) return "none";
          if (k < 0.5 && deg < 5) return "none";
          return "block";
        });
      });
    svgEl.call(zoomBeh);
    svgEl.call(zoomBeh.transform, d3.zoomIdentity.translate(W / 2, H / 2).scale(0.85));

    // Click background to deselect
    svgEl.on("click", function (e) {
      if (e.target === svgEl.node() || e.target.tagName === "svg") {
        B.clearSelection();
        if (window.closeNodePanel) window.closeNodePanel();
      }
    });

    // Edge and node layers (after hull layer)
    edgeSel = gMain.append("g").selectAll("line");
    nodeSel = gMain.append("g").selectAll("g.nd");

    // Simulation — tuned per graph layout research
    sim = d3.forceSimulation([])
      .force("link", d3.forceLink([])
        .id(function (d) { return d.id; })
        .distance(function (d) {
          if (d.rel === "has" || d.rel === "lives on") return 60;
          if (d.causal) return 125;
          return 95;
        })
        .strength(function (d) {
          if (d.causal) return 0.5;
          if (d.rel === "has") return 0.4;
          return 0.25;
        })
        .iterations(3)
      )
      .force("charge", d3.forceManyBody()
        .strength(-200)
        .distanceMin(10)
        .distanceMax(400)
      )
      .force("center", d3.forceCenter(0, 0))
      .force("collide", d3.forceCollide(function (d) {
        return (d.sz || 12) * 2 + 8;
      }).iterations(2))
      .force("x", d3.forceX(function (d) {
        return (clusterPositions[d.community] || {x: 0}).x;
      }).strength(0.08))
      .force("y", d3.forceY(function (d) {
        return (clusterPositions[d.community] || {y: 0}).y;
      }).strength(0.08))
      .alphaDecay(0.015);

    sim.on("tick", onTick);
  };

  // ── HULL DRAWING ────────────────────────────────────────────
  function drawHulls() {
    if (!hullLayer) return;
    hullLayer.selectAll("*").remove();
    var HULL_COLORS = [
      "rgba(77,166,255,0.05)", "rgba(168,127,255,0.05)",
      "rgba(78,203,141,0.05)", "rgba(240,160,64,0.05)",
      "rgba(255,143,171,0.05)", "rgba(126,200,227,0.05)"
    ];
    var groups = {};
    brainNodes.forEach(function (n) {
      if (!n.x || !n.y) return;
      if (!groups[n.community]) groups[n.community] = [];
      groups[n.community].push([n.x, n.y]);
    });
    Object.keys(groups).forEach(function (cid, i) {
      var pts = groups[cid];
      if (pts.length < 3) return;
      var hull = d3.polygonHull(pts);
      if (!hull || hull.length < 3) return;
      var cx = d3.mean(hull, function (p) { return p[0]; });
      var cy = d3.mean(hull, function (p) { return p[1]; });
      var padded = hull.map(function (p) {
        var dx = p[0] - cx, dy = p[1] - cy;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        return [p[0] + (dx / dist) * 30, p[1] + (dy / dist) * 30];
      });
      var pathData = "M" + padded.map(function (p) {
        return p[0].toFixed(1) + "," + p[1].toFixed(1);
      }).join("L") + "Z";
      hullLayer.append("path")
        .attr("d", pathData)
        .attr("fill", HULL_COLORS[i % HULL_COLORS.length])
        .attr("filter", "url(#hull-blur)")
        .attr("stroke", "none");
    });
  }
  B.drawHulls = drawHulls;

  // ── UPDATE ────────────────────────────────────────────────────
  B.updateGraph = function (nodes, links) {
    console.log('[graph] updateGraph called, nodes:', nodes.length, 'links:', links.length);
    brainNodes = nodes;
    brainLinks = links;

    // Compute degree map
    degreeMap = {};
    brainNodes.forEach(function (n) { degreeMap[n.id] = 0; });
    brainLinks.forEach(function (l) {
      var sid = typeof l.source === "object" ? l.source.id : l.source;
      var tid = typeof l.target === "object" ? l.target.id : l.target;
      if (sid in degreeMap) degreeMap[sid]++;
      if (tid in degreeMap) degreeMap[tid]++;
    });

    // ── Louvain community detection ──
    var communityMap = {};
    if (typeof graphology !== "undefined" && typeof graphologyLibrary !== "undefined") {
      try {
        var g = new graphology.UndirectedGraph();
        brainNodes.forEach(function (n) { g.addNode(n.id); });
        brainLinks.forEach(function (l) {
          var sid = typeof l.source === "object" ? l.source.id : l.source;
          var tid = typeof l.target === "object" ? l.target.id : l.target;
          if (g.hasNode(sid) && g.hasNode(tid) && !g.hasEdge(sid, tid)) {
            g.addEdge(sid, tid);
          }
        });
        communityMap = graphologyLibrary.communitiesLouvain(g);
        console.log("[graph] Louvain detected communities");
      } catch (e) {
        console.warn("[graph] Louvain failed:", e);
      }
    }
    brainNodes.forEach(function (n) {
      n.community = communityMap[n.id] !== undefined ? communityMap[n.id] : 0;
    });

    // Compute cluster center positions radially
    var uniqueComms = [], seenComms = {};
    brainNodes.forEach(function (n) {
      if (!seenComms[n.community]) { uniqueComms.push(n.community); seenComms[n.community] = true; }
    });
    clusterPositions = {};
    uniqueComms.forEach(function (c, i) {
      var angle = (i / uniqueComms.length) * 2 * Math.PI;
      clusterPositions[c] = {
        x: 250 * Math.cos(angle),
        y: 250 * Math.sin(angle)
      };
    });

    // Compute node visual properties
    brainNodes.forEach(function (n) {
      var degree = degreeMap[n.id] || 0;
      var conf = n.confidence_score || 0.3;
      var baseSize = {Concept: 22, Company: 18, Surface: 14, Feature: 12, Outcome: 11, Decision: 11, Person: 10}[n.type] || 10;
      n.sz = Math.min(baseSize + degree * 1.2, 28);
      if (n.memory_type === "semantic") {
        n.viz_opacity = Math.min(0.35 + conf * 0.6 + Math.min(degree * 0.05, 0.2), 1.0);
      } else {
        n.viz_opacity = Math.min(0.35 + conf * 0.4 + Math.min(degree * 0.03, 0.1), 0.70);
      }
      n.show_label = degree >= 3 || n.type === "Company" || n.type === "Person" || n.type === "Concept";
      var mc = Object.keys(n.metrics || {}).length;
      var dl = (n.desc || "").length;
      n.depth_score = Math.min((degree * 0.15) + (mc * 0.1) + (Math.min(dl / 200, 0.3)) + (conf * 0.3), 1.0);

      // Pre-position near cluster center
      delete n.vx; delete n.vy; delete n.fx; delete n.fy;
      var cp = clusterPositions[n.community] || {x: 0, y: 0};
      n.x = cp.x + (Math.random() - 0.5) * 60;
      n.y = cp.y + (Math.random() - 0.5) * 60;
    });

    // Filter orphan links
    var validIds = {};
    brainNodes.forEach(function (n) { validIds[n.id] = true; });
    brainLinks = brainLinks.filter(function (l) {
      var sid = typeof l.source === "object" ? l.source.id : l.source;
      var tid = typeof l.target === "object" ? l.target.id : l.target;
      return validIds[sid] && validIds[tid];
    });

    // Update cluster gravity forces
    sim.force("x").x(function (d) { return (clusterPositions[d.community] || {x: 0}).x; });
    sim.force("y").y(function (d) { return (clusterPositions[d.community] || {y: 0}).y; });

    // ── Build concept membership map ──
    memberToConceptMap = {};
    var conceptIds = {};
    brainNodes.forEach(function (n) { if (n.type === "Concept") conceptIds[n.id] = true; });
    brainLinks.forEach(function (l) {
      var src = typeof l.source === "object" ? l.source.id : l.source;
      var tgt = typeof l.target === "object" ? l.target.id : l.target;
      if ((l.rel || "").toLowerCase() === "instance_of") {
        if (conceptIds[tgt]) memberToConceptMap[src] = tgt;
        else if (conceptIds[src]) memberToConceptMap[tgt] = src;
      }
    });
    brainNodes.forEach(function (n) {
      if (n.type === "Concept" && conceptExpanded[n.id] === undefined) conceptExpanded[n.id] = false;
    });

    // Filter visibility based on concept collapse state
    var hiddenByConceptIds = {};
    brainNodes.forEach(function (n) {
      var cid = memberToConceptMap[n.id];
      if (cid && !conceptExpanded[cid]) hiddenByConceptIds[n.id] = true;
    });
    var simNodes = brainNodes.filter(function (n) { return !hiddenByConceptIds[n.id]; });
    var simLinks = brainLinks.filter(function (l) {
      var src = typeof l.source === "object" ? l.source.id : l.source;
      var tgt = typeof l.target === "object" ? l.target.id : l.target;
      if (hiddenByConceptIds[src] || hiddenByConceptIds[tgt]) return false;
      return true;
    });

    // ── EDGES ──
    var edgeG = gMain.select("g:nth-child(2)");
    edgeSel = edgeG.selectAll("line").data(simLinks, function (d) {
      return d.source + "_" + d.target + "_" + d.rel;
    });
    edgeSel.exit().remove();
    edgeSel = edgeSel.enter().append("line")
      .attr("stroke", function (d) {
        if (d.causal) return "rgba(255,100,100,0.65)";
        if (d.memory_type === "semantic") return "rgba(255,255,255,0.12)";
        return "rgba(255,255,255,0.04)";
      })
      .attr("stroke-width", function (d) {
        if (d.causal) return 2;
        var w = d.weight || 1.0;
        return Math.max(0.5, Math.min(w * 1.2, 2.5));
      })
      .attr("stroke-opacity", function (d) {
        if (d.causal) return 0.65;
        var w = d.weight || 1.0;
        var base = d.memory_type === "semantic" ? 0.15 : 0.06;
        return Math.min(base * w, 0.5);
      })
      .attr("marker-end", function (d) {
        return d.causal ? "url(#arrc)" : d.rel === "has" ? "url(#arrs)" : "url(#arr)";
      })
      .merge(edgeSel);

    // ── NODES ──
    var nodeG = gMain.select("g:last-child");
    nodeSel = nodeG.selectAll("g.nd").data(simNodes, function (d) { return d.id; });
    nodeSel.exit().remove();

    var enter = nodeSel.enter().append("g")
      .attr("class", "nd")
      .style("cursor", "pointer")
      .call(d3.drag()
        .on("start", function (ev, d) {
          if (!ev.active) sim.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        })
        .on("drag", function (ev, d) { d.fx = ev.x; d.fy = ev.y; })
        .on("end", function (ev, d) {
          if (!ev.active) sim.alphaTarget(0);
          d.fx = null; d.fy = null;
          setTimeout(drawHulls, 600);
        })
      )
      .on("mouseover", onNodeHover)
      .on("mousemove", onNodeMove)
      .on("mouseleave", onNodeLeave)
      .on("click", onNodeClick);

    enter.each(function (d) {
      var el = d3.select(this);
      var col = TC[d.type] || "#888";
      var r = d.sz;

      el.attr("opacity", d.viz_opacity);

      if (d.type === "Concept") {
        var hp = [], isExp = conceptExpanded[d.id];
        for (var hi = 0; hi < 6; hi++) { var ha = (Math.PI / 3) * hi - Math.PI / 6; hp.push(r * Math.cos(ha) + "," + r * Math.sin(ha)); }
        el.append("polygon").attr("points", hp.join(" ")).attr("fill", col + "22").attr("stroke", col).attr("stroke-width", 2);
        var hp2 = [];
        for (var hi2 = 0; hi2 < 6; hi2++) { var ha2 = (Math.PI / 3) * hi2 - Math.PI / 6; hp2.push((r * 0.6 * Math.cos(ha2)) + "," + (r * 0.6 * Math.sin(ha2))); }
        el.append("polygon").attr("points", hp2.join(" ")).attr("fill", isExp ? col + "44" : col + "18").attr("stroke", "none");
        // Member count in center
        var mc = Object.keys(memberToConceptMap).filter(function (mid) { return memberToConceptMap[mid] === d.id; }).length;
        if (mc > 0) el.append("text").attr("text-anchor", "middle").attr("dy", "0.35em").attr("fill", col).attr("font-size", 9).attr("font-family", "monospace").attr("pointer-events", "none").text(mc);
        // Expand/collapse hint above hexagon
        el.append("text").attr("text-anchor", "middle").attr("dy", -(r + 4)).attr("fill", "rgba(224,228,240,.5)").attr("font-size", 8).attr("pointer-events", "none").text(isExp ? "\u25BE collapse" : "\u25B8 " + mc + " nodes");
      } else if (d.type === "Surface") {
        el.append("rect")
          .attr("x", -r).attr("y", -r * 0.55)
          .attr("width", r * 2).attr("height", r * 1.1).attr("rx", 4)
          .attr("fill", col + "14").attr("stroke", col).attr("stroke-width", 0.6);
      } else {
        el.append("circle").attr("r", r)
          .attr("fill", col + "14").attr("stroke", col)
          .attr("stroke-width", d.type === "Company" ? 1.2 : 0.7);
        if (d.type === "Company") {
          el.append("circle").attr("r", r * 0.45)
            .attr("fill", col + "22").attr("stroke", "none");
        }
      }

      if (d.depth_score > 0.2) {
        el.append("circle")
          .attr("r", r + 2).attr("fill", "none")
          .attr("stroke", col).attr("stroke-width", d.depth_score * 2.5)
          .attr("opacity", 0.3);
      }

      if (d.show_label) {
        var lbl = d.label.length > 16 ? d.label.slice(0, 14) + "\u2026" : d.label;
        el.append("text")
          .attr("text-anchor", "middle")
          .attr("dy", r + 10)
          .attr("fill", col)
          .attr("font-size", Math.max(d.sz * 0.65, 8))
          .attr("font-family", "-apple-system, sans-serif")
          .attr("font-weight", "500")
          .attr("pointer-events", "none")
          .text(lbl);
      }
    });

    nodeSel = enter.merge(nodeSel);

    sim.nodes(simNodes);
    sim.force("link").links(simLinks);
    sim.alpha(0.4).restart();

    setTimeout(function () {
      if (brainNodes.length > 0) {
        Brain.zoomFit();
        drawHulls();
      }
    }, 1200);

    B.renderLegend();
    B.applyTypeFilter();
  };

  // ── TICK ──────────────────────────────────────────────────────
  function onTick() {
    if (!edgeSel || !nodeSel) return;
    if (!edgeSel.data || !edgeSel.data().length) return;
    edgeSel
      .attr("x1", function (d) { return d.source.x; })
      .attr("y1", function (d) { return d.source.y; })
      .attr("x2", function (d) { return d.target.x; })
      .attr("y2", function (d) { return d.target.y; });
    nodeSel.attr("transform", function (d) {
      return "translate(" + d.x + "," + d.y + ")";
    });
  }

  // ── TOOLTIP ───────────────────────────────────────────────────
  var ntip = document.getElementById("ntip");
  var ntipName = document.getElementById("ntip-name");
  var ntipType = document.getElementById("ntip-type");
  var ntipWhere = document.getElementById("ntip-where");

  function onNodeHover(ev, d) {
    ntip.classList.add("show");
    ntipName.textContent = d.label;
    if (d.type === "Concept") {
      var membs = Object.keys(memberToConceptMap).filter(function (mid) { return memberToConceptMap[mid] === d.id; });
      var mLabels = membs.map(function (mid) { var n = brainNodes.find(function (bn) { return bn.id === mid; }); return n ? n.label : mid; });
      ntipType.textContent = "CONCEPT \u00B7 " + membs.length + " members";
      ntipWhere.textContent = conceptExpanded[d.id] ? "Click to collapse" : "Click to expand \u00B7 " + mLabels.slice(0, 3).join(", ") + (mLabels.length > 3 ? " +" + (mLabels.length - 3) + " more" : "");
    } else {
      ntipType.textContent = d.type;
      var deg = degreeMap[d.id] || 0;
      var wStr = d.where || "";
      ntipWhere.textContent = wStr ? wStr + " \u00B7 " + deg + " connections" : deg + " connections";
    }
  }
  function onNodeMove(ev) {
    ntip.style.left = (ev.clientX + 14) + "px";
    ntip.style.top = (ev.clientY - 28) + "px";
  }
  function onNodeLeave() { ntip.classList.remove("show"); }

  // ── CLICK → HIGHLIGHT + PANEL ─────────────────────────────────
  function onNodeClick(ev, d) {
    ev.stopPropagation();
    // Concept expand/collapse
    if (d.type === "Concept") {
      conceptExpanded[d.id] = !conceptExpanded[d.id];
      B.updateGraph(brainNodes, brainLinks);
      if (!conceptExpanded[d.id]) return; // collapsed — don't open panel
    }
    var connected = {};
    connected[d.id] = true;
    brainLinks.forEach(function (l) {
      var sid = typeof l.source === "object" ? l.source.id : l.source;
      var tid = typeof l.target === "object" ? l.target.id : l.target;
      if (sid === d.id) connected[tid] = true;
      if (tid === d.id) connected[sid] = true;
    });
    // Trace downstream causal chain (CCP blast radius)
    var causalDown = {};
    function traceCausal(nid, depth) {
      if (depth > 4 || causalDown[nid] !== undefined) return;
      causalDown[nid] = depth;
      brainLinks.forEach(function (l) {
        var sid = typeof l.source === "object" ? l.source.id : l.source;
        var tid = typeof l.target === "object" ? l.target.id : l.target;
        if (l.causal && sid === nid) traceCausal(tid, depth + 1);
      });
    }
    traceCausal(d.id, 0);
    nodeSel.style("opacity", function (n) {
      if (n.id === d.id) return 1.0;
      if (causalDown[n.id] !== undefined) return Math.max(0.4, 1.0 - causalDown[n.id] * 0.2);
      if (connected[n.id]) return 0.8;
      return 0.07;
    });
    edgeSel.style("opacity", function (l) {
      var sid = typeof l.source === "object" ? l.source.id : l.source;
      var tid = typeof l.target === "object" ? l.target.id : l.target;
      if (sid === d.id || tid === d.id) return 1.0;
      if (l.causal && causalDown[sid] !== undefined) return 0.8;
      return l.causal ? 0.15 : 0.03;
    });
    B.applyTypeFilter(); // re-apply type filter on top of selection
    if (window.openNodePanel) window.openNodePanel(d, brainNodes, brainLinks);
  }

  B.clearSelection = function () {
    if (nodeSel) nodeSel.style("opacity", function (d) { return d.viz_opacity || 1; });
    if (edgeSel) edgeSel.style("opacity", 1);
    B.applyTypeFilter(); // restore type filter after deselect
  };

  // ── ZOOM ──────────────────────────────────────────────────────
  B.zoomIn = function () { svgEl.transition().duration(250).call(zoomBeh.scaleBy, 1.4); };
  B.zoomOut = function () { svgEl.transition().duration(250).call(zoomBeh.scaleBy, 0.7); };
  B.zoomFit = function () {
    var container = document.getElementById("right");
    W = container.clientWidth;
    H = container.clientHeight;
    svgEl.transition().duration(350).call(
      zoomBeh.transform,
      d3.zoomIdentity.translate(W / 2, H / 2).scale(0.85)
    );
  };

  B.getTypeColor = function (type) { return TC[type] || "#888"; };

  // ── LEGEND FILTER ───────────────────────────────────────────
  B.renderLegend = function () {
    var container = document.getElementById("legend-node-filters");
    if (!container) return;
    container.innerHTML = "";
    var typeCounts = {}, typeVisible = {};
    brainNodes.forEach(function (n) {
      typeCounts[n.type] = (typeCounts[n.type] || 0) + 1;
      var cid = memberToConceptMap[n.id];
      if (!cid || conceptExpanded[cid]) typeVisible[n.type] = (typeVisible[n.type] || 0) + 1;
      else typeVisible[n.type] = typeVisible[n.type] || 0;
    });
    NODE_TYPES_ORDER.forEach(function (type) {
      var col = TC[type] || "#888";
      var isHidden = hiddenTypes[type];
      var total = typeCounts[type] || 0;
      var vis = typeVisible[type] || 0;
      var count = total;
      var countStr = vis < total ? vis + "/" + total : String(total);
      var chip = document.createElement("div");
      chip.className = "legend-filter-chip" + (isHidden ? " hidden-type" : "");
      chip.dataset.type = type;
      chip.title = isHidden ? "Click to show " + type + " (" + total + ")" : "Click to hide " + type + " (" + countStr + ")";
      chip.innerHTML =
        '<span class="legend-dot" style="background:' + (isHidden ? "rgba(255,255,255,.15)" : col) + '"></span>' +
        '<span class="legend-type-label" style="color:' + (isHidden ? "var(--dim)" : "var(--muted)") + '">' + type + ' <span style="color:var(--dim)">(' + countStr + ')</span></span>' +
        (isHidden ? '<span class="legend-hidden-icon">\u2715</span>' : '');
      chip.addEventListener("click", function () {
        if (hiddenTypes[type]) delete hiddenTypes[type];
        else hiddenTypes[type] = true;
        B.applyTypeFilter();
        B.renderLegend();
      });
      container.appendChild(chip);
    });
  };

  B.applyTypeFilter = function () {
    if (!nodeSel || !edgeSel) return;
    nodeSel.style("display", function (d) { return hiddenTypes[d.type] ? "none" : null; });
    edgeSel.style("display", function (l) {
      var sid = typeof l.source === "object" ? l.source.id : l.source;
      var tid = typeof l.target === "object" ? l.target.id : l.target;
      var srcNode = brainNodes.find(function (n) { return n.id === sid; });
      var tgtNode = brainNodes.find(function (n) { return n.id === tid; });
      if (srcNode && hiddenTypes[srcNode.type]) return "none";
      if (tgtNode && hiddenTypes[tgtNode.type]) return "none";
      return null;
    });
  };

  // ── REARRANGE ─────────────────────────────────────────────────
  B.rearrange = function () {
    if (!brainNodes || !brainNodes.length) return;
    var btn = document.querySelector('.gc-btn[title*="Rearrange"]');
    if (btn) { btn.style.opacity = "0.4"; btn.style.pointerEvents = "none"; }

    // Recompute communities fresh
    var communityMap = {};
    if (typeof graphology !== "undefined" && typeof graphologyLibrary !== "undefined") {
      try {
        var g = new graphology.UndirectedGraph();
        brainNodes.forEach(function (n) { g.addNode(n.id); });
        brainLinks.forEach(function (l) {
          var sid = typeof l.source === "object" ? l.source.id : l.source;
          var tid = typeof l.target === "object" ? l.target.id : l.target;
          if (g.hasNode(sid) && g.hasNode(tid) && !g.hasEdge(sid, tid)) g.addEdge(sid, tid);
        });
        communityMap = graphologyLibrary.communitiesLouvain(g);
      } catch (e) { console.warn("[rearrange] Louvain:", e); }
    }
    brainNodes.forEach(function (n) {
      n.community = communityMap[n.id] !== undefined ? communityMap[n.id] : 0;
    });

    // Recompute cluster centers
    var uniqueComms = [], seenComms = {};
    brainNodes.forEach(function (n) {
      if (!seenComms[n.community]) { uniqueComms.push(n.community); seenComms[n.community] = true; }
    });
    var container = document.getElementById("right");
    var radius = Math.min(container ? container.clientWidth : 800, container ? container.clientHeight : 600) * 0.28;
    clusterPositions = {};
    uniqueComms.forEach(function (c, i) {
      var angle = (i / uniqueComms.length) * 2 * Math.PI - Math.PI / 2;
      clusterPositions[c] = { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
    });

    // Reset positions near cluster centers
    brainNodes.forEach(function (n) {
      delete n.fx; delete n.fy;
      var cp = clusterPositions[n.community] || {x: 0, y: 0};
      n.x = cp.x + (Math.random() - 0.5) * 80;
      n.y = cp.y + (Math.random() - 0.5) * 80;
      n.vx = 0; n.vy = 0;
    });

    // Update force targets
    if (sim.force("x")) sim.force("x").x(function (d) { return (clusterPositions[d.community] || {x: 0}).x; });
    if (sim.force("y")) sim.force("y").y(function (d) { return (clusterPositions[d.community] || {y: 0}).y; });

    sim.alpha(0.8).alphaTarget(0).restart();
    setTimeout(function () {
      Brain.zoomFit(); drawHulls();
      if (btn) { btn.style.opacity = ""; btn.style.pointerEvents = ""; }
    }, 1600);
  };

  // ── HIGHLIGHT ─────────────────────────────────────────────────
  B.highlightNodes = function (nodeIds) {
    if (!nodeSel || !edgeSel) return;
    var idSet = {};
    nodeIds.forEach(function (id) { idSet[id] = true; });
    nodeSel.style("opacity", function (d) { return idSet[d.id] ? 1 : 0.15; });
    nodeSel.each(function (d) {
      if (idSet[d.id]) {
        d3.select(this).raise();
        var col = TC[d.type] || "#888";
        d3.select(this).append("circle")
          .attr("r", d.sz + 6).attr("fill", "none")
          .attr("stroke", col).attr("stroke-width", 1.5).attr("opacity", 0.7)
          .attr("class", "cite-ring")
          .transition().duration(4000).attr("opacity", 0).attr("r", d.sz + 12).remove();
      }
    });
    edgeSel.style("opacity", function (l) {
      var sid = typeof l.source === "object" ? l.source.id : l.source;
      var tid = typeof l.target === "object" ? l.target.id : l.target;
      return (idSet[sid] || idSet[tid]) ? 0.8 : 0.05;
    });
  };

  B.clearHighlight = B.clearSelection;

  B.pulseNodes = function (ids) {
    if (!nodeSel) return;
    var idSet = {};
    ids.forEach(function (id) { idSet[id] = true; });
    nodeSel.filter(function (d) { return idSet[d.id]; })
      .each(function (d) {
        var el = d3.select(this);
        var col = TC[d.type] || "#888";
        var r = d.sz || 12;
        el.append("circle")
          .attr("r", r + 3).attr("fill", "none")
          .attr("stroke", col).attr("stroke-width", 1.5).attr("opacity", 0.8)
          .attr("class", "new-node-ring");
        setTimeout(function () { el.select(".new-node-ring").remove(); }, 1200);
        el.select("circle:first-child, rect:first-child")
          .transition().duration(150).attr("fill", col + "40")
          .transition().duration(800).ease(d3.easeCubicOut).attr("fill", col + "14");
      });
    if (edgeSel) {
      edgeSel.filter(function (l) {
        var sid = typeof l.source === "object" ? l.source.id : l.source;
        var tid = typeof l.target === "object" ? l.target.id : l.target;
        return idSet[sid] || idSet[tid];
      })
        .transition().duration(200).attr("stroke-width", 2.5)
        .transition().duration(1000).ease(d3.easeCubicOut)
        .attr("stroke-width", function (d) { return d.causal ? 2 : 0.5; });
    }
  };

  // ── SEARCH ─────────────────────────────────────────────────
  B.searchNodes = function (query) {
    var clearEl = document.getElementById("node-search-clear");
    var countEl = document.getElementById("node-search-count");
    if (!nodeSel || !edgeSel) return;
    if (!query || !query.trim()) {
      if (clearEl) clearEl.style.display = "none";
      if (countEl) countEl.textContent = "";
      if (nodeSel) nodeSel.style("opacity", function (d) { return d.viz_opacity || 1; });
      if (edgeSel) edgeSel.style("opacity", function (l) { return l.causal ? 0.65 : 0.04; });
      return;
    }
    if (clearEl) clearEl.style.display = "";
    var q = query.toLowerCase();
    var matchIds = {}, matchCount = 0;
    brainNodes.forEach(function (n) {
      if (n.label.toLowerCase().indexOf(q) >= 0 ||
          (n.desc || "").toLowerCase().indexOf(q) >= 0 ||
          (n.type || "").toLowerCase().indexOf(q) >= 0) {
        matchIds[n.id] = true; matchCount++;
      }
    });
    if (countEl) countEl.textContent = matchCount + " found";
    nodeSel.style("opacity", function (d) { return matchIds[d.id] ? 1 : 0.06; });
    edgeSel.style("opacity", function (l) {
      var sid = typeof l.source === "object" ? l.source.id : l.source;
      var tid = typeof l.target === "object" ? l.target.id : l.target;
      return (matchIds[sid] && matchIds[tid]) ? 0.6 : 0.02;
    });
  };

  // Stub for agent highlighting
  B.highlightForAgent = function () {};

}(Brain));
