// Unit tests for the designer's pure flow serialization (FDCore). Run with:
//   node --test packages/maverick-dashboard/tests/js/
// Covers the nested foreach-body / parallel-branch drill-in serialization the
// visual designer relies on, which is otherwise DOM-bound and untested.
'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const FDCore = require(path.join(__dirname, '..', '..', 'maverick_dashboard',
  'templates', 'flow_designer_core.js'));

test('normalizeNode fills defaults and an id fallback', () => {
  const n = FDCore.normalizeNode({ kind: 'agent', brief: 'hi' }, 2);
  assert.equal(n.id, 'n2');
  assert.equal(n.kind, 'agent');
  assert.equal(n.brief, 'hi');
  assert.deepEqual(n.params, {});
  assert.equal(n.next, null);
});

test('serializeNode omits empty scalars and keeps set ones', () => {
  const o = FDCore.serializeNode(FDCore.normalizeNode({ id: 'a', kind: 'agent', brief: 'do it' }, 0));
  assert.equal(o.id, 'a');
  assert.equal(o.kind, 'agent');
  assert.equal(o.brief, 'do it');
  // empty scalars and empty params are dropped; non-empty defaults (var) are kept
  assert.ok(!('tool' in o) && !('params' in o) && !('condition' in o) && !('next' in o));
});

test('serializeNode carries retry_backoff only when set', () => {
  const on = FDCore.serializeNode(FDCore.normalizeNode(
    { id: 'a', kind: 'action', tool: 't', retries: 3, retry_backoff: 2.5 }, 0));
  assert.equal(on.retry_backoff, 2.5);
  const off = FDCore.serializeNode(FDCore.normalizeNode(
    { id: 'b', kind: 'action', tool: 't', retries: 3 }, 0));
  assert.ok(!('retry_backoff' in off));   // 0 -> omitted (tight loop, no wire noise)
});

test('serializeFlow round-trips a nested foreach body', () => {
  const flow = {
    id: 'f', name: 'F', start: 'a',
    nodes: [FDCore.normalizeNode({
      id: 'a', kind: 'foreach', items: 'rows',
      body: { id: '', name: 'body', start: 'x',
        nodes: [FDCore.normalizeNode({ id: 'x', kind: 'agent', brief: 'per {{item}}' }, 0)] }
    }, 0)]
  };
  const out = FDCore.serializeFlow(flow);
  assert.equal(out.nodes[0].kind, 'foreach');
  assert.equal(out.nodes[0].body.nodes[0].brief, 'per {{item}}');
  assert.equal(out.nodes[0].body.start, 'x');
});

test('serializeFlow keeps non-empty parallel branches, drops empty ones', () => {
  const flow = {
    id: 'f', name: 'F', start: 'p',
    nodes: [FDCore.normalizeNode({
      id: 'p', kind: 'parallel',
      branches: [
        { id: '', name: 'b1', start: 'l', nodes: [FDCore.normalizeNode({ id: 'l', kind: 'agent', brief: 'left' }, 0)] },
        { id: '', name: 'b2', start: '', nodes: [] }  // empty -> dropped
      ]
    }, 0)]
  };
  const out = FDCore.serializeFlow(flow);
  assert.equal(out.nodes[0].branches.length, 1);
  assert.equal(out.nodes[0].branches[0].nodes[0].brief, 'left');
});

test('a foreach whose body has no nodes drops the body entirely', () => {
  const flow = {
    id: 'f', name: 'F', start: 'a',
    nodes: [FDCore.normalizeNode({ id: 'a', kind: 'foreach', items: 'rows',
      body: { id: '', name: 'body', start: '', nodes: [] } }, 0)]
  };
  const out = FDCore.serializeFlow(flow);
  assert.ok(!('body' in out.nodes[0]));   // reverts to the engine's "no body" validation
});

test('serializeNode works on a raw (never-normalized) nested dict', () => {
  // a flow loaded from the server: nested nodes are raw dicts, not normalized
  const raw = { id: 'a', kind: 'foreach', items: 'r',
    body: { id: '', name: '', start: 'x', nodes: [{ id: 'x', kind: 'agent', brief: 'b' }] } };
  const o = FDCore.serializeNode(raw);
  assert.equal(o.body.nodes[0].brief, 'b');
});

