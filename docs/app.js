// Hitze-Check Deutschland — Kartenlogik (Phase 2: Grundkarte)

const map = L.map("map").setView([51.16, 10.45], 6); // Zentrum Deutschland

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 18,
}).addTo(map);

async function loadStations() {
  const res = await fetch("data/stations.json");
  const stations = await res.json();

  stations.forEach((station) => {
    L.circleMarker([station.lat, station.lon], {
      radius: 10,
      fillColor: "#4A90D9",
      color: "#fff",
      weight: 2,
      fillOpacity: 0.9,
      className: "station-marker",
    })
      .addTo(map)
      .bindTooltip(station.name);
  });
}

loadStations();
