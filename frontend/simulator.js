/* eslint-disable no-console */
// Genesys Cloud Agent Desktop simulator
// - getUserMedia at native rate → ScriptProcessor downsamples to 8 kHz
// - µ-law encoding in JS
// - Interleaves customer/agent channels in a single binary frame
// - Talks the AudioHook v2 protocol against /ws/audiohook

(() => {
  const $ = (id) => document.getElementById(id);
  const log = (msg) => {
    const el = $("log");
    const t = new Date().toISOString().substring(11, 23);
    el.innerHTML += `[${t}] ${msg}<br>`;
    el.scrollTop = el.scrollHeight;
  };

  // ───── State ──────────────────────────────────────────────
  const state = {
    ws: null,
    audioCtx: null,
    sourceNode: null,
    processor: null,
    sessionId: null,
    conversationId: null,
    serverSeq: 0,
    clientSeq: 0,
    open: false,
    // Push-to-talk
    customerActive: false,
    agentActive: false,
    // Buffers (Int16, 8 kHz) per channel — written by audio worklet
    custBuf: [],
    agentBuf: [],
    // Frame size = 20 ms @ 8 kHz = 160 samples
    frameSamples: 160,
    flushTimer: null,
  };

  // ───── µ-law encode ───────────────────────────────────────
  function linearToUlaw(sample) {
    const BIAS = 0x84, CLIP = 32635;
    let sign = (sample >> 8) & 0x80;
    if (sign) sample = -sample;
    if (sample > CLIP) sample = CLIP;
    sample = sample + BIAS;
    let exponent = 7;
    for (let mask = 0x4000; (sample & mask) === 0 && exponent > 0; mask >>= 1) exponent--;
    const mantissa = (sample >> (exponent + 3)) & 0x0f;
    const ulaw = ~(sign | (exponent << 4) | mantissa) & 0xff;
    return ulaw;
  }

  // Simple downsample by integer factor (linear average). Browser
  // typically gives 48000 → 8000 = factor 6.
  function downsampleFloat32ToInt16(float32, srcRate, dstRate) {
    if (srcRate === dstRate) {
      const out = new Int16Array(float32.length);
      for (let i = 0; i < float32.length; i++) {
        const s = Math.max(-1, Math.min(1, float32[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      return out;
    }
    const ratio = srcRate / dstRate;
    const newLen = Math.floor(float32.length / ratio);
    const out = new Int16Array(newLen);
    let pos = 0;
    for (let i = 0; i < newLen; i++) {
      const next = Math.floor((i + 1) * ratio);
      let sum = 0, count = 0;
      for (let j = pos; j < next && j < float32.length; j++) {
        sum += float32[j];
        count++;
      }
      const avg = count ? sum / count : 0;
      const s = Math.max(-1, Math.min(1, avg));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      pos = next;
    }
    return out;
  }

  // ───── AudioHook helpers ──────────────────────────────────
  function uuid() {
    return crypto.randomUUID();
  }

  function nextClientSeq() { state.clientSeq += 1; return state.clientSeq; }

  function pos() {
    if (!state.startTime) state.startTime = performance.now();
    const sec = (performance.now() - state.startTime) / 1000;
    return `PT${sec.toFixed(3)}S`;
  }

  function send(obj) {
    if (!state.ws || state.ws.readyState !== 1) return;
    state.ws.send(JSON.stringify(obj));
  }

  function sendOpen(agentId, language) {
    state.sessionId = uuid();
    state.conversationId = uuid();
    $("convPill").textContent = "conversation: " + state.conversationId.substring(0, 8);
    send({
      version: "2",
      id: state.sessionId,
      type: "open",
      seq: nextClientSeq(),
      serverseq: state.serverSeq,
      position: pos(),
      parameters: {
        organizationId: uuid(),
        conversationId: state.conversationId,
        participant: { id: uuid(), ani: "+34666000000", aniName: "Simulator", dnis: "+34900000000" },
        media: [{ type: "audio", format: "PCMU", channels: ["external", "internal"], rate: 8000 }],
        language,
        inputVariables: { agentId },
      },
    });
  }

  function sendPing() {
    send({
      version: "2", id: state.sessionId, type: "ping",
      seq: nextClientSeq(), serverseq: state.serverSeq, position: pos(),
      parameters: {},
    });
  }

  function sendClose() {
    if (!state.sessionId) return;
    send({
      version: "2", id: state.sessionId, type: "close",
      seq: nextClientSeq(), serverseq: state.serverSeq, position: pos(),
      parameters: { reason: "end" },
    });
  }

  // ───── Audio pipeline ─────────────────────────────────────
  async function startAudio(deviceId) {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: deviceId ? { exact: deviceId } : undefined,
               echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const srcRate = state.audioCtx.sampleRate;
    log(`Audio context rate: ${srcRate} Hz`);

    state.sourceNode = state.audioCtx.createMediaStreamSource(stream);
    // ScriptProcessor is deprecated but widely supported and easy.
    // bufferSize=2048 @ 48kHz ≈ 42 ms — fine for streaming.
    state.processor = state.audioCtx.createScriptProcessor(2048, 1, 1);
    state.sourceNode.connect(state.processor);
    state.processor.connect(state.audioCtx.destination);

    state.processor.onaudioprocess = (e) => {
      const inBuf = e.inputBuffer.getChannelData(0);
      const i16_8k = downsampleFloat32ToInt16(inBuf, srcRate, 8000);
      // Route to the active push-to-talk channel(s); the other is filled with silence.
      for (let i = 0; i < i16_8k.length; i++) {
        state.custBuf.push(state.customerActive ? i16_8k[i] : 0);
        state.agentBuf.push(state.agentActive ? i16_8k[i] : 0);
      }
      flushFrames();
    };
  }

  function flushFrames() {
    while (state.custBuf.length >= state.frameSamples &&
           state.agentBuf.length >= state.frameSamples) {
      const n = state.frameSamples;
      // Interleave [c0, a0, c1, a1, ...] µ-law
      const frame = new Uint8Array(n * 2);
      for (let i = 0; i < n; i++) {
        frame[2 * i] = linearToUlaw(state.custBuf[i]);
        frame[2 * i + 1] = linearToUlaw(state.agentBuf[i]);
      }
      state.custBuf.splice(0, n);
      state.agentBuf.splice(0, n);
      if (state.ws && state.ws.readyState === 1 && state.open) {
        state.ws.send(frame);
      }
    }
  }

  async function stopAudio() {
    try { state.processor && state.processor.disconnect(); } catch (e) {}
    try { state.sourceNode && state.sourceNode.disconnect(); } catch (e) {}
    try { state.audioCtx && (await state.audioCtx.close()); } catch (e) {}
    state.processor = state.sourceNode = state.audioCtx = null;
    state.custBuf = []; state.agentBuf = [];
  }

  // ───── Lifecycle ──────────────────────────────────────────
  async function listMics() {
    const sel = $("micSelect");
    sel.innerHTML = "";
    // Trigger permissions so labels appear
    try { await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) {}
    const devs = await navigator.mediaDevices.enumerateDevices();
    devs.filter(d => d.kind === "audioinput").forEach((d, i) => {
      const o = document.createElement("option");
      o.value = d.deviceId;
      o.textContent = d.label || `Microphone ${i + 1}`;
      sel.appendChild(o);
    });
  }

  async function loadAgents() {
    const sel = $("agentSelect");
    sel.innerHTML = "";
    try {
      const r = await fetch("/api/agents");
      const items = await r.json();
      items.forEach(a => {
        const o = document.createElement("option");
        o.value = a.id; o.textContent = `${a.name} — ${a.id}`;
        sel.appendChild(o);
      });
    } catch (e) {
      log("Could not load agents: " + e.message);
    }
  }

  async function startCall() {
    const agentId = $("agentSelect").value;
    const language = $("lang").value || "es";
    if (!agentId) { alert("Pick an agent"); return; }

    $("btnStart").disabled = true;
    $("btnStart").textContent = "⏳ Connecting call…";

    // Reset the agent-assist iframe to its placeholder state for the new
    // call. We do this here (and NOT on End call) so that the previous
    // call's transcript + suggestions + summary remain visible until the
    // user explicitly starts a new conversation.
    try { $("assist").src = "/agent-assist"; $("iframeUrl").textContent = ""; } catch (e) {}

    // Audio first so mic permission is requested before WS open
    await startAudio($("micSelect").value);

    const proto = location.protocol === "https:" ? "wss" : "ws";
    state.ws = new WebSocket(`${proto}://${location.host}/ws/audiohook`);
    state.ws.binaryType = "arraybuffer";

    state.ws.onopen = () => {
      setStatus(true);
      sendOpen(agentId, language);
    };
    state.ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (typeof m.seq === "number") state.serverSeq = m.seq;
        if (m.type === "opened") {
          state.open = true;
          log("session opened");
          // Load iframe now that we have a conversationId
          const url = `/agent-assist?conversationId=${state.conversationId}`;
          $("assist").src = url;
          $("iframeUrl").textContent = url;
          $("btnEnd").disabled = false;
          $("btnWrapup").disabled = false;
          $("pttCustomer").disabled = false;
          $("pttAgent").disabled = false;
          $("btnStart").disabled = true;
          $("btnStart").textContent = "● Connected";
          // ping every 15s
          state.pingTimer = setInterval(sendPing, 15000);
        } else if (m.type === "closed") {
          log("session closed by server");
        }
      } catch (e) { /* ignore non-json */ }
    };
    state.ws.onclose = () => { setStatus(false); log("WS closed"); cleanup(); };
    state.ws.onerror = (e) => { log("WS error"); console.error(e); };
  }

  async function endCall() {
    sendClose();
    setTimeout(cleanup, 200);
    // wrap up server-side session
    try {
      await fetch(`/api/sessions/${state.conversationId}/close`, { method: "POST" });
    } catch (e) {}
  }

  async function wrapUp() {
    if (!state.conversationId) return;
    log("requesting wrap-up summary…");
    try {
      const r = await fetch(`/api/wrapup/${state.conversationId}`, { method: "POST" });
      const j = await r.json();
      log("summary done (" + (j.categories || []).join(", ") + ")");
    } catch (e) {
      log("wrap-up error: " + e.message);
    }
  }

  function cleanup() {
    state.open = false;
    if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
    try { state.ws && state.ws.close(); } catch (e) {}
    state.ws = null;
    stopAudio();
    $("btnStart").disabled = false;
    $("btnStart").textContent = "▶ Start call";
    $("btnEnd").disabled = true;
    $("pttCustomer").disabled = true;
    $("pttAgent").disabled = true;
    // NOTE: We intentionally do NOT reset the agent-assist iframe here. The
    // transcript + suggestions + summary stay visible after End call so the
    // user can still trigger Generate summary. The iframe is reset on the
    // next Start call.
  }

  function setStatus(live) {
    $("dot").className = "status-dot " + (live ? "live" : "");
    $("connText").textContent = live ? "connected" : "disconnected";
  }

  // ───── PTT handlers ───────────────────────────────────────
  function bindPTT(btnId, key) {
    const btn = $(btnId);
    const on = () => { state[key] = true; btn.classList.add("active"); };
    const off = () => { state[key] = false; btn.classList.remove("active"); };
    btn.addEventListener("mousedown", on);
    btn.addEventListener("mouseup", off);
    btn.addEventListener("mouseleave", off);
    btn.addEventListener("touchstart", (e) => { e.preventDefault(); on(); });
    btn.addEventListener("touchend", (e) => { e.preventDefault(); off(); });
  }

  // ───── Init ───────────────────────────────────────────────
  (async function init() {
    await listMics();
    await loadAgents();
    try {
      const cfg = await (await fetch("/api/config")).json();
      $("lang").value = cfg.language || "es";
    } catch (e) {}
    bindPTT("pttCustomer", "customerActive");
    bindPTT("pttAgent", "agentActive");
    $("btnStart").addEventListener("click", startCall);
    $("btnEnd").addEventListener("click", endCall);
    $("btnWrapup").addEventListener("click", wrapUp);
  })();
})();