test('cloneFlow is a deep, independent snapshot', () => {
  const flow = { id: 'f', name: 'F', start: 'a',
    nodes: [FDCore.normalizeNode({ id: 'a', kind: 'agent', brief: 'one' }, 0)] };
  const snap = FDCore.cloneFlow(flow);
  flow.nodes[0].brief = 'changed';
  assert.equal(snap.nodes[0].brief, 'one');
});

test('upstreamOutputs returns only keys from nodes that reach the target', () => {
  const flow = { id: 'f', name: 'F', start: 'a', nodes: [
    FDCore.normalizeNode({ id: 'a', kind: 'agent', brief: 'x', output: 'summary', next: 'b' }, 0),
    FDCore.normalizeNode({ id: 'b', kind: 'branch', condition: 'x == 1', if_true: 'c', if_false: 'd' }, 1),
    FDCore.normalizeNode({ id: 'c', kind: 'action', tool: 't', output: 'sent' }, 2),
    FDCore.normalizeNode({ id: 'd', kind: 'agent', brief: 'y', output: 'other' }, 3),
  ] };
  // c sees a's output (a -> b -> c) but not d's (a sibling branch arm)
  assert.deepEqual(FDCore.upstreamOutputs(flow, 'c'), ['summary']);
  // a has nothing upstream; its own output is not offered to itself
  assert.deepEqual(FDCore.upstreamOutputs(flow, 'a'), []);
});

test('upstreamOutputs follows on_error routing and survives cycles', () => {
  const flow = { id: 'f', name: 'F', start: 'a', nodes: [
    FDCore.normalizeNode({ id: 'a', kind: 'action', tool: 't', output: 'r', on_error: 'h', next: 'a' }, 0),
    FDCore.normalizeNode({ id: 'h', kind: 'agent', brief: 'recover' }, 1),
  ] };
  assert.deepEqual(FDCore.upstreamOutputs(flow, 'h'), ['r']);
});

test('serializeNode carries setvar assignments', () => {
  const sv = FDCore.serializeNode(FDCore.normalizeNode(
    { id: 'v', kind: 'setvar', assignments: { n: '{{add(c,1)}}' } }, 0));
  assert.deepEqual(sv.assignments, { n: '{{add(c,1)}}' });
});

test('normalizeNode gives mutable defaults independent objects', () => {
  const a = FDCore.normalizeNode({ id: 'a', kind: 'subflow', flow_ref: 'trusted' }, 0);
  const b = FDCore.normalizeNode({ id: 'b', kind: 'subflow', flow_ref: 'untrusted' }, 1);
  assert.notStrictEqual(a.params, b.params);
  assert.notStrictEqual(a.assignments, b.assignments);
  assert.notStrictEqual(a.subflow_inputs, b.subflow_inputs);
  assert.notStrictEqual(a.cases, b.cases);
  assert.notStrictEqual(a.branches, b.branches);

  a.subflow_inputs.secret = '{{secret_token}}';
  assert.deepEqual(b.subflow_inputs, {});

  const out = FDCore.serializeFlow({ id: 'f', name: 'F', start: 'a', nodes: [a, b] });
  assert.deepEqual(out.nodes[0].subflow_inputs, { secret: '{{secret_token}}' });
  assert.ok(!('subflow_inputs' in out.nodes[1]));
});

test('serializeNode carries subflow_inputs only when set', () => {
  const on = FDCore.serializeNode(FDCore.normalizeNode(
    { id: 's', kind: 'subflow', flow_ref: 'child', subflow_inputs: { who: '{{name}}' } }, 0));
  assert.deepEqual(on.subflow_inputs, { who: '{{name}}' });
  const off = FDCore.serializeNode(FDCore.normalizeNode(
    { id: 's2', kind: 'subflow', flow_ref: 'child' }, 0));
  assert.ok(!('subflow_inputs' in off));   // empty mapping omitted
});

test('serializeNode keeps switch cases, approval choices, concurrent flag', () => {
  const o = FDCore.serializeNode(FDCore.normalizeNode({
    id: 's', kind: 'switch', condition: 'k',
    cases: [{ value: 'a', to: 'x' }], choices: [], concurrent: false }, 0));
  assert.deepEqual(o.cases, [{ value: 'a', to: 'x' }]);
  assert.ok(!('choices' in o) && !('concurrent' in o));   // empty/false dropped
  const f = FDCore.serializeNode(FDCore.normalizeNode({
    id: 'l', kind: 'foreach', items: 'r', concurrent: true,
    body: { id: '', name: '', start: 'n', nodes: [{ id: 'n', kind: 'agent', brief: 'b' }] } }, 0));
  assert.equal(f.concurrent, true);
  const a = FDCore.serializeNode(FDCore.normalizeNode({
    id: 'ap', kind: 'approval', prompt: 'p', choices: ['ship', 'hold'] }, 0));
  assert.deepEqual(a.choices, ['ship', 'hold']);
});

