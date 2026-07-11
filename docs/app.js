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

// Divergierende Skala fuer die Abweichungs-Ansicht des laufenden Jahres (Temperatur-
// Abweichung ggue. 1991-2020, siehe current_year_anomaly) - 7 Stufen mit einer
// zusaetzlichen 1,5-°C-Zwischenstufe (Feinschliff-Anfrage), symmetrisch um 0.
// WICHTIG: Die 1,5-°C-Stufe ist reine Darstellungsfeinheit fuer die Abweichung
// EINER Station in EINEM (unvollstaendigen) Jahr - nicht zu verwechseln mit dem
// 1,5-°C-Ziel des Pariser Klimaabkommens (globale, langfristige Erwaermung ggue.
// 1850-1900). Siehe Hinweis-Icon in der Legende (index.html).
function colorForAnomaly(anomaly) {
  if (anomaly === null || anomaly === undefined || Number.isNaN(anomaly)) return NO_DATA_COLOR;
  if (anomaly <= -3) return "#1565C0"; // deutlich kuehler
  if (anomaly <= -1.5) return "#42A5F5"; // kuehler
  if (anomaly < 0) return "#90CAF9"; // leicht kuehler
  if (anomaly === 0) return "#eeeeee"; // wie im Schnitt
  if (anomaly < 1.5) return "#FFAB91"; // leicht waermer
  if (anomaly < 3) return "#EF5350"; // waermer
  return "#C62828"; // deutlich waermer
}

// Ordnet jeden von colorForTemp/colorForDiff/colorForAnomaly moeglichen Hex-Wert
// einer CSS-Klasse zu (siehe .swatch-* in style.css) - fuer clusterIconCreateFunction,
// die den Cluster-Farbpunkt per HTML-String erzeugt und daher keine inline style=""
// nutzen darf (Content-Security-Policy ohne style-src 'unsafe-inline').
const SWATCH_CLASS_BY_COLOR = {
  "#4A90D9": "swatch-blau",
  "#7CB342": "swatch-gruen",
  "#FB8C00": "swatch-orange",
  "#E53935": "swatch-rot",
  "#8E24AA": "swatch-lila",
  "#bbb": "swatch-nodata",
  "#1565C0": "swatch-cold-strong",
  "#42A5F5": "swatch-cold-mid",
  "#90CAF9": "swatch-cold-light",
  "#eeeeee": "swatch-neutral",
  "#FFAB91": "swatch-warm-light",
  "#EF5350": "swatch-warm-mid",
  "#C62828": "swatch-warm-strong",
};

function formatSignedNumber(value, decimals = 0) {
  const rounded = decimals > 0 ? value.toFixed(decimals).replace(".", ",") : String(Math.round(value));
  return (value > 0 ? "+" : "") + rounded;
}

// Kennzeichnet unvollstaendige Jahre (v. a. das laufende Jahr), damit ein
// "bisher"-Wert nicht mit einem vollen Jahr verwechselt wird.
function incompleteNote(year, stats) {
  if (!stats || stats.complete) return "";
  return ` Jahr ${year} bisher, Stand ${formatDateGerman(stats.last_date)}.`;
}

// Klartext-Satz zur Temperatur-Abweichung des laufenden Jahres ggue. 1991-2020
// (siehe current_year_anomaly, von der Pipeline pro Station berechnet).
function anomalyText(anomaly) {
  if (!meta) return "";
  const refPeriod = `${meta.baseline_start_year}–${meta.baseline_end_year}`;
  const bis = `bis ${formatDateGerman(meta.data_stand)}`;
  if (anomaly === null || anomaly === undefined) {
    return `Abweichung vom Mittel ${refPeriod} für diese Station nicht verfügbar (Reihe beginnt erst nach ${meta.baseline_start_year}).`;
  }
  if (anomaly === 0) return `Genau im Schnitt ${refPeriod} (${bis}).`;
  const richtung = anomaly > 0 ? "wärmer" : "kühler";
  const abs = Math.abs(anomaly).toFixed(1).replace(".", ",");
  return `${abs} °C ${richtung} als im Schnitt ${refPeriod} (${bis}).`;
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

// zoomControl:false + eigene Position unten rechts, damit Leaflets Standard-Zoomregler
// nicht mit dem eigenen Kontroll-Panel oben links (Suche, Jahr, Modus) ueberlappt.
const map = L.map("map", { zoomControl: false }).setView([51.16, 10.45], 6); // Zentrum Deutschland
L.control.zoom({ position: "bottomright" }).addTo(map);

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
  moreDetailsOpen: false,
};

