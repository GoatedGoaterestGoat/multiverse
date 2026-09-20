// Native extension contract exercised through real Unix sockets, no model calls.
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, appendFileSync, rmSync } from 'node:fs';
import { connect } from 'node:net';
import { join } from 'node:path';
import { TranscriptReceipts } from '../src/multiverse/harnesses/pi/transcript.ts';
import multiverse from '../src/multiverse/harnesses/pi/extension.ts';
const directories = [], fixtures = [];
const originalInterval = global.setInterval;
global.setInterval = () => { throw Error('No periodic polling permitted'); };
async function fixture({ failSend = false, wrongSession = false, stale = false } = {}) {
  const directory = mkdtempSync('/tmp/mv2-pi-'); directories.push(directory);
  const file = join(directory, 'session.jsonl'), socket = join(directory, 'control.sock');
  writeFileSync(file, JSON.stringify({ type: 'session', id: 'native' }) + '\n');
  Object.assign(process.env, { MV2_AGENT_ID: 'agent', MV2_LAUNCH_ID: 'launch', MV2_AGENT_DIR: directory,
    MV2_CONTROL_SOCKET: socket, MV2_UNIVERSE: '/tmp/u', MV2_CLI: '/tmp/mv2', MV2_PYTHON: 'python' });
  const events = {}, calls = [], sent = [], warnings = [];
  let currentFile = wrongSession ? '/other/file' : file;
  const pi = { on(name, fn) { events[name] = fn; }, registerCommand() {},
    sendMessage(message, options) {
      if (failSend) throw Error('native failure');
      sent.push({ message, options });
    } };
  const run = async (_, args) => {
    calls.push(args.slice(3));
    assert(!['inbox', 'ack', 'claim-inbox'].includes(args[3]));
    if (stale && args[3] === 'session-bind') throw Error('Outdated launch');
    return { stdout: '{}' };
  };
  multiverse(pi, run);
  await events.session_start({}, { ui: { notify: value => warnings.push(value) }, sessionManager: {
    getSessionId: () => 'native', getSessionFile: () => currentFile } });
  const request = (fields = {}) => new Promise((resolve, reject) => {
    const client = connect(socket); let data = '';
    client.setTimeout(2000, () => { client.destroy(); reject(Error('timeout')); });
    client.on('error', reject);
    client.on('connect', () => client.write(JSON.stringify({ operation: 'send', launch_id: 'launch',
      native_id: 'native', message_id: 'one', text: 'literal $(false) 日本語', mode: 'steer', ...fields }) + '\n'));
    client.on('data', chunk => { data += chunk; });
    client.on('end', () => { try { resolve(JSON.parse(data)); } catch (e) { reject(e); } });
  });
  const f = { events, calls, sent, warnings, request, file, changeFile: value => { currentFile = value; } };
  fixtures.push(f); return f;
}
try {
  const f = await fixture();
  const [urgent, normal] = await Promise.all([f.request(), f.request({ message_id: 'two', mode: 'follow-up' })]);
  assert.equal(urgent.state, 'native_queued'); assert.equal(normal.state, 'native_queued');
  assert.deepEqual(f.sent.map(x => x.options.deliverAs), ['steer', 'followUp']);
  assert.equal(f.sent[0].message.content, 'literal $(false) 日本語');
  assert.equal((await f.request()).state, 'native_queued'); assert.equal(f.sent.length, 2);
  assert.equal((await f.request({ launch_id: 'stale', message_id: 'bad' })).state, 'error');
  assert.equal((await f.request({ native_id: 'other', message_id: 'bad' })).state, 'error');
  assert.equal((await f.request({ mode: 'unknown', message_id: 'bad' })).state, 'error');
  assert.equal(f.sent.length, 2);
  appendFileSync(f.file, JSON.stringify({ type: 'custom_message', customType: 'multiverse-inbox', details: { ids: ['persisted'] } }) + '\n');
  const persisted = await f.request({ message_id: 'persisted' });
  assert.equal(persisted.value.transcript_confirmed, true); assert.equal(f.sent.length, 2);
  await f.events.agent_start(); await f.events.agent_settled();
  assert.deepEqual(f.calls.filter(c => c[0] === 'runtime-update').map(c => c[2]), ['idle', 'working', 'idle']);
  f.changeFile('/other/session');
  assert.equal((await f.request({ message_id: 'changed' })).state, 'error');
  const broken = await fixture({ failSend: true });
  assert.equal((await broken.request()).state, 'unknown');
  // Unknown outcome is not retried by core; extension must not call native twice.
  await broken.request(); assert.equal(broken.sent.length, 0);
  for (const options of [{ wrongSession: true }, { stale: true }]) {
    const blocked = await fixture(options); await assert.rejects(blocked.request());
  }
  const reader = new TranscriptReceipts();
  const file = f.file;
  appendFileSync(file, '{"type":"custom_message"');
  assert.equal((await reader.scan(file, 'native')).complete, false);
  appendFileSync(file, ',"customType":"multiverse-inbox","details":{"ids":["late"]}}\n');
  assert((await reader.scan(file, 'native')).ids.has('late'));
  const wrong = new TranscriptReceipts();
  await assert.rejects(wrong.scan(file, 'different'), /identity mismatch/);
  console.log('Pi socket: concurrent modes, dedup, transcript evidence, exact identity, failures, no inbox polling/ack passed');
} finally {
  for (const f of fixtures) await f.events.session_shutdown();
  for (const d of directories) rmSync(d, { recursive: true, force: true });
  global.setInterval = originalInterval;
}
