let THREE = null;
let GLTFLoader = null;

const visualizer = document.getElementById("visualizer");
const visualLoops = new Set();
const stopVisualLoops = new Set();
const motionPreference = matchMedia("(prefers-reduced-motion: reduce)");
let reducedMotion = motionPreference.matches;
const visualsAllowed = () => !document.hidden && !reducedMotion;

const visualState = {
  active: false,
  burst: 0,
  bass: 0,
  startedAt: 0,
};

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function createThreeSpeaker() {
  const container = document.getElementById("stage-3d");
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, window.innerWidth / window.innerHeight, 0.1, 100);
  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
  const speakerRig = new THREE.Group();
  const speakerFace = new THREE.Group();
  const haloRig = new THREE.Group();
  let speaker = null;

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.22;
  container.appendChild(renderer.domElement);

  camera.position.set(0, 0.32, 7.6);
  scene.add(speakerRig);
  speakerRig.add(speakerFace);
  scene.add(haloRig);

  const keyLight = new THREE.PointLight(0x55e6ff, 4.2, 18);
  keyLight.position.set(-3.4, 2.8, 4.2);
  scene.add(keyLight);

  const rimLight = new THREE.PointLight(0xff4fd8, 3.8, 16);
  rimLight.position.set(3.1, -1.8, 3.8);
  scene.add(rimLight);

  const fillLight = new THREE.HemisphereLight(0xb7f7ff, 0x181022, 2.2);
  scene.add(fillLight);

  for (let i = 0; i < 4; i += 1) {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(1.35 + i * 0.26, 0.012, 10, 120),
      new THREE.MeshBasicMaterial({
        color: i % 2 === 0 ? 0x55e6ff : 0xff4fd8,
        transparent: true,
        opacity: 0,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      })
    );
    ring.rotation.x = Math.PI / 2;
    ring.userData.offset = i * 0.17;
    haloRig.add(ring);
  }

  function addFallbackSpeaker() {
    const body = new THREE.Mesh(
      new THREE.CylinderGeometry(0.85, 0.98, 0.72, 64),
      new THREE.MeshStandardMaterial({
        color: 0x202733,
        metalness: 0.55,
        roughness: 0.28,
        emissive: 0x07141c,
      })
    );
    const cone = new THREE.Mesh(
      new THREE.CylinderGeometry(0.24, 0.68, 0.4, 64),
      new THREE.MeshStandardMaterial({
        color: 0x0b1218,
        metalness: 0.2,
        roughness: 0.46,
        emissive: 0x153241,
        emissiveIntensity: 0.45,
      })
    );
    const center = new THREE.Mesh(
      new THREE.SphereGeometry(0.23, 40, 24),
      new THREE.MeshStandardMaterial({
        color: 0x55e6ff,
        emissive: 0x55e6ff,
        emissiveIntensity: 0.55,
        metalness: 0.18,
        roughness: 0.22,
      })
    );
    body.rotation.x = Math.PI / 2;
    cone.rotation.x = Math.PI / 2;
    cone.position.z = 0.28;
    center.position.z = 0.52;
    speakerFace.add(body, cone, center);
    speaker = speakerFace;
  }

  new GLTFLoader().load(
    "/static/speaker.glb",
    (gltf) => {
      speaker = gltf.scene;
      const box = new THREE.Box3().setFromObject(speaker);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      const maxAxis = Math.max(size.x, size.y, size.z) || 1;

      speaker.position.sub(center);
      speaker.scale.setScalar(1.35 / maxAxis);
      speaker.rotation.set(0, 0, 0);
      speaker.traverse((node) => {
        if (node.isMesh && node.material) {
          node.castShadow = true;
          node.receiveShadow = true;
          node.material = node.material.clone();
          node.material.envMapIntensity = 1.35;
        }
      });
      speakerFace.add(speaker);
    },
    undefined,
    (error) => {
      console.error("Failed to load speaker.glb:", error);
      addFallbackSpeaker();
    }
  );

  function resize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  }

  window.addEventListener("resize", resize);

  let animationFrame = null;
  let lastRender = 0;
  function wake() {
    if (animationFrame === null && visualsAllowed()) animationFrame = requestAnimationFrame(frame);
  }
  function stop() {
    cancelAnimationFrame(animationFrame);
    animationFrame = null;
    renderer.clear();
  }
  stopVisualLoops.add(stop);
  visualLoops.add(wake);

  function frame(time) {
    animationFrame = null;
    if (!visualsAllowed()) return;
    if (!visualState.active && visualState.burst <= 0.02) {
      renderer.clear();
      return;
    }
    if (time - lastRender < 1000 / 30) { wake(); return; }
    lastRender = time;
    const seconds = time * 0.001;
    const beat = visualState.active ? 1 : 0;
    const pulse = visualState.bass;
    const burst = visualState.burst;

    speakerRig.visible = visualState.active || burst > 0.02;
    speakerRig.rotation.y = Math.sin(seconds * 0.72) * 0.28;
    speakerRig.rotation.x = Math.sin(seconds * 1.7) * 0.05 + pulse * 0.04;
    speakerRig.rotation.z = Math.sin(seconds * 2.1) * 0.025;
    speakerRig.scale.setScalar(1 + pulse * 0.22 + burst * 0.12);
    speakerRig.position.y = Math.sin(seconds * 2.4) * 0.08;
    speakerFace.position.z = burst * 0.08 + pulse * 0.05;

    keyLight.intensity = 2.8 + beat * 1.8 + pulse * 3.4;
    rimLight.intensity = 2.2 + burst * 5.2;

    haloRig.children.forEach((ring, index) => {
      const phase = (seconds * 0.74 + ring.userData.offset) % 1;
      const activeOpacity = visualState.active ? 0.34 : 0;
      ring.scale.setScalar(0.42 + phase * 1.15 + burst * 0.18);
      ring.material.opacity = activeOpacity * (1 - phase) + burst * 0.14;
      ring.rotation.z = seconds * (0.24 + index * 0.08);
    });

    visualState.bass *= 0.91;
    visualState.burst *= 0.88;
    renderer.render(scene, camera);
    wake();
  }

  wake();
}