let stations = [];
let mapIndexByStation = {}; // station_id -> schlanker Jahres-Index (max_temp/hot_days) aus map_index.json
let anomalyByStation = {}; // station_id -> current_year_anomaly (Temperatur-Abweichung ggue. 1991-2020)
let meta = null; // meta.json: laufendes Jahr, Datenstand, letztes vollstaendiges Jahr, Referenzperiode
let seriesByStation = {}; // station_id -> volle series/<id>.json, WIRD ERST BEIM KLICK NACHGELADEN
let markersByStation = {}; // station_id -> Leaflet-Marker
let areaLayersByStation = {}; // station_id -> Leaflet-GeoJSON-Layer (Voronoi-Zelle)
let germanyGeoJson = null;
let detailChart = null; // Chart.js-Instanz des Verlaufsdiagramms
let areasCreated = false; // Voronoi-Flaechen werden erst beim ersten Wechsel in den Bereiche-Modus berechnet

// Marker-Clustering (viele Stationen bundesweit): rausgezoomt Cluster mit Anzahl,
// reingezoomt einzelne, weiterhin individuell eingefaerbte Stationen.
const stationsLayerGroup = L.markerClusterGroup({
  maxClusterRadius: 50,
  iconCreateFunction: clusterIconCreateFunction,
}).addTo(map);
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

// Findet unter den vorhandenen Stationen die naechstgelegene zu einem Punkt.
function nearestStation(lat, lon) {
  let best = null;
  let bestDistanceKm = Infinity;
  stations.forEach((station) => {
    const d = haversineKm([lon, lat], [station.lon, station.lat]);
    if (d < bestDistanceKm) {
      bestDistanceKm = d;
      best = station;
    }
  });
  return { station: best, distanceKm: bestDistanceKm };
}

// Ortssuche ueber die oeffentliche Nominatim-API (OpenStreetMap) - liefert die
// Koordinaten des gesuchten Orts, keine eigene Ortsdatenbank noetig.
async function geocodePlace(query) {
  const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=de&q=${encodeURIComponent(query)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error("Geocoding fehlgeschlagen");
  const results = await res.json();
  if (results.length === 0) return null;
  return { lat: parseFloat(results[0].lat), lon: parseFloat(results[0].lon) };
}

function setupPlaceSearch() {
  const input = document.getElementById("place-search");
  const button = document.getElementById("place-search-btn");
  const status = document.getElementById("search-status");

  async function runSearch() {
    const query = input.value.trim();
    if (!query) return;

    status.textContent = "Suche läuft …";
    status.classList.remove("hidden");
    button.disabled = true;

    try {
      const place = await geocodePlace(query);
      if (!place) {
        status.textContent = `Kein Ort gefunden für „${query}“.`;
        return;
      }
      const { station, distanceKm } = nearestStation(place.lat, place.lon);
      map.setView([station.lat, station.lon], 9);
      selectStation(station.id, { distanceKm, label: `„${query}“` });
      status.classList.add("hidden");
    } catch (e) {
      status.textContent = "Suche momentan nicht möglich. Bitte später erneut versuchen.";
    } finally {
      button.disabled = false;
    }
  }

  button.addEventListener("click", runSearch);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      runSearch();
    }
  });
}

function formatDateGerman(isoDate) {
  const [y, m, d] = isoDate.split("-");
  return `${d}.${m}.${y}`;
}

