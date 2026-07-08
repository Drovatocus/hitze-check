// Hitze-Check Deutschland — Kartenlogik

const COLOR_SCALE = [
  { max: 20, color: "#4A90D9" }, // < 20 °C
  { max: 25, color: "#7CB342" }, // 20–24,9 °C
  { max: 30, color: "#FB8C00" }, // 25–29,9 °C
  { max: 40, color: "#E53935" }, // 30–39,9 °C
  { max: Infinity, color: "#8E24AA" }, // >= 40 °C (Extremwert)
];
const NO_DATA_COLOR = "#bbb";

function colorForTemp(temp) {
  if (temp === null || temp === undefined || Number.isNaN(temp)) return NO_DATA_COLOR;
  const step = COLOR_SCALE.find((s) => temp < s.max);
  return step.color;
}

// Divergierende Skala fuer den Vergleichsmodus (Differenz heisse Tage, Jahr B minus Jahr A)
function colorForDiff(diff) {
  if (diff === null || diff === undefined || Number.isNaN(diff)) return NO_DATA_COLOR;
  if (diff <= -10) return "#1565C0";
  if (diff < 0) return "#90CAF9";
  if (diff === 0) return "#eeeeee";
  if (diff < 10) return "#FFAB91";
  return "#C62828";
}

function formatSignedNumber(value, decimals = 0) {
  const rounded = decimals > 0 ? value.toFixed(decimals).replace(".", ",") : String(Math.round(value));
  return (value > 0 ? "+" : "") + rounded;
}

// Haelt den aktuellen Zustand in der URL fest, damit sich eine konkrete Ansicht
// (Jahr/Zeitraum/Modus/Station) per Link teilen und beim Laden wiederherstellen laesst.
function syncUrl() {
  const params = new URLSearchParams();
  params.set("y", state.year);
  params.set("p", state.period);
  params.set("m", state.mode);
  if (state.compareMode) {
    params.set("cmp", "1");
    params.set("a", state.compareYearA);
    params.set("b", state.compareYearB);
  }
  if (state.selectedStation) params.set("station", state.selectedStation);
  const newUrl = `${window.location.pathname}?${params.toString()}`;
  window.history.replaceState(null, "", newUrl);
}

const map = L.map("map").setView([51.16, 10.45], 6); // Zentrum Deutschland

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 18,
}).addTo(map);

// Zustand: aktuell gewaehltes Jahr + Zeitraum (annual/summer) + angeklickte Station
const state = {
  year: null,
  period: "annual",
  selectedStation: null,
  clickInfo: null, // { distanceKm } wenn die Station per Klick im Bereiche-Modus gewaehlt wurde
  theme: "light",
  mode: "stations", // "stations" | "areas"
  compareMode: false,
  compareYearA: null,
  compareYearB: null,
};

let stations = [];
let seriesByStation = {}; // station_id -> geladenes series/<id>.json
let markersByStation = {}; // station_id -> Leaflet-Marker
let areaLayersByStation = {}; // station_id -> Leaflet-GeoJSON-Layer (Voronoi-Zelle)
let germanyGeoJson = null;
let detailChart = null; // Chart.js-Instanz des Verlaufsdiagramms

const stationsLayerGroup = L.layerGroup().addTo(map);
const areasLayerGroup = L.layerGroup();