function createPixiGlitch() {
  const container = document.getElementById("glitch-stage");
  const app = new PIXI.Application({
    width: container.clientWidth,
    height: container.clientHeight,
    backgroundAlpha: 0,
    autoStart: false,
    antialias: true,
    autoDensity: true,
    resolution: Math.min(window.devicePixelRatio || 1, 1.5),
  });
  const slices = new PIXI.Container();
  const noiseCanvas = document.createElement("canvas");
  const noiseCtx = noiseCanvas.getContext("2d");
  const displacementSprite = PIXI.Sprite.from(noiseCanvas);
  const DisplacementFilter = PIXI.DisplacementFilter || PIXI.filters.DisplacementFilter;
  const ColorMatrixFilter = PIXI.ColorMatrixFilter || PIXI.filters.ColorMatrixFilter;
  const BlurFilter = PIXI.BlurFilter || PIXI.filters.BlurFilter;
  const displacementFilter = new DisplacementFilter(displacementSprite);
  const colorMatrix = new ColorMatrixFilter();
  const glow = new BlurFilter(0);
  const bars = [];

  container.appendChild(app.view);
  app.stage.filters = [displacementFilter, colorMatrix, glow];
  app.stage.addChild(displacementSprite, slices);
  displacementSprite.texture.baseTexture.wrapMode = PIXI.WRAP_MODES.REPEAT;

  function paintNoise() {
    const size = 128;
    noiseCanvas.width = size;
    noiseCanvas.height = size;
    const image = noiseCtx.createImageData(size, size);
    for (let i = 0; i < image.data.length; i += 4) {
      const value = Math.random() * 255;
      image.data[i] = value;
      image.data[i + 1] = 255 - value;
      image.data[i + 2] = Math.random() * 255;
      image.data[i + 3] = 255;
    }
    noiseCtx.putImageData(image, 0, 0);
    displacementSprite.texture.update();
  }

  function rebuildBars() {
    slices.removeChildren().forEach((child) => child.destroy());
    bars.length = 0;
    const width = container.clientWidth;
    const height = container.clientHeight;
    app.renderer.resize(width, height);
    const count = Math.max(10, Math.round(height / 38));
    for (let i = 0; i < count; i += 1) {
      const bar = new PIXI.Graphics();
      bar.y = (i / count) * height;
      bar.alpha = 0;
      slices.addChild(bar);
      bars.push(bar);
    }
  }

  paintNoise();
  rebuildBars();
  window.addEventListener("resize", rebuildBars);

  app.ticker.maxFPS = 30;
  const wake = () => { if (visualsAllowed()) app.start(); };
  visualLoops.add(wake);
  stopVisualLoops.add(() => { app.stop(); app.renderer.clear(); });
  app.ticker.add(() => {
    if (!visualState.active && visualState.burst <= 0.02) {
      app.stage.visible = false;
      app.renderer.clear();
      app.stop();
      return;
    }
    app.stage.visible = true;
    const energy = visualState.active ? 0.72 + visualState.bass * 1.25 : visualState.burst * 0.9;
    // Decay independently so a failed Three.js renderer cannot keep Pixi's
    // animation loop alive forever after playback ends.
    visualState.burst *= 0.94;
    const time = app.ticker.lastTime * 0.001;
    const width = container.clientWidth;
    const stageHeight = container.clientHeight;

    if (Math.random() < 0.09 + energy * 0.14) {
      paintNoise();
    }

    displacementSprite.x = Math.sin(time * 2.7) * 58;
    displacementSprite.y = Math.cos(time * 3.1) * 42;
    displacementFilter.scale.set(energy * 18, energy * 8);
    glow.blur = energy * 1.4;
    colorMatrix.reset();
    colorMatrix.saturate(1 + energy * 1.4, false);
    colorMatrix.hue(Math.sin(time * 2.2) * energy * 22, false);

    bars.forEach((bar, index) => {
      const barHeight = 3 + ((index * 17) % 12);
      const y = (index / bars.length) * stageHeight;
      const barWidth = width * (0.2 + Math.random() * 0.52);
      const xShift = (Math.random() - 0.5) * energy * 68;
      const hue = index % 3 === 0 ? 0x55e6ff : index % 3 === 1 ? 0xff4fd8 : 0xffd166;

      bar.clear();
      bar.beginFill(hue, 0.13 + energy * 0.25);
      bar.drawRect(width * 0.5 - barWidth * 0.5 + xShift, y, barWidth, barHeight);
      bar.endFill();
      bar.alpha = energy * (0.16 + Math.random() * 0.5);
    });
  });
  wake();
}

