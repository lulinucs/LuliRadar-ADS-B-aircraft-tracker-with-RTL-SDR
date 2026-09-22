"use strict";

/* ============================================================
 * Estado global e helpers de API
 *
 * IMPORTANTE: `state.tab` (aba visualizada) e `state.activeMode` (modo
 * REALMENTE ativo no SDR, vindo do backend) são coisas DIFERENTES e
 * propositalmente independentes. Navegar entre abas (setTab) NUNCA chama
 * /api/mode/* nem /api/stop - só troca o que é exibido. Só as ações
 * explícitas (Iniciar/Parar/Assumir SDR) tocam o hardware.
 *
 * O mesmo vale para `state.adsbSubview` (AO VIVO / HISTÓRICO): é navegação
 * PURAMENTE visual dentro do modo ADS-B, nunca chama rotas de troca de modo.
 * ============================================================ */
const state = {
  tab: "adsb",
  activeMode: "idle", // idle | adsb | radio | switching | error
  adsbSubview: "live", // live | history
  sort: "distance",
  selectedIcao: null,
  selectedSessionId: null,
  map: null,
  historyMap: null,
  markers: {},          // icao -> {marker, trail}
  historyLayer: null,
  historySessions: [],  // último resultado de /api/history/flights
  receiver: { lat: null, lon: null, configured: false },
  presets: [],
  timers: {},
};

