/* eslint-disable no-console */
// Agent Assist iframe UI — connects to /ws/assist/{conversationId}
// and renders live transcript on the left and streaming suggestions
// (plus the wrap-up summary) on the right.

(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const convId = params.get("conversationId") || "";
  $("convPill").textContent = "conversation: " + (convId.substring(0, 8) || "—");

  const transcriptEl = $("transcript");
  const suggEl = $("suggestions");

  // Holds the current interim bubble per channel (replaced as deltas arrive)
  const interim = { customer: null, agent: null };
  let currentSuggestion = null;

  function setStatus(text, cls) {
    $("dot").className = "status-dot " + (cls || "");
    $("statusText").textContent = text;
  }

  function appendBubble(channel, text, isInterim) {
    const div = document.createElement("div");
    div.className = "bubble " + channel + (isInterim ? " interim" : "");
    div.innerHTML = `<div class="who">${channel === "customer" ? "Customer" : "Agent"}</div><div class="text"></div>`;
    div.querySelector(".text").textContent = text;
    transcriptEl.appendChild(div);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    return div;
  }

  function onTranscript(m) {
    if (!m.final) {
      // Replace interim bubble for the channel
      if (!interim[m.channel]) {
        interim[m.channel] = appendBubble(m.channel, "", true);
      }
      const t = interim[m.channel].querySelector(".text");
      t.textContent = (t.textContent + " " + m.text).trim();
    } else {
      // Drop interim; commit final bubble
      if (interim[m.channel]) {
        interim[m.channel].remove();
        interim[m.channel] = null;
      }
      appendBubble(m.channel, m.text, false);
    }
  }

  function onSuggestionStarted() {
    currentSuggestion = document.createElement("div");
    currentSuggestion.className = "suggestion streaming";
    currentSuggestion.textContent = "";
    suggEl.appendChild(currentSuggestion);
    suggEl.scrollTop = suggEl.scrollHeight;
  }

  function onSuggestionDelta(text) {
    if (!currentSuggestion) onSuggestionStarted();
    currentSuggestion.textContent += text;
    suggEl.scrollTop = suggEl.scrollHeight;
  }

  function onSuggestionCompleted(text) {
    if (currentSuggestion) {
      currentSuggestion.classList.remove("streaming");
      if (text && text.length > currentSuggestion.textContent.length) {
        currentSuggestion.textContent = text;
      }
      currentSuggestion = null;
    } else {
      const div = document.createElement("div");
      div.className = "suggestion";
      div.textContent = text;
      suggEl.appendChild(div);
    }
  }

  function onSummary(text, categories) {
    const div = document.createElement("div");
    div.className = "summary";
    div.textContent = text + (categories && categories.length ? "\n\nCategories: " + categories.join(", ") : "");
    suggEl.appendChild(div);
    suggEl.scrollTop = suggEl.scrollHeight;
  }

  function onSnapshot(turns) {
    transcriptEl.innerHTML = "";
    (turns || []).forEach(t => appendBubble(t.channel, t.text, false));
  }

  // ───── WebSocket lifecycle ──────────────────────────────
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/assist/${convId}`);
    setStatus("connecting…", "warn");

    ws.onopen = () => setStatus("live", "live");
    ws.onclose = () => {
      setStatus("disconnected", "");
      // auto-reconnect
      setTimeout(connect, 1500);
    };
    ws.onerror = () => setStatus("error", "warn");
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      switch (m.type) {
        case "session.snapshot": onSnapshot(m.turns); break;
        case "session.notfound": setStatus("waiting call…", "warn"); break;
        case "session.started": setStatus("live", "live"); break;
        case "session.closed": setStatus("call ended", ""); break;
        case "transcript": onTranscript(m); break;
        case "suggestion.started": onSuggestionStarted(); break;
        case "suggestion.delta": onSuggestionDelta(m.text); break;
        case "suggestion.completed": onSuggestionCompleted(m.text); break;
        case "suggestion.error":
          onSuggestionCompleted("⚠️ " + m.text); break;
        case "summary": onSummary(m.text, m.categories); break;
      }
    };
  }

  if (!convId) {
    setStatus("waiting for call…", "warn");
    const ph = document.createElement("div");
    ph.style.cssText = "color:var(--muted);font-size:12px;padding:4px 2px";
    ph.textContent = "Transcript will appear here once the call starts.";
    transcriptEl.appendChild(ph);
    const ph2 = document.createElement("div");
    ph2.style.cssText = "color:var(--muted);font-size:12px;padding:4px 2px";
    ph2.textContent = "Agent suggestions and the wrap-up summary will appear here.";
    suggEl.appendChild(ph2);
  } else {
    connect();
  }
})();