function triggerVisuals() {
  visualState.active = true;
  visualState.burst = 1;
  visualState.bass = 1;
  visualState.startedAt = performance.now();
  visualizer.classList.add("visible");
  visualLoops.forEach((wake) => wake());
}

function relaxVisuals() {
  visualState.active = false;
  visualizer.classList.remove("visible");
}

const emoteLayer = document.getElementById("emote-layer");
const EMOTE_COUNT = 52;
const EMOTE_EASES = [
  "cubic-bezier(0.12,0.8,0.28,1)",
  "cubic-bezier(0.2,0.6,0.3,1)",
  "cubic-bezier(0.08,0.9,0.22,1)",
];

function spawnEmotes(emotes) {
  if (!visualsAllowed() || !emotes || emotes.length === 0) return;

  const cx = window.innerWidth / 2;
  const cy = window.innerHeight / 2;

  for (let i = 0; i < EMOTE_COUNT; i++) {
    if (!visualsAllowed() || emoteLayer.childElementCount >= EMOTE_COUNT * 2) break;
    const emote = emotes[i % emotes.length];
    const delay = Math.random() * 2800;
    const angle = Math.random() * Math.PI * 2;
    const dist = 180 + Math.random() * Math.min(cx, cy) * 1.1;
    const tx = Math.cos(angle) * dist;
    const ty = Math.sin(angle) * dist;
    const size = 30 + Math.random() * 38;
    const dur = 1400 + Math.random() * 1800;
    const r0 = (Math.random() - 0.5) * 60;
    const r1 = r0 + (Math.random() - 0.5) * 200;
    const ease = EMOTE_EASES[Math.floor(Math.random() * EMOTE_EASES.length)];

    const startX = cx + (Math.random() - 0.5) * 140;
    const startY = cy + (Math.random() - 0.5) * 120;

    const img = new Image();
    img.className = "emote-particle";
    img.src = emote.url;
    img.alt = emote.name;
    img.style.cssText = [
      `left:${startX - size / 2}px`,
      `top:${startY - size / 2}px`,
      `width:${size}px`,
      `height:${size}px`,
      `--tx:${tx}px`,
      `--ty:${ty}px`,
      `--r0:${r0}deg`,
      `--r1:${r1}deg`,
      `--dur:${dur}ms`,
      `--delay:${delay}ms`,
      `--ease:${ease}`,
    ].join(";");

    emoteLayer.appendChild(img);
    const cleanup = setTimeout(() => img.remove(), delay + dur + 1000);
    img.addEventListener("animationend", () => { clearTimeout(cleanup); img.remove(); }, { once: true });
  }
}

