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

    layer.on("click", (e) => {
      const distanceKm = haversineKm([e.latlng.lng, e.latlng.lat], [station.lon, station.lat]);
      selectStation(station.id, { distanceKm });
    });

    areaLayersByStation[station.id] = layer;
  });
}

function updateMarkers() {
  stations.forEach((station) => {
    const stats = statsFor(station.id, state.year, state.period);
    const color = colorForTemp(stats ? stats.max_temp : null);
    markersByStation[station.id].setStyle({ fillColor: color });
    if (areaLayersByStation[station.id]) {
      areaLayersByStation[station.id].setStyle({ fillColor: color });
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
}

function closeDetailPanel() {
  state.selectedStation = null;
  document.getElementById("detail-panel").classList.add("hidden");
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

  document.getElementById("metric-hotdays").textContent = stats ? stats.hot_days : "–";
  document.getElementById("metric-mean").textContent = stats ? formatTemp(stats.mean_temp) : "–";
  document.getElementById("metric-max").textContent = stats
    ? `${formatTemp(stats.max_temp)} am ${formatDateGerman(stats.max_temp_date)}`
    : "–";

  const periodLabel = state.period === "summer" ? "Sommer" : "ganzes Jahr";
  document.getElementById("detail-period-note").textContent = stats
    ? `Zeitraum: ${state.year}, ${periodLabel}`
    : `Für ${state.year} (${periodLabel}) liegen keine Daten vor.`;

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
  }

  btnModeStations.addEventListener("click", () => setMode("stations"));
  btnModeAreas.addEventListener("click", () => setMode("areas"));
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

async function init() {
  await loadData();
  createMarkers();
  createAreaLayers();
  setupControls();
  setupThemeToggle();
  updateMarkers();
  document.getElementById("detail-close").addEventListener("click", closeDetailPanel);
}

init();