function formatTemp(value) {
  if (value === null || value === undefined) return "keine Daten";
  return `${value.toFixed(1).replace(".", ",")} °C`;
}

// Beim Start werden nur die schlanken Listen geladen (stations.json, map_index.json),
// nicht alle vollen series/<id>.json - das waere bei bundesweit ~380 Stationen zu
// viel. Die vollen Detaildaten einer Station werden erst beim Anklicken nachgeladen
// (siehe selectStation).
async function loadData() {
  const stationsRes = await fetch("data/stations.json");
  stations = await stationsRes.json();

  const mapIndexRes = await fetch("data/map_index.json");
  const mapIndex = await mapIndexRes.json();
  mapIndex.forEach((entry) => {
    mapIndexByStation[entry.id] = entry.years;
    anomalyByStation[entry.id] = entry.current_year_anomaly ?? null;
  });

  const metaRes = await fetch("data/meta.json");
  meta = await metaRes.json();

  const germanyRes = await fetch("data/germany.geo.json");
  const germanyCollection = await germanyRes.json();
  germanyGeoJson = germanyCollection.features[0];
}

function allAvailableYears() {
  const years = new Set();
  Object.values(mapIndexByStation).forEach((stationYears) => {
    Object.keys(stationYears).forEach((y) => years.add(Number(y)));
  });
  return Array.from(years).sort((a, b) => a - b);
}

// Schlanke Kennzahlen (nur max_temp/hot_days) aus dem Karten-Index - reicht fuer
// Einfaerbung, Tooltip und Jahresbereich, ohne die volle Station laden zu muessen.
function lightStatsFor(stationId, year, period) {
  const stationYears = mapIndexByStation[stationId];
  if (!stationYears) return null;
  const yearData = stationYears[String(year)];
  if (!yearData) return null;
  return yearData[period] || null;
}

