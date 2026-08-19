"use strict";

const attackTimes = ["0 ms", "30 ms", "100 ms", "200 ms", "500 ms", "1000 ms", "2000 ms", "4000 ms"];
const holdTimes = ["0 ms", "30 ms", "100 ms", "200 ms", "500 ms", "1000 ms", "2500 ms", "Infinite"];
const releaseTimes = ["Background", "30 ms", "100 ms", "200 ms", "500 ms", "1000 ms", "2000 ms", "4000 ms"];
const randomPercent = ["0%", "10%", "20%", "35%", "50%", "65%", "80%", "95%"];
const storageKey = "concert-led-bracelet-studio-flow-v1";
const legacyStorageKey = "pixmob-flow-builder-v1";

const elements = {};
const elementIds = [
  "hardware-dot", "hardware-name", "frequency-label", "gain-label", "block-search",
  "block-library", "root-flow-list", "flow-summary", "undo-button", "clear-flow-button",
  "selected-kind", "inspector-content", "clear-log", "log-output", "preview-summary",
  "preview-flow-button", "timeline-segments", "arm-toggle", "arm-help", "run-flow-button",
  "tx-gain-slider", "tx-gain-output", "run-help", "ready-state", "safety-state",
  "mode-flow-button", "mode-music-button", "flow-mode", "music-mode",
  "music-device", "music-signal-dot", "music-signal-label", "music-meter-fill",
  "music-sample-rate", "music-monitor-button", "music-live-state", "music-waveform",
  "music-bass-bar", "music-mid-bar", "music-treble-bar", "music-color-preview",
  "music-color-label", "music-bpm", "music-energy", "music-beat-count",
  "music-history-dots", "music-palette", "music-sensitivity", "music-sensitivity-output",
  "music-brightness", "music-brightness-output", "music-interval", "music-interval-output",
  "music-clear-log", "music-log-output", "music-monitor-badge", "music-monitor-state",
  "music-monitor-detail", "music-tx-gain-slider", "music-tx-gain-output",
  "music-arm-toggle", "music-start-button", "music-start-help"
];

let serverConfig = null;
let csrfToken = "";
let presets = [];
let flowBlocks = [];
let selectedBlockId = null;
let draggedItem = null;
let historyStack = [];
let previewTimer = null;
let previewRequestId = 0;
let currentFlowReport = null;
let flowRunning = false;
let activeStudioMode = "flow";
let musicDevices = [];
let musicDevicesLoaded = false;
let musicStatus = null;
let musicPollTimer = null;
let lastMusicBeatCount = 0;
let musicRequestError = null;
let lastRenderedMusicTransmitError = null;

