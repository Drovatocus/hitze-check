// Hitze-Check Deutschland — Kartenlogik

const COLOR_SCALE = [
  { max: 20, color: "#4A90D9" }, // < 20 °C
  { max: 25, color: "#7CB342" }, // 20–24,9 °C
  { max: 30, color: "#FB8C00" }, // 25–29,9 °C
  { max: Infinity, color: "#E53935" }, // >= 30 °C
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

// Zustand: aktuell gewaehltes Jahr + Zeitraum (annual/summer)
const state = {
  year: null,
  period: "annual",
};

let stations = [];
let seriesByStation = {}; // station_id -> geladenes series/<id>.json
let markersByStation = {}; // station_id -> Leaflet-Marker

async function loadData() {
  const stationsRes = await fetch("data/stations.json");
  stations = await stationsRes.json();

  await Promise.all(
    stations.map(async (station) => {
      const res = await fetch(`data/series/${station.id}.json`);
      seriesByStation[station.id] = await res.json();
    })
  );
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
    }).addTo(map);
    marker.bindTooltip(station.name);
    markersByStation[station.id] = marker;
  });
}

function updateMarkers() {
  stations.forEach((station) => {
    const marker = markersByStation[station.id];
    const stats = statsFor(station.id, state.year, state.period);
    marker.setStyle({ fillColor: colorForTemp(stats ? stats.max_temp : null) });
  });
}

function setupControls() {
  const years = allAvailableYears();
  const minYear = years[0];
  const maxYear = years[years.length - 1];

  const slider = document.getElementById("year-slider");
  slider.min = minYear;
  slider.max = maxYear;
  slider.value = maxYear; // beim Laden: aktuellstes verfuegbares Jahr
  state.year = maxYear;
  document.getElementById("year-value").textContent = maxYear;

  slider.addEventListener("input", () => {
    state.year = Number(slider.value);
    document.getElementById("year-value").textContent = state.year;
    updateMarkers();
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
}

async function init() {
  await loadData();
  createMarkers();
  setupControls();
  updateMarkers();
}

init();
