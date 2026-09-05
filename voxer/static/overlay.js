/* Shared OBS connection and playback runtime. Rendering hooks are optional. */
(function () {
  "use strict";

  const MAX_QUEUE = 50;
  const MAX_QUEUE_AGE_MS = 120000;
  const WATCHDOG_MS = 120000;
  const RECONNECT_BASE_MS = 1000;
  const RECONNECT_MAX_MS = 30000;
  let initialized = false;

  function init(hooks) {
    if (initialized) return;
    initialized = true;
    hooks = hooks ?? {};
    const params = new URLSearchParams(location.search);
    let volume = parseFloat(params.get("volume"));
    if (!Number.isFinite(volume)) volume = 1;
    volume = Math.max(0, Math.min(1, volume));

    const queue = [];
    let ws = null;
    let reconnectDelay = RECONNECT_BASE_MS;
    let reconnectTimer = null;
    let isPlaying = false;
    let activeCancel = null;
    let stopped = false;
    let autoplayArmed = false;
    let lastState = "disconnected";
    let lastRetrySeconds = null;
    let cardHideTimer = null;
    const npCard = document.getElementById("np-card");
    const statusPill = document.getElementById("status-pill");
    const autoplayHint = document.getElementById("autoplay-hint");

    if (statusPill) {
      const debug = params.get("debug");
      let visible = typeof window.obsstudio === "undefined";
      if (debug === "1") visible = true;
      if (debug === "0") visible = false;
      statusPill.style.display = visible ? "" : "none";
    }

    function callHook(name, argument) {
      if (typeof hooks[name] !== "function") return;
      try { return hooks[name](argument); }
      catch (error) { console.warn(name + " hook failed:", error); }
    }

    function reportStatus(state, retryInSeconds) {
      if (state) {
        lastState = state;
        lastRetrySeconds = retryInSeconds;
      }
      const status = { state: lastState, queueDepth: queue.length };
      if (lastState === "reconnecting") status.retryInSeconds = lastRetrySeconds;
      if (statusPill) {
        const dot = statusPill.querySelector(".dot");
        if (dot) dot.dataset.state = status.state;
        let label = status.state === "reconnecting"
          ? "reconnecting in " + status.retryInSeconds + "s" : status.state;
        if (queue.length) label += " \u00b7 queue " + queue.length;
        const text = statusPill.querySelector(".txt");
        if (text) text.textContent = label;
      }
      callHook("onStatus", status);
    }

    function safeImage(value) {
      if (typeof value !== "string" || value.length > 2048) return null;
      try {
        const url = new URL(value, location.origin);
        if (url.username || url.password) return null;
        return url.protocol === "https:" || (url.origin === location.origin && url.pathname.startsWith("/static/"))
          ? url.href : null;
      } catch (error) { return null; }
    }

    function parseEvent(raw, socket) {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
      if (typeof raw.audio_url !== "string" || !/^\/audio\/[A-Za-z0-9_-]{1,128}\.mp3(?![\s\S])/.test(raw.audio_url)) return null;
      if (typeof raw.username !== "string" || raw.username.length > 64) return null;
      const emotes = Array.isArray(raw.emotes) ? raw.emotes.slice(0, 64) : [];
      return {
        audio_url: raw.audio_url,
        username: raw.username,
        avatar_url: safeImage(raw.avatar_url),
        emotes: emotes.flatMap((item) => {
          const url = safeImage(item?.url);
          return url ? [{ url, name: typeof item.name === "string" ? item.name.slice(0, 128) : "" }] : [];
        }),
        receivedAt: Date.now(),
        socket: socket,
      };
    }

    function showCard(event) {
      if (!npCard) return;
      clearTimeout(cardHideTimer);
      npCard.classList.remove("leaving");
      const avatar = npCard.querySelector(".np-avatar");
      if (avatar) {
        avatar.onerror = function () { avatar.onerror = null; avatar.src = "/static/speaker.svg"; };
        avatar.src = event.avatar_url || "/static/speaker.svg";
      }
      const name = npCard.querySelector(".np-username");
      if (name) name.textContent = "@" + event.username;
      const strip = npCard.querySelector(".np-emotes");
      if (strip) {
        strip.textContent = "";
        const seen = new Set();
        event.emotes.forEach(function (emote) {
          if (seen.size >= 8 || seen.has(emote.url)) return;
          seen.add(emote.url);
          const img = document.createElement("img");
          img.src = emote.url;
          img.alt = emote.name;
          img.width = 24;
          img.height = 24;
          strip.appendChild(img);
        });
      }
      npCard.classList.add("visible");
      npCard.setAttribute("aria-hidden", "false");
    }

    function hideCard() {
      if (!npCard) return;
      clearTimeout(cardHideTimer);
      npCard.classList.remove("visible");
      npCard.setAttribute("aria-hidden", "true");
      npCard.classList.add("leaving");
      cardHideTimer = setTimeout(function () { npCard.classList.remove("leaving"); }, 400);
    }

    function notifyDone(item) {
      // A receipt belongs to the connection that received it. Never replay
      // acknowledgements on a replacement socket with different ownership.
      if (item.socket !== ws || !ws || ws.readyState !== WebSocket.OPEN) return;
      try { ws.send(JSON.stringify({ done: item.audio_url.split("/").pop() })); }
      catch (error) { /* Disconnect cleanup and the hard server TTL own it now. */ }
    }

    function resume() {
      if (!autoplayArmed) return;
      autoplayArmed = false;
      if (autoplayHint) autoplayHint.classList.remove("visible");
      playNext();
    }

    function requestUserGesture() {
      if (autoplayArmed) return;
      autoplayArmed = true;
      if (autoplayHint) {
        autoplayHint.classList.add("visible");
        autoplayHint.focus({ preventScroll: true });
      }
    }

    if (autoplayHint) autoplayHint.addEventListener("click", resume);

    function playNext() {
      if (isPlaying || stopped || autoplayArmed) return;
      let item;
      while (queue.length) {
        item = queue.shift();
        if (Date.now() - item.receivedAt < MAX_QUEUE_AGE_MS && item.socket === ws) break;
        notifyDone(item);
        item = null;
      }
      reportStatus();
      if (!item) return;
      isPlaying = true;
      const audio = new Audio(location.origin + item.audio_url);
      audio.volume = volume;
      const releaseHook = callHook("onAudioElement", audio);
      let settled = false;
      let started = false;
      let watchdog;

      function release() {
        clearTimeout(watchdog);
        audio.removeEventListener("ended", settle);
        audio.removeEventListener("error", settle);
        audio.pause();
        audio.removeAttribute("src");
        audio.load();
        if (typeof releaseHook === "function") {
          try { releaseHook(); } catch (error) { console.warn("Audio cleanup failed:", error); }
        }
        activeCancel = null;
      }

      function settle() {
        if (settled) return;
        settled = true;
        release();
        notifyDone(item);
        isPlaying = false;
        if (started) { hideCard(); callHook("onPlayEnd", item); }
        playNext();
      }

      activeCancel = settle;
      audio.addEventListener("ended", settle);
      audio.addEventListener("error", settle);
      watchdog = setTimeout(settle, WATCHDOG_MS);
      function failed(error) {
        if (settled) return;
        if (error && error.name === "NotAllowedError") {
          settled = true;
          release();
          isPlaying = false;
          queue.unshift(item);
          requestUserGesture();
          reportStatus();
        } else { settle(); }
      }
      try {
        Promise.resolve(audio.play()).then(function () {
          if (settled) return;
          started = true;
          showCard(item);
          callHook("onPlayStart", item);
        }, failed);
      } catch (error) { failed(error); }
    }

    function connect() {
      if (stopped) return;
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(protocol + "://" + location.host + "/ws");
      ws = socket;
      socket.onopen = function () {
        if (socket !== ws || stopped) return;
        reconnectDelay = RECONNECT_BASE_MS;
        reportStatus("connected");
      };
      socket.onclose = function () {
        if (socket !== ws || stopped) return;
        queue.length = 0;
        // The server releases this socket's receipts. Stop playback too,
        // since a partially fetched audio resource may already be gone.
        if (activeCancel) activeCancel();
        const delay = Math.round(reconnectDelay * (0.8 + Math.random() * 0.4));
        reportStatus("reconnecting", Math.max(1, Math.round(delay / 1000)));
        reconnectTimer = setTimeout(connect, delay);
        reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      };
      socket.onerror = function () { socket.close(); };
      socket.onmessage = function (message) {
        if (socket !== ws || stopped || typeof message.data !== "string" || message.data.length > 65536) return;
        let item;
        try { item = parseEvent(JSON.parse(message.data), socket); }
        catch (error) { return; }
        if (!item) return;
        queue.push(item);
        while (queue.length > MAX_QUEUE) notifyDone(queue.shift());
        reportStatus();
        playNext();
      };
    }

    window.addEventListener("pagehide", function () {
      stopped = true;
      clearTimeout(reconnectTimer);
      queue.length = 0;
      if (activeCancel) activeCancel();
      clearTimeout(cardHideTimer);
      if (npCard) npCard.classList.remove("leaving");
      autoplayArmed = false;
      if (autoplayHint) autoplayHint.classList.remove("visible");
      if (ws) ws.close();
    });
    window.addEventListener("pageshow", (event) => {
      if (!event.persisted || !stopped) return;
      stopped = false;
      reconnectDelay = RECONNECT_BASE_MS;
      reportStatus("disconnected");
      connect();
    });
    reportStatus("disconnected");
    connect();
  }

  window.VoxerOverlay = { init: init };
})();
