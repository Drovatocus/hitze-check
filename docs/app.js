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
  theme: "light",
};

let stations = [];
let seriesByStation = {}; // station_id -> geladenes series/<id>.json
let markersByStation = {}; // station_id -> Leaflet-Marker
let detailChart = null; // Chart.js-Instanz des Verlaufsdiagramms

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
    marker.on("click", () => selectStation(station.id));
    markersByStation[station.id] = marker;
  });
}

function updateMarkers() {
  stations.forEach((station) => {
    const marker = markersByStation[station.id];
    const stats = statsFor(station.id, state.year, state.period);
    marker.setStyle({ fillColor: colorForTemp(stats ? stats.max_temp : null) });
  });
  if (state.selectedStation) {
    renderDetailPanel(state.selectedStation);
  }
}

function selectStation(stationId) {
  state.selectedStation = stationId;
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
  setupControls();
  setupThemeToggle();
  updateMarkers();
  document.getElementById("detail-close").addEventListener("click", closeDetailPanel);
}

init();
