let activeEmotes  = [];
let spawnTimer    = null;
const pending     = new Set();
const particles   = new Map();
const motionPreference = matchMedia("(prefers-reduced-motion: reduce)");
let reducedMotion = motionPreference.matches;

// ── Helpers ──────────────────────────────────────────────────────────────
const rnd  = (a, b) => a + Math.random() * (b - a);
const pick = arr   => arr[Math.floor(Math.random() * arr.length)];

function later(fn, ms) {
  const t = setTimeout(() => { pending.delete(t); fn(); }, ms);
  pending.add(t);
  return t;
}

function particle(emote, cls, left, top, size, props) {
  if (document.hidden || reducedMotion || particles.size >= 160) return;
  const img = new Image();
  img.className = `fp ${cls}`;
  img.src = emote.url;
  img.alt = "";
  img.setAttribute("aria-hidden", "true");
  const s = [
    `left:${left.toFixed(1)}px`,
    `top:${top.toFixed(1)}px`,
    `width:${size.toFixed(1)}px`,
    `height:${size.toFixed(1)}px`,
  ];
  for (const [k, v] of Object.entries(props)) s.push(`${k}:${v}`);
  img.style.cssText = s.join(";");
  document.body.appendChild(img);
  function remove() { particles.delete(img); img.remove(); }
  const cleanup = setTimeout(remove, 10000);
  particles.set(img, cleanup);
  img.addEventListener("animationend", () => { clearTimeout(cleanup); remove(); }, { once: true });
}

// ── Effect 1: Corner Sparks ───────────────────────────────────────────────
const SPARK_EASES = [
  "cubic-bezier(.10,.82,.28,1)",
  "cubic-bezier(.16,.74,.30,1)",
  "cubic-bezier(.08,.88,.22,1)",
];

function spawnSpark(ci) {
  if (!activeEmotes.length) return;
  const iw = window.innerWidth, ih = window.innerHeight;
  const corners = [
    { x:[0,90],     y:[ih-90,ih],  dx:[40,200],   dy:[-260,-480] }, // BL ↑→
    { x:[iw-90,iw], y:[ih-90,ih],  dx:[-40,-200], dy:[-260,-480] }, // BR ↑←
    { x:[0,90],     y:[0,90],      dx:[40,200],   dy:[260,480]   }, // TL ↓→
    { x:[iw-90,iw], y:[0,90],      dx:[-40,-200], dy:[260,480]   }, // TR ↓←
  ];
  const c = corners[ci], size = rnd(22,46);
  const r0 = rnd(-22,22), r1 = r0 + rnd(-45,45);
  particle(pick(activeEmotes), "fp-spark",
    rnd(c.x[0],c.x[1]) - size/2, rnd(c.y[0],c.y[1]) - size/2, size, {
      "--dx":    `${rnd(c.dx[0],c.dx[1]).toFixed(1)}px`,
      "--dy":    `${rnd(c.dy[0],c.dy[1]).toFixed(1)}px`,
      "--w":     `${rnd(-44,44).toFixed(1)}px`,
      "--dur":   `${Math.round(rnd(1300,2700))}ms`,
      "--delay": `${Math.round(rnd(0,160))}ms`,
      "--ease":  pick(SPARK_EASES),
      "--r0":    `${r0.toFixed(1)}deg`,
      "--r1":    `${r1.toFixed(1)}deg`,
    });
}

function effectCornerSparks() {
  for (let ci = 0; ci < 4; ci++)
    for (let i = 0; i < 16; i++)
      later(() => spawnSpark(ci), i * 48);
  spawnTimer = setInterval(() => {
    for (let ci = 0; ci < 4; ci++) {
      spawnSpark(ci);
      if (Math.random() < .55) spawnSpark(ci);
    }
  }, 100);
}

// ── Effect 2: Corner Drops ────────────────────────────────────────────────
function spawnDrop(ci) {
  if (!activeEmotes.length) return;
  const iw = window.innerWidth, ih = window.innerHeight;
  const corners = [
    { x:[0,80],     y:[0,80],      dx:[15,110],   dy:[220,500]  }, // TL ↓→
    { x:[iw-80,iw], y:[0,80],      dx:[-15,-110], dy:[220,500]  }, // TR ↓←
    { x:[0,80],     y:[ih-80,ih],  dx:[15,110],   dy:[-220,-500]}, // BL ↑→
    { x:[iw-80,iw], y:[ih-80,ih],  dx:[-15,-110], dy:[-220,-500]}, // BR ↑←
  ];
  const c = corners[ci], size = rnd(24,44);
  const r0 = rnd(-15,15);
  particle(pick(activeEmotes), "fp-drop",
    rnd(c.x[0],c.x[1]) - size/2, rnd(c.y[0],c.y[1]) - size/2, size, {
      "--dx":    `${rnd(c.dx[0],c.dx[1]).toFixed(1)}px`,
      "--dy":    `${rnd(c.dy[0],c.dy[1]).toFixed(1)}px`,
      "--r0":    `${r0.toFixed(1)}deg`,
      "--dur":   `${Math.round(rnd(900,1600))}ms`,
      "--delay": `${Math.round(rnd(0,120))}ms`,
    });
}