// Volle Kennzahlen aus der (lazy nachgeladenen) series/<id>.json - nur verfuegbar,
// nachdem die Station einmal angeklickt/geladen wurde.
function statsFor(stationId, year, period) {
  const series = seriesByStation[stationId];
  if (!series) return null;
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
    marker.hitzeCheckStationId = station.id; // Rueckverweis fuer clusterIconCreateFunction()
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

// Das laufende (unvollstaendige) Jahr bekommt im Einzeljahr-Modus eine eigene
// Abweichungs-Ansicht (statt Absolutwerte, die es faelschlich "kuehl" aussehen
// liessen) - siehe colorForAnomaly. Gilt bewusst nicht im Vergleichsmodus, da der
// dortige Differenz-Farbmodus (colorForDiff) bereits relativ ist und die
// unvollstaendigen Jahre schon per incompleteNote() im Detail-Panel kennzeichnet.
function isRunningYearSingle() {
  return !state.compareMode && meta !== null && state.year === meta.running_year;
}

function colorForStationNow(stationId) {
  if (isRunningYearSingle()) return colorForAnomaly(anomalyByStation[stationId]);
  const stats = lightStatsFor(stationId, state.year, state.period);
  return colorForTemp(stats ? stats.max_temp : null);
}

// Liefert denselben Zahlenwert, der auch fuer die Einzelmarker-Einfaerbung
// herangezogen wird (Hoechsttemperatur / Abweichung / Vergleichs-Differenz je nach
// aktivem Modus) - Grundlage fuer den Cluster-Durchschnitt in clusterIconCreateFunction.
function clusterValueForStation(stationId) {
  if (state.compareMode) {
    const statsA = lightStatsFor(stationId, state.compareYearA, state.period);
    const statsB = lightStatsFor(stationId, state.compareYearB, state.period);
    if (!statsA || !statsB) return null;
    return statsB.hot_days - statsA.hot_days;
  }
  if (isRunningYearSingle()) return anomalyByStation[stationId] ?? null;
  const stats = lightStatsFor(stationId, state.year, state.period);
  return stats ? stats.max_temp : null;
}

function colorForClusterValue(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_DATA_COLOR;
  if (state.compareMode) return colorForDiff(value);
  if (isRunningYearSingle()) return colorForAnomaly(value);
  return colorForTemp(value);
}

// Faerbt Cluster-Marker (zusammengefasste Stationen, siehe stationsLayerGroup) nach
// dem DURCHSCHNITT der aktuell dargestellten Werte ihrer enthaltenen Stationen ein,
// statt der leaflet.markercluster-Standardfarbe. Stationen ohne Wert (z. B. "keine
// Referenzdaten") werden von der Mittelwertbildung ausgenommen. Wird bei jeder
// Aenderung (Jahr/Zeitraum/Modus/Vergleich) per stationsLayerGroup.refreshClusters()
// in updateMarkers() neu berechnet.
function clusterIconCreateFunction(cluster) {
  const values = cluster
    .getAllChildMarkers()
    .map((m) => clusterValueForStation(m.hitzeCheckStationId))
    .filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  const avg = values.length > 0 ? values.reduce((a, b) => a + b, 0) / values.length : null;
  const color = avg === null ? NO_DATA_COLOR : colorForClusterValue(avg);
  // Farb-Klasse statt inline style="" - noetig fuer eine Content-Security-Policy
  // ohne style-src 'unsafe-inline' (siehe .swatch-* in style.css).
  const swatchClass = SWATCH_CLASS_BY_COLOR[color] || "swatch-nodata";
  return L.divIcon({
    html: `<div class="cluster-marker-inner ${swatchClass}"><span>${cluster.getChildCount()}</span></div>`,
    className: "station-cluster-icon",
    iconSize: [40, 40],
    iconAnchor: [20, 20],
  });
}

function colorForComparison(stationId) {
  const statsA = lightStatsFor(stationId, state.compareYearA, state.period);
  const statsB = lightStatsFor(stationId, state.compareYearB, state.period);
  if (!statsA || !statsB) return NO_DATA_COLOR;
  return colorForDiff(statsB.hot_days - statsA.hot_days);
}

// Zeigt den Zahlenwert immer im Tooltip an (nicht nur ueber die Farbe erkennbar) -
// wichtig u. a. bei Farbsehschwaeche. Nutzt den schlanken Karten-Index, damit
// das Hovern ueber eine Station keine eigene Netzwerkanfrage ausloest.
function tooltipTextFor(stationId) {
  const station = stations.find((s) => s.id === stationId);
  if (state.compareMode) {
    const statsA = lightStatsFor(stationId, state.compareYearA, state.period);
    const statsB = lightStatsFor(stationId, state.compareYearB, state.period);
    if (!statsA || !statsB) return `${station.name}: keine Daten für mind. eines der Jahre`;
    const diff = statsB.hot_days - statsA.hot_days;
    return `${station.name}: ${formatSignedNumber(diff)} heiße Tage (${state.compareYearB} ggü. ${state.compareYearA})`;
  }
  if (isRunningYearSingle()) {
    const anomaly = anomalyByStation[stationId];
    if (anomaly === null || anomaly === undefined) return `${station.name}: Abweichung nicht verfügbar`;
    return `${station.name}: ${formatSignedNumber(anomaly, 1)} °C ggü. Mittel ${meta.baseline_start_year}–${meta.baseline_end_year} (bisher ${state.year})`;
  }
  const stats = lightStatsFor(stationId, state.year, state.period);
  if (!stats) return `${station.name}: keine Daten für ${state.year}`;
  return `${station.name}: ${formatTemp(stats.max_temp)}`;
}

// Blendet den "laufendes Jahr"-Hinweisbanner ein/aus und formuliert den Text
// aus meta.json (Datenstand, Anteil des Jahres, das schon erfasst ist).
function updateRunningYearBanner(unfinished) {
  const banner = document.getElementById("running-year-banner");
  if (!unfinished) {
    banner.classList.add("hidden");
    return;
  }
  const pct = String(meta.running_year_coverage_pct).replace(".", ",");
  banner.textContent =
    `⚠️ ${meta.running_year} läuft noch – Stand ${formatDateGerman(meta.data_stand)}, erst rund ${pct} % ` +
    `des Jahres. Absolutwerte sind noch nicht mit vollen Jahren vergleichbar, daher zeigt die Karte ` +
    `stattdessen die Abweichung vom Mittel ${meta.baseline_start_year}–${meta.baseline_end_year}.`;
  banner.classList.remove("hidden");
}

// Waehlt die passende Legende (Absolutwerte / Vergleich / Abweichung laufendes Jahr) -
// zentral hier statt verstreut in den einzelnen Steuerungs-Handlern, damit Jahr-,
// Zeitraum- und Vergleichsmodus-Wechsel immer konsistent zur richtigen Legende fuehren.
function updateLegendMode(runningYearActive) {
  const showCompare = state.compareMode;
  const showAnomaly = !showCompare && runningYearActive;
  const showAbsolute = !showCompare && !showAnomaly;
  document.getElementById("legend-absolute").classList.toggle("hidden", !showAbsolute);
  document.getElementById("legend-compare").classList.toggle("hidden", !showCompare);
  document.getElementById("legend-anomaly").classList.toggle("hidden", !showAnomaly);
}

function updateMarkers() {
  const runningYearActive = isRunningYearSingle();
  // "Unfertig"-Optik (gestrichelter Rand, mehr Transparenz), damit ein erst
  // teilweise vergangenes Jahr nicht wie ein regulaeres volles Jahr wirkt.
  const markerStyle = runningYearActive ? { dashArray: "3,3", fillOpacity: 0.55 } : { dashArray: null, fillOpacity: 0.9 };
  const areaStyle = runningYearActive ? { dashArray: "4,3", fillOpacity: 0.55 } : { dashArray: null, fillOpacity: 0.75 };

  stations.forEach((station) => {
    const color = state.compareMode ? colorForComparison(station.id) : colorForStationNow(station.id);
    const tooltipText = tooltipTextFor(station.id);
    markersByStation[station.id].setStyle({ fillColor: color, ...markerStyle });
    markersByStation[station.id].setTooltipContent(tooltipText);
    if (areaLayersByStation[station.id]) {
      areaLayersByStation[station.id].setStyle({ fillColor: color, ...areaStyle });
      areaLayersByStation[station.id].setTooltipContent(tooltipText);
    }
  });
  updateRunningYearBanner(runningYearActive);
  updateLegendMode(runningYearActive);
  // Cluster-Icons cachen ihre Farbe (siehe clusterIconCreateFunction) - nach jeder
  // Aenderung neu berechnen, sonst bleibt die Durchschnittsfarbe beim alten Zustand.
  stationsLayerGroup.refreshClusters();
  if (state.selectedStation) {
    renderDetailPanel(state.selectedStation);
  }
}

// Die volle Station (series/<id>.json) wird erst beim Anklicken nachgeladen und
// dann fuer die Sitzung zwischengespeichert (seriesByStation). Bis die Antwort da
// ist, zeigt renderDetailPanel einen Ladezustand.
async function selectStation(stationId, clickInfo = null) {
  if (state.selectedStation !== stationId) {
    state.moreDetailsOpen = false; // beim Stationswechsel wieder einklappen
  }
  state.selectedStation = stationId;
  state.clickInfo = clickInfo;
  document.getElementById("detail-panel").classList.remove("hidden");
  syncUrl();

  if (!seriesByStation[stationId]) {
    renderDetailPanel(stationId); // Ladezustand anzeigen
    try {
      const res = await fetch(`data/series/${stationId}.json`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      seriesByStation[stationId] = await res.json();
    } catch (e) {
      console.warn(`Konnte Stationsdaten fuer ${stationId} nicht laden:`, e);
      if (state.selectedStation === stationId) {
        document.getElementById("detail-title").textContent = "Fehler beim Laden der Stationsdaten";
      }
      return;
    }
  }

  // Falls der Nutzer zwischenzeitlich eine andere Station gewaehlt hat: nicht mehr
  // die (jetzt veraltete) Antwort dieser Anfrage anzeigen.
  if (state.selectedStation !== stationId) return;
  renderDetailPanel(stationId);
}

function closeDetailPanel() {
  state.selectedStation = null;
  document.getElementById("detail-panel").classList.add("hidden");
  syncUrl();
}

function renderDetailPanel(stationId) {
  const station = stations.find((s) => s.id === stationId);
  const series = seriesByStation[stationId];

  if (!series) {
    // Volle Daten noch nicht geladen: nur Ladezustand zeigen, Rest ausblenden.
    document.getElementById("detail-title").textContent = `${station.name} – lädt …`;
    document.getElementById("detail-click-distance").classList.add("hidden");
    document.getElementById("single-metrics").classList.remove("hidden");
    document.getElementById("compare-metrics").classList.add("hidden");
    ["metric-hotdays", "metric-mean", "metric-max"].forEach((id) => (document.getElementById(id).textContent = "…"));
    document.getElementById("detail-period-note").textContent = "";
    document.getElementById("detail-info").innerHTML = "";
    document.getElementById("more-details").classList.add("hidden");
    return;
  }

  const stats = statsFor(stationId, state.year, state.period);

  document.getElementById("detail-title").textContent = station.name;

  const clickNote = document.getElementById("detail-click-distance");
  if (state.clickInfo) {
    const label = state.clickInfo.label || "Angeklickter Punkt";
    clickNote.textContent = `${label}: ${state.clickInfo.distanceKm.toFixed(1)} km von dieser Station entfernt.`;
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
        + `${formatSignedNumber(statsB.hot_days - statsA.hot_days)} heiße Tage.`
        + incompleteNote(state.compareYearA, statsA) + incompleteNote(state.compareYearB, statsB);
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

    if (!stats) {
      periodNote.textContent = `Für ${state.year} (${periodLabel}) liegen keine Daten vor.`;
    } else if (!stats.complete) {
      periodNote.textContent =
        `Zeitraum: ${state.year} (bisher, Stand ${formatDateGerman(stats.last_date)}), ${periodLabel} — die Zahl kann noch steigen.`;
      if (meta && state.year === meta.running_year) {
        periodNote.textContent += " " + anomalyText(series.current_year_anomaly);
      }
    } else {
      periodNote.textContent = `Zeitraum: ${state.year}, ${periodLabel}`;
    }
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
  renderMoreDetails(stationId);
  document.getElementById("more-details").classList.toggle("hidden", !state.moreDetailsOpen);
  document.getElementById("more-details-toggle").textContent = state.moreDetailsOpen
    ? "🔎 Weniger Details"
    : "🔎 Mehr Details";

  document.getElementById("detail-csv").onclick = () => {
    const link = document.createElement("a");
    link.href = `data/raw/${stationId}.csv`;
    link.download = `${stationId}.csv`;
    link.click();
  };
}

function renderDecadeBars(decades) {
  const container = document.getElementById("md-decades");
  container.innerHTML = "";
  const entries = Object.entries(decades).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) {
    container.textContent = "nicht verfügbar";
    return;
  }
  const maxVal = Math.max(...entries.map(([, v]) => v));
  entries.forEach(([decade, avg]) => {
    const row = document.createElement("div");
    row.className = "decade-row";

    const label = document.createElement("span");
    label.textContent = decade;

    const barWrap = document.createElement("span");
    barWrap.className = "decade-bar-wrap";
    const bar = document.createElement("span");
    bar.className = "decade-bar";
    bar.style.width = `${maxVal > 0 ? (avg / maxVal) * 100 : 0}%`;
    barWrap.appendChild(bar);

    const value = document.createElement("span");
    value.className = "decade-value";
    value.textContent = avg.toFixed(1).replace(".", ",");

    row.append(label, barWrap, value);
    container.appendChild(row);
  });
}

// "Mehr Details": Trend, Sommertage/Tropennaechte, Extremjahre, Datenvollstaendigkeit,
// Stationshoehe, Dekaden-Vergleich. Fehlende Werte werden als "nicht verfügbar"
// angezeigt, nie als 0.
function renderMoreDetails(stationId) {
  const station = stations.find((s) => s.id === stationId);
  const series = seriesByStation[stationId];
  const analysis = series.analysis;

  document.getElementById("md-trend").textContent =
    analysis.trend_hot_days_per_decade !== null
      ? `${formatSignedNumber(analysis.trend_hot_days_per_decade, 1)} heiße Tage pro Jahrzehnt`
      : "nicht verfügbar (zu kurze Datenreihe)";

  // Sommertage/Tropennaechte beziehen sich auf das aktuell gewaehlte Jahr
  // (im Vergleichsmodus auf Jahr B) und den gewaehlten Zeitraum.
  const refYear = state.compareMode ? state.compareYearB : state.year;
  const refStats = statsFor(stationId, refYear, state.period);
  document.getElementById("md-summer-days").textContent = refStats ? refStats.summer_days : "nicht verfügbar";
  document.getElementById("md-tropical-nights").textContent =
    refStats && refStats.tropical_nights !== null ? refStats.tropical_nights : "nicht verfügbar";

  document.getElementById("md-hottest").textContent =
    `${analysis.hottest_year.year} (${analysis.hottest_year.hot_days} heiße Tage)`;
  document.getElementById("md-mildest").textContent =
    `${analysis.mildest_year.year} (${analysis.mildest_year.hot_days} heiße Tage)`;

  let completenessText = `${String(analysis.completeness_pct).replace(".", ",")} % der Tage seit ${station.data_from} vorhanden`;
  if (analysis.data_gaps.length > 0) {
    const gapTexts = analysis.data_gaps.map(
      (g) => `${formatDateGerman(g.from)}–${formatDateGerman(g.to)} (${g.days} Tage)`
    );
    completenessText += `. Größere Lücke(n): ${gapTexts.join(", ")}`;
  }
  document.getElementById("md-completeness").textContent = completenessText;

  document.getElementById("md-elevation").textContent =
    station.elevation_m !== null && station.elevation_m !== undefined
      ? `${String(station.elevation_m).replace(".", ",")} m ü. NN`
      : "nicht verfügbar";

  renderDecadeBars(analysis.decades);
}

function renderChart(series) {
  const years = Object.keys(series.years).sort();
  const hotDays = years.map((y) => series.years[y][state.period]?.hot_days ?? 0);

  const datasets = [
    {
      type: "bar",
      label: "Heiße Tage (≥ 30 °C)",
      data: hotDays,
      backgroundColor: "#4A90D9",
      order: 2,
    },
  ];

  // Trendlinie basiert auf der ganzjaehrigen Reihe (siehe Pipeline) und wird
  // daher nur gezeigt, wenn auch "Ganzes Jahr" ausgewaehlt ist - so bleibt die
  // Linie konsistent mit den angezeigten Balken statt sie zu vermischen.
  const trendLine = series.analysis.trend_line;
  if (state.period === "annual" && trendLine && trendLine.length > 0) {
    const trendByYear = Object.fromEntries(trendLine.map((p) => [String(p.year), p.value]));
    const trendColor = document.documentElement.dataset.theme === "dark" ? "#f5f5f5" : "#222";
    datasets.push({
      type: "line",
      label: "Trend (linear)",
      data: years.map((y) => trendByYear[y] ?? null),
      borderColor: trendColor,
      borderWidth: 2,
      pointRadius: 0,
      fill: false,
      order: 1,
    });
  }

  if (detailChart) {
    detailChart.destroy();
  }
  const ctx = document.getElementById("detail-chart").getContext("2d");
  detailChart = new Chart(ctx, {
    type: "bar",
    data: { labels: years, datasets },
    options: {
      responsive: true,
      plugins: {
        legend: { display: datasets.length > 1 },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 12 } },
        y: { beginAtZero: true, title: { display: true, text: "Anzahl heißer Tage" } },
      },
    },
  });

  renderTrendSummary(series);
}

