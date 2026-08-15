{% raw %}
(function () {
  'use strict';
  var SVG = 'http://www.w3.org/2000/svg';
  var W = 176, H = 64;                 // node card size
  var KIND_ICON = { agent: '🧠', action: '⚙️', branch: '🔀', switch: '🔃',
                    foreach: '🔁', while: '🔄', parallel: '🔱', approval: '✅',
                    delay: '⏱', wait_event: '📥', scope: '🛡', subflow: '📎', setvar: '📝' };
  var BODY_KINDS = { foreach: '🔁 loop body', while: '🔄 while body', scope: '🛡 try body' };

  var flow = { id: '', name: '', start: '', nodes: [] };
  var rootFlow = flow;  // the top-level flow (save/run/history operate on this)
  var stack = [];       // drill-in frames: {flow, selected} for each level above root
  var crumbs = ['Main flow'];   // breadcrumb labels; length === stack.length + 1
  var proposals = {};   // node_id -> {to_kind, reason, inferred_tool} from grounded per-node outcomes
  var impacts = {};     // node_id -> {changed, applied, impact} measured before/after a rewrite
  var signals = { nodes: {}, trigger: null };   // grounded per-node stats + trigger reliability
  var runState = {};    // node_id -> {status, outcome} from the last/live run
  var runCursor = null; // node a paused run is waiting on
  var flowInputs = [];  // declared manual-run inputs [{key,type,label,required,default}]
  var toolCatalog = {}; // tool name -> {description, params[], category} for the action picker
  var toolCategories = [];   // display-ordered category names from the server
  var toolFilterCategory = ''; // '' = all; else browse one category in the picker
  var flowSchema = {};  // output key -> {type, keys[]} learned from real runs (nested pills)
  var selected = null;
  var multiSel = {};        // node id -> true when marquee-selected (group drag/delete)
  var selectedEdge = null;  // {from, port} when an edge is selected (click to select, Del to cut)
  var view = { x: 40, y: 20, k: 1 };
  var idSeq = 0;
  var undoStack = [], redoStack = [], pendingSnap = null;   // serialized rootFlow snapshots
  var chatHistory = [];     // copilot transcript: {role, content}
  var lastRunId = null;     // most recent run started here (grounds "why did it fail?")
  var runGen = 0;           // bumped per run so a superseded watch stops painting
  var activeES = null;      // the live run's EventSource, closed when a new run starts

  var canvas = document.getElementById('fd-canvas');
  var vp = document.getElementById('fd-viewport');
  var gNodes = document.getElementById('fd-nodes');
  var gEdges = document.getElementById('fd-edges');
  var tempEdge = document.getElementById('fd-temp-edge');
  var props = document.getElementById('fd-props');
  var msg = document.getElementById('fd-msg');
  var nameInput = document.getElementById('fd-name');
  var idInput = document.getElementById('fd-id');

  function el(tag, attrs, text) {
    var e = document.createElementNS(SVG, tag);
    for (var k in (attrs || {})) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }

  // ---- undo/redo: whole-root snapshots (the same serialized form a save uses),
  // so a chat edit, a draft, or a hand edit all undo the same way ----
  function snapState() {
    return JSON.stringify(FDCore.cloneFlow({
      id: idInput.value || rootFlow.id, name: nameInput.value || rootFlow.name,
      start: rootFlow.start, nodes: rootFlow.nodes }));
  }
  function pushHistory() {
    var s = snapState();
    if (undoStack[undoStack.length - 1] === s) return;
    undoStack.push(s);
    if (undoStack.length > 50) undoStack.shift();
    redoStack = [];
  }
  function histFocus() { pendingSnap = snapState(); }
  function histBlur() {
    if (pendingSnap && pendingSnap !== snapState()) {
      undoStack.push(pendingSnap);
      if (undoStack.length > 50) undoStack.shift();
      redoStack = [];
    }
    pendingSnap = null;
  }
  function restoreState(s) {
    var f = JSON.parse(s);
    stack = []; crumbs = ['Main flow']; renderCrumbs();
    flow = { id: f.id || '', name: f.name || '', start: f.start || '', nodes: [] };
    (f.nodes || []).forEach(function (nd, i) { flow.nodes.push(normalizeNode(nd, i)); });
    rootFlow = flow;
    nameInput.value = flow.name; idInput.value = flow.id;
    selected = null; selectedEdge = null;
    autoLayout(); render(); renderProps();
  }
  function undo() {
    if (!undoStack.length) { say('Nothing to undo.'); return; }
    redoStack.push(snapState());
    restoreState(undoStack.pop());
    say('↶ undone');
  }
  function redo() {
    if (!redoStack.length) { say('Nothing to redo.'); return; }
    undoStack.push(snapState());
    restoreState(redoStack.pop());
    say('↷ redone');
  }
  function nodeById(id) { for (var i = 0; i < flow.nodes.length; i++) if (flow.nodes[i].id === id) return flow.nodes[i]; return null; }
  function newId() { var id; do { id = 'n' + (idSeq++); } while (nodeById(id)); return id; }
  function say(t, err) { msg.textContent = t || ''; msg.style.color = err ? 'var(--danger)' : 'var(--muted)'; }
  function trunc(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

  function summary(n) {
    if (n.kind === 'agent') return trunc(n.brief || '(no brief)', 30);
    if (n.kind === 'action') return trunc(n.tool || '(no tool)', 30);
    if (n.kind === 'branch') return trunc(n.condition || '(condition)', 30);
    if (n.kind === 'switch') return 'on ' + trunc(n.condition || '…', 16) + ' (' + ((n.cases || []).length) + ' cases)';
    if (n.kind === 'foreach') return 'for each ' + trunc(n.items || '…', 20) + (n.concurrent ? ' ∥' : '');
    if (n.kind === 'while') return 'while ' + trunc(n.condition || '…', 24);
    if (n.kind === 'parallel') return (n.branches ? n.branches.length : 0) + ' branches';
    if (n.kind === 'approval') return trunc(n.prompt || '(approve?)', 30);
    if (n.kind === 'delay') return (n.seconds || 0) + 's';
    if (n.kind === 'wait_event') return trunc(n.prompt || '(wait for event)', 30);
    if (n.kind === 'scope') return 'try ' + (((n.body || {}).nodes || []).length) + ' step(s)';
    if (n.kind === 'subflow') return '↳ ' + trunc(n.flow_ref || '(flow)', 24);
    if (n.kind === 'switch') return (n.cases ? n.cases.length : 0) + ' cases';
    if (n.kind === 'setvar') return 'set ' + trunc(Object.keys(n.assignments || {}).join(', ') || '…', 22);
    if (n.kind === 'try') return 'try / catch';
    return n.kind;
  }
  function ports(n) {
    if (n.kind === 'branch') return [
      { key: 'if_true', x: 50, cls: 't', label: 'yes' },
      { key: 'if_false', x: W - 50, cls: 'f', label: 'no' }];
    return [{ key: 'next', x: W / 2, cls: '', label: '' }];
  }
  function runClass(id) {
    if (id === runCursor) return ' run-active';        // where the run is waiting
    var rs = runState[id]; if (!rs) return '';
    if (rs.outcome === 1) return ' run-ok';
    if (rs.outcome === 0) return ' run-err';
    return ' run-info';                                // a control step that ran
  }
  function runBadge(id) {
    if (id === runCursor) return '⏳';
    var rs = runState[id]; if (!rs) return '';
    if (rs.outcome === 1) return '🟢';
    if (rs.outcome === 0) return '🔴';
    if (rs.status === 'true') return '✓';
    if (rs.status === 'false') return '✗';
    return '🟠';
  }

  function render() {
    vp.setAttribute('transform', 'translate(' + view.x + ',' + view.y + ') scale(' + view.k + ')');
    gEdges.textContent = ''; gNodes.textContent = '';
    // edges
    flow.nodes.forEach(function (n) {
      ports(n).forEach(function (p) {
        var tgt = n[p.key]; if (!tgt) return;
        var t = nodeById(tgt); if (!t) return;
        var px = n.x + p.x, py = n.y + H, tx = t.x + W / 2, ty = t.y;
        var d = 'M' + px + ' ' + py + ' C ' + px + ' ' + (py + 46) + ', ' + tx + ' ' + (ty - 46) + ', ' + tx + ' ' + ty;
        var isSel = selectedEdge && selectedEdge.from === n.id && selectedEdge.port === p.key;
        gEdges.appendChild(el('path', { class: 'fd-edge ' + p.cls + (isSel ? ' selected' : ''), d: d }));
        // invisible fat twin so a 2px curve is actually clickable (click = select,
        // Delete = cut; a second click on the same edge also cuts)
        var hit = el('path', { class: 'fd-edge-hit', d: d });
        hit.setAttribute('data-edge-from', n.id); hit.setAttribute('data-edge-port', p.key);
        gEdges.appendChild(hit);
        if (p.label) gEdges.appendChild(el('text', { class: 'fd-edge-label', x: px + 4, y: py + 14 }, p.label));
      });
    });
    // nodes
    flow.nodes.forEach(function (n) {
      var g = el('g', { class: 'fd-node' + ((n.id === selected || multiSel[n.id]) ? ' selected' : '') + (n.id === flow.start ? ' start' : '') + runClass(n.id),
                        transform: 'translate(' + n.x + ',' + n.y + ')' });
      g.setAttribute('data-node', n.id);
      g.appendChild(el('rect', { class: 'card', width: W, height: H, rx: 10 }));
      g.appendChild(el('rect', { class: 'swatch k-' + n.kind, x: 0, y: 0, width: 8, height: H, rx: 4 }));
      g.appendChild(el('text', { class: 'kind', x: 16, y: 20 }, (KIND_ICON[n.kind] || '') + ' ' + n.kind));
      g.appendChild(el('text', { x: 16, y: 40 }, trunc(n.label || summary(n), 26)));
      var badge = runBadge(n.id);
      if (badge) g.appendChild(el('text', { class: 'runbadge', x: W - 20, y: 20 }, badge));
      if (proposals[n.id]) {
        var lamp = el('text', { class: 'runbadge', x: W - (badge ? 40 : 20), y: 20 }, '💡');
        lamp.appendChild(el('title', {}, 'Learned suggestion — select this node to review it'));
        g.appendChild(lamp);
      }
      if (n.id === flow.start) g.appendChild(el('text', { x: 16, y: 56, class: 'fd-edge-label' }, '▶ start'));
      ports(n).forEach(function (p) {
        var c = el('circle', { class: 'port', cx: p.x, cy: H, r: 6 });
        c.setAttribute('data-port', p.key); c.setAttribute('data-node', n.id);
        g.appendChild(c);
      });
      gNodes.appendChild(g);
    });
    // Skip the minimap rebuild mid-drag (render() fires every mousemove frame);
    // the mouseup handler refreshes it once the drag settles.
    if (!drag || drag.kind === 'connect' || drag.kind === 'marquee') renderMinimap();
  }

  // ---- minimap: a scaled overview with the viewport rect; click to jump ----
  var MINI_W = 160, MINI_H = 110, MINI_PAD = 8;
  function miniScale() {
    var minX = 0, minY = 0, maxX = 400, maxY = 300;
    if (flow.nodes.length) {
      minX = Infinity; minY = Infinity; maxX = -Infinity; maxY = -Infinity;
      flow.nodes.forEach(function (n) {
        minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
        maxX = Math.max(maxX, n.x + W); maxY = Math.max(maxY, n.y + H);
      });
    }
    var r = canvas.getBoundingClientRect();
    // include the visible viewport so the view rect always fits on the map
    minX = Math.min(minX, -view.x / view.k); minY = Math.min(minY, -view.y / view.k);
    maxX = Math.max(maxX, (-view.x + r.width) / view.k); maxY = Math.max(maxY, (-view.y + r.height) / view.k);
    var k = Math.min((MINI_W - 2 * MINI_PAD) / Math.max(1, maxX - minX),
                     (MINI_H - 2 * MINI_PAD) / Math.max(1, maxY - minY));
    return { minX: minX, minY: minY, k: k };
  }
  function renderMinimap() {
    var mm = document.getElementById('fd-minimap'); if (!mm) return;
    mm.textContent = '';
    var s = miniScale();
    flow.nodes.forEach(function (n) {
      mm.appendChild(el('rect', { class: 'mini-node',
        x: MINI_PAD + (n.x - s.minX) * s.k, y: MINI_PAD + (n.y - s.minY) * s.k,
        width: Math.max(2, W * s.k), height: Math.max(2, H * s.k), rx: 1 }));
    });
    var r = canvas.getBoundingClientRect();
    mm.appendChild(el('rect', { class: 'mini-view',
      x: MINI_PAD + (-view.x / view.k - s.minX) * s.k,
      y: MINI_PAD + (-view.y / view.k - s.minY) * s.k,
      width: (r.width / view.k) * s.k, height: (r.height / view.k) * s.k }));
  }

  // ---- coordinate + interaction ----
  function worldPt(evt) {
    var r = canvas.getBoundingClientRect();
    return { x: (evt.clientX - r.left - view.x) / view.k, y: (evt.clientY - r.top - view.y) / view.k };
  }
  var drag = null;   // {kind:'node'|'pan'|'connect', ...}

  canvas.addEventListener('mousedown', function (evt) {
    var portEl = evt.target.closest('[data-port]');
    var nodeEl = evt.target.closest('[data-node]');
    var edgeEl = evt.target.closest('[data-edge-from]');
    if (portEl) {
      drag = { kind: 'connect', from: portEl.getAttribute('data-node'), port: portEl.getAttribute('data-port') };
      evt.preventDefault(); return;
    }
    if (edgeEl) {
      var ef = edgeEl.getAttribute('data-edge-from'), ep = edgeEl.getAttribute('data-edge-port');
      if (selectedEdge && selectedEdge.from === ef && selectedEdge.port === ep) {
        deleteEdge(selectedEdge);              // second click on a selected edge cuts it
      } else {
        selectedEdge = { from: ef, port: ep };
        selected = null; renderProps(); render();
        say('Edge selected — click it again (or press Delete) to cut it.');
      }
      evt.preventDefault(); return;
    }
    selectedEdge = null;
    if (nodeEl) {
      var id = nodeEl.getAttribute('data-node');
      var n = nodeById(id), p = worldPt(evt);
      if (multiSel[id] && Object.keys(multiSel).length > 1) {
        // dragging one selected node moves the whole marquee selection
        var offs = {};
        flow.nodes.forEach(function (m) { if (multiSel[m.id]) offs[m.id] = { dx: p.x - m.x, dy: p.y - m.y }; });
        drag = { kind: 'group', offs: offs };
        evt.preventDefault(); return;
      }
      multiSel = {};
      selectNode(id);
      drag = { kind: 'node', id: id, dx: p.x - n.x, dy: p.y - n.y };
      evt.preventDefault(); return;
    }
    if (evt.shiftKey) {           // shift-drag on empty canvas: marquee select
      var mp = worldPt(evt);
      // ex/ey start AT the origin (a click with no drag = an empty box), so the
      // mouseup reader needs no undefined-guard.
      drag = { kind: 'marquee', sx: mp.x, sy: mp.y, ex: mp.x, ey: mp.y };
      evt.preventDefault(); return;
    }
    drag = { kind: 'pan', sx: evt.clientX - view.x, sy: evt.clientY - view.y };
    canvas.classList.add('panning');
    multiSel = {};
    selectNode(null);
  });
  function deleteEdge(edge) {
    var n = nodeById(edge.from);
    if (n) { pushHistory(); n[edge.port] = null; }
    selectedEdge = null; render(); renderProps();
    say('Edge removed.');
  }
  canvas.addEventListener('dblclick', function (evt) {
    var nodeEl = evt.target.closest('[data-node]'); if (!nodeEl) return;
    var n = nodeById(nodeEl.getAttribute('data-node')); if (!n) return;
    if (BODY_KINDS[n.kind]) { evt.preventDefault(); enterSub(n, n.kind); }
    else if (n.kind === 'parallel') { evt.preventDefault(); enterSub(n, 'parallel', 0); }
  });
  window.addEventListener('mousemove', function (evt) {
    if (!drag) return;
    if (drag.kind === 'node') {
      var n = nodeById(drag.id); if (!n) return;
      var p = worldPt(evt);
      // snap to a 16px grid so hand-laid flows line up without guides
      n.x = Math.round((p.x - drag.dx) / 16) * 16;
      n.y = Math.round((p.y - drag.dy) / 16) * 16;
      render();
    } else if (drag.kind === 'group') {
      var gp = worldPt(evt);
      flow.nodes.forEach(function (m) {
        var o = drag.offs[m.id]; if (!o) return;
        m.x = Math.round((gp.x - o.dx) / 16) * 16;
        m.y = Math.round((gp.y - o.dy) / 16) * 16;
      });
      render();
    } else if (drag.kind === 'marquee') {
      var mq = document.getElementById('fd-marquee');
      var mp2 = worldPt(evt);
      drag.ex = mp2.x; drag.ey = mp2.y;
      mq.style.display = '';
      mq.setAttribute('x', Math.min(drag.sx, mp2.x)); mq.setAttribute('y', Math.min(drag.sy, mp2.y));
      mq.setAttribute('width', Math.abs(mp2.x - drag.sx)); mq.setAttribute('height', Math.abs(mp2.y - drag.sy));
    } else if (drag.kind === 'pan') {
      view.x = evt.clientX - drag.sx; view.y = evt.clientY - drag.sy; render();
    } else if (drag.kind === 'connect') {
      var from = nodeById(drag.from); if (!from) return;
      var pd = ports(from).filter(function (x) { return x.key === drag.port; })[0] || { x: W / 2 };
      var px = from.x * view.k + view.x + (pd.x) * view.k, py = (from.y + H) * view.k + view.y;
      var r = canvas.getBoundingClientRect();
      var mx = evt.clientX - r.left, my = evt.clientY - r.top;
      tempEdge.style.display = ''; tempEdge.removeAttribute('transform');
      tempEdge.setAttribute('d', 'M' + ((px)) + ' ' + (py) + ' L ' + mx + ' ' + my);
      // The temp edge is built in screen space but lives inside the (translated +
      // scaled) viewport, so apply the viewport's exact inverse: undo the scale
      // AND the translate under that scale -> scale(1/k) THEN translate(-x,-y)
      // (SVG applies the rightmost transform first).
      tempEdge.setAttribute('transform', 'scale(' + (1 / view.k) + ') translate(' + (-view.x) + ',' + (-view.y) + ')');
    }
  });
  window.addEventListener('mouseup', function (evt) {
    canvas.classList.remove('panning');
    tempEdge.style.display = 'none';
    if (drag && drag.kind === 'marquee') {
      document.getElementById('fd-marquee').style.display = 'none';
      var x0 = Math.min(drag.sx, drag.ex), x1 = Math.max(drag.sx, drag.ex);
      var y0 = Math.min(drag.sy, drag.ey), y1 = Math.max(drag.sy, drag.ey);
      multiSel = {};
      flow.nodes.forEach(function (n) {
        var cx = n.x + W / 2, cy = n.y + H / 2;
        if (cx >= x0 && cx <= x1 && cy >= y0 && cy <= y1) multiSel[n.id] = true;
      });
      selected = null;
      var count = Object.keys(multiSel).length;
      say(count ? count + ' node' + (count === 1 ? '' : 's') + ' selected — drag to move together, Delete to remove.' : '');
      renderProps(); render();
      drag = null; return;
    }
    if (drag && drag.kind === 'connect') {
      var nodeEl = evt.target.closest('[data-node]');
      if (nodeEl) {
        var to = nodeEl.getAttribute('data-node'), from = nodeById(drag.from);
        if (from && to !== from.id) { pushHistory(); from[drag.port] = to; renderProps(); render(); }
      }
    }
    var wasDragging = drag && (drag.kind === 'node' || drag.kind === 'group' || drag.kind === 'pan');
    drag = null;
    // The minimap is skipped mid-drag (see render); refresh it once now that the
    // drag settled, so it isn't left stale.
    if (wasDragging) renderMinimap();
  });
  canvas.addEventListener('wheel', function (evt) {
    evt.preventDefault();
    var f = evt.deltaY < 0 ? 1.1 : 0.9;
    var nk = Math.max(0.35, Math.min(2.2, view.k * f));
    var r = canvas.getBoundingClientRect(), mx = evt.clientX - r.left, my = evt.clientY - r.top;
    view.x = mx - (mx - view.x) * (nk / view.k); view.y = my - (my - view.y) * (nk / view.k);
    view.k = nk; render();
  }, { passive: false });

  // ---- nodes ----
  function addNode(kind) {
    pushHistory();
    var n = normalizeNode({ id: newId(), kind: kind,
      x: Math.round((-view.x + 220) / view.k), y: Math.round((-view.y + 80) / view.k) }, 0);
    // an edge is selected -> insert the new node INTO that edge
    if (selectedEdge) {
      var from = nodeById(selectedEdge.from);
      if (from) {
        n.next = from[selectedEdge.port] || null;
        from[selectedEdge.port] = n.id;
        var tgt = n.next && nodeById(n.next);
        n.x = tgt && from ? Math.round((from.x + tgt.x) / 2) + 24 : from.x + 24;
        n.y = tgt && from ? Math.round((from.y + tgt.y) / 2) : from.y + 110;
        selectedEdge = null;
        flow.nodes.push(n); selectNode(n.id); render();
        say('Inserted into the edge.');
        return;
      }
    }
    // else place under the currently selected node if any
    var sel = selected && nodeById(selected);
    if (sel) { n.x = sel.x; n.y = sel.y + 110; if (!sel.next && sel.kind !== 'branch') sel.next = n.id; }
    if (!flow.nodes.length) flow.start = n.id;
    flow.nodes.push(n); selectNode(n.id); render();
  }
  function deleteNode(id) {
    pushHistory();
    flow.nodes = flow.nodes.filter(function (n) { return n.id !== id; });
    flow.nodes.forEach(function (n) {
      ['next', 'if_true', 'if_false', 'on_error', 'on_expire'].forEach(function (k) { if (n[k] === id) n[k] = null; });
      // Also clear a switch node's case targets, or the deleted id survives in
      // cases[].to (invisible on canvas, but serialized + routed on Run).
      (n.cases || []).forEach(function (c) { if (c.to === id) c.to = null; });
    });
    if (flow.start === id) flow.start = flow.nodes.length ? flow.nodes[0].id : '';
    selectNode(null); render();
  }
  function deleteSelection() {
    var ids = Object.keys(multiSel);
    if (!ids.length) return;
    pushHistory();
    flow.nodes = flow.nodes.filter(function (n) { return !multiSel[n.id]; });
    flow.nodes.forEach(function (n) {
      ['next', 'if_true', 'if_false', 'on_error', 'on_expire'].forEach(function (k) { if (multiSel[n[k]]) n[k] = null; });
      (n.cases || []).forEach(function (c) { if (multiSel[c.to]) c.to = null; });
    });
    if (multiSel[flow.start]) flow.start = flow.nodes.length ? flow.nodes[0].id : '';
    multiSel = {};
    selectNode(null); render();
    say('Removed ' + ids.length + ' node' + (ids.length === 1 ? '' : 's') + '.');
  }
  // Build an independent copy of a serialized node with a fresh id and ALL
  // outgoing edges cleared -- so a clone never smuggles a stale reference to the
  // original's downstream. The one place the edge-field list is maintained
  // (next/if_true/if_false/on_error AND a switch node's cases[].to).
  function makeClone(serialized, x, y) {
    var copy = normalizeNode(JSON.parse(JSON.stringify(serialized)), 0);
    copy.id = newId();
    copy.x = x; copy.y = y;
    copy.next = null; copy.if_true = null; copy.if_false = null; copy.on_error = null; copy.on_expire = null;
    if (copy.cases && copy.cases.length) copy.cases = copy.cases.map(function (c) { return { value: c.value, to: null }; });
    return copy;
  }
  function duplicateNode(id) {
    var src = nodeById(id); if (!src) return;
    pushHistory();
    var copy = makeClone(FDCore.serializeNode(src), (src.x || 0) + 30, (src.y || 0) + 40);
    flow.nodes.push(copy); selectNode(copy.id); render();
    say('Duplicated as ' + copy.id + '.');
  }
  function selectNode(id) { selected = id; if (id) selectedEdge = null; renderProps(); render(); }
  var clipboard = null;    // serialized node (Ctrl+C), pasted with Ctrl+V
  function copyNode(id) {
    var n = nodeById(id); if (!n) return;
    clipboard = FDCore.serializeNode(n);
    say('Copied ' + id + ' — Ctrl+V pastes it.');
  }
  function pasteNode() {
    if (!clipboard) { say('Nothing copied yet.'); return; }
    pushHistory();
    var copy = makeClone(clipboard, (clipboard.x || 60) + 32, (clipboard.y || 60) + 48);
    flow.nodes.push(copy); selectNode(copy.id); render();
    say('Pasted as ' + copy.id + '.');
  }
  function zoomToFit() {
    var r = canvas.getBoundingClientRect();
    view = FDCore.fitView(flow.nodes, r.width, r.height, W, H, 40);
    render();
  }

  // ---- nested sub-flow editing (foreach body / parallel branches) ----
  // Pure serialization/normalization lives in FDCore (flow_designer_core.js) so
  // it's unit-testable under node without a DOM.
  var normalizeNode = FDCore.normalizeNode;
  function normalizeFlow(f) {
    // Upgrade a raw nested-flow dict's nodes into editable node objects (in place,
    // so the parent node's body/branches[i] keeps the same reference).
    f.nodes = (f.nodes || []).map(normalizeNode);
    if (!f.start && f.nodes.length) f.start = f.nodes[0].id;
  }
  function commitFlowRaw(f) {
    // flush any buffered JSON textareas for the nodes of ONE flow (current view)
    (f.nodes || []).forEach(function (n) {
      if (n._paramsRaw !== undefined) { try { n.params = n._paramsRaw.trim() ? JSON.parse(n._paramsRaw) : {}; } catch (e) { /* reported on save */ } delete n._paramsRaw; }
    });
  }
  function enterSub(node, kind, index) {
    commitFlowRaw(flow);
    var sub, label;
    if (BODY_KINDS[kind]) {
      if (!node.body) node.body = { id: '', name: 'body', start: '', nodes: [] };
      sub = node.body; label = (node.label ? node.label + ' · ' : '') + BODY_KINDS[kind];
    } else {
      node.branches = node.branches || [];
      if (!node.branches[index]) node.branches[index] = { id: '', name: 'branch ' + (index + 1), start: '', nodes: [] };
      sub = node.branches[index]; label = '🔱 ' + (node.label || 'parallel') + ' · branch ' + (index + 1);
    }
    normalizeFlow(sub);
    stack.push({ flow: flow, selected: selected });
    crumbs.push(label);
    flow = sub; selected = null;
    autoLayout(); renderCrumbs(); render(); renderProps();
  }
  function exitToDepth(d) {
    commitFlowRaw(flow);
    while (stack.length > d) { var fr = stack.pop(); flow = fr.flow; selected = fr.selected; }
    crumbs.length = d + 1;
    renderCrumbs(); render(); renderProps();
  }
  function renderCrumbs() {
    var c = document.getElementById('fd-crumbs'); if (!c) return;
    c.textContent = '';
    if (!stack.length) { c.classList.remove('on'); return; }   // hidden at the root
    c.classList.add('on');
    crumbs.forEach(function (label, i) {
      if (i) c.appendChild(document.createTextNode(' ▸ '));
      if (i < crumbs.length - 1) {
        var b = document.createElement('button'); b.type = 'button'; b.className = 'fd__crumb';
        b.textContent = label; b.addEventListener('click', function () { exitToDepth(i); });
        c.appendChild(b);
      } else {
        var s = document.createElement('span'); s.className = 'fd__crumb fd__crumb--cur'; s.textContent = label; c.appendChild(s);
      }
    });
  }

  // ---- property panel ----
  function field(label, inputEl) {
    var wrap = document.createElement('div'); wrap.className = 'field';
    var l = document.createElement('label'); l.textContent = label; wrap.appendChild(l); wrap.appendChild(inputEl); return wrap;
  }
  function input(val, oninput, ph) {
    var i = document.createElement('input'); i.className = 'input'; i.value = val || ''; if (ph) i.placeholder = ph;
    i.addEventListener('input', function () { oninput(i.value); });
    i.addEventListener('focus', histFocus); i.addEventListener('blur', histBlur);
    return i;
  }
  function textarea(val, oninput, ph) {
    var t = document.createElement('textarea'); t.className = 'input'; t.value = val || ''; if (ph) t.placeholder = ph;
    t.addEventListener('input', function () { oninput(t.value); });
    t.addEventListener('focus', histFocus); t.addEventListener('blur', histBlur);
    return t;
  }
  function select(val, opts, onchange) {
    var s = document.createElement('select'); s.className = 'input';
    opts.forEach(function (o) { var op = document.createElement('option'); op.value = o.v; op.textContent = o.t; if (o.v === val) op.selected = true; s.appendChild(op); });
    s.addEventListener('change', function () { onchange(s.value); }); return s;
  }
  function renderProps() {
    // Preserve focus + caret in the connector-search field across a rebuild:
    // the debounced registry search repaints this panel while the user is
    // still typing in it (loadTools -> renderProps). Without this the field
    // would blur mid-search.
    var _toolCaret = null;
    if (document.activeElement && document.activeElement.id === 'fd-tool-input') {
      try { _toolCaret = document.activeElement.selectionStart; } catch (e) { _toolCaret = -1; }
    }
    props.textContent = '';
    var selCount = Object.keys(multiSel).length;
    if (selCount) {
      var mh = document.createElement('div'); mh.style.fontWeight = '600'; mh.style.marginBottom = 'var(--space-3)';
      mh.textContent = selCount + ' nodes selected';
      props.appendChild(mh);
      props.appendChild(hint('Drag any selected node to move the group. Delete removes them all; Esc deselects.'));
      var db = document.createElement('button'); db.className = 'btn btn--ghost'; db.type = 'button';
      db.textContent = '🗑 Delete ' + selCount + ' nodes';
      db.addEventListener('click', deleteSelection);
      props.appendChild(db);
      return;
    }
    if (!selected) {
      var p = document.createElement('p'); p.className = 'fd__empty';
      p.textContent = "Select a node to edit it, or add a step. Connect steps by dragging from a node's bottom dot to another node. Shift-drag selects several.";
      props.appendChild(p);
      // Flow-level: declared manual-run inputs (a typed run form + validation).
      var ih = document.createElement('div'); ih.style.fontWeight = '600'; ih.style.margin = 'var(--space-4) 0 var(--space-2)';
      ih.textContent = 'Manual-run inputs'; props.appendChild(ih);
      flowInputs.forEach(function (spec, ii) {
        var row = document.createElement('div'); row.style.display = 'flex'; row.style.gap = '4px'; row.style.marginBottom = '4px'; row.style.flexWrap = 'wrap';
        var keyIn = input(spec.key || '', function (v) { spec.key = v; }, 'key'); keyIn.style.flex = '2';
        var typeSel = select(spec.type || 'text',
          ['text', 'number', 'bool', 'date'].map(function (t) { return { v: t, t: t }; }),
          function (v) { spec.type = v; }); typeSel.style.flex = '1';
        var reqWrap = document.createElement('label'); reqWrap.style.display = 'flex'; reqWrap.style.alignItems = 'center'; reqWrap.style.gap = '2px'; reqWrap.style.fontSize = '11px';
        var reqCb = document.createElement('input'); reqCb.type = 'checkbox'; reqCb.checked = !!spec.required;
        reqCb.addEventListener('change', function () { spec.required = reqCb.checked; });
        reqWrap.appendChild(reqCb); reqWrap.appendChild(document.createTextNode('req'));
        var rm = document.createElement('button'); rm.className = 'btn btn--ghost'; rm.type = 'button'; rm.textContent = '🗑';
        rm.setAttribute('aria-label', 'Remove input');
        rm.addEventListener('click', function () { flowInputs.splice(ii, 1); renderProps(); });
        row.appendChild(keyIn); row.appendChild(typeSel); row.appendChild(reqWrap); row.appendChild(rm);
        props.appendChild(row);
      });
      var addI = document.createElement('button'); addI.className = 'btn btn--ghost'; addI.type = 'button'; addI.style.width = '100%';
      addI.textContent = '+ Add input';
      addI.addEventListener('click', function () { flowInputs.push({ key: '', type: 'text', required: false }); renderProps(); });
      props.appendChild(addI);
      props.appendChild(hint('Declared inputs make the Run button prompt for typed values and are validated/coerced server-side.'));
      return;
    }
    var n = nodeById(selected); if (!n) return;
    var head = document.createElement('div'); head.style.fontWeight = '600'; head.style.marginBottom = 'var(--space-3)';
    head.textContent = (KIND_ICON[n.kind] || '') + ' ' + n.kind + ' node';
    props.appendChild(head);
    props.appendChild(field('Label (optional)', input(n.label, function (v) { n.label = v; render(); }, 'shown on the card')));
    if (n.kind === 'agent') {
      var briefTa = textarea(n.brief, function (v) { n.brief = v; render(); }, 'Summarize {{issue_body}} and draft a reply');
      props.appendChild(withPills(n.id, field('Brief (what the agent should do)', briefTa), briefTa));
    }
    if (n.kind === 'action') {
      // Category filter: browse one bucket instead of a 3k-name typeahead.
      if (toolCategories.length) {
        var catOpts = [{ v: '', t: 'All categories' }].concat(
          toolCategories.map(function (c) { return { v: c, t: c }; }));
        var catSel = select(toolFilterCategory, catOpts, function (v) {
          toolFilterCategory = v;
          loadTools('', v);   // browse the whole bucket (or curated set when cleared)
        });
        props.appendChild(field('Category', catSel));
      }
      // No renderProps() here: rebuilding the panel on every keystroke would
      // destroy this very input and blur it. The debounced scheduleToolSearch
      // refreshes the catalog + meta hints, and renderProps restores focus.
      var toolIn = input(n.tool, function (v) { n.tool = v; scheduleToolSearch(v); render(); },
        toolFilterCategory ? ('search ' + toolFilterCategory + '…') : 'search all connectors…');
      toolIn.setAttribute('list', 'fd-tools');
      toolIn.id = 'fd-tool-input';
      props.appendChild(field('Connector / tool', toolIn));
      var meta = toolCatalog[n.tool];
      if (meta && meta.category) {
        var tc = document.createElement('p'); tc.className = 'toolhint'; tc.style.opacity = '0.75';
        tc.textContent = 'Category: ' + meta.category; props.appendChild(tc);
      }
      if (meta && meta.description) { var th = document.createElement('p'); th.className = 'toolhint'; th.textContent = meta.description; props.appendChild(th); }
      // Known params -> a labeled input each (no raw JSON for a recognized tool)
      var known = (meta && meta.params) || [];
      if (known.length) {
        known.forEach(function (pk) {
          var pin = input(n.params[pk] != null ? String(n.params[pk]) : '',
            function (v) { if (v === '') delete n.params[pk]; else n.params[pk] = v; }, '');
          props.appendChild(withPills(n.id, field(pk, pin), pin));
        });
        props.appendChild(hint('Use {{key}} to insert an earlier step’s result.'));
      } else {
        var pta = textarea(jsonOr(n.params), function (v) { n._paramsRaw = v; });
        props.appendChild(withPills(n.id, field('Params (JSON)', pta), pta));
      }
    }
    if (n.kind === 'branch') {
      props.appendChild(conditionField(n, 'Condition', 'amount > 100'));
      props.appendChild(hint('Drag the green (yes) and red (no) dots to the next steps.'));
    }
    if (n.kind === 'switch') {
      var keyIn = input(n.condition, function (v) { n.condition = v; render(); }, 'status');
      props.appendChild(withPills(n.id, field('Route on (data key)', keyIn), keyIn, true));
      var cwrap = document.createElement('div'); cwrap.className = 'field';
      var cl = document.createElement('label'); cl.textContent = 'Cases (value → step)'; cwrap.appendChild(cl);
      var targets = [{ v: '', t: '(pick a step)' }].concat(
        flow.nodes.filter(function (m) { return m.id !== n.id; })
          .map(function (m) { return { v: m.id, t: (m.label || m.id) }; }));
      (n.cases = n.cases || []).forEach(function (c, ci) {
        var rowc = document.createElement('div'); rowc.style.display = 'flex'; rowc.style.gap = '4px'; rowc.style.marginBottom = '4px';
        var vi = input(c.value != null ? String(c.value) : '', function (v) { c.value = v; render(); }, 'value');
        vi.style.flex = '1';
        var ts = select(c.to || '', targets, function (v) { c.to = v || null; render(); });
        ts.style.flex = '1';
        var rm = document.createElement('button'); rm.className = 'btn btn--ghost'; rm.type = 'button'; rm.textContent = '🗑';
        rm.setAttribute('aria-label', 'Remove case');
        rm.title = 'Remove case';
        rm.addEventListener('click', function () { n.cases.splice(ci, 1); renderProps(); render(); });
        rowc.appendChild(vi); rowc.appendChild(ts); rowc.appendChild(rm); cwrap.appendChild(rowc);
      });
      var addc = document.createElement('button'); addc.className = 'btn btn--ghost'; addc.type = 'button'; addc.style.width = '100%';
      addc.textContent = '+ Add case';
      addc.addEventListener('click', function () { n.cases.push({ value: '', to: null }); renderProps(); });
      cwrap.appendChild(addc); props.appendChild(cwrap);
      props.appendChild(hint('No match falls through to this node’s normal next step (the default).'));
    }
    if (n.kind === 'while') {
      props.appendChild(conditionField(n, 'Keep looping while', 'done != yes'));
      props.appendChild(field('Max passes (0 = 100)', input(String(n.limit || ''), function (v) { n.limit = parseInt(v, 10) || 0; }, '100')));
      var wbodyN = (n.body && n.body.nodes && n.body.nodes.length) || 0;
      var wb = document.createElement('button'); wb.className = 'btn btn--ghost'; wb.type = 'button';
      wb.style.width = '100%'; wb.textContent = '✏️ Edit loop body (' + wbodyN + (wbodyN === 1 ? ' step)' : ' steps)') + ' ↳';
      wb.addEventListener('click', function () { enterSub(n, 'while'); });
      props.appendChild(field('Loop body (visual)', wb));
      props.appendChild(hint('The body runs on the SAME data, so it can change what the condition sees; it may set _break to stop early.'));
    }
    if (n.kind === 'wait_event') {
      props.appendChild(field('Waiting for (shown while paused)', input(n.prompt, function (v) { n.prompt = v; render(); }, 'the payment provider callback')));
      props.appendChild(hint('The run pauses here until an event resumes it: POST /webhook/run {"resume": "<run id>", "data": {...}} — the data merges into the flow.'));
    }
    if (n.kind === 'scope') {
      var sbodyN = (n.body && n.body.nodes && n.body.nodes.length) || 0;
      var sb = document.createElement('button'); sb.className = 'btn btn--ghost'; sb.type = 'button';
      sb.style.width = '100%'; sb.textContent = '✏️ Edit try body (' + sbodyN + (sbodyN === 1 ? ' step)' : ' steps)') + ' ↳';
      sb.addEventListener('click', function () { enterSub(n, 'scope'); });
      props.appendChild(field('Try these steps (visual)', sb));
      var scErr = [{ v: '', t: '(no catch — a failure fails the run)' }].concat(
        flow.nodes.filter(function (m) { return m.id !== n.id; })
          .map(function (m) { return { v: m.id, t: (m.label || m.id) }; }));
      props.appendChild(field('On failure, go to (the catch step)', select(n.on_error || '', scErr, function (v) { n.on_error = v || null; render(); })));
      props.appendChild(hint('The catch step can read what went wrong from {{_error}}.'));
    }
    if (n.kind === 'foreach') {
      props.appendChild(field('Items (data key with a list)', input(n.items, function (v) { n.items = v; render(); }, 'rows')));
      props.appendChild(field('Loop variable', input(n.var, function (v) { n.var = v; }, 'item')));
      props.appendChild(field('Max iterations (0 = all)', input(String(n.limit || ''), function (v) { n.limit = parseInt(v, 10) || 0; }, '0')));
      props.appendChild(field('Concurrency (1 = one at a time)', input(String(n.concurrency || ''), function (v) { n.concurrency = parseInt(v, 10) || 1; }, '1')));
      var bodyN = (n.body && n.body.nodes && n.body.nodes.length) || 0;
      var eb = document.createElement('button'); eb.className = 'btn btn--ghost'; eb.type = 'button';
      eb.style.width = '100%'; eb.textContent = '✏️ Edit loop body (' + bodyN + (bodyN === 1 ? ' step)' : ' steps)') + ' ↳';
      eb.addEventListener('click', function () { enterSub(n, 'foreach'); });
      props.appendChild(field('Loop body (visual)', eb));
      var conc = document.createElement('label');
      conc.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:var(--text-sm);margin-bottom:var(--space-3);';
      var cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = !!n.concurrent;
      cb.addEventListener('change', function () { n.concurrent = cb.checked; render(); });
      conc.appendChild(cb); conc.appendChild(document.createTextNode('Run iterations in parallel (bounded pool; _break doesn’t apply)'));
      props.appendChild(conc);
      props.appendChild(hint('Opens the body as its own canvas. It can set _break to true to stop early.'));
    }
    if (n.kind === 'parallel') {
      var wrap = document.createElement('div'); wrap.className = 'field';
      var wl = document.createElement('label'); wl.textContent = 'Branches (each runs concurrently)'; wrap.appendChild(wl);
      (n.branches || []).forEach(function (br, bi) {
        var cnt = (br && br.nodes && br.nodes.length) || 0;
        var rowb = document.createElement('div'); rowb.style.display = 'flex'; rowb.style.gap = '4px'; rowb.style.marginBottom = '4px';
        var edit = document.createElement('button'); edit.className = 'btn btn--ghost'; edit.type = 'button'; edit.style.flex = '1';
        edit.textContent = '✏️ Branch ' + (bi + 1) + ' (' + cnt + ')'; edit.addEventListener('click', function () { enterSub(n, 'parallel', bi); });
        var rm = document.createElement('button'); rm.className = 'btn btn--ghost'; rm.type = 'button'; rm.textContent = '🗑';
        rm.setAttribute('aria-label', 'Remove branch');
        rm.title = 'Remove branch ' + (bi + 1);
        rm.addEventListener('click', function () { n.branches.splice(bi, 1); renderProps(); render(); });
        rowb.appendChild(edit); rowb.appendChild(rm); wrap.appendChild(rowb);
      });
      var add = document.createElement('button'); add.className = 'btn btn--ghost'; add.type = 'button'; add.style.width = '100%';
      add.textContent = '+ Add branch';
      add.addEventListener('click', function () { n.branches = n.branches || []; n.branches.push({ id: '', name: 'branch ' + (n.branches.length + 1), start: '', nodes: [] }); renderProps(); render(); });
      wrap.appendChild(add); props.appendChild(wrap);
      props.appendChild(hint('Each branch is an isolated sub-flow; they run in parallel and their outputs merge.'));
    }
    if (n.kind === 'approval') {
      props.appendChild(field('Prompt (what a human approves)', input(n.prompt, function (v) { n.prompt = v; render(); }, 'Send this reply?')));
      props.appendChild(field('Assignee (who to notify, optional)',
        input(n.assignee, function (v) { n.assignee = v; }, 'ops@team or @alice')));
      props.appendChild(field('Choices beyond approve/reject (comma-separated, optional)',
        input((n.choices || []).join(', '), function (v) {
          n.choices = v.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
        }, 'ship, hold, escalate')));
      props.appendChild(field('Expires after (seconds; 0 = waits forever)',
        input(String(n.expires_after || ''), function (v) { n.expires_after = parseFloat(v) || 0; render(); }, '0')));
      var expOpts = [{ v: '', t: '(on expiry: reject)' }].concat(
        flow.nodes.filter(function (m) { return m.id !== n.id; })
          .map(function (m) { return { v: m.id, t: 'escalate to ' + (m.label || m.id) }; }));
      props.appendChild(field('On expiry, go to', select(n.on_expire || '', expOpts, function (v) { n.on_expire = v || null; render(); })));
      // form fields collected on sign-off (merged into flow data)
      var fwrap = document.createElement('div'); fwrap.className = 'field';
      var fl = document.createElement('label'); fl.textContent = 'Collect on approval (form fields, optional)'; fwrap.appendChild(fl);
      (n.form = n.form || []).forEach(function (fld, fi) {
        var frow = document.createElement('div'); frow.style.cssText = 'display:flex;gap:4px;margin-bottom:4px;';
        var nm = input(fld.name || '', function (v) { fld.name = v; }, 'key'); nm.style.flex = '1';
        var lb = input(fld.label || '', function (v) { fld.label = v; }, 'prompt shown'); lb.style.flex = '1';
        var rm = document.createElement('button'); rm.className = 'btn btn--ghost'; rm.type = 'button'; rm.textContent = '🗑';
        rm.setAttribute('aria-label', 'Remove field');
        rm.addEventListener('click', function () { n.form.splice(fi, 1); renderProps(); });
        frow.appendChild(nm); frow.appendChild(lb); frow.appendChild(rm); fwrap.appendChild(frow);
      });
      var addF = document.createElement('button'); addF.className = 'btn btn--ghost'; addF.type = 'button'; addF.style.width = '100%';
      addF.textContent = '+ Add field'; addF.addEventListener('click', function () { n.form.push({ name: '', label: '' }); renderProps(); });
      fwrap.appendChild(addF); props.appendChild(fwrap);
      props.appendChild(hint('A chosen verdict + any form fields are recorded in flow data, so a switch after this can route on them.'));
    }
    if (n.kind === 'delay') props.appendChild(field('Seconds', input(String(n.seconds || ''), function (v) { n.seconds = parseFloat(v) || 0; render(); }, '60')));
    if (n.kind === 'subflow') {
      props.appendChild(field('Flow to run (saved flow id)', input(n.flow_ref, function (v) { n.flow_ref = v; render(); }, 'onboard-customer')));
      var si = document.createElement('div'); si.className = 'field';
      var sil = document.createElement('label'); sil.textContent = 'Inputs to pass (child key ← value/{{…}})'; si.appendChild(sil);
      n.subflow_inputs = n.subflow_inputs || {};
      Object.keys(n.subflow_inputs).forEach(function (k) {
        var rowi = document.createElement('div'); rowi.style.display = 'flex'; rowi.style.gap = '4px'; rowi.style.marginBottom = '4px';
        var keyIn = input(k, null, 'child key'); keyIn.style.flex = '1';
        keyIn.addEventListener('change', function () { var nv = keyIn.value.trim(); if (nv && nv !== k) { n.subflow_inputs[nv] = n.subflow_inputs[k]; delete n.subflow_inputs[k]; renderProps(); } });
        var valIn = input(n.subflow_inputs[k], function (v) { n.subflow_inputs[k] = v; }, '{{order_id}}'); valIn.style.flex = '2';
        var rm = document.createElement('button'); rm.className = 'btn btn--ghost'; rm.type = 'button'; rm.textContent = '🗑';
        rm.setAttribute('aria-label', 'Remove input');
        rm.addEventListener('click', function () { delete n.subflow_inputs[k]; renderProps(); render(); });
        rowi.appendChild(keyIn); rowi.appendChild(valIn); rowi.appendChild(rm); si.appendChild(rowi);
      });
      var addi = document.createElement('button'); addi.className = 'btn btn--ghost'; addi.type = 'button'; addi.style.width = '100%';
      addi.textContent = '+ Add input';
      addi.addEventListener('click', function () { var key = 'in' + (Object.keys(n.subflow_inputs).length + 1); n.subflow_inputs[key] = ''; renderProps(); render(); });
      si.appendChild(addi); props.appendChild(si);
      props.appendChild(hint(Object.keys(n.subflow_inputs).length
        ? 'Isolated: the child sees ONLY these inputs (can’t read or clobber this flow’s other data); its result returns under the Output key.'
        : 'No inputs mapped → the child shares this flow’s data (reads it, and its outputs flow back). Add inputs to isolate it. Cyclic references are blocked.'));
    }
    if (n.kind === 'setvar') {
      var sv = document.createElement('div'); sv.className = 'field';
      var svl = document.createElement('label'); svl.textContent = 'Set data keys (value can use {{…}})'; sv.appendChild(svl);
      n.assignments = n.assignments || {};
      Object.keys(n.assignments).forEach(function (k) {
        var rowv = document.createElement('div'); rowv.style.display = 'flex'; rowv.style.gap = '4px'; rowv.style.marginBottom = '4px';
        var keyIn = input(k, null, 'key'); keyIn.style.flex = '1';
        keyIn.addEventListener('change', function () { var nv = keyIn.value.trim(); if (nv && nv !== k) { n.assignments[nv] = n.assignments[k]; delete n.assignments[k]; renderProps(); } });
        var valIn = input(n.assignments[k], function (v) { n.assignments[k] = v; }, '{{add(count,1)}}'); valIn.style.flex = '2';
        var rm = document.createElement('button'); rm.className = 'btn btn--ghost'; rm.type = 'button'; rm.textContent = '🗑';
        rm.setAttribute('aria-label', 'Remove assignment');
        rm.addEventListener('click', function () { delete n.assignments[k]; renderProps(); render(); });
        rowv.appendChild(keyIn); rowv.appendChild(valIn); rowv.appendChild(rm); sv.appendChild(rowv);
      });
      var addv = document.createElement('button'); addv.className = 'btn btn--ghost'; addv.type = 'button'; addv.style.width = '100%';
      addv.textContent = '+ Add assignment';
      addv.addEventListener('click', function () { var key = 'var' + (Object.keys(n.assignments).length + 1); n.assignments[key] = ''; renderProps(); render(); });
      sv.appendChild(addv); props.appendChild(sv);
    }
    if (['agent', 'action', 'foreach', 'parallel', 'while', 'approval', 'subflow'].indexOf(n.kind) >= 0)
      props.appendChild(field('Output key (optional)', input(n.output, function (v) { n.output = v; }, 'store result under…')));
    // error handling: retry (work nodes) then route on failure (work + subflow;
    // a scope node's catch arm is edited in its own props block above)
    if (n.kind === 'agent' || n.kind === 'action') {
      props.appendChild(field('Retries on failure', input(String(n.retries || ''), function (v) { n.retries = parseInt(v, 10) || 0; }, '0')));
      props.appendChild(field('Retry backoff (base seconds, 0 = no wait)', input(String(n.retry_backoff || ''), function (v) { n.retry_backoff = parseFloat(v) || 0; }, '0')));
      props.appendChild(field('Timeout (seconds, 0 = none)', input(String(n.timeout || ''), function (v) { n.timeout = parseFloat(v) || 0; }, '0')));
    }
    if (['agent', 'action', 'subflow'].indexOf(n.kind) >= 0) {
      var errOpts = [{ v: '', t: '(none — continue)' }].concat(flow.nodes.filter(function (m) { return m.id !== n.id; }).map(function (m) { return { v: m.id, t: (m.label || m.id) }; }));
      props.appendChild(field('On failure, go to', select(n.on_error || '', errOpts, function (v) { n.on_error = v || null; render(); })));
    }
    // last run outcome for this node (from the live/last run overlay)
    var rs = runState[n.id];
    if (rs || n.id === runCursor) {
      var badge = runBadge(n.id);
      var rl = document.createElement('p'); rl.className = 'fd__empty'; rl.style.marginTop = 'var(--space-2)';
      rl.textContent = 'Last run: ' + (n.id === runCursor ? 'waiting here'
        : (rs.status || '') + (rs.outcome != null ? ' (' + (rs.outcome === 1 ? 'ok' : 'failed') + ')' : ''))
        + (rs && rs.seconds != null ? ' · ' + rs.seconds + 's' : '') + ' ' + badge;
      props.appendChild(rl);
    }
    // grounded signal for this node across ALL its runs (not just the last):
    // an approval's human accept-rate, a work node's success rate.
    var sig = signals.nodes[n.id];
    if (sig && sig.n) {
      var sl = document.createElement('p'); sl.className = 'fd__empty'; sl.style.marginTop = 'var(--space-2)';
      var pct = Math.round(sig.mean * 100);
      if (n.kind === 'approval') sl.textContent = '🧑‍⚖️ humans approved ' + pct + '% of ' + sig.n + ' decisions';
      else sl.textContent = '📊 succeeds ' + pct + '% over ' + sig.n + ' runs';
      props.appendChild(sl);
    }
    // measured impact of a prior self-rewrite on this node (before vs. after)
    var imp = impacts[n.id];
    if (imp && imp.changed && imp.impact) {
      var d = imp.impact.delta;
      var il = document.createElement('p'); il.className = 'fd__empty'; il.style.marginTop = 'var(--space-2)';
      il.textContent = '📈 since → ' + (imp.applied && imp.applied.to_kind) + ': '
        + (typeof d === 'number' ? (d >= 0 ? '+' : '') + Math.round(d * 100) + '% vs before'
          : 'measuring (' + (imp.impact.after && imp.impact.after.n || 0) + ' runs so far)');
      props.appendChild(il);
    }
    // self-rewrite proposal for this node (from grounded per-node outcomes)
    var prop = proposals[n.id];
    if (prop) {
      var pbox = document.createElement('div');
      pbox.style.cssText = 'margin-top:var(--space-3);padding:var(--space-2);border:1px solid var(--accent);border-radius:8px;';
      var pl = document.createElement('p'); pl.className = 'fd__empty'; pl.style.marginBottom = 'var(--space-2)';
      pl.textContent = '💡 ' + prop.reason;
      // Persisted apply: saves a new version + logs the change so its impact is measured.
      var pb = document.createElement('button'); pb.className = 'btn btn--primary'; pb.type = 'button';
      pb.textContent = 'Apply & track (→ ' + prop.to_kind
        + (prop.inferred_tool ? ' · ' + prop.inferred_tool : '') + ')';
      if (prop.applicable === false) {
        pb.disabled = true;
        pb.title = prop.blocking_reason || 'Review parameter bindings before applying.';
      }
      pb.addEventListener('click', function () { applyProposal(n, prop); });
      pbox.appendChild(pl); pbox.appendChild(pb); props.appendChild(pbox);
    }
    // actions
    var row = document.createElement('div'); row.style.display = 'flex'; row.style.gap = 'var(--space-2)'; row.style.marginTop = 'var(--space-3)'; row.style.flexWrap = 'wrap';
    var mk = document.createElement('button'); mk.className = 'btn btn--ghost'; mk.type = 'button'; mk.textContent = 'Make start';
    mk.addEventListener('click', function () { flow.start = n.id; render(); });
    var dup = document.createElement('button'); dup.className = 'btn btn--ghost'; dup.type = 'button'; dup.textContent = '⧉ Duplicate';
    dup.title = 'Ctrl+D';
    dup.addEventListener('click', function () { duplicateNode(n.id); });
    var del = document.createElement('button'); del.className = 'btn btn--ghost'; del.type = 'button'; del.textContent = '🗑 Delete';
    del.addEventListener('click', function () { deleteNode(n.id); });
    row.appendChild(mk); row.appendChild(dup); row.appendChild(del); props.appendChild(row);
    if (_toolCaret !== null) {
      var _ti = document.getElementById('fd-tool-input');
      if (_ti) {
        _ti.focus();
        if (_toolCaret >= 0) { try { _ti.setSelectionRange(_toolCaret, _toolCaret); } catch (e) {} }
      }
    }
  }
  function hint(t) { var p = document.createElement('p'); p.className = 'fd__empty'; p.textContent = t; return p; }

  // ---- data pills: what THIS node can reference, one click to insert ----
  function availableKeys(nodeId) {
    var keys = FDCore.upstreamOutputs(flow, nodeId);
    // Nested keys learned from real runs: for an upstream output that produced
    // an object/array-of-objects, offer key.field so the user needn't guess.
    keys.slice().forEach(function (k) {
      var shape = flowSchema[k];
      if (shape && shape.keys && shape.keys.length) {
        shape.keys.forEach(function (f) {
          var nested = k + '.' + f;
          if (keys.indexOf(nested) < 0) keys.push(nested);
        });
      }
    });
    try {   // the sample-input keys are the trigger payload's shape
      var raw = document.getElementById('fd-sample').value.trim();
      if (raw) Object.keys(JSON.parse(raw)).forEach(function (k) {
        if (keys.indexOf(k) < 0) keys.push(k);
      });
    } catch (e) { /* not JSON yet -- no payload pills */ }
    return keys;
  }
  function insertAtCursor(target, text) {
    var s = target.selectionStart, e = target.selectionEnd, v = target.value;
    if (s == null) { target.value = v + text; }
    else { target.value = v.slice(0, s) + text + v.slice(e); target.selectionStart = target.selectionEnd = s + text.length; }
    target.dispatchEvent(new Event('input', { bubbles: false }));
    target.focus();
  }
  function pillsRow(nodeId, target, raw) {
    var keys = availableKeys(nodeId);
    if (!keys.length) return null;
    var row = document.createElement('div'); row.className = 'fd__pills';
    keys.forEach(function (k) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'fd__pill';
      b.textContent = raw ? k : '{{' + k + '}}'; b.title = 'Insert ' + (raw ? k : '{{' + k + '}}');
      b.addEventListener('mousedown', function (evt) { evt.preventDefault(); });   // keep target's focus
      b.addEventListener('click', function () { insertAtCursor(target, raw ? k : '{{' + k + '}}'); });
      row.appendChild(b);
    });
    return row;
  }
  function withPills(nodeId, fieldEl, target, raw) {
    var pills = pillsRow(nodeId, target, raw);
    if (pills) fieldEl.appendChild(pills);
    return fieldEl;
  }
  function jsonOr(v) { try { return v && (typeof v === 'object') ? JSON.stringify(v, null, 2) : (v || ''); } catch (e) { return ''; } }

  // ---- structured condition builder (key / op / value) with a raw fallback ----
  // Grammar lives in FDCore (the unit-tested shared module) -- no copy here.
  var COND_OPS = FDCore.COND_OPS;
  var COND_RE = FDCore.COND_RE;
  function conditionField(n, label, placeholder) {
    var wrap = document.createElement('div'); wrap.className = 'field';
    var head = document.createElement('div'); head.style.cssText = 'display:flex;justify-content:space-between;align-items:baseline;';
    var l = document.createElement('label'); l.textContent = label; head.appendChild(l);
    var isAdvanced = n._condAdvanced || / (and|or) /.test(n.condition || '') || (!COND_RE.test(n.condition || '') && !!(n.condition || '').trim());
    var toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'fd__pill';
    toggle.textContent = isAdvanced ? 'simple' : 'advanced';
    toggle.title = 'Switch between the dropdown builder and raw text (for and/or)';
    toggle.addEventListener('click', function () { n._condAdvanced = !isAdvanced; renderProps(); });
    head.appendChild(toggle); wrap.appendChild(head);
    if (isAdvanced) {
      var ta = input(n.condition, function (v) { n.condition = v; render(); }, (placeholder || 'a > 1') + ' and b == x');
      wrap.appendChild(ta);
      var pills = pillsRow(n.id, ta, true); if (pills) wrap.appendChild(pills);
      return wrap;
    }
    var m = COND_RE.exec(n.condition || '');
    var cur = { key: m ? m[1] : '', op: m ? m[2] : '==', value: m ? m[3] : '' };
    function commit() { n.condition = (cur.key + ' ' + cur.op + ' ' + cur.value).trim(); render(); }
    var row = document.createElement('div'); row.style.cssText = 'display:flex;gap:4px;';
    var keyIn = input(cur.key, function (v) { cur.key = v; commit(); }, 'data key');
    keyIn.setAttribute('list', 'fd-condkeys'); keyIn.style.flex = '1';
    var opSel = select(cur.op, COND_OPS.map(function (o) { return { v: o, t: o }; }), function (v) { cur.op = v; commit(); });
    opSel.style.flex = '0 0 88px';
    var valIn = input(cur.value, function (v) { cur.value = v; commit(); }, 'value');
    valIn.style.flex = '1';
    row.appendChild(keyIn); row.appendChild(opSel); row.appendChild(valIn);
    wrap.appendChild(row);
    // a datalist of the keys this node can see (upstream outputs + payload)
    var dl = document.getElementById('fd-condkeys');
    if (!dl) { dl = document.createElement('datalist'); dl.id = 'fd-condkeys'; document.body.appendChild(dl); }
    dl.textContent = '';
    availableKeys(n.id).forEach(function (k) { var o = document.createElement('option'); o.value = k; dl.appendChild(o); });
    return wrap;
  }

  // ---- serialize / api ----
  function walkFlows(f, fn) {
    // depth-first over a flow and every nested body/branch sub-flow
    fn(f);
    (f.nodes || []).forEach(function (n) {
      if (n.body) walkFlows(n.body, fn);
      (n.branches || []).forEach(function (b) { walkFlows(b, fn); });
    });
  }
  function commitRawFields() {
    // parse the buffered params JSON across the root AND every nested sub-flow
    var bad = [];
    walkFlows(rootFlow, function (f) {
      (f.nodes || []).forEach(function (n) {
        if (n._paramsRaw !== undefined) { try { n.params = n._paramsRaw.trim() ? JSON.parse(n._paramsRaw) : {}; } catch (e) { bad.push(n.id + '.params'); } delete n._paramsRaw; }
      });
    });
    return bad;
  }
  var serializeFlow = FDCore.serializeFlow;
  function toApiFlow() {
    var notifyEl = document.getElementById('fd-notify');
    var out = serializeFlow({ id: (idInput.value || rootFlow.id || '').trim(),
      name: nameInput.value || rootFlow.name || 'Untitled flow', start: rootFlow.start, nodes: rootFlow.nodes });
    out.notify = !!(notifyEl && notifyEl.checked);
    out.max_seconds = parseFloat((document.getElementById('fd-maxsec') || {}).value) || 0;
    out.max_dollars = parseFloat((document.getElementById('fd-maxdollars') || {}).value) || 0;
    out.schedule = ((document.getElementById('fd-schedule') || {}).value || '').trim();
    out.timezone = ((document.getElementById('fd-tz') || {}).value || '').trim();
    out.max_concurrent = parseInt((document.getElementById('fd-maxconc') || {}).value, 10) || 0;
    out.inputs = flowInputs.filter(function (i) { return (i.key || '').trim(); });
    out.version = parseInt(rootFlow.version, 10) || 0;
    out.revision = String(rootFlow.revision || '');
    return out;
  }
  function fromApiFlow(f) {
    runState = {}; runCursor = null; impacts = {};
    stack = []; crumbs = ['Main flow']; renderCrumbs();
    signals = { nodes: {}, trigger: null };
    var sigEl = document.getElementById('fd-signal'); if (sigEl) sigEl.hidden = true;
    var lg = document.getElementById('fd-legend'); if (lg) lg.classList.remove('on');
    flow = { id: f.id || '', name: f.name || '', start: f.start || '', nodes: [],
      max_seconds: f.max_seconds || 0, version: parseInt(f.version, 10) || 0,
      revision: String(f.revision || ''), published: !!f.published,
      published_version: parseInt(f.published_version, 10) || 0,
      published_revision: String(f.published_revision || '') };
    var notifyEl = document.getElementById('fd-notify'); if (notifyEl) notifyEl.checked = !!f.notify;
    var msEl = document.getElementById('fd-maxsec'); if (msEl) msEl.value = f.max_seconds || '';
    var mdEl = document.getElementById('fd-maxdollars'); if (mdEl) mdEl.value = f.max_dollars || '';
    var schEl = document.getElementById('fd-schedule'); if (schEl) schEl.value = f.schedule || '';
    var tzEl = document.getElementById('fd-tz'); if (tzEl) tzEl.value = f.timezone || '';
    var mcEl = document.getElementById('fd-maxconc'); if (mcEl) mcEl.value = f.max_concurrent || '';
    flowInputs = (f.inputs || []).map(function (i) { return Object.assign({}, i); });
    (f.nodes || []).forEach(function (nd, i) { flow.nodes.push(normalizeNode(nd, i)); });
    rootFlow = flow;
    autoLayout();
    nameInput.value = flow.name; idInput.value = flow.id;
    selected = null; selectedEdge = null; view = { x: 40, y: 20, k: 1 }; render(); renderProps();
  }
  function autoLayout() {
    var need = flow.nodes.some(function (n) { return !n.x && !n.y; });
    if (!need) return;
    var col = {}, depth = {}, seen = {};
    var order = [], queue = flow.start ? [flow.start] : (flow.nodes[0] ? [flow.nodes[0].id] : []);
    while (queue.length) {
      var id = queue.shift(); if (seen[id]) continue; seen[id] = 1;
      var n = nodeById(id); if (!n) continue; order.push(n);
      var d = depth[id] || 0;
      ['next', 'if_true', 'if_false'].forEach(function (k) { if (n[k] && !seen[n[k]]) { depth[n[k]] = d + 1; queue.push(n[k]); } });
    }
    flow.nodes.forEach(function (n) { if (!(n.id in depth) && n.id !== flow.start) depth[n.id] = 0; });
    var rowCount = {};
    flow.nodes.forEach(function (n) {
      var d = depth[n.id] || 0; rowCount[d] = (rowCount[d] || 0);
      n.x = 60 + rowCount[d] * 220; n.y = 40 + d * 110; rowCount[d]++;
    });
  }

  function apiFetch(url, opts) {
    return fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  }
  function slugify(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
  }
  function save(cb) {
    var bad = commitRawFields();
    if (bad.length) { say('Fix invalid JSON in: ' + bad.join(', '), true); return; }
    var body = toApiFlow();
    if (!body.id) { body.id = slugify(body.name); idInput.value = body.id; }   // auto-id from the name
    if (!body.id) { say('Give the flow a name (top-left).', true); return; }
    if (!body.nodes.length) { say('Add at least one step.', true); return; }
    var verrs = FDCore.validateFlow(body);
    if (verrs.length) { say('Fix first: ' + verrs.slice(0, 3).join(' · '), true); return; }
    say('Saving…');
    apiFetch('/api/v1/flows', { method: 'POST', body: JSON.stringify({ flow: body }) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { say(res.j.detail || 'Save failed', true); return; }
        rootFlow.id = body.id;
        rootFlow.version = parseInt(res.j.version, 10) || rootFlow.version || 1;
        rootFlow.revision = String(res.j.revision || rootFlow.revision || '');
        rootFlow.published = !!res.j.published;
        rootFlow.published_version = parseInt(res.j.published_version, 10) || 0;
        rootFlow.published_revision = String(res.j.published_revision || '');
        say('Saved ✓'); if (cb) cb();
      }).catch(function () { say('Save failed', true); });
  }
  function publishFlow() {
    // Publish is the explicit activation authority. Save the reviewed canvas as
    // a draft, then bind activation to the exact CAS tokens the save returned.
    save(function () {
      apiFetch('/api/v1/flows/' + encodeURIComponent(rootFlow.id) + '/publish', {
        method: 'POST', body: JSON.stringify({
          version: rootFlow.version, revision: rootFlow.revision
        })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: r.ok, j: j };
        });
      }).then(function (res) {
        if (!res.ok) { say(res.j.detail || 'Publish failed', true); return; }
        rootFlow.published = true;
        rootFlow.published_version = parseInt(res.j.published_version, 10) || 0;
        rootFlow.published_revision = String(res.j.published_revision || '');
        say('Published v' + rootFlow.published_version);
      }).catch(function () { say('Publish failed', true); });
    });
  }
  function inputFields() {
    // The declared manual-run inputs as an mvForm field spec.
    return flowInputs.filter(function (s) { return (s.key || '').trim(); }).map(function (s) {
      return { key: s.key, label: s.label || s.key, type: (s.type || 'text'),
               required: !!s.required, default: (s.default != null ? s.default : undefined) };
    });
  }
  function run(dryRun) {
    var data = {};
    var raw = document.getElementById('fd-sample').value.trim();
    if (raw) { try { data = JSON.parse(raw); } catch (e) { say('Sample input is not valid JSON.', true); return; } }
    function proceed() {
      var request;
      if (dryRun) {
        var bad = commitRawFields();
        if (bad.length) { say('Fix invalid JSON in: ' + bad.join(', '), true); return; }
        var draftBody = toApiFlow();
        if (!draftBody.id) draftBody.id = slugify(draftBody.name);
        if (!draftBody.id) { say('Give the flow a name (top-left).', true); return; }
        if (!draftBody.nodes.length) { say('Add at least one step.', true); return; }
        var errors = FDCore.validateFlow(draftBody);
        if (errors.length) { say('Fix first: ' + errors.slice(0, 3).join(' / '), true); return; }
        // The unsaved graph is the execution input. This endpoint does not save,
        // publish, or let a draft schedule enter the scheduler inventory.
        request = apiFetch('/api/v1/flows/dry-run', {
          method: 'POST', body: JSON.stringify({ flow: draftBody, data: data })
        });
      } else {
        if (!rootFlow.id) { say('Publish the flow before running it for real.', true); return; }
        if (!rootFlow.published) { say('Publish a reviewed draft before running it for real.', true); return; }
        request = apiFetch('/api/v1/flows/' + encodeURIComponent(rootFlow.id) + '/run', {
          method: 'POST', body: JSON.stringify({ data: data, dry_run: false })
        });
      }
        request
          .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
          .then(function (res) {
            // Check res.ok first: a 4xx/5xx returns {detail} with no run_id, and
            // treating that as "Run started" would swallow the real failure.
            if (!res.ok) { say(res.j.detail || ('Run failed (' + res.status + ')'), true); return; }
            var j = res.j;
            if (!j.run_id) { say('Run started'); return; }
            runState = {}; runCursor = null; lastRunId = j.run_id;
            say((dryRun ? '🧪 Dry run ' : 'Running ') + '(run ' + j.run_id.slice(0, 8) + '…)');
            document.getElementById('fd-legend').classList.add('on');
            watchRun(j.run_id);
          }).catch(function () { say('Run failed', true); });
    }
    var fields = inputFields();
    if (fields.length) {   // the typed-input form doubles as the run confirmation
      window.mvForm((dryRun ? 'Dry run' : 'Run published') + ' “' + (rootFlow.name || rootFlow.id || 'flow') + '”',
        fields, { okText: dryRun ? 'Dry run' : 'Run published' }).then(function (vals) {
        if (vals === null) { say('Run cancelled.'); return; }
        Object.keys(vals).forEach(function (k) { data[k] = vals[k]; });   // declared inputs win over sample
        proceed();
      });
      return;
    }
    if (dryRun) { proceed(); return; }
    window.mvConfirm('Run the published revision? This creates real goals and calls real connectors.', { okText: 'Run published' })
      .then(function (ok) { if (ok) proceed(); });
  }
  function runUrl(runId) {
    return '/flows/' + encodeURIComponent(rootFlow.id) + '/runs/' + encodeURIComponent(runId);
  }
  function sayWithRunLink(text, runId) {
    say(text);
    var a = document.createElement('a'); a.href = runUrl(runId); a.textContent = 'view run →';
    a.style.marginLeft = '6px';
    msg.appendChild(a);
  }
  function handleRunUpdate(run, runId) {
    // Paint one run snapshot onto the canvas; returns true when the run has
    // reached a state that needs no further watching.
    runState = run.nodes || {};
    runCursor = (['paused_approval', 'paused_delay', 'paused_event'].indexOf(run.status) >= 0) ? run.cursor : null;
    render(); if (selected) renderProps();
    if (run.status === 'paused_approval') { sayWithRunLink('⏸ waiting for approval —', runId); return true; }
    if (run.status === 'paused_event') { sayWithRunLink('📥 waiting for an external event —', runId); return true; }
    if (run.status === 'indeterminate') {
      sayWithRunLink('⚠️ effect state is indeterminate; reconcile the external system before retrying —', runId);
      return true;
    }
    if (run.status === 'failed') {
      sayWithRunLink(runSummary(run) + ' ·', runId);
      offerFixIt(runId);
      return true;
    }
    if (['completed', 'rejected'].indexOf(run.status) >= 0) { sayWithRunLink(runSummary(run) + ' ·', runId); return true; }
    return false;
  }
  function watchRun(runId) {
    // Live status over SSE; falls back to polling where EventSource or the
    // stream isn't available (older proxies, stream-slot exhaustion).
    // Supersede any previous run's watch: close its stream and bump the
    // generation so a stale snapshot/poll can't repaint over the new run.
    if (activeES) { try { activeES.close(); } catch (e) {} activeES = null; }
    var gen = ++runGen;
    if (typeof EventSource === 'undefined') { pollRun(runId, 0, gen); return; }
    var es;
    try { es = new EventSource('/api/v1/flows/runs/' + encodeURIComponent(runId) + '/events'); }
    catch (e) { pollRun(runId, 0, gen); return; }
    activeES = es;
    var gotAny = false;
    function drop() { es.close(); if (activeES === es) activeES = null; }
    function onSnap(evt) {
      if (gen !== runGen) { drop(); return; }   // a newer run started; stop
      gotAny = true;
      try { if (handleRunUpdate(JSON.parse(evt.data), runId)) drop(); } catch (e) { /* keep streaming */ }
    }
    es.addEventListener('snapshot', onSnap);
    es.addEventListener('terminal', function (evt) { onSnap(evt); drop(); });
    es.addEventListener('timeout', function () { drop(); pollRun(runId, 0, gen); });
    es.onerror = function () {
      // never connected -> fall back to polling; a drop mid-stream lets
      // EventSource retry on its own
      if (!gotAny) { drop(); pollRun(runId, 0, gen); }
    };
  }
  function pollRun(runId, tick, gen) {
    // Poll until a terminal/paused state: fast at first, backing off to 3s so a
    // long run keeps updating the canvas instead of silently going dark.
    if (gen != null && gen !== runGen) return;   // superseded by a newer run
    apiFetch('/api/v1/flows/runs/' + encodeURIComponent(runId))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (run) {
        if (gen != null && gen !== runGen) return;
        if (!run) return;
        if (handleRunUpdate(run, runId)) return;
        var wait = Math.min(3000, 900 + (tick || 0) * 150);
        setTimeout(function () { pollRun(runId, (tick || 0) + 1, gen); }, wait);
      }).catch(function () {});
  }
  function offerFixIt(runId) {
    // One-click repair: open the copilot pre-armed with a diagnose+fix turn
    // grounded in the failed run's trace.
    var a = document.createElement('button');
    a.className = 'btn btn--ghost'; a.type = 'button'; a.textContent = '🔧 Fix it';
    a.style.marginLeft = '6px';
    a.addEventListener('click', function () {
      lastRunId = runId;
      chatToggle(true);
      var inp = document.getElementById('fd-chat-input');
      inp.value = 'The last run failed. Diagnose why from the run trace and propose the smallest patch that fixes it.';
      chatSend();
    });
    msg.appendChild(a);
  }
  function runSummary(run) {
    var ok = 0, err = 0, ns = run.nodes || {};
    for (var k in ns) { if (ns[k].outcome === 1) ok++; else if (ns[k].outcome === 0) err++; }
    var head = run.status === 'completed' ? '✓ completed' : (run.status === 'rejected' ? '✋ rejected' : '⚠ ' + (run.error || run.status));
    var cost = run.cost_dollars ? ' · $' + Number(run.cost_dollars).toFixed(2) : '';
    var origin = (run.origin && run.origin !== 'manual') ? ' · via ' + run.origin : '';
    return head + ' · ' + ok + ' ok' + (err ? ', ' + err + ' failed' : '') + cost + origin;
  }
  function draft() {
    var desc = document.getElementById('fd-nl').value.trim();
    if (!desc) { say('Describe your workflow first.', true); return; }
    say('Drafting… (a few seconds)');
    apiFetch('/api/v1/flows/draft', { method: 'POST', body: JSON.stringify({ description: desc, flow_id: idInput.value || '' }) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { say(res.j.detail || 'Draft failed', true); return; }
        pushHistory();                    // the pre-draft canvas is one Ctrl+Z away
        fromApiFlow(res.j.flow);
        say((res.j.notes && res.j.notes.length) ? res.j.notes[0] : 'Drafted ✓ — edit and save.');
      }).catch(function () { say('Draft failed', true); });
  }
  function load(id) {
    apiFetch('/api/v1/flows/' + encodeURIComponent(id)).then(function (r) {
      if (!r.ok) { say('Could not load flow', true); return; }
      return r.json().then(fromApiFlow);
    }).catch(function () { say('Could not load flow', true); });
    loadSuggestions(id);
    loadSchema(id);
  }
  function loadSchema(id) {
    // learned output shapes -> nested data pills (order.total, etc.)
    apiFetch('/api/v1/flows/' + encodeURIComponent(id) + '/schema')
      .then(function (r) { return r.ok ? r.json() : { schema: {} }; })
      .then(function (j) { flowSchema = j.schema || {}; if (selected) renderProps(); })
      .catch(function () {});
  }
  function loadSuggestions(id) {
    // self-rewrite suggestions (harden a reliable agent / soften a flaky action)
    apiFetch('/api/v1/flows/' + encodeURIComponent(id) + '/proposals').then(function (r) {
      return r.ok ? r.json() : { proposals: [] };
    }).then(function (j) {
      proposals = {};
      (j.proposals || []).forEach(function (p) { proposals[p.node_id] = p; });
      if (j.proposals && j.proposals.length) say('💡 ' + j.proposals.length + ' learned suggestion(s) — select a node to see them.');
      if (selected) renderProps();
    }).catch(function () {});
    // measured impact of changes already applied (before/after), via the aggregate
    apiFetch('/api/v1/flows/insights').then(function (r) { return r.ok ? r.json() : { flows: [] }; })
      .then(function (j) {
        impacts = {};
        (j.flows || []).forEach(function (f) {
          if (f.flow_id !== id) return;
          (f.changes || []).forEach(function (ch) { impacts[ch.node_id] = { changed: true, applied: ch, impact: ch.impact }; });
        });
        if (selected) renderProps();
      }).catch(function () {});
    // grounded learning signals: per-node success, approval accept-rate, trigger reliability
    apiFetch('/api/v1/flows/' + encodeURIComponent(id) + '/signals').then(function (r) {
      return r.ok ? r.json() : { nodes: {}, trigger: null };
    }).then(function (j) {
      signals = { nodes: j.nodes || {}, trigger: j.trigger || null };
      var lg = document.getElementById('fd-signal');
      if (lg) {
        if (signals.trigger && signals.trigger.n) {
          lg.textContent = '📊 triggered runs succeed ' + Math.round(signals.trigger.mean * 100) +
            '% (' + signals.trigger.n + ')';
          lg.hidden = false;
        } else { lg.hidden = true; }
      }
      if (selected) renderProps();
    }).catch(function () {});
  }
  function applyProposal(n, prop) {
    // Persisted apply: save the current graph, then enact the swap server-side so
    // it becomes a new version and its before/after impact is measured. Hardening
    // needs a tool: the node's own, else the one the loop inferred it keeps calling.
    var tool = n.tool || (prop.inferred_tool || '');
    if (prop.to_kind === 'action' && !tool) {
      say('Give this step a Tool first, then Apply (an action needs one).', true); return;
    }
    save(function () {
      var body = {
        node_id: n.id, to_kind: prop.to_kind, tool: tool,
        brief: n.brief || n.label || '',
        version: rootFlow.version, revision: rootFlow.revision
      };
      apiFetch('/api/v1/flows/' + encodeURIComponent(rootFlow.id) + '/apply', { method: 'POST', body: JSON.stringify(body) })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (!res.ok) { say(res.j.detail || 'Apply failed', true); return; }
          delete proposals[n.id];
          say('Applied ✓ — now v' + res.j.version + '. Impact appears as new runs land.');
          load(rootFlow.id);                          // reload the new version + refresh
        }).catch(function () { say('Apply failed', true); });
    });
  }
  function showHistory() {
    if (!rootFlow.id) { say('Save the flow first.', true); return; }
    apiFetch('/api/v1/flows/' + encodeURIComponent(rootFlow.id) + '/versions')
      .then(function (r) { return r.ok ? r.json() : { versions: [] }; })
      .then(function (j) {
        props.textContent = '';
        var h = document.createElement('div'); h.style.fontWeight = '600'; h.style.marginBottom = 'var(--space-3)';
        h.textContent = '⏱ Version history';
        props.appendChild(h);
        (j.versions || []).forEach(function (v) {
          var row = document.createElement('div'); row.className = 'field';
          var kinds = Object.keys(v.kinds || {}).map(function (k) { return v.kinds[k] + ' ' + k; }).join(', ');
          var lbl = document.createElement('div'); lbl.textContent = 'v' + v.version + ' — ' + v.nodes + ' nodes (' + kinds + ')';
          row.appendChild(lbl);
          var rb = document.createElement('button'); rb.className = 'btn btn--ghost'; rb.type = 'button'; rb.textContent = 'Roll back to v' + v.version;
          rb.addEventListener('click', function () { rollback(v.version); });
          row.appendChild(rb); props.appendChild(row);
        });
      }).catch(function () { say('Could not load history', true); });
  }
  function rollback(version) {
    apiFetch('/api/v1/flows/' + encodeURIComponent(rootFlow.id) + '/rollback', {
      method: 'POST',
      body: JSON.stringify({
        version: version,
        current_version: rootFlow.version,
        revision: rootFlow.revision
      })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { say(res.j.detail || 'Rollback failed', true); return; }
        say('Rolled back — now v' + res.j.version + '.'); load(rootFlow.id);
      }).catch(function () { say('Rollback failed', true); });
  }

  var EXAMPLES = [
    'When a new GitHub issue is opened, summarize it and post to Slack, but ask me before posting',
    'Every morning, pull yesterday’s Stripe charges and email me a one-paragraph summary',
    'When a HubSpot deal is marked won, create an onboarding task in Asana and notify the team',
  ];
  function buildExamples() {
    var box = document.getElementById('fd-examples'); if (!box) return;
    EXAMPLES.forEach(function (ex) {
      var c = document.createElement('button'); c.type = 'button'; c.className = 'fd__chip';
      c.textContent = '✨ ' + trunc(ex, 40); c.title = ex;
      c.addEventListener('click', function () { var nl = document.getElementById('fd-nl'); nl.value = ex; nl.focus(); });
      box.appendChild(c);
    });
  }
  // ---- export / import (flow JSON as a file) ----
  function exportJson() {
    var bad = commitRawFields();
    if (bad.length) { say('Fix invalid JSON in: ' + bad.join(', '), true); return; }
    var body = toApiFlow();
    var blob = new Blob([JSON.stringify(body, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (body.id || slugify(body.name) || 'flow') + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    say('Exported.');
  }
  function importJsonFile(file) {
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var f = JSON.parse(String(reader.result || ''));
        if (!f || !Array.isArray(f.nodes)) throw new Error('not a flow');
        pushHistory();
        fromApiFlow(f);
        say('Loaded from file — review and Save.');
      } catch (e) { say('That file is not a flow JSON.', true); }
    };
    reader.readAsText(file);
  }

  // ---- triggers panel: everything that starts THIS flow, in one place ----
  function showTriggers() {
    if (!rootFlow.id) { say('Save the flow first.', true); return; }
    props.textContent = '';
    var h = document.createElement('div'); h.style.fontWeight = '600'; h.style.marginBottom = 'var(--space-3)';
    h.textContent = '⚡ What starts this flow';
    props.appendChild(h);
    var sch = ((document.getElementById('fd-schedule') || {}).value || '').trim();
    props.appendChild(hint(sch ? ('⏰ Cron schedule: ' + sch + ' (edit it in the toolbar, then Save).')
                               : '⏰ No cron schedule — set one in the toolbar field.'));
    // inbound webhook triggers bound to this flow
    apiFetch('/api/v1/triggers').then(function (r) { return r.ok ? r.json() : { triggers: [] }; })
      .then(function (j) {
        var mine = (j.triggers || []).filter(function (t) { return t.flow === rootFlow.id; });
        var wl = document.createElement('div'); wl.className = 'field';
        var lab = document.createElement('label'); lab.textContent = '📡 Inbound webhooks (POST ' + (j.webhook_url || '/webhook/run') + ')';
        wl.appendChild(lab);
        mine.forEach(function (t) {
          var row = document.createElement('div'); row.style.display = 'flex'; row.style.gap = '4px'; row.style.marginBottom = '4px';
          var nm = document.createElement('span'); nm.style.flex = '1'; nm.textContent = '‘' + t.name + '’ → this flow';
          var rm = document.createElement('button'); rm.className = 'btn btn--ghost'; rm.type = 'button'; rm.textContent = '🗑';
          rm.setAttribute('aria-label', 'Remove webhook trigger');
          rm.addEventListener('click', function () {
            apiFetch('/api/v1/triggers/' + encodeURIComponent(t.name), { method: 'DELETE' })
              .then(function () { showTriggers(); }).catch(function () {});
          });
          row.appendChild(nm); row.appendChild(rm); wl.appendChild(row);
        });
        if (!mine.length) {
          var none = document.createElement('div'); none.className = 'fd__empty'; none.textContent = 'None yet.'; wl.appendChild(none);
        }
        var add = document.createElement('button'); add.className = 'btn btn--ghost'; add.type = 'button'; add.style.width = '100%';
        add.textContent = '+ Add a webhook trigger for this flow';
        add.addEventListener('click', function () {
          apiFetch('/api/v1/triggers', { method: 'POST', body: JSON.stringify({ flow: rootFlow.id }) })
            .then(function (r) { return r.json().then(function (jj) { return { ok: r.ok, j: jj }; }); })
            .then(function (res) {
              if (!res.ok) { say(res.j.detail || 'Could not create the trigger', true); return; }
              if (!res.j.secret_configured) say('Trigger armed — set a [webhooks] secret so it can fire.', true);
              showTriggers();
            }).catch(function () { say('Could not create the trigger', true); });
        });
        wl.appendChild(add); props.appendChild(wl);
      }).catch(function () {});
    // polled event triggers bound to this flow (managed on Automations)
    apiFetch('/api/v1/event-triggers').then(function (r) { return r.ok ? r.json() : { triggers: [] }; })
      .then(function (j) {
        var mine = (j.triggers || []).filter(function (t) { return t.flow === rootFlow.id; });
        var el2 = document.createElement('div'); el2.className = 'field';
        var lab2 = document.createElement('label'); lab2.textContent = '🛰 Polled event triggers';
        el2.appendChild(lab2);
        mine.forEach(function (t) {
          var d = document.createElement('div'); d.className = 'fd__empty';
          d.textContent = '‘' + t.name + '’ — ' + t.source; el2.appendChild(d);
        });
        var link = document.createElement('a'); link.href = '/automations';
        link.textContent = (mine.length ? 'Manage' : 'Add one') + ' on the Automations page →';
        el2.appendChild(link); props.appendChild(el2);
      }).catch(function () {});
  }

  // ---- gallery: curated starter graphs (one click -> editable canvas) ----
  function loadGallery() {
    apiFetch('/api/v1/flows/gallery').then(function (r) {
        // Flow engine off (the default install): say so up front instead of
        // letting the user build a whole flow and only learn it on Save.
        if (r.status === 403) {
          var msg = document.getElementById('fd-msg');
          if (msg && !msg.textContent) {
            msg.textContent = 'Flows are disabled on this server — you can design and export, '
              + 'but Save/Run need an administrator to enable [flows].';
          }
        }
        return r.ok ? r.json() : { flows: [] };
      })
      .then(function (j) {
        var box = document.getElementById('fd-examples'); if (!box) return;
        (j.flows || []).forEach(function (g) {
          var c = document.createElement('button'); c.type = 'button'; c.className = 'fd__chip';
          c.textContent = '📋 ' + g.name; c.title = (g.description || '') + ' (loads onto the canvas)';
          c.addEventListener('click', function () {
            pushHistory();
            fromApiFlow(g);
            var schEl = document.getElementById('fd-schedule');
            if (schEl && g.schedule) schEl.value = g.schedule;
            say('Loaded “' + g.name + '” — edit it, or tell the Copilot what to change.');
          });
          box.appendChild(c);
        });
      }).catch(function () {});
  }

  // ---- copilot chat: converse to build, edit, explain, and repair the flow ----
  function chatMsgEl(role, text) {
    var d = document.createElement('div'); d.className = 'fd__chat-msg ' + role; d.textContent = text; return d;
  }
  function chatAppend(role, text) {
    var log = document.getElementById('fd-chat-log');
    var el = chatMsgEl(role, text);
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }
  function placeNewNodes(f) {
    // Give canvas positions to nodes the copilot just added (patched nodes keep
    // theirs), so an AI edit lands NEXT TO its neighbors instead of re-laying
    // out the user's whole arrangement.
    var byId = {};
    (f.nodes || []).forEach(function (n) { byId[n.id] = n; });
    var maxY = 40;
    (f.nodes || []).forEach(function (n) { if (n.y) maxY = Math.max(maxY, n.y); });
    (f.nodes || []).forEach(function (n) {
      if (n.x || n.y) return;
      var pred = (f.nodes || []).filter(function (m) {
        return m !== n && (m.next === n.id || m.if_true === n.id || m.if_false === n.id || m.on_error === n.id);
      })[0];
      if (pred && (pred.x || pred.y)) { n.x = (pred.x || 0) + 24; n.y = (pred.y || 0) + 110; }
      else { n.x = 60; n.y = maxY + 110; }
      maxY = Math.max(maxY, n.y);
    });
  }
  function chatSend() {
    var inp = document.getElementById('fd-chat-input');
    var text = (inp.value || '').trim();
    if (!text) return;
    var bad = commitRawFields();
    if (bad.length) { say('Fix invalid JSON in: ' + bad.join(', '), true); return; }
    inp.value = '';
    chatAppend('user', text);
    chatHistory.push({ role: 'user', content: text });
    // Track THIS request's note element so concurrent sends each remove their
    // own "…thinking" line instead of blindly deleting whatever is last.
    var thinking = chatAppend('note', '…thinking');
    function clearThinking() { if (thinking && thinking.parentNode) thinking.parentNode.removeChild(thinking); }
    var body = { message: text, flow: toApiFlow(), history: chatHistory.slice(-12), run_id: lastRunId || '' };
    apiFetch('/api/v1/flows/chat', { method: 'POST', body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        clearThinking();
        if (!res.ok) { chatAppend('note', res.j.detail || 'The copilot call failed.'); return; }
        var j = res.j;
        chatAppend('assistant', j.reply || 'Done.');
        chatHistory.push({ role: 'assistant', content: j.reply || '' });
        (j.notes || []).forEach(function (nt) { chatAppend('note', nt); });
        if (j.flow) {
          pushHistory();                                      // "undo what the AI did" is one Ctrl+Z
          placeNewNodes(j.flow);
          var keep = { x: view.x, y: view.y, k: view.k };
          fromApiFlow(j.flow);
          view = keep; render();
          if (j.applied && j.applied.length) chatAppend('note', '✏️ ' + j.applied.join(' · '));
          say('🤖 Copilot edited the flow — review and Save.');
        }
      }).catch(function () {
        clearThinking();
        chatAppend('note', 'The copilot call failed.');
      });
  }
  function chatToggle(showOnly) {
    var box = document.getElementById('fd-chat');
    if (showOnly) box.classList.add('on'); else box.classList.toggle('on');
    if (box.classList.contains('on')) document.getElementById('fd-chat-input').focus();
  }

  function loadTools(q, category) {
    var params = [];
    if (q) params.push('q=' + encodeURIComponent(q));
    if (category) params.push('category=' + encodeURIComponent(category));
    var url = '/api/v1/flows/tools' + (params.length ? ('?' + params.join('&')) : '');
    apiFetch(url).then(function (r) { return r.ok ? r.json() : { tools: [] }; })
      .then(function (j) {
        if (j.categories && j.categories.length) toolCategories = j.categories;
        var dl = document.getElementById('fd-tools'); if (dl) dl.textContent = '';
        (j.tools || []).forEach(function (t) {
          toolCatalog[t.name] = { description: t.description || '', params: t.params || [], category: t.category || '' };
          if (dl) {
            var o = document.createElement('option'); o.value = t.name;
            // Native datalist option label = category + description, so the
            // typeahead itself shows which bucket each connector is in.
            var lbl = t.category ? ('[' + t.category + '] ') : '';
            if (t.description) o.label = lbl + t.description; else if (lbl) o.label = lbl;
            dl.appendChild(o);
          }
        });
        if (selected) renderProps();
      }).catch(function () {});
  }
  // Debounced registry search: typing in an action node's tool field searches
  // EVERY live connector (built-ins + enabled enterprise connectors), narrowed
  // to the chosen category when one is picked. An empty query with a category
  // browses that whole bucket; an empty query with no category = curated set.
  var toolSearchTimer = null;
  function scheduleToolSearch(q) {
    if (toolSearchTimer) clearTimeout(toolSearchTimer);
    toolSearchTimer = setTimeout(function () {
      loadTools((q || '').trim().length >= 2 ? q.trim() : '', toolFilterCategory);
    }, 250);
  }

  // ---- wire ----
  document.querySelectorAll('[data-add]').forEach(function (b) {
    b.addEventListener('click', function () { addNode(b.getAttribute('data-add')); });
  });
  document.getElementById('fd-draft').addEventListener('click', draft);
  document.getElementById('fd-save').addEventListener('click', function () { save(); });
  document.getElementById('fd-publish').addEventListener('click', publishFlow);
  document.getElementById('fd-run').addEventListener('click', function () { run(true); });
  document.getElementById('fd-run-real').addEventListener('click', function () { run(false); });
  document.getElementById('fd-history').addEventListener('click', showHistory);
  document.getElementById('fd-undo').addEventListener('click', undo);
  document.getElementById('fd-redo').addEventListener('click', redo);
  document.getElementById('fd-fit').addEventListener('click', zoomToFit);
  document.getElementById('fd-triggers').addEventListener('click', showTriggers);
  document.getElementById('fd-minimap').addEventListener('mousedown', function (evt) {
    // click the overview to center the canvas on that spot
    evt.preventDefault();
    var mm = document.getElementById('fd-minimap');
    var mr = mm.getBoundingClientRect();
    var s = miniScale();
    var wx = (evt.clientX - mr.left - MINI_PAD) / s.k + s.minX;
    var wy = (evt.clientY - mr.top - MINI_PAD) / s.k + s.minY;
    var r = canvas.getBoundingClientRect();
    view.x = r.width / 2 - wx * view.k;
    view.y = r.height / 2 - wy * view.k;
    render();
  });
  document.getElementById('fd-export').addEventListener('click', exportJson);
  document.getElementById('fd-import-json').addEventListener('click', function () {
    document.getElementById('fd-import-file').click();
  });
  document.getElementById('fd-import-file').addEventListener('change', function (e) {
    if (e.target.files && e.target.files[0]) { importJsonFile(e.target.files[0]); e.target.value = ''; }
  });
  document.getElementById('fd-chat-toggle').addEventListener('click', function () { chatToggle(); });
  document.getElementById('fd-chat-close').addEventListener('click', function () { chatToggle(); });
  document.getElementById('fd-chat-form').addEventListener('submit', function (e) { e.preventDefault(); chatSend(); });
  document.getElementById('fd-nl').addEventListener('keydown', function (e) { if (e.key === 'Enter') draft(); });
  nameInput.addEventListener('input', function () { rootFlow.name = nameInput.value; });
  // keyboard: Delete removes the selected node/edge, Esc deselects, Ctrl+Z/Y
  // undo/redo, Ctrl+D duplicates (all no-ops while typing in a field)
  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    var typing = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable);
    var mod = e.ctrlKey || e.metaKey;
    if (mod && !typing && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
    if (mod && !typing && (e.key === 'y' || e.key === 'Y')) { e.preventDefault(); redo(); return; }
    if (mod && !typing && (e.key === 'd' || e.key === 'D') && selected) { e.preventDefault(); duplicateNode(selected); return; }
    if (mod && !typing && (e.key === 'c' || e.key === 'C') && selected) { e.preventDefault(); copyNode(selected); return; }
    if (mod && !typing && (e.key === 'v' || e.key === 'V')) { e.preventDefault(); pasteNode(); return; }
    if (e.key === 'Escape' && !typing) { selected = null; selectedEdge = null; multiSel = {}; render(); renderProps(); }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && !typing) {
      if (selectedEdge) { e.preventDefault(); deleteEdge(selectedEdge); }
      else if (Object.keys(multiSel).length) { e.preventDefault(); deleteSelection(); }
      else if (selected) { e.preventDefault(); deleteNode(selected); }
    }
  });

  buildExamples();
  loadGallery();
  loadTools();
  var editId = canvas.getAttribute('data-flow-id');
  var handoff = null;
  try {
    handoff = JSON.parse(sessionStorage.getItem('lightwork.authoring-handoff') || 'null');
    if (!handoff || handoff.kind !== 'flow' || !handoff.brief
        || Date.now() - Number(handoff.created_at || 0) > 10 * 60 * 1000) handoff = null;
    if (handoff) sessionStorage.removeItem('lightwork.authoring-handoff');
  } catch (e) { handoff = null; }
  var seedEl = document.getElementById('fd-seed');
  var seed = null;
  if (seedEl) { try { seed = JSON.parse(seedEl.textContent); } catch (e) { seed = null; } }
  if (editId) load(editId);
  else if (handoff) {
    render(); renderProps();
    document.getElementById('fd-nl').value = String(handoff.brief).slice(0, 8000);
    draft();
  }
  else if (seed && seed.nodes) {   // ?from_template= -> the template as a 1-node flow
    fromApiFlow(seed);
    say('Loaded the template as a flow — add branches, approvals, or loops around it.');
  } else { render(); renderProps(); }

  // The friendly Steps view renders the LIVE graph (not just the server seed),
  // so nodes built on the canvas show up the moment you switch views.
  window.__fdLiveFlow = toApiFlow;
})();
{% endraw %}