async function apiGet(url) {
  const r = await fetch(url);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function apiPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function showToast(message, type = "error") {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

function fmt(value, digits = 0, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(digits) + suffix;
}

// Formatação ÚNICA de frequência em MHz usada em toda a interface - sempre
// 3 casas decimais sobre um valor que já chega em MHz do backend (nunca
// multiplica/divide aqui; a única conversão para Hz acontece no backend,
// dentro de RadioService, ao montar o comando do rtl_fm).
function formatFrequencyMhz(mhz) {
  if (mhz === null || mhz === undefined || Number.isNaN(Number(mhz))) return "---.---";
  return Number(mhz).toFixed(3);
}

// Identificador principal de uma aeronave: usa o callsign quando existe;
// cai para o ICAO quando não há callsign, em vez de mostrar "-" com um
// dado (o ICAO) disponível sendo descartado.
function primaryId(ac) {
  return ac.callsign || ac.icao;
}

/* ============================================================
 * Ícones (SVG inline, sem CDN/framework) - usados nos poucos lugares
 * montados dinamicamente via JS. O resto dos ícones (estáticos: header,
 * botões, KPIs, histórico) já está direto no template.
 * ============================================================ */
const STATE_ICON_PATHS = {
  trendingUp: '<path d="M3 17 9 11 13 15 21 7"/><polyline points="15 7 21 7 21 13"/>',
  trendingDown: '<path d="M3 7 9 13 13 9 21 17"/><polyline points="21 11 21 17 15 17"/>',
  minus: '<line x1="5" y1="12" x2="19" y2="12"/>',
  alertTriangle: '<path d="M12 3 22 20 2 20Z"/><line x1="12" y1="9" x2="12" y2="14"/><circle cx="12" cy="17" r="1" fill="currentColor" stroke="none"/>',
  signalOff: '<circle cx="12" cy="12" r="9"/><line x1="8" y1="8" x2="16" y2="16"/><line x1="16" y1="8" x2="8" y2="16"/>',
};

function svgIcon(inner) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
}

function stateIconFor(code) {
  if (code === "PERDIDO") return svgIcon(STATE_ICON_PATHS.signalOff);
  if (code === "SUBINDO") return svgIcon(STATE_ICON_PATHS.trendingUp);
  if (code === "DESCENDO") return svgIcon(STATE_ICON_PATHS.trendingDown);
  if (code === "BAIXA_ALTITUDE") return svgIcon(STATE_ICON_PATHS.alertTriangle);
  return svgIcon(STATE_ICON_PATHS.minus);
}

/* ============================================================
 * Cabeçalho / status geral - reflete o HARDWARE, nunca a aba visualizada
 * ============================================================ */
function dotClassFor(sdrState) {
  if (sdrState === "adsb" || sdrState === "radio") return "online";
  if (sdrState === "switching") return "switching";
  if (sdrState === "error") return "error";
  return "offline";
}

function labelFor(status) {
  switch (status.sdr_state) {
    case "adsb": return "SDR ONLINE (ADS-B)";
    case "radio": return "SDR ONLINE (RÁDIO)";
    case "switching": return "TROCANDO MODO...";
    case "error": return "ERRO" + (status.error ? ": " + status.error : "");
    default: return "SDR OFFLINE / IDLE";
  }
}

function setBtnLabel(btn, text) {
  const label = btn.querySelector(".btn-label");
  if (label) label.textContent = text;
  else btn.textContent = text; // fallback pra botões sem ícone (ex.: Sintonizar)
}

function applyStatus(status) {
  state.receiver = status.receiver || state.receiver;
  state.activeMode = status.sdr_state;

  document.getElementById("sdr-dot").className = "dot " + dotClassFor(status.sdr_state);
  document.getElementById("sdr-status-text").textContent = labelFor(status);

  document.getElementById("btn-mode-adsb").classList.toggle("active", state.tab === "adsb");
  document.getElementById("btn-mode-radio").classList.toggle("active", state.tab === "radio");

  const warn = document.getElementById("warning-banner");
  if (!status.receiver.configured) {
    warn.hidden = false;
    warn.textContent = "⚠ Posição do receptor não configurada (.env: RECEIVER_LAT/RECEIVER_LON) — distâncias e trilha ficam sem referência.";
  } else {
    warn.hidden = true;
  }

  if (status.adsb.error) showToastOnce("adsb-error", status.adsb.error);
  if (status.radio.error) showToastOnce("radio-error", status.radio.error);

  // KPIs ADS-B
  document.getElementById("m-sdr").textContent = status.sdr_state !== "idle" ? "conectado" : "ocioso";
  document.getElementById("m-dump1090").textContent = status.adsb.running ? "rodando" : "parado";
  document.getElementById("m-aircraft-count").textContent = status.adsb.aircraft_count ?? 0;
  document.getElementById("m-msg-rate").textContent = fmt(status.adsb.messages_per_second, 1);
  document.getElementById("m-msg-total").textContent = status.adsb.messages_total ?? 0;
  document.getElementById("m-last-update").textContent = status.adsb.last_update
    ? new Date(status.adsb.last_update * 1000).toLocaleTimeString()
    : "-";

  renderModeBanners();
}

const _toastSeen = {};
function showToastOnce(key, message) {
  if (_toastSeen[key] === message) return;
  _toastSeen[key] = message;
  showToast(message);
}

async function refreshStatus() {
  try {
    const status = await apiGet("/api/status");
    applyStatus(status);
  } catch (e) {
    /* silencioso: já mostramos erro específico de request de ação */
  }
}

/* ============================================================
 * Banners + habilitação de botões de acordo com o modo REALMENTE ativo
 * ============================================================ */
function renderModeBanners() {
  const mode = state.activeMode;
  const switching = mode === "switching";

  const adsbBanner = document.getElementById("adsb-sdr-banner");
  const btnAdsbStart = document.getElementById("btn-adsb-start");
  const btnAdsbStop = document.getElementById("btn-adsb-stop");

  if (mode === "radio") {
    adsbBanner.hidden = false;
    adsbBanner.textContent = "SDR atualmente em uso pelo RÁDIO.";
    setBtnLabel(btnAdsbStart, "ASSUMIR SDR / INICIAR ADS-B");
  } else if (mode === "error") {
    adsbBanner.hidden = false;
    adsbBanner.textContent = "SDR em erro - veja o indicador no topo. Você pode tentar iniciar novamente.";
    setBtnLabel(btnAdsbStart, "Iniciar ADS-B");
  } else {
    adsbBanner.hidden = true;
    setBtnLabel(btnAdsbStart, "Iniciar ADS-B");
  }
  btnAdsbStop.disabled = switching || mode !== "adsb";
  btnAdsbStart.disabled = switching || mode === "adsb";

  const radioBanner = document.getElementById("radio-sdr-banner");
  const btnRadioStart = document.getElementById("btn-radio-start");
  const btnRadioStop = document.getElementById("btn-radio-stop");
  const btnRadioTune = document.getElementById("btn-radio-tune");

  if (mode === "adsb") {
    radioBanner.hidden = false;
    radioBanner.textContent = "SDR atualmente em uso pelo ADS-B.";
    setBtnLabel(btnRadioStart, "ASSUMIR SDR / INICIAR RÁDIO");
  } else if (mode === "error") {
    radioBanner.hidden = false;
    radioBanner.textContent = "SDR em erro - veja o indicador no topo. Você pode tentar iniciar novamente.";
    setBtnLabel(btnRadioStart, "Iniciar Rádio");
  } else {
    radioBanner.hidden = true;
    setBtnLabel(btnRadioStart, mode === "radio" ? "Reiniciar rádio" : "Iniciar Rádio");
  }
  btnRadioStop.disabled = switching || mode !== "radio";
  btnRadioTune.disabled = switching || mode !== "radio";
  btnRadioStart.disabled = switching;
}

function setOptimisticSwitching() {
  document.getElementById("sdr-status-text").textContent = "TROCANDO MODO...";
  document.getElementById("sdr-dot").className = "dot switching";
  state.activeMode = "switching";
  renderModeBanners();
}

/* ============================================================
 * Navegação entre abas (ADS-B / RÁDIO) - NUNCA toca em subprocessos
 * ============================================================ */
function setTab(tab) {
  state.tab = tab;
  document.getElementById("view-adsb").hidden = tab !== "adsb";
  document.getElementById("view-radio").hidden = tab !== "radio";
  document.getElementById("btn-mode-adsb").classList.toggle("active", tab === "adsb");
  document.getElementById("btn-mode-radio").classList.toggle("active", tab === "radio");

  clearInterval(state.timers.adsb);
  clearInterval(state.timers.radio);
  clearInterval(state.timers.recordings);

  if (tab === "adsb") {
    if (state.map) setTimeout(() => state.map.invalidateSize(), 0);
    if (state.adsbSubview === "history" && state.historyMap) {
      setTimeout(() => state.historyMap.invalidateSize(), 0);
    }
    startAdsbPolling();
  } else {
    startRadioPolling();
    loadRecordings();
  }
}

/* ============================================================
 * Navegação AO VIVO / HISTÓRICO - INTERNA ao modo ADS-B, puramente visual.
 * Não chama /api/mode/*, /api/stop, nem mexe no polling do ao-vivo (que
 * continua rodando em segundo plano mesmo com o histórico visível).
 * ============================================================ */
function setAdsbSubview(view) {
  state.adsbSubview = view;
  document.getElementById("adsb-live").hidden = view !== "live";
  document.getElementById("adsb-history").hidden = view !== "history";
  document.getElementById("btn-sub-live").classList.toggle("active", view === "live");
  document.getElementById("btn-sub-history").classList.toggle("active", view === "history");

  if (view === "live") {
    if (state.map) setTimeout(() => state.map.invalidateSize(), 0);
  } else {
    if (state.historyMap) setTimeout(() => state.historyMap.invalidateSize(), 0);
    loadHistory();
  }
}

/* ============================================================
 * Ações que REALMENTE trocam o modo do SDR (só disparadas por clique
 * explícito em Iniciar/Parar/Assumir - nunca pela navegação de abas)
 * ============================================================ */
async function startAdsb() {
  setOptimisticSwitching();
  try {
    const status = await apiPost("/api/mode/adsb");
    applyStatus(status);
    showToast("ADS-B ativo", "info");
  } catch (e) {
    showToast("Falha ao ativar ADS-B: " + e.message);
  } finally {
    refreshStatus();
  }
}

async function stopAdsb() {
  setOptimisticSwitching();
  try {
    const status = await apiPost("/api/stop");
    applyStatus(status);
    showToast("SDR liberado (IDLE)", "info");
  } catch (e) {
    showToast("Falha ao parar ADS-B: " + e.message);
  } finally {
    refreshStatus();
  }
}

/* ============================================================
 * ADS-B: mapa
 * ============================================================ */
function initMap() {
  const center = state.receiver.configured ? [state.receiver.lat, state.receiver.lon] : [0, 0];
  state.map = L.map("map", { zoomControl: true }).setView(center, state.receiver.configured ? 10 : 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 18,
  }).addTo(state.map);

  if (state.receiver.configured) {
    L.circleMarker(center, {
      radius: 6, color: "#4fb0ff", fillColor: "#4fb0ff", fillOpacity: 0.9,
    }).addTo(state.map).bindTooltip(state.receiver.label || "Receptor");
  }
}