function haversineKm(a, b) {
  // a, b jeweils [lon, lat]
  const R = 6371;
  const dLat = ((b[1] - a[1]) * Math.PI) / 180;
  const dLon = ((b[0] - a[0]) * Math.PI) / 180;
  const lat1 = (a[1] * Math.PI) / 180;
  const lat2 = (b[1] * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function formatDateGerman(isoDate) {
  const [y, m, d] = isoDate.split("-");
  return `${d}.${m}.${y}`;
}

function formatTemp(value) {
  if (value === null || value === undefined) return "keine Daten";
  return `${value.toFixed(1).replace(".", ",")} °C`;
}

async function loadData() {
  const stationsRes = await fetch("data/stations.json");
  stations = await stationsRes.json();

  await Promise.all(
    stations.map(async (station) => {
      const res = await fetch(`data/series/${station.id}.json`);
      seriesByStation[station.id] = await res.json();
    })
  );

  const germanyRes = await fetch("data/germany.geo.json");
  const germanyCollection = await germanyRes.json();
  germanyGeoJson = germanyCollection.features[0];
}

function allAvailableYears() {
  const years = new Set();
  Object.values(seriesByStation).forEach((series) => {
    Object.keys(series.years).forEach((y) => years.add(Number(y)));
  });
  return Array.from(years).sort((a, b) => a - b);
}

function statsFor(stationId, year, period) {
  const series = seriesByStation[stationId];
  const yearData = series.years[String(year)];
  if (!yearData) return null;
  return yearData[period] || null;
}

function createMarkers() {
  stations.forEach((station) => {
    const marker = L.circleMarker([station.lat, station.lon], {
      radius: 10,
      fillColor: NO_DATA_COLOR,
      color: "#fff",
      weight: 2,
      fillOpacity: 0.9,
      className: "station-marker",
    }).addTo(stationsLayerGroup);
    marker.bindTooltip(station.name);
    marker.on("click", () => selectStation(station.id));
    markersByStation[station.id] = marker;
  });
}

// Voronoi-Diagramm (naechste-Station-Flaechen), an der Landesgrenze zugeschnitten.
// Wird einmalig beim Laden berechnet, da sich die Stationskoordinaten nicht aendern.
function createAreaLayers() {
  // Einfache Plattkarten-Naeherung (Laengengrad mit cos(Breitengrad) gestaucht),
  // damit die Zellen bei Deutschlands Breite nicht in Ost-West-Richtung verzerrt wirken.
  const meanLat = stations.reduce((sum, s) => sum + s.lat, 0) / stations.length;
  const cosLat = Math.cos((meanLat * Math.PI) / 180);
  const project = (lon, lat) => [lon * cosLat, lat];
  const unproject = ([x, y]) => [x / cosLat, y];

  const points = stations.map((s) => project(s.lon, s.lat));
  const delaunay = d3.Delaunay.from(points);

  const bbox = turf.bbox(germanyGeoJson);
  const pad = 2; // Grad Puffer rundherum, damit Randstationen vollstaendige Zellen bekommen
  const bounds = [
    ...project(bbox[0] - pad, bbox[1] - pad),
    ...project(bbox[2] + pad, bbox[3] + pad),
  ];
  const voronoi = delaunay.voronoi(bounds);

  stations.forEach((station, i) => {
    const cell = voronoi.cellPolygon(i);
    if (!cell) return;

    const ring = cell.map(unproject);
    const first = ring[0];
    const last = ring[ring.length - 1];
    if (first[0] !== last[0] || first[1] !== last[1]) ring.push(first);

    const cellFeature = turf.polygon([ring]);
    let clipped;
    try {
      clipped = turf.intersect(turf.featureCollection([cellFeature, germanyGeoJson]));
    } catch (e) {
      console.warn(`Voronoi-Zuschnitt fuer ${station.id} fehlgeschlagen:`, e);
      return;
    }
    if (!clipped) return;

    const layer = L.geoJSON(clipped, {
      style: {
        fillColor: NO_DATA_COLOR,
        fillOpacity: 0.75,
        color: "#fff",
        weight: 1,
      },
    }).addTo(areasLayerGroup);

    layer.bindTooltip(station.name);
    layer.on("click", (e) => {
      const distanceKm = haversineKm([e.latlng.lng, e.latlng.lat], [station.lon, station.lat]);
      selectStation(station.id, { distanceKm });
    });

    areaLayersByStation[station.id] = layer;
  });
}

function colorForStationNow(stationId) {
  const stats = statsFor(stationId, state.year, state.period);
  return colorForTemp(stats ? stats.max_temp : null);
}

function colorForComparison(stationId) {
  const statsA = statsFor(stationId, state.compareYearA, state.period);
  const statsB = statsFor(stationId, state.compareYearB, state.period);
  if (!statsA || !statsB) return NO_DATA_COLOR;
  return colorForDiff(statsB.hot_days - statsA.hot_days);
}

// Zeigt den Zahlenwert immer im Tooltip an (nicht nur ueber die Farbe erkennbar) -
// wichtig u. a. bei Farbsehschwaeche.
function tooltipTextFor(stationId) {
  const station = stations.find((s) => s.id === stationId);
  if (state.compareMode) {
    const statsA = statsFor(stationId, state.compareYearA, state.period);
    const statsB = statsFor(stationId, state.compareYearB, state.period);
    if (!statsA || !statsB) return `${station.name}: keine Daten für mind. eines der Jahre`;
    const diff = statsB.hot_days - statsA.hot_days;
    return `${station.name}: ${formatSignedNumber(diff)} heiße Tage (${state.compareYearB} ggü. ${state.compareYearA})`;
  }
  const stats = statsFor(stationId, state.year, state.period);
  if (!stats) return `${station.name}: keine Daten für ${state.year}`;
  return `${station.name}: ${formatTemp(stats.max_temp)}`;
}

function updateMarkers() {
  stations.forEach((station) => {
    const color = state.compareMode ? colorForComparison(station.id) : colorForStationNow(station.id);
    const tooltipText = tooltipTextFor(station.id);
    markersByStation[station.id].setStyle({ fillColor: color });
    markersByStation[station.id].setTooltipContent(tooltipText);
    if (areaLayersByStation[station.id]) {
      areaLayersByStation[station.id].setStyle({ fillColor: color });
      areaLayersByStation[station.id].setTooltipContent(tooltipText);
    }
  });
  if (state.selectedStation) {
    renderDetailPanel(state.selectedStation);
  }
}

function selectStation(stationId, clickInfo = null) {
  state.selectedStation = stationId;
  state.clickInfo = clickInfo;
  document.getElementById("detail-panel").classList.remove("hidden");
  renderDetailPanel(stationId);
  syncUrl();
}

function closeDetailPanel() {
  state.selectedStation = null;
  document.getElementById("detail-panel").classList.add("hidden");
  syncUrl();
}

function renderDetailPanel(stationId) {
  const station = stations.find((s) => s.id === stationId);
  const series = seriesByStation[stationId];
  const stats = statsFor(stationId, state.year, state.period);

  document.getElementById("detail-title").textContent = station.name;

  const clickNote = document.getElementById("detail-click-distance");
  if (state.clickInfo) {
    clickNote.textContent = `Angeklickter Punkt: ${state.clickInfo.distanceKm.toFixed(1)} km von dieser Station entfernt.`;
    clickNote.classList.remove("hidden");
  } else {
    clickNote.classList.add("hidden");
  }

  const periodLabel = state.period === "summer" ? "Sommer" : "ganzes Jahr";
  const singleMetrics = document.getElementById("single-metrics");
  const compareMetrics = document.getElementById("compare-metrics");
  const periodNote = document.getElementById("detail-period-note");

  if (state.compareMode) {
    singleMetrics.classList.add("hidden");
    compareMetrics.classList.remove("hidden");

    const statsA = statsFor(stationId, state.compareYearA, state.period);
    const statsB = statsFor(stationId, state.compareYearB, state.period);

    document.getElementById("compare-th-a").textContent = state.compareYearA;
    document.getElementById("compare-th-b").textContent = state.compareYearB;

    document.getElementById("cmp-hotdays-a").textContent = statsA ? statsA.hot_days : "–";
    document.getElementById("cmp-hotdays-b").textContent = statsB ? statsB.hot_days : "–";
    document.getElementById("cmp-mean-a").textContent = statsA ? formatTemp(statsA.mean_temp) : "–";
    document.getElementById("cmp-mean-b").textContent = statsB ? formatTemp(statsB.mean_temp) : "–";
    document.getElementById("cmp-max-a").textContent = statsA ? formatTemp(statsA.max_temp) : "–";
    document.getElementById("cmp-max-b").textContent = statsB ? formatTemp(statsB.max_temp) : "–";

    if (statsA && statsB) {
      document.getElementById("cmp-hotdays-diff").textContent = formatSignedNumber(statsB.hot_days - statsA.hot_days);
      document.getElementById("cmp-mean-diff").textContent =
        statsA.mean_temp !== null && statsB.mean_temp !== null
          ? formatSignedNumber(statsB.mean_temp - statsA.mean_temp, 1) + " °C"
          : "–";
      document.getElementById("cmp-max-diff").textContent =
        formatSignedNumber(statsB.max_temp - statsA.max_temp, 1) + " °C";
      periodNote.textContent = `Vergleich (${periodLabel}): ${state.compareYearB} gegenüber ${state.compareYearA}. `
        + `${formatSignedNumber(statsB.hot_days - statsA.hot_days)} heiße Tage.`;
    } else {
      ["cmp-hotdays-diff", "cmp-mean-diff", "cmp-max-diff"].forEach((id) => (document.getElementById(id).textContent = "–"));
      periodNote.textContent = "Für mindestens eines der beiden Jahre liegen keine Daten vor.";
    }
  } else {
    singleMetrics.classList.remove("hidden");
    compareMetrics.classList.add("hidden");

    document.getElementById("metric-hotdays").textContent = stats ? stats.hot_days : "–";
    document.getElementById("metric-mean").textContent = stats ? formatTemp(stats.mean_temp) : "–";
    document.getElementById("metric-max").textContent = stats
      ? `${formatTemp(stats.max_temp)} am ${formatDateGerman(stats.max_temp_date)}`
      : "–";

    periodNote.textContent = stats
      ? `Zeitraum: ${state.year}, ${periodLabel}`
      : `Für ${state.year} (${periodLabel}) liegen keine Daten vor.`;
  }

  const infoList = document.getElementById("detail-info");
  infoList.innerHTML = "";
  const infoItems = [
    ["Angezeigter Ort", station.name],
    ["Messstation", `${station.meteostat_station_name} (${station.meteostat_station_id})`],
    ["Daten verfügbar ab", station.data_from],
    ["Stand", formatDateGerman(station.last_data)],
    [
      'Rekord <span class="info-icon" title="Rekordtag: höchster je an dieser Station gemessener Tageshöchstwert">ⓘ</span>',
      `${formatTemp(series.record.temp)} am ${formatDateGerman(series.record.date)}`,
    ],
  ];
  infoItems.forEach(([label, value]) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${label}</span><span>${value}</span>`;
    infoList.appendChild(li);
  });

  renderChart(series);

  document.getElementById("detail-csv").onclick = () => {
    const link = document.createElement("a");
    link.href = `data/raw/${stationId}.csv`;
    link.download = `${stationId}.csv`;
    link.click();
  };
}

function renderChart(series) {
  const years = Object.keys(series.years).sort();
  const hotDays = years.map((y) => series.years[y][state.period]?.hot_days ?? 0);
  const recordYear = series.record.date.slice(0, 4);

  const barColors = years.map((y) => (y === recordYear ? "#E53935" : "#4A90D9"));

  if (detailChart) {
    detailChart.destroy();
  }
  const ctx = document.getElementById("detail-chart").getContext("2d");
  detailChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: years,
      datasets: [
        {
          label: "Heiße Tage (≥ 30 °C)",
          data: hotDays,
          backgroundColor: barColors,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            afterLabel: (item) => (item.label === recordYear ? "Rekordjahr (Höchsttemperatur)" : ""),
          },
        },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 12 } },
        y: { beginAtZero: true, title: { display: true, text: "Anzahl heißer Tage" } },
      },
    },
  });
}

function setupControls() {
  // Ganz am Anfang lesen: setYear()/setMode() etc. rufen syncUrl() auf und wuerden
  // die urspruenglichen URL-Parameter sonst schon mit den Defaults ueberschreiben.
  const urlParams = new URLSearchParams(window.location.search);

  const years = allAvailableYears();
  const minYear = years[0];
  const maxYear = years[years.length - 1];

  const slider = document.getElementById("year-slider");
  const yearInput = document.getElementById("year-input");
  const yearMinus = document.getElementById("year-minus");
  const yearPlus = document.getElementById("year-plus");

  slider.min = minYear;
  slider.max = maxYear;
  yearInput.min = minYear;
  yearInput.max = maxYear;

  // Regler, Stepper-Buttons und Eingabefeld immer synchron halten.
  function setYear(year) {
    const clamped = Math.min(maxYear, Math.max(minYear, year));
    state.year = clamped;
    slider.value = clamped;
    yearInput.value = clamped;
    document.getElementById("year-value").textContent = clamped;
    updateMarkers();
    syncUrl();
  }

  setYear(maxYear); // beim Laden: aktuellstes verfuegbares Jahr

  slider.addEventListener("input", () => setYear(Number(slider.value)));

  yearMinus.addEventListener("click", () => setYear(state.year - 1));
  yearPlus.addEventListener("click", () => setYear(state.year + 1));

  yearInput.addEventListener("change", () => {
    const parsed = Number(yearInput.value);
    // Ungueltige oder leere Eingaben abfangen: zurueck auf den aktuellen Stand setzen.
    if (!Number.isFinite(parsed) || yearInput.value.trim() === "") {
      yearInput.value = state.year;
      return;
    }
    setYear(Math.round(parsed));
  });

  const btnAnnual = document.getElementById("btn-annual");
  const btnSummer = document.getElementById("btn-summer");

  function setPeriod(period) {
    state.period = period;
    btnAnnual.classList.toggle("active", period === "annual");
    btnSummer.classList.toggle("active", period === "summer");
    updateMarkers();
    syncUrl();
  }

  btnAnnual.addEventListener("click", () => setPeriod("annual"));
  btnSummer.addEventListener("click", () => setPeriod("summer"));

  const btnModeStations = document.getElementById("btn-mode-stations");
  const btnModeAreas = document.getElementById("btn-mode-areas");
  const areasNote = document.getElementById("areas-note");

  function setMode(mode) {
    state.mode = mode;
    btnModeStations.classList.toggle("active", mode === "stations");
    btnModeAreas.classList.toggle("active", mode === "areas");
    areasNote.classList.toggle("hidden", mode !== "areas");

    if (mode === "areas") {
      map.removeLayer(stationsLayerGroup);
      map.addLayer(areasLayerGroup);
    } else {
      map.removeLayer(areasLayerGroup);
      map.addLayer(stationsLayerGroup);
    }
    syncUrl();
  }

  btnModeStations.addEventListener("click", () => setMode("stations"));
  btnModeAreas.addEventListener("click", () => setMode("areas"));

  // --- Vergleichsmodus (zwei Jahre gegenueberstellen) ---
  const compareToggle = document.getElementById("compare-toggle");
  const singleYearControls = document.getElementById("single-year-controls");
  const singleYearStepper = document.getElementById("single-year-stepper");
  const compareYearControls = document.getElementById("compare-year-controls");
  const legendAbsolute = document.getElementById("legend-absolute");
  const legendCompare = document.getElementById("legend-compare");
  const yearA = document.getElementById("compare-year-a");
  const yearB = document.getElementById("compare-year-b");

  yearA.min = minYear;
  yearA.max = maxYear;
  yearB.min = minYear;
  yearB.max = maxYear;
  // Sinnvoller Default: ein frueheres Jahrzehnt gegen das aktuellste verfuegbare Jahr.
  state.compareYearA = Math.max(minYear, maxYear - 10);
  state.compareYearB = maxYear;
  yearA.value = state.compareYearA;
  yearB.value = state.compareYearB;

  function setCompareYear(which, value) {
    const clamped = Math.min(maxYear, Math.max(minYear, value));
    if (which === "a") {
      state.compareYearA = clamped;
      yearA.value = clamped;
    } else {
      state.compareYearB = clamped;
      yearB.value = clamped;
    }
    updateMarkers();
    syncUrl();
  }

  function handleCompareYearInput(input, which) {
    input.addEventListener("change", () => {
      const parsed = Number(input.value);
      if (!Number.isFinite(parsed) || input.value.trim() === "") {
        input.value = which === "a" ? state.compareYearA : state.compareYearB;
        return;
      }
      setCompareYear(which, Math.round(parsed));
    });
  }
  handleCompareYearInput(yearA, "a");
  handleCompareYearInput(yearB, "b");

  compareToggle.addEventListener("change", () => {
    state.compareMode = compareToggle.checked;
    singleYearControls.classList.toggle("hidden", state.compareMode);
    singleYearStepper.classList.toggle("hidden", state.compareMode);
    compareYearControls.classList.toggle("hidden", !state.compareMode);
    legendAbsolute.classList.toggle("hidden", state.compareMode);
    legendCompare.classList.toggle("hidden", !state.compareMode);
    updateMarkers();
    syncUrl();
  });

  // --- Zustand aus der URL wiederherstellen (fuer geteilte Links) ---
  if (urlParams.has("y")) setYear(Number(urlParams.get("y")));
  if (urlParams.has("p")) setPeriod(urlParams.get("p") === "summer" ? "summer" : "annual");
  if (urlParams.has("m")) setMode(urlParams.get("m") === "areas" ? "areas" : "stations");
  if (urlParams.get("cmp") === "1") {
    compareToggle.checked = true;
    state.compareMode = true;
    singleYearControls.classList.add("hidden");
    singleYearStepper.classList.add("hidden");
    compareYearControls.classList.remove("hidden");
    legendAbsolute.classList.add("hidden");
    legendCompare.classList.remove("hidden");
    if (urlParams.has("a")) setCompareYear("a", Number(urlParams.get("a")));
    if (urlParams.has("b")) setCompareYear("b", Number(urlParams.get("b")));
  }
  if (urlParams.has("station") && stations.some((s) => s.id === urlParams.get("station"))) {
    selectStation(urlParams.get("station"));
  }
}

function setupShareLink() {
  const button = document.getElementById("copy-link");
  const originalText = button.textContent;

  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      button.textContent = "✅ Link kopiert!";
    } catch (e) {
      // Fallback, falls die Zwischenablage nicht verfuegbar ist (z. B. kein HTTPS-Kontext).
      window.prompt("Link zum Kopieren (Strg+C):", window.location.href);
    }
    setTimeout(() => (button.textContent = originalText), 1800);
  });
}

function setupThemeToggle() {
  const button = document.getElementById("theme-toggle");
  // Nur fuer die laufende Sitzung gemerkt (einfache Variable im state, kein localStorage noetig).
  function applyTheme() {
    document.documentElement.dataset.theme = state.theme;
    button.textContent = state.theme === "dark" ? "☀️ Hell" : "🌙 Dunkel";
  }
  button.addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    applyTheme();
  });
  applyTheme();
}

function setupAboutOverlay() {
  const overlay = document.getElementById("about-overlay");
  const openBtn = document.getElementById("about-link");
  const closeBtn = document.getElementById("about-close");

  function open() {
    overlay.classList.remove("hidden");
  }
  function close() {
    overlay.classList.add("hidden");
  }

  openBtn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  // Klick auf den dunklen Hintergrund (nicht auf die Box selbst) schliesst das Overlay.
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
}

async function init() {
  await loadData();
  createMarkers();
  createAreaLayers();
  setupControls();
  setupThemeToggle();
  setupShareLink();
  setupAboutOverlay();
  updateMarkers();
  document.getElementById("detail-close").addEventListener("click", closeDetailPanel);
}

init();