function cacheElements() {
  elementIds.forEach((id) => { elements[id] = document.getElementById(id); });
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function uid(prefix = "block") {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return `${prefix}-${window.crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatFrequency(hz) {
  return `${(hz / 1_000_000).toFixed(3)} MHz`;
}

function formatDuration(microseconds) {
  if (microseconds >= 60_000_000) return `${(microseconds / 60_000_000).toFixed(2)} min`;
  if (microseconds >= 1_000_000) return `${(microseconds / 1_000_000).toFixed(2)} s`;
  if (microseconds >= 1_000) return `${(microseconds / 1_000).toFixed(0)} ms`;
  return `${microseconds} µs`;
}

function hexByte(value) {
  return Number(value).toString(16).padStart(2, "0").toUpperCase();
}

function rgbHex(block) {
  return `#${hexByte(block.red)}${hexByte(block.green)}${hexByte(block.blue)}`;
}

function log(message, kind = "info") {
  const row = document.createElement("div");
  row.className = `log-entry ${kind}`;
  const time = new Date().toLocaleTimeString([], { hour12: false });
  row.innerHTML = "<span class=\"log-time\"></span><span class=\"log-indicator\"></span><span></span>";
  row.children[0].textContent = time;
  row.children[2].textContent = message;
  elements["log-output"].append(row);
  elements["log-output"].scrollTop = elements["log-output"].scrollHeight;
}

async function api(path, body = null) {
  const options = body === null ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PixMob-Token": csrfToken },
    body: JSON.stringify(body)
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function musicLog(message, kind = "info") {
  const row = document.createElement("div");
  row.className = `log-entry ${kind}`;
  const time = new Date().toLocaleTimeString([], { hour12: false });
  row.innerHTML = "<span class=\"log-time\"></span><span class=\"log-indicator\"></span><span></span>";
  row.children[0].textContent = time;
  row.children[2].textContent = message;
  elements["music-log-output"].append(row);
  elements["music-log-output"].scrollTop = elements["music-log-output"].scrollHeight;
}

function setTxGain(value) {
  if (!serverConfig) return;
  const gain = Math.max(0, Math.min(47, Number.parseInt(value, 10) || 0));
  serverConfig.txGainDb = gain;
  elements["tx-gain-slider"].value = String(gain);
  elements["music-tx-gain-slider"].value = String(gain);
  elements["tx-gain-output"].textContent = `${gain} dB`;
  elements["music-tx-gain-output"].textContent = `${gain} dB`;
  elements["gain-label"].textContent = `${gain} dB`;
  updateRunState();
  updateMusicControls();
}

function setArmState(armed, { announce = true } = {}) {
  const canTransmit = Boolean(serverConfig?.transmitAllowed && serverConfig?.hackrfTransferFound);
  const next = canTransmit && Boolean(armed);
  elements["arm-toggle"].checked = next;
  elements["music-arm-toggle"].checked = next;
  elements["arm-help"].textContent = next
    ? "Stays armed until switched off. Preview remains passive."
    : "Enable once; switch off when finished.";
  elements["safety-state"].textContent = next
    ? "RF armed — verify the wristband is nearby"
    : canTransmit ? "RF locked — no transmission armed" : "Dry run — no RF emitted";
  if (announce) {
    const message = next ? "RF transmit armed" : "RF transmit disarmed";
    log(message, next ? "warning" : "info");
    musicLog(message, next ? "warning" : "info");
  }
  updateRunState();
  updateMusicControls();
}

function rgbToHex(rgb) {
  if (!Array.isArray(rgb) || rgb.length !== 3) return "#000000";
  return `#${rgb.map((value) => hexByte(Math.max(0, Math.min(255, value)))).join("")}`;
}

function setStudioMode(mode) {
  activeStudioMode = mode === "music" ? "music" : "flow";
  const musicSelected = activeStudioMode === "music";
  elements["flow-mode"].hidden = musicSelected;
  elements["music-mode"].hidden = !musicSelected;
  elements["mode-flow-button"].classList.toggle("selected", !musicSelected);
  elements["mode-music-button"].classList.toggle("selected", musicSelected);
  elements["mode-flow-button"].setAttribute("aria-pressed", String(!musicSelected));
  elements["mode-music-button"].setAttribute("aria-pressed", String(musicSelected));
  if (musicSelected) {
    void loadMusicDevices();
    startMusicPolling();
  }
}

function selectedMusicDevice() {
  const id = Number.parseInt(elements["music-device"].value, 10);
  return musicDevices.find((device) => device.id === id) || null;
}

function updateSelectedMusicDevice() {
  const device = selectedMusicDevice();
  elements["music-sample-rate"].textContent = device ? `${(device.sampleRate / 1000).toFixed(0)} kHz` : "—";
}

async function loadMusicDevices() {
  if (musicDevicesLoaded) return;
  musicDevicesLoaded = true;
  try {
    const response = await api("/api/music/devices");
    if (!response.available) throw new Error(response.error || "Audio monitoring is unavailable");
    musicDevices = response.devices || [];
    elements["music-device"].replaceChildren();
    for (const device of musicDevices) {
      const option = document.createElement("option");
      option.value = String(device.id);
      option.textContent = `${device.name}${device.recommended ? " · recommended" : ""}`;
      elements["music-device"].append(option);
    }
    const stereoMix = musicDevices.find((device) => device.name.toLowerCase().includes("stereo mix"));
    const recommended = stereoMix || musicDevices.find((device) => device.recommended) || musicDevices[0];
    if (recommended) elements["music-device"].value = String(recommended.id);
    elements["music-monitor-button"].disabled = !recommended;
    updateSelectedMusicDevice();
    musicLog(recommended ? `Audio source ready: ${recommended.name}` : "No Windows audio input was found", recommended ? "success" : "error");
    await refreshMusicStatus();
  } catch (error) {
    musicDevicesLoaded = false;
    elements["music-device"].replaceChildren(new Option("Audio input unavailable", ""));
    elements["music-monitor-button"].disabled = true;
    musicLog(error.message, "error");
  }
}

function musicRequest(transmit) {
  return {
    deviceId: Number.parseInt(elements["music-device"].value, 10),
    sensitivity: Number.parseInt(elements["music-sensitivity"].value, 10),
    brightness: Number.parseInt(elements["music-brightness"].value, 10),
    minIntervalMs: Number.parseInt(elements["music-interval"].value, 10),
    palette: elements["music-palette"].value,
    txGainDb: serverConfig.txGainDb,
    transmit,
    armed: transmit && elements["music-arm-toggle"].checked,
    confirmation: transmit ? "TRANSMIT" : ""
  };
}

function appendBeatHistory(color, count) {
  const additions = Math.min(6, Math.max(1, count - lastMusicBeatCount));
  for (let index = 0; index < additions; index += 1) {
    const dot = document.createElement("span");
    dot.className = "music-history-dot";
    dot.style.background = color;
    dot.style.color = color;
    dot.title = `Beat ${count - additions + index + 1}`;
    elements["music-history-dots"].append(dot);
  }
  while (elements["music-history-dots"].children.length > 24) {
    elements["music-history-dots"].firstElementChild.remove();
  }
  lastMusicBeatCount = count;
}

function renderMusicFrame(frame) {
  if (!frame) {
    elements["music-meter-fill"].style.width = "0%";
    elements["music-signal-dot"].classList.add("offline");
    elements["music-signal-label"].textContent = musicStatus?.running ? "Listening for audio" : "Monitor stopped";
    return;
  }
  const hasSignal = frame.peak > 0.001;
  elements["music-signal-dot"].classList.toggle("offline", !hasSignal);
  elements["music-signal-label"].textContent = hasSignal ? "Audio signal present" : "No audio signal";
  elements["music-meter-fill"].style.width = `${Math.max(0, Math.min(100, frame.energy))}%`;

  const values = Array.isArray(frame.waveform) ? frame.waveform : [];
  if (values.length > 1) {
    const points = values.map((value, index) => {
      const x = (index / (values.length - 1)) * 1000;
      const y = 110 - Math.max(-1, Math.min(1, value)) * 92;
      return `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    });
    elements["music-waveform"].setAttribute("d", points.join(" "));
  }
  const bandHeight = (value) => `${Math.max(4, Math.min(100, Math.sqrt(Math.max(0, value)) * 100))}%`;
  elements["music-bass-bar"].style.height = hasSignal ? bandHeight(frame.bass) : "4%";
  elements["music-mid-bar"].style.height = hasSignal ? bandHeight(frame.mid) : "4%";
  elements["music-treble-bar"].style.height = hasSignal ? bandHeight(frame.treble) : "4%";
  const color = hasSignal ? rgbToHex(frame.rgb) : "#000000";
  elements["music-color-preview"].style.background = color;
  elements["music-color-preview"].style.boxShadow = `0 0 34px ${color}66`;
  elements["music-color-label"].textContent = color;
  elements["music-bpm"].textContent = frame.bpm > 0 ? String(Math.round(frame.bpm)) : "—";
  elements["music-energy"].textContent = `${frame.energy}%`;
  elements["music-beat-count"].textContent = String(frame.beatCount);
  if (frame.beatCount > lastMusicBeatCount) appendBeatHistory(color, frame.beatCount);
}

function updateMusicControls() {
  if (!serverConfig) return;
  const running = Boolean(musicStatus?.running);
  const transmitting = Boolean(musicStatus?.transmit);
  const available = musicStatus?.available !== false;
  const canTransmit = Boolean(serverConfig.transmitAllowed && serverConfig.hackrfTransferFound);
  const armed = canTransmit && elements["music-arm-toggle"].checked;
  const hasDevice = Boolean(selectedMusicDevice());
  const transmitError = musicStatus?.transmitError || musicRequestError;

  elements["music-device"].disabled = running || !available;
  elements["music-palette"].disabled = running || !available;
  elements["music-sensitivity"].disabled = running || !available;
  elements["music-brightness"].disabled = running || !available;
  elements["music-interval"].disabled = running || !available;
  elements["music-monitor-button"].disabled = !available || !hasDevice || transmitting;
  elements["music-monitor-button"].textContent = running && !transmitting ? "Stop Passive Monitor" : transmitting ? "Monitor Active" : "Start Passive Monitor";
  elements["music-tx-gain-slider"].disabled = !canTransmit || transmitting || flowRunning;
  elements["music-arm-toggle"].disabled = !canTransmit || transmitting || flowRunning;
  elements["music-start-button"].disabled = transmitting ? false : flowRunning || !(running && !transmitting && armed);
  elements["music-start-button"].classList.toggle("stop", transmitting);
  elements["music-start-button"].textContent = transmitting ? "Stop Music Sync" : "Start Music Sync";
  elements["music-start-help"].textContent = transmitting
    ? `${musicStatus.transmissionCount || 0} beat transmissions sent at ${serverConfig.txGainDb} dB${musicStatus.retryCount ? ` · ${musicStatus.retryCount} USB retries recovered` : ""}.`
    : flowRunning
      ? "A Flow Builder transmission is in progress."
    : transmitError
      ? "Reconnect the HackRF, then click Start Music Sync again. RF remains armed."
    : !canTransmit
      ? "RF transmission is disabled by the server."
      : !running
        ? "Start the passive monitor, then arm RF."
        : !armed
          ? "Audio is live. Arm RF transmit to enable sync."
          : `Ready at ${serverConfig.txGainDb} dB. Starts only when clicked.`;
}

function renderMusicStatus(status) {
  musicStatus = status;
  const running = Boolean(status.running);
  const transmitting = Boolean(status.transmit);
  const transmitError = status.transmitError || musicRequestError;
  elements["music-live-state"].textContent = status.error
    ? "Audio input error"
    : transmitError && running
      ? "Analyzing locally · RF stopped"
      : transmitting ? "Reacting and transmitting" : running ? "Analyzing locally" : "Waiting for monitor";
  elements["music-monitor-badge"].textContent = transmitting ? "RF Sync" : running ? "Active" : "Stopped";
  elements["music-monitor-badge"].classList.toggle("active", running && !transmitting);
  elements["music-monitor-badge"].classList.toggle("transmitting", transmitting);
  elements["music-monitor-state"].textContent = status.error
    ? "Audio monitor stopped with an error"
    : transmitError && running
      ? "RF sync stopped; passive monitoring continues"
      : transmitting ? "Beat-triggered RF sync is active" : running ? "Local audio analysis is active" : "No audio monitor running";
  elements["music-monitor-detail"].textContent = status.error
    ? status.error
    : transmitError
      ? transmitError
    : transmitting
      ? `${status.transmissionCount || 0} RF transmissions sent${status.retryCount ? ` · ${status.retryCount} transient USB retries recovered` : ""}. Use Stop Music Sync to end.`
      : running ? "No RF is emitted while passive monitoring is active." : "Choose an input and start passive monitoring.";
  if (status.transmitError && status.transmitError !== lastRenderedMusicTransmitError) {
    musicLog(`RF sync stopped: ${status.transmitError}`, "error");
    lastRenderedMusicTransmitError = status.transmitError;
    elements["hardware-dot"].classList.add("offline");
    elements["hardware-name"].textContent = "HackRF disconnected";
  }
  renderMusicFrame(status.frame);
  updateMusicControls();
}

async function refreshMusicStatus() {
  try {
    renderMusicStatus(await api("/api/music/status"));
  } catch (error) {
    elements["music-live-state"].textContent = "Controller unavailable";
    musicLog(error.message, "error");
  }
}

function startMusicPolling() {
  if (musicPollTimer !== null) return;
  void refreshMusicStatus();
  musicPollTimer = window.setInterval(() => {
    if (activeStudioMode === "music" || musicStatus?.running) void refreshMusicStatus();
  }, 150);
}

async function togglePassiveMonitor() {
  if (musicStatus?.transmit) return;
  elements["music-monitor-button"].disabled = true;
  try {
    if (musicStatus?.running) {
      musicRequestError = null;
      renderMusicStatus(await api("/api/music/stop", {}));
      musicLog("Passive audio monitor stopped");
    } else {
      musicRequestError = null;
      renderMusicStatus(await api("/api/music/start", musicRequest(false)));
      musicLog("Passive audio monitor started — no RF emitted", "success");
    }
  } catch (error) {
    musicLog(error.message, "error");
    await refreshMusicStatus();
  }
}

async function toggleMusicSync() {
  elements["music-start-button"].disabled = true;
  try {
    if (musicStatus?.transmit) {
      musicRequestError = null;
      renderMusicStatus(await api("/api/music/stop", {}));
      setArmState(false, { announce: false });
      musicLog("Music sync stopped and RF disarmed", "success");
    } else {
      musicRequestError = null;
      renderMusicStatus(await api("/api/music/start", musicRequest(true)));
      lastRenderedMusicTransmitError = null;
      elements["hardware-dot"].classList.remove("offline");
      elements["hardware-name"].textContent = "HackRF tools ready";
      musicLog(`Music sync started at ${serverConfig.txGainDb} dB`, "warning");
    }
  } catch (error) {
    musicRequestError = error.message;
    if (/hackrf/i.test(error.message)) {
      elements["hardware-dot"].classList.add("offline");
      elements["hardware-name"].textContent = "HackRF disconnected";
    }
    musicLog(error.message, "error");
    await refreshMusicStatus();
  }
}

function presetByName(name) {
  return presets.find((preset) => preset.name === name);
}

function blockFromPreset(preset, type = "color") {
  return {
    id: uid(type),
    type,
    label: preset.label,
    target: preset.name,
    red: preset.red,
    green: preset.green,
    blue: preset.blue,
    attack: preset.attack,
    hold: preset.hold,
    release: preset.release,
    random: preset.random,
    group: preset.group,
    mode: preset.mode,
    repeatCount: 3
  };
}

function makeBlock(type) {
  if (type === "color") return blockFromPreset(presetByName("red"), "color");
  if (type === "fade") return blockFromPreset(presetByName("fade-gold"), "fade");
  if (type === "wait") return { id: uid("wait"), type: "wait", label: "Wait 500 ms", durationMs: 500 };
  if (type === "loop") return { id: uid("loop"), type: "loop", label: "Loop 3x", count: 3, loopDelayMs: 0, children: [] };
  if (type === "wake") {
    const wake = presetByName("wake");
    return {
      id: uid("wake"), type: "wake", label: "Wake", target: "wake",
      red: 0, green: 0, blue: 0, attack: wake.attack, hold: wake.hold,
      release: wake.release, random: wake.random, group: 0,
      mode: wake.mode, wakeDurationS: 20
    };
  }
  if (type === "off") return { id: uid("off"), type: "off", label: "Off", target: "off", group: 0, repeatCount: 3 };
  throw new Error(`Unknown block type: ${type}`);
}

function seedFlow() {
  const fade = blockFromPreset(presetByName("fade-gold"), "fade");
  const blue = blockFromPreset(presetByName("blue"), "color");
  blue.label = "Blue";
  return [
    fade,
    { id: uid("wait"), type: "wait", label: "Wait 1.0 s", durationMs: 1000 },
    {
      id: uid("loop"), type: "loop", label: "Loop 3x", count: 3, loopDelayMs: 0,
      children: [blue, { id: uid("wait"), type: "wait", label: "Wait 500 ms", durationMs: 500 }]
    },
    { id: uid("off"), type: "off", label: "Off", target: "off", group: 0, repeatCount: 3 }
  ];
}

function persistFlow() {
  try {
    localStorage.setItem(storageKey, JSON.stringify(flowBlocks));
  } catch (_error) {
    // Persistence is a convenience; the controller remains fully usable without it.
  }
}

function loadPersistedFlow() {
  try {
    const savedValue = localStorage.getItem(storageKey) ?? localStorage.getItem(legacyStorageKey);
    const saved = JSON.parse(savedValue);
    if (Array.isArray(saved) && saved.length) {
      localStorage.setItem(storageKey, JSON.stringify(saved));
      return saved;
    }
  } catch (_error) {
    // Invalid local state falls back to the safe example flow.
  }
  return seedFlow();
}

function recordHistory() {
  historyStack.push(deepClone(flowBlocks));
  if (historyStack.length > 30) historyStack.shift();
  elements["undo-button"].disabled = historyStack.length === 0;
}

function commitMutation(mutator) {
  recordHistory();
  mutator();
  persistFlow();
  renderFlowCanvas();
  renderInspector();
  scheduleFlowPreview();
}

function findBlockById(id, blocks = flowBlocks, parentId = "root") {
  for (let index = 0; index < blocks.length; index += 1) {
    const block = blocks[index];
    if (block.id === id) return { block, blocks, index, parentId };
    if (block.type === "loop") {
      const found = findBlockById(id, block.children || [], block.id);
      if (found) return found;
    }
  }
  return null;
}

function childrenFor(parentId) {
  if (parentId === "root") return flowBlocks;
  const parent = findBlockById(parentId);
  if (!parent || parent.block.type !== "loop") return null;
  parent.block.children ||= [];
  return parent.block.children;
}

function blockContains(block, id) {
  if (block.id === id) return true;
  return block.type === "loop" && (block.children || []).some((child) => blockContains(child, id));
}

function countBlocks(blocks = flowBlocks) {
  return blocks.reduce((total, block) => total + 1 + (block.type === "loop" ? countBlocks(block.children || []) : 0), 0);
}

function cloneBlock(block) {
  const copy = deepClone(block);
  function refreshIds(item) {
    item.id = uid(item.type);
    if (item.type === "loop") (item.children || []).forEach(refreshIds);
  }
  refreshIds(copy);
  return copy;
}

function blockIconMarkup(type, block = null) {
  if (type === "color" || type === "fade") {
    const color = block ? rgbHex(block) : null;
    const background = type === "fade" && color
      ? `linear-gradient(135deg, #241B06, ${color})`
      : color;
    const style = background ? ` style=\"background:${background}\"` : "";
    return `<span class=\"block-icon ${type === "fade" ? "fade-icon" : "color-icon"}\"${style}></span>`;
  }
  if (type === "wait") return "<span class=\"block-icon wait-icon\"><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"12\" r=\"8\"></circle><path d=\"M12 7v5l3 2\"></path></svg></span>";
  if (type === "loop") return "<span class=\"block-icon loop-icon\"><svg viewBox=\"0 0 24 24\"><path d=\"M18 8a7 7 0 1 0 1 7\"></path><path d=\"m18 4 .2 4.2L14 8\"></path></svg></span>";
  if (type === "wake") return "<span class=\"block-icon wake-icon\"><span></span></span>";
  return "<span class=\"block-icon off-icon\"></span>";
}

function blockSummary(block) {
  if (block.type === "color" || block.type === "fade") {
    return `${rgbHex(block)} · ${attackTimes[block.attack]} / ${holdTimes[block.hold]} / ${releaseTimes[block.release]} · ${block.repeatCount} RF retries`;
  }
  if (block.type === "wait") return `Delay for ${(block.durationMs / 1000).toFixed(block.durationMs % 1000 ? 2 : 1)} seconds`;
  if (block.type === "loop") return `Repeat ${block.children?.length || 0} nested blocks ${block.count} times`;
  if (block.type === "wake") return `Keepalive transmission for ${block.wakeDurationS} seconds`;
  return `Turn band off · ${block.repeatCount} RF retries`;
}

function actionButtonsMarkup() {
  return `
    <span class="flow-grip" aria-hidden="true">⠿</span>
    <button type="button" class="icon-button duplicate-action" data-action="duplicate" title="Duplicate block" aria-label="Duplicate block">
      <svg viewBox="0 0 24 24"><rect x="8" y="8" width="11" height="11" rx="1"></rect><path d="M16 8V5H5v11h3"></path></svg>
    </button>
    <button type="button" class="icon-button delete" data-action="delete" title="Delete block" aria-label="Delete block">
      <svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13"></path></svg>
    </button>`;
}

function makeDropSlot(parentId, index) {
  const slot = document.createElement("div");
  slot.className = "drop-slot";
  slot.dataset.parentId = parentId;
  slot.dataset.index = String(index);
  slot.addEventListener("dragover", (event) => { event.preventDefault(); slot.classList.add("drag-over"); });
  slot.addEventListener("dragleave", () => slot.classList.remove("drag-over"));
  slot.addEventListener("drop", (event) => {
    event.preventDefault();
    event.stopPropagation();
    slot.classList.remove("drag-over");
    handleDrop(parentId, index);
  });
  return slot;
}

function wireBlockActions(element, block) {
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action === "delete") {
      event.stopPropagation();
      commitMutation(() => {
        const found = findBlockById(block.id);
        if (found) found.blocks.splice(found.index, 1);
        if (selectedBlockId === block.id) selectedBlockId = null;
      });
      return;
    }
    if (action === "duplicate") {
      event.stopPropagation();
      commitMutation(() => {
        const found = findBlockById(block.id);
        if (!found) return;
        const copy = cloneBlock(found.block);
        found.blocks.splice(found.index + 1, 0, copy);
        selectedBlockId = copy.id;
      });
      return;
    }
    selectedBlockId = block.id;
    renderFlowCanvas();
    renderInspector();
  });

  element.draggable = true;
  element.addEventListener("dragstart", (event) => {
    event.stopPropagation();
    draggedItem = { kind: "flow", id: block.id };
    element.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", block.id);
  });
  element.addEventListener("dragend", () => {
    draggedItem = null;
    element.classList.remove("dragging");
    document.querySelectorAll(".drop-slot.drag-over").forEach((slot) => slot.classList.remove("drag-over"));
  });
}

function renderBlock(block) {
  const element = document.createElement("article");
  element.className = `flow-block ${block.type}-block${selectedBlockId === block.id ? " selected" : ""}`;
  element.dataset.blockId = block.id;

  if (block.type === "loop") {
    const header = document.createElement("div");
    header.className = "loop-header";
    header.innerHTML = `${blockIconMarkup("loop")}<span class="flow-block-main"><strong></strong><small></small></span><span class="flow-actions">${actionButtonsMarkup()}</span>`;
    header.querySelector("strong").textContent = block.label;
    header.querySelector("small").textContent = blockSummary(block);
    element.append(header);
    const nested = document.createElement("div");
    nested.className = "flow-list nested-flow-list";
    renderFlowList(nested, block.children || [], block.id);
    element.append(nested);
    wireBlockActions(element, block);
    return element;
  }

  element.innerHTML = `${blockIconMarkup(block.type, block)}<span class="flow-block-main"><strong></strong><small></small></span><span class="flow-actions">${actionButtonsMarkup()}</span>`;
  element.querySelector("strong").textContent = block.label;
  element.querySelector("small").textContent = blockSummary(block);
  wireBlockActions(element, block);
  return element;
}

function renderFlowList(container, blocks, parentId) {
  container.replaceChildren();
  container.append(makeDropSlot(parentId, 0));
  if (!blocks.length && parentId !== "root") {
    const empty = document.createElement("div");
    empty.className = "nested-empty";
    empty.textContent = "Drop blocks inside this loop";
    container.append(empty);
  }
  blocks.forEach((block, index) => {
    container.append(renderBlock(block));
    container.append(makeDropSlot(parentId, index + 1));
  });
}

function renderFlowCanvas() {
  renderFlowList(elements["root-flow-list"], flowBlocks, "root");
  const blockTotal = countBlocks();
  const duration = currentFlowReport ? formatDuration(currentFlowReport.totalDurationUs) : "preview pending";
  elements["flow-summary"].textContent = `${blockTotal} block${blockTotal === 1 ? "" : "s"} · ${duration}`;
  elements["undo-button"].disabled = historyStack.length === 0;
}

function handleDrop(parentId, index) {
  if (!draggedItem) return;
  if (draggedItem.kind === "library") {
    const block = makeBlock(draggedItem.type);
    commitMutation(() => {
      const target = childrenFor(parentId);
      if (!target) return;
      target.splice(Math.min(index, target.length), 0, block);
      selectedBlockId = block.id;
    });
    log(`${block.label} block added`);
    return;
  }

  const moving = findBlockById(draggedItem.id);
  if (!moving) return;
  if (moving.block.id === parentId || blockContains(moving.block, parentId)) {
    log("A loop cannot be dropped inside itself", "warning");
    return;
  }

  commitMutation(() => {
    const sourceParentId = moving.parentId;
    const sourceIndex = moving.index;
    const [block] = moving.blocks.splice(sourceIndex, 1);
    const target = childrenFor(parentId);
    if (!target) {
      moving.blocks.splice(sourceIndex, 0, block);
      return;
    }
    let targetIndex = index;
    if (sourceParentId === parentId && sourceIndex < index) targetIndex -= 1;
    target.splice(Math.max(0, Math.min(targetIndex, target.length)), 0, block);
    selectedBlockId = block.id;
  });
}

function descriptionFor(type) {
  return {
    color: "Send a color/effect command. RF retries repeat the radio packet for reliability.",
    fade: "Send a timed fade command using the selected attack, hold, and release values.",
    wait: "Pause the software flow before the next block. No RF is emitted during a wait.",
    loop: "Repeat every nested block in order. The optional loop delay is added between iterations.",
    wake: "Repeat the known keepalive frame to wake a sleeping receiver.",
    off: "Send the known off payload using a small number of RF retries."
  }[type];
}

function presetOptions(block) {
  const allowed = presets.filter((preset) => !["wake", "off"].includes(preset.name));
  const options = ["<option value=\"custom\">Custom</option>"];
  allowed.forEach((preset) => {
    options.push(`<option value="${preset.name}"${block.target === preset.name ? " selected" : ""}>${preset.label}</option>`);
  });
  return options.join("");
}

function inspectorIntro(block) {
  return `<section class="inspector-intro"><div class="inspector-title">${blockIconMarkup(block.type, block)}<h3>${block.label}</h3></div><p>${descriptionFor(block.type)}</p></section>`;
}

function commonNameField(block) {
  return `<label class="form-row"><span>Block name</span><input type="text" maxlength="80" value="${escapeAttribute(block.label)}" data-field="label"></label>`;
}

function escapeAttribute(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function colorInspector(block) {
  const persistentDisabled = !serverConfig.persistentAllowed ? " disabled" : "";
  return `${inspectorIntro(block)}
    <section class="inspector-section"><h4>Color source</h4>
      ${commonNameField(block)}
      <label class="form-row"><span>Preset</span><select data-role="preset">${presetOptions(block)}</select></label>
      <label class="form-row"><span>Color</span><span class="color-input-row"><input type="color" value="${rgbHex(block)}" data-role="color-picker"><input type="text" maxlength="7" value="${rgbHex(block)}" data-role="hex"></span></label>
      <label class="inspector-slider red"><span>Red</span><input type="range" min="0" max="255" value="${block.red}" data-field="red"><output data-output-for="red">${block.red}</output></label>
      <label class="inspector-slider green"><span>Green</span><input type="range" min="0" max="255" value="${block.green}" data-field="green"><output data-output-for="green">${block.green}</output></label>
      <label class="inspector-slider blue"><span>Blue</span><input type="range" min="0" max="255" value="${block.blue}" data-field="blue"><output data-output-for="blue">${block.blue}</output></label>
    </section>
    <section class="inspector-section"><h4>Effect timing</h4>
      <label class="form-row"><span>Mode</span><select data-field="mode"><option value="continuous"${block.mode === "continuous" ? " selected" : ""}>Continuous</option><option value="one-shot"${block.mode === "one-shot" ? " selected" : ""}>One shot</option><option value="forever"${block.mode === "forever" ? " selected" : ""}${persistentDisabled}>Forever</option></select></label>
      <label class="inspector-slider"><span>Attack</span><input type="range" min="0" max="7" value="${block.attack}" data-field="attack"><output data-output-for="attack">${attackTimes[block.attack]}</output></label>
      <label class="inspector-slider"><span>Hold</span><input type="range" min="0" max="7" value="${block.hold}" data-field="hold"><output data-output-for="hold">${holdTimes[block.hold]}</output></label>
      <label class="inspector-slider"><span>Release</span><input type="range" min="0" max="7" value="${block.release}" data-field="release"><output data-output-for="release">${releaseTimes[block.release]}</output></label>
      <label class="inspector-slider"><span>Random</span><input type="range" min="0" max="7" value="${block.random}" data-field="random"><output data-output-for="random">${randomPercent[block.random]}</output></label>
      <div class="field-pair">
        <label class="form-stack"><span>Group</span><input type="number" min="0" max="31" value="${block.group}" data-field="group"></label>
        <label class="form-stack"><span>RF retries</span><input type="number" min="1" max="100" value="${block.repeatCount}" data-field="repeatCount"></label>
      </div>
    </section>`;
}

function renderInspector() {
  const found = selectedBlockId ? findBlockById(selectedBlockId) : null;
  if (!found) {
    elements["selected-kind"].textContent = "No selection";
    elements["inspector-content"].innerHTML = "<div class=\"empty-inspector\"><svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M4 4h6v6H4zM14 14h6v6h-6zM7 10v4h7\"></path></svg><strong>Select a block</strong><p>Its color, timing, loop, and RF retry settings will appear here.</p></div>";
    return;
  }

  const block = found.block;
  elements["selected-kind"].textContent = block.type;
  if (block.type === "color" || block.type === "fade") {
    elements["inspector-content"].innerHTML = colorInspector(block);
    return;
  }
  if (block.type === "wait") {
    elements["inspector-content"].innerHTML = `${inspectorIntro(block)}<section class="inspector-section"><h4>Wait settings</h4>${commonNameField(block)}<label class="form-row"><span>Duration</span><span class="inline-unit"><input type="number" min="0" max="60000" step="50" value="${block.durationMs}" data-field="durationMs"><span>ms</span></span></label></section>`;
    return;
  }
  if (block.type === "loop") {
    const childCount = countBlocks(block.children || []);
    elements["inspector-content"].innerHTML = `${inspectorIntro(block)}<section class="inspector-section"><h4>Loop settings</h4>${commonNameField(block)}<label class="form-row"><span>Loop count</span><input type="number" min="1" max="100" value="${block.count}" data-field="count"></label><label class="form-row"><span>Loop delay</span><span class="inline-unit"><input type="number" min="0" max="60000" step="50" value="${block.loopDelayMs}" data-field="loopDelayMs"><span>ms</span></span></label></section><section class="inspector-section"><h4>Nested content</h4><div class="nested-summary"><strong>${childCount} block${childCount === 1 ? "" : "s"}</strong>Drag blocks into the outlined loop on the canvas. They run in order on every iteration.</div></section>`;
    return;
  }
  if (block.type === "wake") {
    elements["inspector-content"].innerHTML = `${inspectorIntro(block)}<section class="inspector-section"><h4>Wake settings</h4>${commonNameField(block)}<label class="form-row"><span>Duration</span><span class="inline-unit"><input type="number" min="1" max="60" step="1" value="${block.wakeDurationS}" data-field="wakeDurationS"><span>sec</span></span></label><label class="form-row"><span>Group</span><input type="number" min="0" max="31" value="${block.group}" data-field="group"></label></section>`;
    return;
  }
  elements["inspector-content"].innerHTML = `${inspectorIntro(block)}<section class="inspector-section"><h4>Off settings</h4>${commonNameField(block)}<div class="field-pair"><label class="form-stack"><span>Group</span><input type="number" min="0" max="31" value="${block.group}" data-field="group"></label><label class="form-stack"><span>RF retries</span><input type="number" min="1" max="100" value="${block.repeatCount}" data-field="repeatCount"></label></div></section>`;
}

function refreshInspectorOutputs(block) {
  const labels = {
    attack: attackTimes[block.attack], hold: holdTimes[block.hold],
    release: releaseTimes[block.release], random: randomPercent[block.random],
    red: block.red, green: block.green, blue: block.blue
  };
  Object.entries(labels).forEach(([field, label]) => {
    const output = elements["inspector-content"].querySelector(`[data-output-for="${field}"]`);
    if (output) output.textContent = label;
  });
  const hex = rgbHex(block);
  const colorPicker = elements["inspector-content"].querySelector("[data-role=color-picker]");
  const hexInput = elements["inspector-content"].querySelector("[data-role=hex]");
  if (colorPicker) colorPicker.value = hex;
  if (hexInput && document.activeElement !== hexInput) hexInput.value = hex;
}

function updateSelectedBlockFromInput(event) {
  const found = selectedBlockId ? findBlockById(selectedBlockId) : null;
  if (!found) return;
  const block = found.block;
  const control = event.target;
  const field = control.dataset.field;
  if (field) {
    const numeric = control.type === "range" || control.type === "number";
    block[field] = numeric ? Number(control.value) : control.value;
    if (["red", "green", "blue"].includes(field)) block.target = "custom";
    if (field === "count" && !block.label.match(/custom/i)) block.label = `Loop ${block.count}x`;
    if (field === "durationMs" && !block.label.match(/custom/i)) block.label = `Wait ${block.durationMs >= 1000 ? `${(block.durationMs / 1000).toFixed(1)} s` : `${block.durationMs} ms`}`;
  }

  if (control.dataset.role === "hex") {
    const normalized = control.value.trim().replace(/^#/, "");
    if (/^[0-9a-fA-F]{6}$/.test(normalized)) {
      block.red = parseInt(normalized.slice(0, 2), 16);
      block.green = parseInt(normalized.slice(2, 4), 16);
      block.blue = parseInt(normalized.slice(4, 6), 16);
      block.target = "custom";
    }
  }

  if (control.dataset.role === "color-picker") {
    const normalized = control.value.slice(1);
    block.red = parseInt(normalized.slice(0, 2), 16);
    block.green = parseInt(normalized.slice(2, 4), 16);
    block.blue = parseInt(normalized.slice(4, 6), 16);
    block.target = "custom";
  }

  refreshInspectorOutputs(block);
  persistFlow();
  renderFlowCanvas();
  scheduleFlowPreview();
}

function applyPresetToSelected(name) {
  const found = selectedBlockId ? findBlockById(selectedBlockId) : null;
  if (!found || !["color", "fade"].includes(found.block.type)) return;
  if (name === "custom") {
    found.block.target = "custom";
    return;
  }
  const preset = presetByName(name);
  if (!preset) return;
  const block = found.block;
  Object.assign(block, {
    label: preset.label, target: preset.name, red: preset.red, green: preset.green,
    blue: preset.blue, attack: preset.attack, hold: preset.hold, release: preset.release,
    random: preset.random, group: preset.group, mode: preset.mode
  });
  persistFlow();
  renderFlowCanvas();
  renderInspector();
  scheduleFlowPreview();
}

function scheduleFlowPreview() {
  currentFlowReport = null;
  updateRunState();
  window.clearTimeout(previewTimer);
  previewTimer = window.setTimeout(() => previewFlow(), 180);
}

function clearTimeline() {
  elements["timeline-segments"].replaceChildren();
}

function renderTimeline(report) {
  clearTimeline();
  const total = Math.max(1, report.totalDurationUs);
  const namespace = "http://www.w3.org/2000/svg";
  report.timeline.forEach((step) => {
    const rect = document.createElementNS(namespace, "rect");
    const x = (step.startUs / total) * 1000;
    const width = Math.max(3, (step.durationUs / total) * 1000);
    rect.setAttribute("x", String(x));
    rect.setAttribute("width", String(Math.min(width, 1000 - x)));
    rect.setAttribute("rx", "2");
    rect.classList.add("timeline-segment");
    if (step.kind === "wait") {
      rect.setAttribute("y", "72");
      rect.setAttribute("height", "15");
      rect.classList.add("timeline-wait");
    } else {
      rect.setAttribute("y", "22");
      rect.setAttribute("height", "58");
      rect.setAttribute("fill", step.color || "#9b7cff");
    }
    const title = document.createElementNS(namespace, "title");
    title.textContent = `${step.label}: ${formatDuration(step.durationUs)}`;
    rect.append(title);
    elements["timeline-segments"].append(rect);
  });
}

async function previewFlow({ announce = false } = {}) {
  const requestId = ++previewRequestId;
  elements["ready-state"].textContent = "Planning flow";
  try {
    const report = await api("/api/flow/preview", { blocks: flowBlocks });
    if (requestId !== previewRequestId) return null;
    currentFlowReport = report;
    renderTimeline(report);
    elements["preview-summary"].textContent = `${report.transmissionCount} transmissions · ${formatDuration(report.totalDurationUs)} total`;
    elements["flow-summary"].textContent = `${report.blockCount} blocks · ${formatDuration(report.totalDurationUs)}`;
    elements["ready-state"].textContent = "Ready";
    if (announce) {
      log(`Flow preview: ${report.expandedStepCount} actions, ${formatDuration(report.totalDurationUs)}`);
      log("Passive preview complete — no RF emitted", "success");
    }
    updateRunState();
    return report;
  } catch (error) {
    if (requestId !== previewRequestId) return null;
    currentFlowReport = null;
    clearTimeline();
    elements["preview-summary"].textContent = error.message;
    elements["flow-summary"].textContent = `${countBlocks()} blocks · preview needs attention`;
    elements["ready-state"].textContent = "Flow needs attention";
    if (announce) log(error.message, "error");
    updateRunState();
    return null;
  }
}

function updateRunState() {
  const canTransmit = Boolean(serverConfig?.transmitAllowed && serverConfig?.hackrfTransferFound);
  const armed = canTransmit && elements["arm-toggle"].checked;
  const musicTransmitting = Boolean(musicStatus?.transmit);
  elements["run-flow-button"].disabled = flowRunning || musicTransmitting || !armed || !currentFlowReport;
  elements["run-help"].textContent = flowRunning
    ? "Flow is running."
    : musicTransmitting
      ? "Music sync is using the HackRF."
    : !canTransmit
      ? "Transmission disabled by server."
      : !armed
        ? "Arm RF transmit to enable."
        : !currentFlowReport
          ? "Fix the flow before running."
          : `${currentFlowReport.transmissionCount} RF transmissions ready at ${serverConfig.txGainDb} dB.`;
}

async function runFlow() {
  if (!elements["arm-toggle"].checked || flowRunning) return;
  const report = await previewFlow();
  if (!report) return;
  flowRunning = true;
  elements["run-flow-button"].disabled = true;
  elements["arm-toggle"].disabled = true;
  elements["music-arm-toggle"].disabled = true;
  elements["tx-gain-slider"].disabled = true;
  elements["music-tx-gain-slider"].disabled = true;
  elements["ready-state"].textContent = "Running flow";
  elements["safety-state"].textContent = "RF flow transmission in progress";
  log(`Running flow at ${serverConfig.txGainDb} dB: ${report.transmissionCount} transmissions over ${formatDuration(report.totalDurationUs)}`, "warning");
  try {
    const result = await api("/api/flow/transmit", { blocks: flowBlocks, txGainDb: serverConfig.txGainDb, armed: true, confirmation: "TRANSMIT" });
    log(`Flow complete: ${result.transmissionCount} transmissions at ${result.txGainDb} dB`, "success");
    elements["ready-state"].textContent = "Ready";
  } catch (error) {
    log(error.message, "error");
    elements["ready-state"].textContent = "Flow error";
  } finally {
    flowRunning = false;
    const canTransmit = Boolean(serverConfig.transmitAllowed && serverConfig.hackrfTransferFound);
    elements["arm-toggle"].disabled = !canTransmit;
    elements["music-arm-toggle"].disabled = !canTransmit;
    elements["tx-gain-slider"].disabled = !canTransmit;
    elements["music-tx-gain-slider"].disabled = !canTransmit;
    elements["safety-state"].textContent = elements["arm-toggle"].checked
      ? "RF armed — verify the wristband is nearby"
      : "RF locked — no transmission armed";
    updateRunState();
  }
}

function bindEvents() {
  document.querySelectorAll(".library-block").forEach((button) => {
    button.addEventListener("dragstart", (event) => {
      draggedItem = { kind: "library", type: button.dataset.blockType };
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("text/plain", button.dataset.blockType);
    });
    button.addEventListener("dragend", () => { draggedItem = null; });
    button.addEventListener("click", () => {
      const block = makeBlock(button.dataset.blockType);
      commitMutation(() => { flowBlocks.push(block); selectedBlockId = block.id; });
      log(`${block.label} block added`);
    });
  });

  elements["block-search"].addEventListener("input", (event) => {
    const query = event.target.value.trim().toLowerCase();
    document.querySelectorAll(".library-block").forEach((button) => {
      button.hidden = Boolean(query && !button.innerText.toLowerCase().includes(query));
    });
  });

  elements["undo-button"].addEventListener("click", () => {
    const prior = historyStack.pop();
    if (!prior) return;
    flowBlocks = prior;
    if (selectedBlockId && !findBlockById(selectedBlockId)) selectedBlockId = null;
    persistFlow();
    renderFlowCanvas();
    renderInspector();
    scheduleFlowPreview();
    elements["undo-button"].disabled = historyStack.length === 0;
    log("Last flow edit undone");
  });

  elements["clear-flow-button"].addEventListener("click", () => {
    if (!flowBlocks.length) return;
    commitMutation(() => { flowBlocks = []; selectedBlockId = null; currentFlowReport = null; });
    log("Flow cleared — Undo is available", "warning");
  });

  elements["inspector-content"].addEventListener("focusin", (event) => {
    if (event.target.matches("input, select") && event.target.dataset.historyCaptured !== "true") {
      recordHistory();
      event.target.dataset.historyCaptured = "true";
    }
  });
  elements["inspector-content"].addEventListener("input", updateSelectedBlockFromInput);
  elements["inspector-content"].addEventListener("change", (event) => {
    if (event.target.dataset.role === "preset") applyPresetToSelected(event.target.value);
    else updateSelectedBlockFromInput(event);
  });

  elements["clear-log"].addEventListener("click", () => elements["log-output"].replaceChildren());
  elements["preview-flow-button"].addEventListener("click", () => previewFlow({ announce: true }));
  elements["tx-gain-slider"].addEventListener("input", () => {
    setTxGain(elements["tx-gain-slider"].value);
  });
  elements["tx-gain-slider"].addEventListener("change", () => {
    log(`TX gain set to ${serverConfig.txGainDb} dB`, serverConfig.txGainDb > 20 ? "warning" : "info");
  });
  elements["arm-toggle"].addEventListener("change", () => {
    setArmState(elements["arm-toggle"].checked);
  });
  elements["run-flow-button"].addEventListener("click", runFlow);

  elements["mode-flow-button"].addEventListener("click", () => setStudioMode("flow"));
  elements["mode-music-button"].addEventListener("click", () => setStudioMode("music"));
  elements["music-device"].addEventListener("change", updateSelectedMusicDevice);
  elements["music-monitor-button"].addEventListener("click", togglePassiveMonitor);
  elements["music-start-button"].addEventListener("click", toggleMusicSync);
  elements["music-clear-log"].addEventListener("click", () => elements["music-log-output"].replaceChildren());
  elements["music-sensitivity"].addEventListener("input", () => {
    elements["music-sensitivity-output"].textContent = `${elements["music-sensitivity"].value}%`;
  });
  elements["music-brightness"].addEventListener("input", () => {
    elements["music-brightness-output"].textContent = `${elements["music-brightness"].value}%`;
  });
  elements["music-interval"].addEventListener("input", () => {
    elements["music-interval-output"].textContent = `${elements["music-interval"].value} ms`;
  });
  elements["music-tx-gain-slider"].addEventListener("input", () => {
    setTxGain(elements["music-tx-gain-slider"].value);
  });
  elements["music-tx-gain-slider"].addEventListener("change", () => {
    musicLog(`TX gain set to ${serverConfig.txGainDb} dB`, serverConfig.txGainDb > 20 ? "warning" : "info");
  });
  elements["music-arm-toggle"].addEventListener("change", () => {
    setArmState(elements["music-arm-toggle"].checked);
  });
}

async function initialize() {
  cacheElements();
  bindEvents();
  try {
    const startup = await api("/api/config");
    serverConfig = startup.config;
    csrfToken = startup.csrfToken;
    presets = startup.presets;
    elements["frequency-label"].textContent = formatFrequency(serverConfig.frequencyHz);
    setTxGain(serverConfig.txGainDb);
    elements["hardware-dot"].classList.toggle("offline", !serverConfig.hackrfTransferFound);
    elements["hardware-name"].textContent = serverConfig.hackrfTransferFound ? "HackRF tools ready" : "HackRF tool missing";
    const canTransmit = serverConfig.transmitAllowed && serverConfig.hackrfTransferFound;
    elements["arm-toggle"].disabled = !canTransmit;
    elements["tx-gain-slider"].disabled = !canTransmit;
    elements["music-arm-toggle"].disabled = !canTransmit;
    elements["music-tx-gain-slider"].disabled = !canTransmit;
    elements["arm-help"].textContent = canTransmit
      ? "Enable once; switch off when finished."
      : serverConfig.transmitAllowed ? "hackrf_transfer not found" : "Restart with --allow-transmit";
    elements["safety-state"].textContent = serverConfig.transmitAllowed
      ? "RF locked — no transmission armed"
      : "Dry run — no RF emitted";

    flowBlocks = loadPersistedFlow();
    selectedBlockId = flowBlocks.find((block) => block.type === "loop")?.id || flowBlocks[0]?.id || null;
    renderFlowCanvas();
    renderInspector();
    log(`Flow Builder ready at ${formatFrequency(serverConfig.frequencyHz)}`, "success");
    log(serverConfig.transmitAllowed ? "RF available but locked" : "Preview-only server — no RF emitted", "success");
    log("Example Fade Gold loop loaded", "success");
    musicLog(`Music Mode ready at ${formatFrequency(serverConfig.frequencyHz)}`, "success");
    musicLog("Start Passive Monitor to verify audio before arming RF", "success");
    await previewFlow();
  } catch (error) {
    elements["ready-state"].textContent = "Connection error";
    log(error.message, "error");
  }
}

window.addEventListener("DOMContentLoaded", initialize);
