# 🌡️ Hitze-Check Deutschland

Ein einfaches, offenes Werkzeug, mit dem normale Menschen Wetter- und Klima-Behauptungen
selbst überprüfen können — jede angezeigte Zahl ist nachprüfbar und mit Quelle belegt.

## Was macht das Projekt?

- Eine interaktive Deutschlandkarte zeigt automatisch alle geeigneten Wetterstationen
  bundesweit (mindestens 30 Jahre Tagesreihe und Daten aus den letzten 2 Jahren).
- Für jedes Jahr (und den meteorologischen Sommer) lassen sich heiße Tage (≥ 30 °C),
  Durchschnittstemperatur und Höchsttemperatur ablesen.
- Jede Station bietet ein Verlaufsdiagramm über alle verfügbaren Jahre, erweiterte
  Auswertungen ("Mehr Details": Trend, Dekaden-Vergleich, Sommertage/Tropennächte,
  Datenvollständigkeit) sowie einen CSV-Download der Rohdaten.
- Bereiche-Modus (Voronoi), Jahresvergleich, Ortssuche und ein Faktencheck zu
  verbreiteten Hitze-Behauptungen ergänzen die Karte.

## Aufbau

```
hitze-check/
├─ scripts/         Python-Pipeline (build_data.py), holt Daten via Meteostat
├─ docs/            fertige Webseite (wird per GitHub Pages veröffentlicht)
│  └─ data/
│     ├─ stations.json       Stationsmetadaten (alle geeigneten Stationen)
│     ├─ map_index.json      schlanker Index (id/name/lat/lon + max_temp/hot_days
│     │                      je Jahr) - laedt die Karte beim Start, statt aller
│     │                      vollen Stationsdateien
│     ├─ series/<id>.json    volle Kennzahlen je Station, erst beim Anklicken
│     │                      nachgeladen
│     ├─ raw/<id>.csv        Tageswerte je Station (CSV-Download)
│     └─ data_report.txt     Datenreport (Stationszahl, Regionen, 40+-Werte
│                            zur manuellen Kontrolle)
├─ PROJEKTPLAN.md    Projektplan & Entscheidungen
└─ README.md
```

## Daten aktualisieren

```
python3 scripts/build_data.py
```

Findet automatisch alle geeigneten Stationen bundesweit und aktualisiert die
Dateien in `docs/data/`. Laedt Jahrzehnte an Tagesdaten fuer ~380 Stationen
parallel herunter - das dauert je nach Netzwerk und Meteostat-Cache spuerbar
lange (von Minuten bis zu mehreren Stunden bei einem komplett frischen Lauf).

## Quelle

Datenbasis: [Meteostat](https://meteostat.net/) (CC BY 4.0), bündelt u. a. Daten des
Deutschen Wetterdienstes (DWD).

Deutschland-Umriss (`docs/data/germany.geo.json`, für den Bereiche-Modus) von
[isellsoap/deutschlandGeoJSON](https://github.com/isellsoap/deutschlandGeoJSON) (Unlicense/Public Domain).