// Sachlicher Klartext-Satz unter dem Diagramm, der den Trend einordnet
// (juengste 10 Jahre der Reihe vs. aelteste 10 Jahre), ohne Wertung.
function renderTrendSummary(series) {
  const el = document.getElementById("trend-summary-text");
  const summary = series.analysis.trend_summary;
  if (!summary) {
    el.textContent = "";
    return;
  }
  const recentAvg = String(summary.recent.avg_hot_days).replace(".", ",");
  const earliestAvg = String(summary.earliest.avg_hot_days).replace(".", ",");
  el.textContent =
    `Die Jahre ${summary.recent.from}–${summary.recent.to} hatten im Schnitt ${recentAvg} heiße Tage im Jahr, ` +
    `die Jahre ${summary.earliest.from}–${summary.earliest.to} dagegen ${earliestAvg}.`;
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

  // Regler, Stepper-Buttons und Eingabefeld immer synchron halten. syncUrl per
  // Default an - beim initialen Setzen des Startjahres (siehe defaultYear unten)
  // bewusst abgeschaltet, damit ein frischer Aufruf der Basis-Adresse (ohne ?y=...)
  // die URL nicht automatisch mit dem Startjahr befuellt. Ein bewusst geteilter
  // Link mit ?y=... oder ein aktiver Regler-/Buttonwechsel schreibt weiterhin.
  function setYear(year, { syncUrl: shouldSyncUrl = true } = {}) {
    const clamped = Math.min(maxYear, Math.max(minYear, year));
    state.year = clamped;
    slider.value = clamped;
    yearInput.value = clamped;
    document.getElementById("year-value").textContent = clamped;
    updateMarkers();
    if (shouldSyncUrl) syncUrl();
  }

  // Beim Laden bewusst NICHT das laufende (unvollstaendige) Jahr zeigen, sondern
  // das letzte VOLLSTAENDIGE Jahr aus meta.json - sonst wirkt die Startansicht wie
  // ein "kuehles" Jahr, obwohl erst ein Teil davon vergangen ist. Ueber Regler/
  // Eingabe bleibt das laufende Jahr jederzeit waehlbar.
  const defaultYear = meta && years.includes(meta.last_complete_year) ? meta.last_complete_year : maxYear;
  setYear(defaultYear, { syncUrl: false });

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
      // Die Voronoi-Flaechen sind bei vielen Stationen (bundesweit) nicht ganz
      // billig zu berechnen - daher erst beim ersten Wechsel in den Modus, nicht
      // schon beim Laden der Seite.
      if (!areasCreated) {
        createAreaLayers();
        areasCreated = true;
        updateMarkers(); // faerbt die gerade erst erzeugten Flaechen ein
      }
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

function setupFactcheckOverlay() {
  const overlay = document.getElementById("factcheck-overlay");
  const closeBtn = document.getElementById("factcheck-close");

  function open() {
    overlay.classList.remove("hidden");
  }
  function close() {
    overlay.classList.add("hidden");
  }

  // Mehrere Einstiege: Footer und der Link im "Ueber dieses Projekt"-Overlay.
  document.querySelectorAll(".open-factcheck").forEach((btn) => btn.addEventListener("click", open));
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
}

function setupMoreDetailsToggle() {
  const button = document.getElementById("more-details-toggle");
  const panel = document.getElementById("more-details");
  button.addEventListener("click", () => {
    state.moreDetailsOpen = !state.moreDetailsOpen;
    panel.classList.toggle("hidden", !state.moreDetailsOpen);
    button.textContent = state.moreDetailsOpen ? "🔎 Weniger Details" : "🔎 Mehr Details";
  });
}

async function init() {
  await loadData();
  createMarkers();
  setupControls();
  setupThemeToggle();
  setupShareLink();
  setupAboutOverlay();
  setupFactcheckOverlay();
  setupMoreDetailsToggle();
  setupPlaceSearch();
  updateMarkers();
  document.getElementById("detail-close").addEventListener("click", closeDetailPanel);
}

init();
