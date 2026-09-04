/*
 * overlay.js — shared browser-overlay runtime for the Voxer TTS bot.
 *
 * Both overlay pages (index.html and simple.html) load this file. It owns
 * everything that is not visual flair: the WebSocket connection to the
 * server, the playback queue, audio playback itself, the "now playing"
 * card (#np-card), the connection status pill (#status-pill), and the
 * click-to-enable-audio hint (#autoplay-hint).
 *
 * A page calls:
 *
 *   VoxerOverlay.init({
 *     onPlayStart:    (event) => { ... start visual effects ... },
 *     onPlayEnd:      (event) => { ... stop visual effects ... },
 *     onAudioElement: (audio) => { ... optional: wire WebAudio analysis ... },
 *     onStatus:       (status) => { ... optional: react to connection state ... },
 *   });
 *
 * `event` is the BroadcastEvent JSON pushed by the server:
 *   { audio_url, username, avatar_url, emotes: [{name, url}, ...] }
 *
 * `status` is { state: 'connected'|'reconnecting'|'disconnected',
 *               retryInSeconds?, queueDepth }.
 *
 * URL query parameters understood here:
 *   ?volume=0.5   playback volume, clamped to 0..1 (default 1)
 *   ?debug=1      force the status pill visible (even inside OBS)
 *   ?debug=0      force the status pill hidden
 */
