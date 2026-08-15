// Pure, DOM-free flow (de)serialization for the visual designer. Kept in its
// own file so it can be unit-tested under node (see tests/js/flow_designer_core.test.js)
// AND included into the designer template. No DOM, no globals beyond FDCore.
var FDCore = (function () {
  'use strict';
  var NODE_DEFAULTS = {
    params: {}, brief: '', tool: '', condition: '', if_true: null, if_false: null,
    cases: [], items: '', var: 'item', limit: 0, concurrent: false, prompt: '',
    choices: [], assignee: '', expires_after: 0, on_expire: null, form: [],
    seconds: 0, flow_ref: '',
    retries: 0, retry_backoff: 0, on_error: null, timeout: 0, output: '', label: '',
    assignments: {}, subflow_inputs: {}, body: null, branches: [], next: null
  };
  var SCALARS = ['next', 'tool', 'brief', 'condition', 'if_true', 'if_false',
    'items', 'var', 'prompt', 'flow_ref', 'on_error', 'output', 'label',
    'assignee', 'on_expire'];

  function defaultValue(v) {
    if (Array.isArray(v)) return v.slice();
    if (v && typeof v === 'object') return Object.assign({}, v);
    return v;
  }

  // Upgrade a raw node dict into an editable node object (defaults + id fallback).
  function normalizeNode(nd, i) {
    var n = {}, k;
    for (k in NODE_DEFAULTS) n[k] = defaultValue(NODE_DEFAULTS[k]);
    for (k in (nd || {})) n[k] = nd[k];
    n.id = n.id || ('n' + i);
    return n;
  }

  // Serialize ONE node to the wire dict the server's Flow.from_dict accepts.
  // Recurses into nested body/branches; drops empty sub-flows so they revert to
  // the engine's "no body/branches" validation. Reads only known fields, so it
  // works on both normalized node objects AND raw (never-edited) node dicts.
  function serializeNode(n) {
    var o = { id: n.id, kind: n.kind };
    SCALARS.forEach(function (k) {
      if (n[k] !== '' && n[k] != null) o[k] = n[k];
    });
    if (n.params && Object.keys(n.params).length) o.params = n.params;
    if (n.seconds) o.seconds = n.seconds;
    if (n.retries) o.retries = n.retries;
    if (n.retry_backoff) o.retry_backoff = n.retry_backoff;
    if (n.timeout) o.timeout = n.timeout;
    if (n.limit) o.limit = n.limit;
    if (n.concurrent) o.concurrent = true;
    if (n.cases && n.cases.length) o.cases = n.cases;
    if (n.choices && n.choices.length) o.choices = n.choices;
    if (n.expires_after) o.expires_after = n.expires_after;
    if (n.form && n.form.length) o.form = n.form.filter(function (f) { return f && f.name; });
    if (n.assignments && Object.keys(n.assignments).length) o.assignments = n.assignments;
    if (n.subflow_inputs && Object.keys(n.subflow_inputs).length) o.subflow_inputs = n.subflow_inputs;
    if (n.body && n.body.nodes && n.body.nodes.length) o.body = serializeFlow(n.body);
    if (n.branches && n.branches.length) {
      var bs = n.branches.map(serializeFlow).filter(function (b) { return b.nodes.length; });
      if (bs.length) o.branches = bs;
    }
    if (n.x || n.y) { o.x = n.x; o.y = n.y; }
    return o;
  }

  function serializeFlow(f) {
    return {
      id: f.id || '', name: f.name || '', start: f.start || '',
      nodes: (f.nodes || []).map(serializeNode)
    };
  }

  // A deep, independent snapshot of a flow (the undo/redo unit). Serialized
  // form, so restoring goes through normalizeNode like a server load.
  function cloneFlow(f) {
    return JSON.parse(JSON.stringify(serializeFlow(f)));
  }

  // Output keys visible to `nodeId` from nodes that can run BEFORE it (any node
  // that reaches it via next/if_true/if_false/on_error) -- the data pills a
  // user clicks to insert an upstream key. Deterministic order: graph order of
  // the flow's nodes. (No double-brace token in this comment: the file is BOTH
  // Jinja-included into the designer AND require()'d by the node tests, so it
  // must stay literal JS with nothing Jinja could try to evaluate.)
  function upstreamOutputs(f, nodeId) {
    var nodes = f.nodes || [];
    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    function reaches(fromId, toId) {
      var seen = {}, queue = [fromId];
      while (queue.length) {
        var id = queue.shift();
        if (id === toId) return true;
        if (!id || seen[id]) continue;
        seen[id] = 1;
        var n = byId[id];
        if (!n) continue;
        ['next', 'if_true', 'if_false', 'on_error'].forEach(function (k) {
          if (n[k]) queue.push(n[k]);
        });
      }
      return false;
    }
    var keys = [];
    nodes.forEach(function (n) {
      if (n.id !== nodeId && n.output && reaches(n.id, nodeId) && keys.indexOf(n.output) < 0)
        keys.push(n.output);
    });
    return keys;
  }

  // Client-side structural validation -- the same rules the server's
  // Flow.validate() enforces, so a broken graph is flagged BEFORE the save
  // round-trip. Returns a list of human-readable problems (empty = valid).
  function validateFlow(f) {
    var errs = [];
    var nodes = f.nodes || [];
    if (!nodes.length) return ['flow has no nodes'];
    var ids = {};
    nodes.forEach(function (n) { ids[n.id] = true; });
    if (!f.start || !ids[f.start]) errs.push('start node "' + f.start + '" is not in the flow');
    nodes.forEach(function (n) {
      ['next', 'if_true', 'if_false', 'on_error', 'on_expire'].forEach(function (k) {
        if (n[k] != null && !ids[n[k]]) errs.push(n.id + ': ' + k + ' points at unknown node "' + n[k] + '"');
      });
      if (n.kind === 'action' && !n.tool) errs.push('action ' + n.id + ' has no tool');
      if (n.kind === 'agent' && !n.brief) errs.push('agent ' + n.id + ' has no brief');
      if (n.kind === 'branch' && !n.condition) errs.push('branch ' + n.id + ' has no condition');
      if (n.kind === 'switch') {
        if (!n.condition) errs.push('switch ' + n.id + ' has no key');
        if (!(n.cases && n.cases.length)) errs.push('switch ' + n.id + ' has no cases');
        (n.cases || []).forEach(function (c, i) {
          if (c.to != null && !ids[c.to]) errs.push('switch ' + n.id + ' case ' + i + ' points at unknown node "' + c.to + '"');
        });
      }
      if (n.kind === 'while') {
        if (!n.condition) errs.push('while ' + n.id + ' has no condition');
        if (!(n.body && n.body.nodes && n.body.nodes.length)) errs.push('while ' + n.id + ' has an empty body');
      }
      if (n.kind === 'scope' && !(n.body && n.body.nodes && n.body.nodes.length)) errs.push('scope ' + n.id + ' has an empty body');
      if (n.kind === 'wait_event' && !n.prompt) errs.push('wait ' + n.id + ' has no prompt');
      if (n.kind === 'foreach') {
        if (!n.items) errs.push('foreach ' + n.id + ' has no items key');
        if (!(n.body && n.body.nodes && n.body.nodes.length)) errs.push('foreach ' + n.id + ' has an empty body');
      }
      if (n.kind === 'parallel' && !(n.branches && n.branches.some(function (b) { return b && b.nodes && b.nodes.length; })))
        errs.push('parallel ' + n.id + ' has no branches');
      if (n.kind === 'approval' && !n.prompt) errs.push('approval ' + n.id + ' has no prompt');
      if (n.kind === 'subflow' && !n.flow_ref) errs.push('subflow ' + n.id + ' has no flow reference');
    });
    return errs;
  }

  // The view {x, y, k} that fits every node into a cw x ch canvas with padding.
  // Zoom is clamped to the designer's wheel range; an empty flow gets the
  // default view.
  function fitView(nodes, cw, ch, nodeW, nodeH, pad) {
    if (!nodes || !nodes.length) return { x: 40, y: 20, k: 1 };
    pad = pad == null ? 40 : pad;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(function (n) {
      minX = Math.min(minX, n.x || 0); minY = Math.min(minY, n.y || 0);
      maxX = Math.max(maxX, (n.x || 0) + nodeW); maxY = Math.max(maxY, (n.y || 0) + nodeH);
    });
    var w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
    var k = Math.min((cw - 2 * pad) / w, (ch - 2 * pad) / h);
    k = Math.max(0.35, Math.min(1, k));
    return {
      x: Math.round((cw - w * k) / 2 - minX * k),
      y: Math.round((ch - h * k) / 2 - minY * k),
      k: k
    };
  }

  // ---- condition grammar (mirrors the server's runner._COND parser) --------
  // Kept HERE (the tested shared module) so the builder UI and any future
  // consumer share one copy; changing an op means updating this + the runner.
  var COND_OPS = ['==', '!=', '>', '<', '>=', '<=', 'contains'];
  var COND_RE = /^\s*(\S+)\s*(==|!=|>=|<=|>|<|contains)\s*(.+?)\s*$/;

  return { normalizeNode: normalizeNode, serializeNode: serializeNode,
           serializeFlow: serializeFlow, cloneFlow: cloneFlow,
           upstreamOutputs: upstreamOutputs, fitView: fitView,
           validateFlow: validateFlow,
           COND_OPS: COND_OPS, COND_RE: COND_RE };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = FDCore;
