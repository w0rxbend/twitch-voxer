/* Run with: node --test tests/test_overlay.cjs */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const runtime = fs.readFileSync(path.join(__dirname, '../voxer/static/overlay.js'), 'utf8');

function harness(hooks = {}) {
  const audios = [];
  const sockets = [];
  const timers = new Map();
  const listeners = new Map();
  const playResults = [];
  const hint = {
    classList: { add() {}, remove() {} },
    addEventListener(name, fn) { listeners.set(`hint:${name}`, fn); },
    focus() {},
  };
  let clock = 0;
  let timerID = 0;
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    open() { this.readyState = 1; this.onopen(); }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() { this.readyState = 3; this.onclose(); }
    event(value) { this.onmessage({ data: JSON.stringify(value) }); }
  }
  class Audio {
    constructor(url) { this.url = url; this.listeners = new Map(); this.paused = true; this.released = false; audios.push(this); }
    addEventListener(name, fn) { this.listeners.set(name, fn); }
    removeEventListener(name) { this.listeners.delete(name); }
    emit(name) { if (this.listeners.has(name)) this.listeners.get(name)(); }
    play() { this.paused = false; return playResults.length ? playResults.shift()() : Promise.resolve(); }
    pause() { this.paused = true; }
    removeAttribute(name) { if (name === 'src') this.released = true; }
    load() {}
  }
  const window = {
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
  };
  const context = {
    window,
    document: { getElementById(id) { return id === 'autoplay-hint' ? hint : null; } },
    location: { origin: 'http://localhost:8080', host: 'localhost:8080', protocol: 'http:', search: '' },
    WebSocket: Socket,
    Audio,
    URL,
    URLSearchParams,
    Date: { now() { return clock; } },
    console: { warn() {}, error() {}, log() {} },
    setTimeout(fn, delay) { const id = ++timerID; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(runtime, context);
  window.VoxerOverlay.init(hooks);
  sockets[0].open();
  return {
    audios, sockets, timers, listeners, playResults, window,
    advance(ms) { clock += ms; },
    runTimer(delay) {
      const found = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(found, `No timer for ${delay}ms`);
      timers.delete(found[0]);
      found[1].fn();
    },
    gesture() { const fn = listeners.get('hint:click'); assert.ok(fn); fn(); },
  };
}

const event = (name = 'clip') => ({ audio_url: `/audio/${name}.mp3`, username: 'chatter', emotes: [] });
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

test('rejects malformed payloads and nonlocal audio URLs', () => {
  const h = harness();
  for (const payload of [null, [], 42, {}, { ...event(), audio_url: '//evil.test/clip.mp3' }, { ...event(), audio_url: '/audio/../secret.mp3' }, { ...event(), username: {} }]) h.sockets[0].event(payload);
  assert.equal(h.audios.length, 0);
  h.sockets[0].event(event());
  assert.equal(h.audios.length, 1);
});

test('audio filenames follow the server contract, including strict end of input', () => {
  const h = harness();
  for (const name of ['clip.v2', 'x'.repeat(129)]) h.sockets[0].event(event(name));
  h.sockets[0].event({ ...event(), audio_url: '/audio/clip.mp3\n' });
  assert.equal(h.audios.length, 0);
  h.sockets[0].event(event('_clip'));
  assert.equal(h.audios.length, 1);
});

test('autoplay rejection waits for a gesture and bounds its pending queue', async () => {
  const h = harness();
  h.playResults.push(() => Promise.reject({ name: 'NotAllowedError' }));
  h.sockets[0].event(event());
  await flush();
  for (let i = 0; i < 60; i++) h.sockets[0].event(event(`queued-${i}`));
  assert.equal(h.audios.length, 1, 'new messages must not repeatedly allocate blocked players');
  assert.equal(h.sockets[0].sent.length, 11, 'overflow items release their own receipts');
  assert.ok(h.audios[0].released);
  h.gesture();
  await flush();
  assert.equal(h.audios.length, 2);
  assert.equal(h.audios[1].url, 'http://localhost:8080/audio/queued-10.mp3');
});

test('watchdog settles once, releases media and ignores a late play promise', async () => {
  const calls = [];
  const h = harness({ onPlayStart() { calls.push('start'); }, onAudioElement() { return () => calls.push('release'); } });
  let resolve;
  h.playResults.push(() => new Promise((done) => { resolve = done; }));
  h.sockets[0].event(event());
  h.runTimer(120000);
  resolve();
  await flush();
  assert.deepEqual(calls, ['release']);
  assert.deepEqual(h.sockets[0].sent, [{ done: 'clip.mp3' }]);
  assert.ok(h.audios[0].paused && h.audios[0].released);
});

test('old queued clips expire before playback', async () => {
  const h = harness();
  h.sockets[0].event(event('first'));
  h.sockets[0].event(event('old'));
  await flush();
  h.advance(120001);
  h.audios[0].emit('ended');
  assert.equal(h.audios.length, 1);
  assert.deepEqual(h.sockets[0].sent, [{ done: 'first.mp3' }, { done: 'old.mp3' }]);
});

test('disconnect abandons old ownership and does not replay receipts after reconnect', async () => {
  const h = harness();
  h.sockets[0].event(event('first'));
  h.sockets[0].event(event('queued'));
  await flush();
  h.sockets[0].close();
  assert.ok(h.audios[0].released);
  const retry = [...h.timers.values()].find((timer) => timer.delay < 2000);
  assert.ok(retry);
  retry.fn();
  h.sockets[1].open();
  assert.deepEqual(h.sockets[1].sent, []);
  h.sockets[1].event(event('new'));
  assert.equal(h.audios.length, 2);
  assert.equal(h.audios[1].url, 'http://localhost:8080/audio/new.mp3');
});

test('sanitizes image sources and caps emotes before rendering hooks', async () => {
  const calls = [];
  const h = harness({ onPlayStart(item) { calls.push(item); } });
  h.sockets[0].event({ ...event(), avatar_url: 'javascript:alert(1)', emotes: [{ url: 'javascript:alert(1)' }, { url: 'http://localhost/secret' }, { name: 'ok', url: 'https://static-cdn.jtvnw.net/a.png' }] });
  await flush();
  assert.equal(calls[0].avatar_url, null);
  assert.equal(calls[0].emotes.length, 1);
  assert.equal(calls[0].emotes[0].name, 'ok');
});

test('page teardown stops audio and reconnection; duplicate init is harmless', () => {
  const h = harness();
  h.window.VoxerOverlay.init({});
  assert.equal(h.sockets.length, 1);
  h.sockets[0].event(event());
  h.listeners.get('pagehide')();
  assert.ok(h.audios[0].released);
  assert.equal(h.timers.size, 0);
  assert.equal(h.sockets[0].readyState, 3);
});

test('restoring from the back-forward cache reconnects without old receipts', async () => {
  const h = harness();
  h.sockets[0].event(event('first'));
  h.sockets[0].event(event('queued'));
  await flush();
  h.listeners.get('pagehide')();
  h.listeners.get('pageshow')({ persisted: true });
  h.sockets[1].open();
  h.sockets[1].event(event('new'));
  await flush();
  assert.equal(h.audios.length, 2);
  assert.equal(h.audios[1].url, 'http://localhost:8080/audio/new.mp3');
  assert.deepEqual(h.sockets[1].sent, []);
  h.listeners.get('pagehide')();
  assert.equal(h.timers.size, 0);
  assert.equal(h.sockets[1].readyState, 3);
});