(function () {
  "use strict";

  var MAX_QUEUE = 50;          // oldest items are dropped beyond this
  var RECONNECT_BASE_MS = 1000;
  var RECONNECT_MAX_MS = 30000;
  var WATCHDOG_MS = 120000;    // give up on an audio element after 2 minutes

  function init(hooks) {
    hooks = hooks || {};

    var params = new URLSearchParams(location.search);

    var volume = parseFloat(params.get("volume"));
    if (!Number.isFinite(volume)) volume = 1;
    volume = Math.max(0, Math.min(1, volume));

    var debugParam = params.get("debug");

    var queue = [];        // BroadcastEvents waiting to be played
    var pendingDone = [];  // done-filenames buffered while disconnected
    var ws = null;
    var reconnectDelay = RECONNECT_BASE_MS;
    var isPlaying = false;
    var lastState = "disconnected";
    var lastRetrySeconds = null;

    var npCard = document.getElementById("np-card");
    var statusPill = document.getElementById("status-pill");
    var autoplayHint = document.getElementById("autoplay-hint");

    // ── Status pill visibility ───────────────────────────────────────────
    // Inside OBS (window.obsstudio is defined by the OBS browser source)
    // the pill is hidden so it never appears on stream. ?debug=1 forces it
    // visible for troubleshooting, ?debug=0 forces it hidden everywhere.
    if (statusPill) {
      var pillVisible = typeof window.obsstudio === "undefined";
      if (debugParam === "1") pillVisible = true;
      else if (debugParam === "0") pillVisible = false;
      statusPill.style.display = pillVisible ? "" : "none";
    }

    // ── Status reporting ─────────────────────────────────────────────────
    function reportStatus(state, retryInSeconds) {
      if (state) {
        lastState = state;
        lastRetrySeconds = retryInSeconds != null ? retryInSeconds : null;
      }
      var status = { state: lastState, queueDepth: queue.length };
      if (lastState === "reconnecting" && lastRetrySeconds != null) {
        status.retryInSeconds = lastRetrySeconds;
      }
      updatePill(status);
      if (typeof hooks.onStatus === "function") {
        try {
          hooks.onStatus(status);
        } catch (error) {
          console.warn("onStatus hook failed:", error);
        }
      }
    }

    function updatePill(status) {
      if (!statusPill) return;
      var dot = statusPill.querySelector(".dot");
      if (dot) dot.dataset.state = status.state;
      var label;
      if (status.state === "connected") {
        label = "connected";
      } else if (status.state === "reconnecting") {
        label = "reconnecting in " + status.retryInSeconds + "s";
      } else {
        label = "disconnected";
      }
      if (status.queueDepth > 0) {
        label += " · queue " + status.queueDepth;
      }
      var text = statusPill.querySelector(".txt");
      if (text) text.textContent = label;
    }

    // ── Now-playing card ─────────────────────────────────────────────────
    var cardHideTimer = null;

    function showCard(event) {
      if (!npCard) return;
      clearTimeout(cardHideTimer);
      npCard.classList.remove("leaving");

      var avatar = npCard.querySelector(".np-avatar");
      if (avatar) {
        // The <img> in the page carries an onerror fallback that swaps in
        // /static/speaker.svg when the avatar URL fails to load. Re-arm it
        // in case a previous avatar already tripped it.
        avatar.onerror = function () {
          avatar.onerror = null;
          avatar.src = "/static/speaker.svg";
        };
        avatar.src = event.avatar_url || "/static/speaker.svg";
      }

      var name = npCard.querySelector(".np-username");
      if (name) name.textContent = "@" + (event.username || "");

      var strip = npCard.querySelector(".np-emotes");
      if (strip) {
        strip.textContent = "";
        var seen = {};
        var shown = 0;
        var emotes = event.emotes || [];
        for (var i = 0; i < emotes.length && shown < 8; i++) {
          var emote = emotes[i];
          if (!emote || !emote.url || seen[emote.url]) continue;
          seen[emote.url] = true;
          var img = document.createElement("img");
          img.src = emote.url;
          img.alt = emote.name || "";
          strip.appendChild(img);
          shown++;
        }
      }

      npCard.classList.add("visible");
    }

    function hideCard() {
      if (!npCard || !npCard.classList.contains("visible")) return;
      npCard.classList.remove("visible");
      npCard.classList.add("leaving");
      var finish = function () {
        clearTimeout(cardHideTimer);
        npCard.removeEventListener("animationend", finish);
        npCard.classList.remove("leaving");
      };
      npCard.addEventListener("animationend", finish);
      // Fallback in case animationend never fires (e.g. animations disabled).
      cardHideTimer = setTimeout(finish, 400);
    }

    // ── Server notification ("done" → server deletes the MP3) ────────────
    // The "done" key is the entire client→server protocol; the server reads it
    // as DONE_FIELD in voxer/server.py, which is the one place it is named.
    function notifyDone(audioUrl) {
      var filename = String(audioUrl).split("/").pop();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ done: filename }));
      } else {
        // Buffer it; flushed on the next successful (re)connect so the
        // server still cleans up the temp file.
        pendingDone.push(filename);
      }
    }

    // ── Autoplay recovery ────────────────────────────────────────────────
    // Browsers block audio until the user interacts with the page. OBS
    // never blocks, but a normal browser tab does — so show a hint and
    // retry on the first click or key press.
    var autoplayArmed = false;

    function requestUserGesture() {
      if (autoplayHint) autoplayHint.classList.add("visible");
      if (autoplayArmed) return;
      autoplayArmed = true;
      var resume = function () {
        window.removeEventListener("pointerdown", resume);
        window.removeEventListener("keydown", resume);
        autoplayArmed = false;
        if (autoplayHint) autoplayHint.classList.remove("visible");
        playNext();
      };
      window.addEventListener("pointerdown", resume);
      window.addEventListener("keydown", resume);
    }

    // ── Playback ─────────────────────────────────────────────────────────
    function playNext() {
      if (isPlaying || queue.length === 0) return;
      var item = queue.shift();
      reportStatus();
      isPlaying = true;

      var fullUrl = location.origin + item.audio_url;
      var audio = new Audio(fullUrl);
      audio.volume = volume;

      // Let the page attach WebAudio analysis BEFORE play() — a
      // MediaElementSource can only be created once per element, and it
      // must exist before playback begins for reactivity to work.
      if (typeof hooks.onAudioElement === "function") {
        try {
          hooks.onAudioElement(audio);
        } catch (error) {
          console.warn("onAudioElement hook failed:", error);
        }
      }

      var settled = false;
      var started = false;
      var watchdog = null;

      // Idempotent: ended, error and the watchdog all funnel through here,
      // and only the first caller wins.
      var settle = function () {
        if (settled) return;
        settled = true;
        clearTimeout(watchdog);
        notifyDone(item.audio_url);
        isPlaying = false;
        if (started) {
          hideCard();
          if (typeof hooks.onPlayEnd === "function") {
            try {
              hooks.onPlayEnd(item);
            } catch (error) {
              console.warn("onPlayEnd hook failed:", error);
            }
          }
        }
        playNext();
      };

      audio.addEventListener("ended", settle);
      audio.addEventListener("error", function () {
        console.error("Failed to play:", fullUrl);
        settle();
      });

      // Guard against elements that stall forever (network hiccup, codec
      // problem): after 2 minutes, force the queue to move on.
      watchdog = setTimeout(function () {
        console.warn("Playback watchdog fired for:", fullUrl);
        try {
          audio.pause();
        } catch (error) {
          /* pause never throws in practice; be safe anyway */
        }
        settle();
      }, WATCHDOG_MS);

      audio.play().then(
        function () {
          started = true;
          showCard(item);
          if (typeof hooks.onPlayStart === "function") {
            try {
              hooks.onPlayStart(item);
            } catch (error) {
              console.warn("onPlayStart hook failed:", error);
            }
          }
        },
        function (error) {
          if (error && error.name === "NotAllowedError") {
            // Autoplay blocked: keep the item (front of the queue), do NOT
            // notify done (the file has not been heard yet), and wait for
            // a user gesture.
            settled = true;
            clearTimeout(watchdog);
            isPlaying = false;
            queue.unshift(item);
            reportStatus();
            requestUserGesture();
          } else {
            console.error("Playback failed:", error);
            settle();
          }
        }
      );
    }

    // ── WebSocket with exponential backoff + jitter ──────────────────────
    function connect() {
      var protocol = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(protocol + "://" + location.host + "/ws");

      ws.onopen = function () {
        console.log("voxer overlay connected");
        reconnectDelay = RECONNECT_BASE_MS;
        // Flush done-notifications buffered while we were offline so the
        // server can delete those MP3s.
        while (pendingDone.length) {
          ws.send(JSON.stringify({ done: pendingDone.shift() }));
        }
        reportStatus("connected");
      };

      ws.onclose = function () {
        // ±20% jitter so many overlay tabs do not reconnect in lockstep.
        var jitter = 0.8 + Math.random() * 0.4;
        var delay = Math.round(reconnectDelay * jitter);
        console.warn("voxer overlay disconnected, reconnecting in " + delay + "ms");
        reportStatus("reconnecting", Math.max(1, Math.round(delay / 1000)));
        setTimeout(connect, delay);
        reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      };

      ws.onerror = function (error) {
        console.error("voxer overlay socket error:", error);
        ws.close();
      };

      ws.onmessage = function (message) {
        var event;
        try {
          event = JSON.parse(message.data);
        } catch (error) {
          console.error("Ignoring malformed broadcast:", error);
          return;
        }
        queue.push(event);
        // Bound the queue: drop the OLDEST item but still tell the server
        // it is done, so its MP3 gets deleted instead of leaking.
        while (queue.length > MAX_QUEUE) {
          var dropped = queue.shift();
          console.warn("Queue overflow, dropping oldest item:", dropped.audio_url);
          notifyDone(dropped.audio_url);
        }
        reportStatus();
        playNext();
      };
    }

    reportStatus("disconnected");
    connect();
  }

  window.VoxerOverlay = { init: init };
})();