test('serializeNode keeps approval human-task fields, drops empty form rows', () => {
  const o = FDCore.serializeNode(FDCore.normalizeNode({
    id: 'ap', kind: 'approval', prompt: 'ok?', assignee: '@alice',
    expires_after: 3600, on_expire: 'esc',
    form: [{ name: 'reason', label: 'Why?' }, { name: '', label: 'blank' }] }, 0));
  assert.equal(o.assignee, '@alice');
  assert.equal(o.expires_after, 3600);
  assert.equal(o.on_expire, 'esc');
  assert.deepEqual(o.form, [{ name: 'reason', label: 'Why?' }]);   // blank row dropped
  // an approval with no human-task extras stays lean
  const bare = FDCore.serializeNode(FDCore.normalizeNode({ id: 'b', kind: 'approval', prompt: 'p' }, 0));
  assert.ok(!('assignee' in bare) && !('expires_after' in bare) && !('form' in bare) && !('on_expire' in bare));
});

test('validateFlow flags a dangling on_expire route', () => {
  const bad = {
    id: 'f', name: 'F', start: 'a',
    nodes: [FDCore.normalizeNode({ id: 'a', kind: 'approval', prompt: 'p', on_expire: 'ghost' }, 0)]
  };
  assert.ok(FDCore.validateFlow(bad).some(e => e.includes('on_expire') && e.includes('ghost')));
});

test('validateFlow flags the same structural problems the server does', () => {
  const bad = {
    id: 'f', name: 'F', start: 'zzz',
    nodes: [
      FDCore.normalizeNode({ id: 'a', kind: 'agent', next: 'ghost' }, 0),
      FDCore.normalizeNode({ id: 's', kind: 'switch', condition: '', cases: [{ value: 'x', to: 'ghost' }] }, 1),
      FDCore.normalizeNode({ id: 'w', kind: 'wait_event' }, 2),
    ]
  };
  const errs = FDCore.validateFlow(bad);
  assert.ok(errs.some(e => e.includes('start node')));
  assert.ok(errs.some(e => e.includes('unknown node "ghost"')));
  assert.ok(errs.some(e => e.includes('has no brief')));
  assert.ok(errs.some(e => e.includes('has no key')));
  assert.ok(errs.some(e => e.includes('has no prompt')));
  const good = {
    id: 'f', name: 'F', start: 'a',
    nodes: [FDCore.normalizeNode({ id: 'a', kind: 'agent', brief: 'do it' }, 0)]
  };
  assert.deepEqual(FDCore.validateFlow(good), []);
});

test('fitView centers and clamps zoom', () => {
  const v = FDCore.fitView([{ x: 0, y: 0 }, { x: 1000, y: 800 }], 600, 400, 176, 64, 40);
  assert.ok(v.k >= 0.35 && v.k <= 1);
  // fitting a tiny flow never zooms IN past 1:1
  const v2 = FDCore.fitView([{ x: 10, y: 10 }], 800, 600, 176, 64, 40);
  assert.equal(v2.k, 1);
  // empty flow -> default view
  assert.deepEqual(FDCore.fitView([], 800, 600, 176, 64, 40), { x: 40, y: 20, k: 1 });
});

test('condition grammar parses simple predicates and rejects composites', () => {
  // one source of truth for the builder UI -- mirrors runner._COND
  const m = FDCore.COND_RE.exec('total >= 100');
  assert.ok(m && m[1] === 'total' && m[2] === '>=' && m[3] === '100');
  const c = FDCore.COND_RE.exec('name contains "bob"');
  assert.ok(c && c[2] === 'contains');
  assert.ok(FDCore.COND_OPS.includes('contains') && FDCore.COND_OPS.length === 7);
  // quoted values keep their quotes (raw mode handles stripping)
  const q = FDCore.COND_RE.exec('status == "open item"');
  assert.equal(q[3], '"open item"');
});