// One shared AudioContext + analyser for every played message.  A context
// per element would accumulate (browsers cap live AudioContexts and each
// holds audio-thread resources); instead each element gets only its own
// MediaElementSource, connected on play and disconnected when it settles.
const reactivity = { context: null, analyser: null, data: null };

function ensureAnalyser() {
  if (reactivity.context) return true;
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) return false;
  reactivity.context = new AudioContext();
  reactivity.analyser = reactivity.context.createAnalyser();
  reactivity.analyser.fftSize = 128;
  reactivity.analyser.smoothingTimeConstant = 0.72;
  reactivity.analyser.connect(reactivity.context.destination);
  reactivity.data = new Uint8Array(reactivity.analyser.frequencyBinCount);
  return true;
}

function connectAudioReactivity(audio) {
  if (reducedMotion) return;
  try {
    if (!ensureAnalyser()) return;
    const source = reactivity.context.createMediaElementSource(audio);
    source.connect(reactivity.analyser);
    let sampleFrame = null;
    let disconnected = false;

    function disconnect() {
      if (disconnected) return;
      disconnected = true;
      stopSample();
      visualLoops.delete(wakeSample);
      stopVisualLoops.delete(stopSample);
      try { source.disconnect(); } catch (e) { /* already disconnected */ }
    }
    audio.addEventListener("ended", disconnect, { once: true });
    audio.addEventListener("error", disconnect, { once: true });

    function sample() {
      sampleFrame = null;
      // Loop for the lifetime of this audio element rather than reading
      // visualState.active: the "play" event can fire before the runtime
      // has invoked onPlayStart (which sets visualState.active).
      // `paused` covers the runtime's watchdog, which pauses a stalled
      // element — without it this rAF loop would never exit.
      if (disconnected || audio.ended || audio.error || audio.paused) { disconnect(); return; }
      if (!visualsAllowed()) return;
      reactivity.analyser.getByteFrequencyData(reactivity.data);
      let sum = 0;
      const count = Math.min(14, reactivity.data.length);
      for (let i = 0; i < count; i++) sum += reactivity.data[i];
      visualState.bass = clamp(sum / (count * 255), 0.08, 1);
      sampleFrame = requestAnimationFrame(sample);
    }

    function wakeSample() {
      if (!disconnected && !audio.paused && sampleFrame === null && visualsAllowed()) sample();
    }
    function stopSample() {
      cancelAnimationFrame(sampleFrame);
      sampleFrame = null;
    }
    visualLoops.add(wakeSample);
    stopVisualLoops.add(stopSample);

    audio.addEventListener("play", () => {
      reactivity.context.resume().catch(() => {});
      sample();
    }, { once: true });
    return disconnect;
  } catch (error) {
    console.warn("Audio analyser unavailable:", error);
  }
}

VoxerOverlay.init({
  onPlayStart: (event) => {
    triggerVisuals();
    spawnEmotes(event.emotes);
  },
  onPlayEnd: () => {
    relaxVisuals();
  },
  onAudioElement: connectAudioReactivity,
});

let visualsInitialized = false;
async function initVisuals() {
  if (visualsInitialized || !visualsAllowed()) return;
  visualsInitialized = true;
  try {
    [THREE, { GLTFLoader }] = await Promise.all([
      import("/static/vendor/three/three.module.js"),
      import("/static/vendor/three/GLTFLoader.js"),
    ]);
    createThreeSpeaker();
  } catch (error) {
    console.error("Three.js visualizer unavailable:", error);
  }

  try {
    if (!window.PIXI) {
      throw new Error("PIXI global was not loaded");
    }
    createPixiGlitch();
  } catch (error) {
    console.error("PixiJS glitch layer unavailable:", error);
  }
}

function syncVisuals() {
  if (visualsAllowed()) {
    initVisuals();
    visualLoops.forEach((wake) => wake());
  } else {
    stopVisualLoops.forEach((stop) => stop());
    emoteLayer.replaceChildren();
  }
}
document.addEventListener("visibilitychange", syncVisuals);
motionPreference.addEventListener("change", (event) => {
  reducedMotion = event.matches;
  syncVisuals();
});
window.addEventListener("pagehide", () => {
  stopVisualLoops.forEach((stop) => stop());
  emoteLayer.replaceChildren();
});
window.addEventListener("pageshow", syncVisuals);

initVisuals();
