// Exercise the shipped TypeScript bridge with deterministic native event/CLI fakes.
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, appendFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TranscriptReceipts } from '../src/multiverse/harnesses/pi/transcript.ts';
import multiverse from '../src/multiverse/harnesses/pi/extension.ts';

Object.assign(process.env, { MV2_AGENT_ID: 'agent', MV2_LAUNCH_ID: 'launch',
  MV2_AGENT_DIR: '/tmp/mv2-bridge-agent', MV2_UNIVERSE: '/tmp/universe', MV2_CLI: '/tmp/mv2', MV2_PYTHON: 'python' });
const directories = [];
let tick;
const originalSetInterval = global.setInterval;
const originalClearInterval = global.clearInterval;
global.setInterval = (fn) => { tick = fn; return { unref() {} }; };
global.clearInterval = () => {};
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };

async function fixture({ busy = false, failSend = false, failAck = false, wrongSession = false, stale = false, persist = true } = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'mv2-pi-bridge-'));
  directories.push(directory);
  process.env.MV2_AGENT_DIR = directory;
  const transcript = join(directory, 'session.jsonl');
  writeFileSync(transcript, JSON.stringify({ type: 'session', id: 'native-id' }) + '\n');
  const persistEntry = entry => appendFileSync(transcript, JSON.stringify(entry) + '\n');
  const events = {}, calls = [], accepted = [], warnings = [];
  let messages = [{ id: 'one', text: 'payload', reply_to: 'prior', delivery: 'steer' }];
  let sends = 0;
  const entries = [];
  const pi = {
    on(name, fn) { events[name] = fn; }, registerCommand() {},
    async sendMessage(message, options) {
      sends++;
      if (failSend) throw Error('native rejection');
      accepted.push({ message, options });
      if (persist) { entries.push({ type: "custom_message", ...message }); persistEntry(entries.at(-1)); }
    },
  };
  const run = async (_python, args) => {
    const command = args[3]; calls.push(args.slice(3));
    if (stale && command === 'session-bind') throw Error('Outdated launch');
    if (command === 'inbox') return { stdout: JSON.stringify({ messages, cursor: 42 }) };
    if (command === 'ack') {
      if (failAck) throw Error('ack failed');
      messages = [];
    }
    return { stdout: '{}' };
  };
  const ctx = { isIdle: () => !busy, ui: { notify: value => warnings.push(value) }, sessionManager: {
    getSessionFile: () => wrongSession ? '/other/session.jsonl' : transcript,
    getSessionId: () => 'native-id',
    getEntries: () => entries,
  } };
  multiverse(pi, run);
  await events.session_start({}, ctx);
  return { events, calls, accepted, warnings, entries, persistEntry, transcript, setMessages: value => { messages = value; },
    drain: async () => { await tick(); await flush(); } };
}

try {
  for (const busy of [false, true]) {
    const f = await fixture({ busy });
    await f.drain();
    assert.equal(f.accepted.length, 1);
    assert.equal(f.accepted[0].options.deliverAs, 'steer');
    assert.equal(f.accepted[0].options.triggerTurn, true);
    assert.equal(JSON.parse(f.accepted[0].message.content.split('\n')[1])[0].reply_to, 'prior');
    assert(f.calls.some(call => call[0] === 'ack'));
    await f.events.agent_start();
    await f.events.agent_settled();
    assert.deepEqual(f.calls.filter(call => call[0] === 'runtime-update').map(call => call[2]), ['idle', 'working', 'idle']);
    await f.events.session_shutdown();
    f.setMessages([{ id: 'after-close' }]);
    await f.drain();
    assert.equal(f.accepted.length, 1);
  }
  const follow = await fixture({ busy: true });
  follow.setMessages([{ id: 'follow', delivery: 'follow-up' }]);
  await follow.drain();
  assert.equal(follow.accepted[0].options.deliverAs, 'followUp');
  const rejected = await fixture({ failSend: true });
  await rejected.drain();
  assert(!rejected.calls.some(call => call[0] === 'ack'));
  assert(rejected.warnings.some(value => value.includes('native rejection')));
  const unacked = await fixture({ failAck: true });
  await unacked.drain(); await unacked.drain();
  assert.equal(unacked.accepted.length, 1); // persisted IDs prevent duplicate native injection
  const delayed = await fixture({ busy: true, persist: false });
  await delayed.drain(); await delayed.drain();
  assert.equal(delayed.accepted.length, 1);
  assert(!delayed.calls.some(call => call[0] === 'ack'));
  delayed.entries.push({ type: 'custom_message', ...delayed.accepted[0].message });
  await delayed.drain();
  assert(!delayed.calls.some(call => call[0] === 'ack')); // in-memory entries are not disk evidence
  delayed.persistEntry(delayed.entries.at(-1));
  await delayed.events.agent_settled(); // finalization race: persistence can arrive later
  await delayed.drain();
  assert(delayed.calls.some(call => call[0] === 'ack'));
  assert.equal(delayed.accepted.length, 1);
  for (const options of [{ wrongSession: true }, { stale: true }]) {
    const blocked = await fixture(options);
    await blocked.drain();
    assert.equal(blocked.accepted.length, 0);
    assert(blocked.warnings.length);
  }
  const corrupt = await fixture();
  writeFileSync(corrupt.transcript, JSON.stringify({ type: 'session', id: 'other' }) + '\n');
  const reader = new TranscriptReceipts();
  await assert.rejects(reader.scan(corrupt.transcript, 'native-id'), /identity mismatch/);
  await assert.rejects(reader.scan(corrupt.transcript, 'native-id'), /identity mismatch/);
  const partial = await fixture();
  const receiptReader = new TranscriptReceipts();
  appendFileSync(partial.transcript, '{"type":"custom_message"');
  assert.equal((await receiptReader.scan(partial.transcript, 'native-id')).complete, false);
  appendFileSync(partial.transcript, ',"customType":"multiverse-inbox","details":{"ids":["persisted"]}}\n');
  assert((await receiptReader.scan(partial.transcript, 'native-id')).ids.has('persisted'));
  console.log('Pi bridge: idle, busy/tool-running native steering, follow-up, settled, shutdown, failed injection/ack, exact session and stale launch passed');
} finally {
  for (const directory of directories) rmSync(directory, { recursive: true, force: true });
  global.setInterval = originalSetInterval;
  global.clearInterval = originalClearInterval;
}