function effectCornerDrops() {
  for (let ci = 0; ci < 4; ci++)
    for (let i = 0; i < 12; i++)
      later(() => spawnDrop(ci), i * 60);
  spawnTimer = setInterval(() => {
    for (let ci = 0; ci < 4; ci++) {
      spawnDrop(ci);
      if (Math.random() < .4) spawnDrop(ci);
    }
  }, 130);
}

// ── Effect 3: Full-screen Rain ────────────────────────────────────────────
function spawnRainDrop() {
  if (!activeEmotes.length) return;
  const iw = window.innerWidth, ih = window.innerHeight;
  const size  = rnd(18,34);
  const angle = rnd(4,16) * Math.PI / 180 * (Math.random() < .5 ? 1 : -1);
  const dist  = ih + size * 2;
  particle(pick(activeEmotes), "fp-rain",
    rnd(0,iw) - size/2, -size, size, {
      "--rdx":   `${(Math.sin(angle) * dist).toFixed(1)}px`,
      "--rdy":   `${(Math.cos(angle) * dist).toFixed(1)}px`,
      "--r":     `${(angle * 180 / Math.PI).toFixed(1)}deg`,
      "--dur":   `${Math.round(rnd(600,1200))}ms`,
      "--delay": `${Math.round(rnd(0,80))}ms`,
    });
}

function effectRain() {
  for (let i = 0; i < 44; i++) later(spawnRainDrop, i * 38);
  spawnTimer = setInterval(() => {
    spawnRainDrop();
    if (Math.random() < .7) spawnRainDrop();
  }, 75);
}

// ── Effect 4: Bottom-line Trains ──────────────────────────────────────────
function spawnTrainCar(row, direction, size, speed) {
  if (!activeEmotes.length) return;
  const iw = window.innerWidth, ih = window.innerHeight;
  const dist = iw + size * 2;
  const left = direction === 1 ? -size : iw;
  const top  = ih - 24 - size - row * (size + 14);
  particle(pick(activeEmotes), "fp-train", left, top, size, {
    "--tdx":   `${(direction * dist).toFixed(1)}px`,
    "--dur":   `${Math.round(speed)}ms`,
    "--delay": "0ms",
  });
}

function launchTrainRow(row) {
  const dir   = Math.random() < .5 ? 1 : -1;
  const count = Math.round(rnd(8,16));
  const size  = rnd(34,48);
  const speed = rnd(2400,3600);
  const gap   = rnd(55,100);
  for (let i = 0; i < count; i++)
    later(() => spawnTrainCar(row, dir, size, speed), i * gap);
}

function effectBottomTrain() {
  function wave() {
    if (!activeEmotes.length) return;
    const rows = Math.round(rnd(2,4));
    for (let r = 0; r < rows; r++)
      later(() => launchTrainRow(r), r * 280);
    later(wave, rnd(3800,5200));
  }
  wave();
}

// ── Effect 5: Smash & Fall ────────────────────────────────────────────────
function spawnSmash() {
  if (!activeEmotes.length) return;
  const iw = window.innerWidth, ih = window.innerHeight;
  const size = rnd(32,58);
  const r0   = rnd(-30,30);
  particle(pick(activeEmotes), "fp-smash",
    rnd(iw*.08, iw*.92) - size/2, rnd(ih*.05, ih*.62) - size/2, size, {
      "--sx":    `${rnd(-50,50).toFixed(1)}px`,
      "--sy":    `${rnd(130,320).toFixed(1)}px`,
      "--r0":    `${r0.toFixed(1)}deg`,
      "--r1":    `${(r0 + rnd(-90,90)).toFixed(1)}deg`,
      "--dur":   `${Math.round(rnd(1200,2200))}ms`,
      "--delay": `${Math.round(rnd(0,100))}ms`,
    });
}

function effectSmashFall() {
  for (let i = 0; i < 22; i++)
    later(spawnSmash, i * 70 + rnd(0,50));
  spawnTimer = setInterval(() => {
    spawnSmash();
    if (Math.random() < .45) spawnSmash();
  }, 180);
}

// ── Random effect picker ──────────────────────────────────────────────────
const EFFECTS = [
  effectCornerSparks,
  effectCornerDrops,
  effectRain,
  effectBottomTrain,
  effectSmashFall,
];

function startFire(emotes) {
  stopFire();
  if (document.hidden || reducedMotion) return;
  activeEmotes = emotes;
  pick(EFFECTS)();
}

function stopFire() {
  clearInterval(spawnTimer);
  spawnTimer = null;
  pending.forEach(clearTimeout);
  pending.clear();
  activeEmotes = [];
}

function clearEffects() {
  stopFire();
  for (const [img, timer] of particles) {
    clearTimeout(timer);
    img.remove();
  }
  particles.clear();
}
document.addEventListener("visibilitychange", () => { if (document.hidden) clearEffects(); });
motionPreference.addEventListener("change", (event) => {
  reducedMotion = event.matches;
  if (reducedMotion) clearEffects();
});
window.addEventListener("pagehide", clearEffects);

// ── Shared runtime (WebSocket, queue, playback, now-playing card) ─────────
VoxerOverlay.init({
  onPlayStart: (event) => {
    if (event.emotes && event.emotes.length > 0) startFire(event.emotes);
  },
  onPlayEnd: () => {
    stopFire();
  },
});