function colorForState(s) {
  const code = (s && s.vertical) || "";
  if (code === "SUBINDO") return "#4fb0ff";
  if (code === "DESCENDO") return "#f5b942";
  if (code === "BAIXA_ALTITUDE") return "#ff5c5c";
  if (code === "PERDIDO" || code === "DESCONHECIDO") return "#7d8fa3";
  return "#33e07a";
}

function aircraftIcon(track, color) {
  return L.divIcon({
    className: "aircraft-icon",
    html: `<div class="ac-triangle" style="transform:rotate(${track || 0}deg);border-bottom-color:${color}"></div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}

function updateMarkers(list) {
  const seen = new Set();
  for (const ac of list) {
    if (ac.lat == null || ac.lon == null) continue;
    seen.add(ac.icao);
    const color = colorForState(ac.state);
    const icon = aircraftIcon(ac.track ?? ac.heading ?? 0, color);
    let entry = state.markers[ac.icao];
    if (!entry) {
      const marker = L.marker([ac.lat, ac.lon], { icon }).addTo(state.map);
      marker.on("click", () => selectAircraft(ac.icao));
      entry = { marker, trail: null };
      state.markers[ac.icao] = entry;
    } else {
      entry.marker.setLatLng([ac.lat, ac.lon]);
      entry.marker.setIcon(icon);
    }
    entry.marker.unbindTooltip();
    entry.marker.bindTooltip(
      `${primaryId(ac)} · FL${Math.round((ac.altitude_baro || 0) / 100)} · ${Math.round(ac.ground_speed || 0)}kt`,
      { className: "ac-tooltip" }
    );
  }
  for (const icao of Object.keys(state.markers)) {
    if (!seen.has(icao)) {
      state.map.removeLayer(state.markers[icao].marker);
      if (state.markers[icao].trail) state.map.removeLayer(state.markers[icao].trail);
      delete state.markers[icao];
    }
  }
}

/* ============================================================
 * ADS-B: lista de aeronaves (CSS grid, não <table> - evita scroll
 * horizontal; callsign+ICAO empilhados na primeira coluna)
 * ============================================================ */
function renderAircraftList(list) {
  const body = document.getElementById("aircraft-tbody");
  body.innerHTML = "";
  if (!list.length) {
    body.innerHTML = '<div class="ac-empty">Nenhuma aeronave no alcance no momento.</div>';
  }
  for (const ac of list) {
    const row = document.createElement("div");
    row.className = "ac-row" + (ac.icao === state.selectedIcao ? " selected" : "");
    row.dataset.icao = ac.icao;
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    const secondaryId = ac.callsign ? ac.icao : "";
    row.innerHTML = `
      <div class="ac-id">
        <span class="ac-callsign">${primaryId(ac)}</span>
        ${secondaryId ? `<span class="ac-icao">${secondaryId}</span>` : ""}
      </div>
      <div class="ac-num">${fmt(ac.altitude_baro)}</div>
      <div class="ac-num">${fmt(ac.ground_speed)}</div>
      <div class="ac-num">${fmt(ac.track ?? ac.heading)}</div>
      <div class="ac-num">${fmt(ac.vertical_rate)}</div>
      <div class="ac-num">${fmt(ac.distance_nm, 1)}</div>
    `;
    row.addEventListener("click", () => selectAircraft(ac.icao));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectAircraft(ac.icao); }
    });
    body.appendChild(row);
  }
  document.querySelectorAll("#aircraft-list .ac-header [data-sort]").forEach((el) => {
    el.classList.toggle("sorted", el.dataset.sort === state.sort);
  });
}

async function tickAdsb() {
  try {
    const list = await apiGet(`/api/aircraft?sort=${state.sort}`);
    renderAircraftList(list);
    updateMarkers(list);
    if (state.selectedIcao && !list.some((a) => a.icao === state.selectedIcao)) {
      hideDetail();
    } else if (state.selectedIcao) {
      const current = list.find((a) => a.icao === state.selectedIcao);
      if (current) renderDetailBasic(current);
    }
  } catch (e) {
    /* dump1090 pode não estar rodando ainda - silencioso, status cuida do aviso */
  }
}

function startAdsbPolling() {
  clearInterval(state.timers.adsb);
  clearInterval(state.timers.status);
  tickAdsb();
  refreshStatus();
  state.timers.adsb = setInterval(tickAdsb, 1000);
  state.timers.status = setInterval(refreshStatus, 2000);
}

/* ============================================================
 * ADS-B: painel de detalhe + trilha
 * ============================================================ */
function renderDetailBasic(ac) {
  document.getElementById("d-callsign").textContent = primaryId(ac);
  document.getElementById("d-icao").textContent = ac.callsign ? ac.icao : "";
  document.getElementById("d-squawk").textContent = ac.squawk || "-";
  document.getElementById("d-category").textContent = ac.category || "-";
  document.getElementById("d-altitude").textContent = fmt(ac.altitude_baro, 0, " ft");
  document.getElementById("d-speed").textContent = fmt(ac.ground_speed, 0, " kt");
  document.getElementById("d-track").textContent = fmt(ac.track ?? ac.heading, 0, "°");
  document.getElementById("d-vrate").textContent = fmt(ac.vertical_rate, 0, " ft/min");
  document.getElementById("d-lat").textContent = fmt(ac.lat, 4);
  document.getElementById("d-lon").textContent = fmt(ac.lon, 4);
  document.getElementById("d-distance").textContent = fmt(ac.distance_nm, 1, " nm");
  document.getElementById("d-rssi").textContent = fmt(ac.rssi, 1, " dB");
  document.getElementById("d-messages").textContent = ac.messages ?? "-";
  document.getElementById("d-seen").textContent = fmt(ac.seen, 0, " s");

  const badge = document.getElementById("d-state");
  const code = (ac.state && ac.state.code) || "";
  const label = (ac.state && ac.state.label) || "-";
  badge.innerHTML = `${stateIconFor(code)}<span>${label}</span>`;
}

function highlightSelectedRow() {
  document.querySelectorAll("#aircraft-tbody .ac-row").forEach((row) => {
    row.classList.toggle("selected", row.dataset.icao === state.selectedIcao);
  });
}

async function selectAircraft(icao) {
  state.selectedIcao = icao;
  highlightSelectedRow();
  document.getElementById("aircraft-detail").hidden = false;
  try {
    const detail = await apiGet(`/api/aircraft/${icao}`);
    renderDetailBasic(detail);
    document.getElementById("d-first-seen").textContent = detail.first_seen
      ? new Date(detail.first_seen).toLocaleString()
      : "-";
    drawTrail(icao, detail.track_positions || []);
  } catch (e) {
    showToast("Não foi possível carregar detalhes: " + e.message);
  }
}

function hideDetail() {
  state.selectedIcao = null;
  highlightSelectedRow();
  document.getElementById("aircraft-detail").hidden = true;
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("d-close").addEventListener("click", hideDetail);
});

function drawTrail(icao, positions) {
  const entry = state.markers[icao];
  if (!entry) return;
  if (entry.trail) state.map.removeLayer(entry.trail);
  const pts = positions.filter((p) => p.lat != null && p.lon != null).map((p) => [p.lat, p.lon]);
  if (pts.length < 2) return;
  entry.trail = L.polyline(pts, { color: "#4fb0ff", weight: 2, opacity: 0.7 }).addTo(state.map);
}

/* ============================================================
 * ADS-B: histórico (área integrada - AO VIVO / HISTÓRICO)
 * ============================================================ */
function initHistoryMap() {
  state.historyMap = L.map("history-map").setView(
    state.receiver.configured ? [state.receiver.lat, state.receiver.lon] : [0, 0],
    state.receiver.configured ? 9 : 2
  );
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.historyMap);
}

function durationLabel(startIso, endIso) {
  if (!startIso || !endIso) return "-";
  const s = (new Date(endIso) - new Date(startIso)) / 1000;
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.floor(s / 60)}min ${Math.round(s % 60)}s`;
}

const HISTORY_LOAD_LIMIT = 500;

async function loadHistory() {
  try {
    state.historySessions = await apiGet(`/api/history/flights?limit=${HISTORY_LOAD_LIMIT}`);
    renderHistorySummary();
    renderHistorySessions();
  } catch (e) {
    showToast("Falha ao carregar histórico: " + e.message);
  }
}

function renderHistorySummary() {
  const rows = state.historySessions;
  const countEl = document.getElementById("hist-count");
  countEl.textContent = rows.length + (rows.length >= HISTORY_LOAD_LIMIT ? "+" : "");
  document.getElementById("hist-unique").textContent = new Set(rows.map((r) => r.icao)).size;
}

function applyHistoryFilters(rows) {
  const q = (document.getElementById("hist-search").value || "").trim().toUpperCase();
  const date = document.getElementById("hist-date").value; // YYYY-MM-DD ou ""
  return rows.filter((r) => {
    if (date && !(r.start_time || "").startsWith(date)) return false;
    if (q) {
      const hay = `${r.callsign || ""} ${r.icao || ""}`.toUpperCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderHistorySessions() {
  const filtered = applyHistoryFilters(state.historySessions);
  const container = document.getElementById("history-sessions");
  container.innerHTML = "";
  if (!filtered.length) {
    container.innerHTML = '<div class="history-empty-list">Nenhuma sessão encontrada.</div>';
    return;
  }
  for (const row of filtered) {
    const card = document.createElement("div");
    card.className = "session-card" + (row.id === state.selectedSessionId ? " selected" : "");
    const date = (row.start_time || "").slice(0, 10);
    const startT = row.start_time ? new Date(row.start_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "-";
    const endT = row.end_time ? new Date(row.end_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "-";
    const label = row.callsign || row.icao;
    const secondary = row.callsign ? row.icao : "";
    card.innerHTML = `
      <div class="session-card-head">
        <span class="session-callsign">${label}</span>
        ${secondary ? `<span class="session-icao">${secondary}</span>` : ""}
      </div>
      <div class="session-meta">${date} · ${startT} → ${endT} · ${durationLabel(row.start_time, row.end_time)}</div>
      <div class="session-stats">
        <span>${fmt(row.max_altitude)} ft</span>
        <span>${fmt(row.closest_distance_nm, 1)} nm</span>
      </div>
    `;
    card.addEventListener("click", () => showHistorySession(row.id));
    container.appendChild(card);
  }
}

async function showHistorySession(id) {
  try {
    const session = await apiGet(`/api/history/flights/${id}`);
    state.selectedSessionId = id;
    renderHistorySessions(); // reflete o card selecionado

    document.getElementById("history-detail-empty").hidden = true;
    document.getElementById("history-detail-content").hidden = false;
    setTimeout(() => state.historyMap.invalidateSize(), 0);

    document.getElementById("hd-callsign").textContent = session.callsign || session.icao;
    document.getElementById("hd-icao").textContent = session.callsign ? session.icao : "";
    document.getElementById("hd-start").textContent = session.start_time ? new Date(session.start_time).toLocaleString() : "-";
    document.getElementById("hd-end").textContent = session.end_time ? new Date(session.end_time).toLocaleString() : "-";
    document.getElementById("hd-duration").textContent = durationLabel(session.start_time, session.end_time);
    document.getElementById("hd-altitude").textContent =
      session.min_altitude != null && session.max_altitude != null
        ? `${fmt(session.min_altitude)}–${fmt(session.max_altitude)} ft`
        : "-";
    document.getElementById("hd-speed").textContent = fmt(session.max_ground_speed, 0, " kt");
    document.getElementById("hd-dist").textContent = fmt(session.closest_distance_nm, 1, " nm");
    document.getElementById("hd-positions").textContent = (session.positions || []).length;

    if (state.historyLayer) {
      state.historyMap.removeLayer(state.historyLayer);
      state.historyLayer = null;
    }
    const pts = (session.positions || [])
      .filter((p) => p.lat != null && p.lon != null)
      .map((p) => [p.lat, p.lon]);
    if (pts.length) {
      state.historyLayer = L.layerGroup([
        L.polyline(pts, { color: "#33e07a", weight: 3 }),
        L.circleMarker(pts[0], { radius: 5, color: "#4fb0ff" }).bindTooltip("Início"),
        L.circleMarker(pts[pts.length - 1], { radius: 5, color: "#ff5c5c" }).bindTooltip("Fim"),
      ]).addTo(state.historyMap);
      state.historyMap.fitBounds(pts, { padding: [24, 24] });
    } else {
      showToast("Sessão sem posições registradas", "info");
    }
  } catch (e) {
    showToast("Falha ao abrir sessão: " + e.message);
  }
}

/* ============================================================
 * Rádio
 * ============================================================ */
async function loadPresets() {
  try {
    state.presets = await apiGet("/api/radio/presets");
  } catch (e) {
    state.presets = [];
  }
  const select = document.getElementById("radio-preset");
  select.innerHTML = '<option value="">— manual —</option>';
  for (const p of state.presets) {
    const opt = document.createElement("option");
    opt.value = p.name;
    opt.textContent = p.frequency_mhz != null
      ? `${p.name} (${formatFrequencyMhz(p.frequency_mhz)} MHz)`
      : `${p.name} (sem freq. configurada)`;
    select.appendChild(opt);
  }
  select.addEventListener("change", () => {
    const freqInput = document.getElementById("radio-frequency");
    const preset = state.presets.find((p) => p.name === select.value);
    if (!preset) return; // "— manual —": não mexe no que já estava digitado
    if (preset.frequency_mhz != null) {
      freqInput.value = preset.frequency_mhz;
    } else {
      // preset sem frequência configurada: NUNCA deixar um valor manual
      // "grudado" de uma seleção anterior associado a este nome (bug
      // relatado: histórico registrando preset errado)
      freqInput.value = "";
    }
  });
}

function readRadioForm() {
  const presetName = document.getElementById("radio-preset").value || null;
  const freqInput = document.getElementById("radio-frequency").value;
  const body = {
    preset: presetName || undefined,
    gain: document.getElementById("radio-gain").value || "auto",
    squelch_dbfs: Number(document.getElementById("radio-squelch").value),
    volume: Number(document.getElementById("radio-volume").value),
    silence_timeout: Number(document.getElementById("radio-silence-timeout").value),
    prebuffer_sec: Number(document.getElementById("radio-prebuffer").value),
  };
  if (freqInput) body.frequency_mhz = Number(freqInput);
  return body;
}

async function startRadio() {
  setOptimisticSwitching();
  try {
    const status = await apiPost("/api/mode/radio", readRadioForm());
    applyStatus(status);
    showToast("Rádio ativo", "info");
  } catch (e) {
    showToast("Falha ao iniciar rádio: " + e.message);
  } finally {
    refreshStatus();
  }
}

async function tuneRadio() {
  try {
    const status = await apiPost("/api/radio/tune", readRadioForm());
    applyStatus(status);
  } catch (e) {
    showToast("Falha ao sintonizar: " + e.message);
  }
}

async function stopRadio() {
  setOptimisticSwitching();
  try {
    const status = await apiPost("/api/stop");
    applyStatus(status);
    showToast("SDR liberado (IDLE)", "info");
  } catch (e) {
    showToast("Falha ao parar rádio: " + e.message);
  } finally {
    refreshStatus();
  }
}

function applyRadioStatus(status) {
  document.getElementById("radio-freq-display").textContent = formatFrequencyMhz(status.frequency_mhz);
  document.getElementById("radio-active-preset").textContent = status.running ? (status.preset || "-") : "-";

  const stateEl = document.getElementById("radio-squelch-state");
  const active = status.squelch_state === "RECEBENDO";
  stateEl.textContent = status.recording ? "GRAVANDO" : status.squelch_state || "SILÊNCIO";
  stateEl.classList.toggle("active", active || status.recording);

  const dbfs = status.level_dbfs ?? -120;
  const pct = Math.max(0, Math.min(100, ((dbfs + 60) / 60) * 100));
  document.getElementById("level-bar").style.width = pct + "%";
  document.getElementById("level-dbfs").textContent = `${fmt(dbfs, 1)} dBFS`;
  document.getElementById("level-rms").textContent = `RMS ${fmt(status.rms, 0)}`;
}

async function tickRadio() {
  try {
    const status = await apiGet("/api/radio/status");
    applyRadioStatus(status);
  } catch (e) {
    /* silencioso */
  }
}

async function loadRecordings() {
  try {
    const rows = await apiGet("/api/radio/recordings?limit=50");
    const tbody = document.getElementById("recordings-tbody");
    tbody.innerHTML = "";
    for (const rec of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${new Date(rec.start_time).toLocaleTimeString()}</td>
        <td>${rec.preset || "-"}</td>
        <td>${formatFrequencyMhz(rec.frequency_mhz)} MHz</td>
        <td>${fmt(rec.duration, 1, "s")}</td>
        <td>${fmt(rec.peak_level, 1)} dBFS</td>
        <td><button class="btn-play">OUVIR</button></td>
      `;
      tr.querySelector(".btn-play").addEventListener("click", () => {
        const player = document.getElementById("recording-player");
        player.src = `/recordings/${rec.filename}`;
        player.play();
      });
      tbody.appendChild(tr);
    }
  } catch (e) {
    /* silencioso */
  }
}

function startRadioPolling() {
  clearInterval(state.timers.radio);
  clearInterval(state.timers.recordings);
  clearInterval(state.timers.status);
  tickRadio();
  refreshStatus();
  state.timers.radio = setInterval(tickRadio, 400);
  state.timers.recordings = setInterval(loadRecordings, 3000);
  state.timers.status = setInterval(refreshStatus, 2000);
}

/* ============================================================
 * Ligações de UI e inicialização
 * ============================================================ */
function bindUI() {
  // navegação de abas: só troca o que é exibido, nunca inicia/encerra nada
  document.getElementById("btn-mode-adsb").addEventListener("click", () => setTab("adsb"));
  document.getElementById("btn-mode-radio").addEventListener("click", () => setTab("radio"));

  // navegação AO VIVO / HISTÓRICO: idem, puramente visual
  document.getElementById("btn-sub-live").addEventListener("click", () => setAdsbSubview("live"));
  document.getElementById("btn-sub-history").addEventListener("click", () => setAdsbSubview("history"));

  // ações que de fato tocam o hardware
  document.getElementById("btn-adsb-start").addEventListener("click", startAdsb);
  document.getElementById("btn-adsb-stop").addEventListener("click", stopAdsb);
  document.getElementById("btn-radio-start").addEventListener("click", startRadio);
  document.getElementById("btn-radio-tune").addEventListener("click", tuneRadio);
  document.getElementById("btn-radio-stop").addEventListener("click", stopRadio);

  document.getElementById("radio-squelch").addEventListener("input", (e) => {
    document.getElementById("radio-squelch-val").textContent = e.target.value;
  });
  document.getElementById("radio-volume").addEventListener("input", (e) => {
    document.getElementById("radio-volume-val").textContent = Number(e.target.value).toFixed(1) + "x";
  });

  document.querySelectorAll("#aircraft-list .ac-header [data-sort]").forEach((el) => {
    el.addEventListener("click", () => {
      state.sort = el.dataset.sort;
      tickAdsb();
    });
  });

  document.getElementById("hist-search").addEventListener("input", renderHistorySessions);
  document.getElementById("hist-date").addEventListener("change", renderHistorySessions);
  document.getElementById("hist-clear-filters").addEventListener("click", () => {
    document.getElementById("hist-search").value = "";
    document.getElementById("hist-date").value = "";
    renderHistorySessions();
  });
}

async function init() {
  bindUI();
  await loadPresets();

  let status = null;
  try {
    status = await apiGet("/api/status");
    state.receiver = status.receiver;
  } catch (e) {
    /* servidor pode estar subindo ainda */
  }

  initMap();
  initHistoryMap();

  // a aba inicial só reflete o que já está ativo no servidor (conveniência),
  // mas isso é só navegação - nenhuma chamada de start/stop acontece aqui.
  // setTab() cuida de esconder/mostrar, invalidateSize() do mapa (o Leaflet
  // foi inicializado com o container ainda "hidden" alguns milissegundos
  // atrás, então precisa recalcular o tamanho ao ficar visível) e de
  // arrancar o polling certo - reaproveitar aqui evita duplicar essa lógica
  // e evita regressões como o mapa nascer com o tamanho errado.
  const initialTab = status && status.sdr_state === "radio" ? "radio" : "adsb";
  setTab(initialTab);

  if (status) applyStatus(status);
}

init();
