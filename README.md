# 🌡️ Hitze-Check Deutschland

Ein einfaches, offenes Werkzeug, mit dem normale Menschen Wetter- und Klima-Behauptungen
selbst überprüfen können — jede angezeigte Zahl ist nachprüfbar und mit Quelle belegt.

## Was macht das Projekt?

- Eine interaktive Deutschlandkarte zeigt 10 Wetterstationen.
- Für jedes Jahr (und den meteorologischen Sommer) lassen sich heiße Tage (≥ 30 °C),
  Durchschnittstemperatur und Höchsttemperatur ablesen.
- Jede Station bietet ein Verlaufsdiagramm über alle verfügbaren Jahre sowie einen
  CSV-Download der Rohdaten.

## Aufbau

```
hitze-check/
├─ scripts/         Python-Pipeline (build_data.py), holt Daten via Meteostat
├─ docs/            fertige Webseite (wird per GitHub Pages veröffentlicht)
│  └─ data/         von der Pipeline erzeugte JSON/CSV-Dateien
├─ PROJEKTPLAN.md    Projektplan & Entscheidungen
└─ README.md
```

## Daten aktualisieren

```
python3 scripts/build_data.py
```

Erzeugt bzw. aktualisiert die Dateien in `docs/data/`.

## Quelle

Datenbasis: [Meteostat](https://meteostat.net/) (CC BY 4.0), bündelt u. a. Daten des
Deutschen Wetterdienstes (DWD).

Deutschland-Umriss (`docs/data/germany.geo.json`, für den Bereiche-Modus) von
[isellsoap/deutschlandGeoJSON](https://github.com/isellsoap/deutschlandGeoJSON) (Unlicense/Public Domain).
