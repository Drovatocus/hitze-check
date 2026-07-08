# 🌡️ Hitze-Check Deutschland — Projektplan

**Mission:** Ein einfaches, offenes Werkzeug, mit dem normale Menschen Wetter- und
Klima-Behauptungen schnell selbst überprüfen können — jede Zahl nachprüfbar und mit Quelle.

---

## ⭐ Leitprinzipien (gelten für ALLE Entscheidungen)

1. **Transparenz:** Jede angezeigte Zahl ist nachprüfbar und hat einen Quellennachweis.
2. **Nutzerkontrolle:** Der Anwender entscheidet selbst, was angezeigt wird.
3. **Ehrlichkeit:** Vorläufige Daten werden als solche gekennzeichnet, Begriffe werden erklärt.
4. **Einfachheit:** Für normale Menschen gemacht, nicht für Experten.

---

## 📊 Fortschritt auf einen Blick

Legende: ⬜ offen · 🔄 in Arbeit · ✅ erledigt

| Phase | Inhalt | Status |
|-------|--------|--------|
| Planung | Alle großen Entscheidungen | ✅ |
| 0 | Setup & Werkzeuge | 🔄 |
| 1 | Datenpipeline (Python → JSON) | ⬜ |
| 2 | Grundkarte | ⬜ |
| 3 | Zeitregler + Farblogik | ⬜ |
| 4 | Detail-Panel (Klick) | ⬜ |
| 5 | Feinschliff & Transparenz | ⬜ |
| 6 | Online stellen → **Alpha live** | ⬜ |
| 7 | Auto-Update (monatlich) | ⬜ |

---

## ✅ Festgelegte Entscheidungen

- **Datenquelle:** Meteostat (Python-Bibliothek, kein API-Key nötig), bündelt u. a. die offiziellen DWD-Daten. Berechnete Werte werden als JSON **fest ins Repo** gelegt (Seite läuft auch, wenn Meteostat mal offline ist).
- **Stationen (10 zum Test, erweiterbar gebaut):** Wuppertal · Hamburg · Rostock-Warnemünde · Hannover · Berlin · Dresden · Frankfurt am Main · Stuttgart · Freiburg · München.
- **Zeitraum:** volle verfügbare Historie je Station; „Daten verfügbar ab" wird je Station angezeigt.
- **Zeitsteuerung:** Jahr + Saison (meteorologischer Sommer = Juni–August).
- **Karte:** farbige, klickbare Punkte (V1). Punktfarbe = **heißester Tag (Tmax)** des gewählten Zeitraums; feste Skala, **≥ 30 °C = rot**, gleich für alle Jahre (Vergleichbarkeit). Start = letzte bekannte Daten.
- **Detail-Panel (Klick):** heiße Tage (≥ 30 °C) · Durchschnittstemperatur · Höchsttemperatur **mit Datum** · Verlaufsdiagramm über alle Jahre mit markiertem Rekord · Stationsinfos + Quelle · **CSV-Rohdaten-Export**.
- **Technik:** statische Seite (HTML + Leaflet-Karte + Chart.js-Diagramm) + Python-Pipeline; Hosting GitHub Pages (kostenlos); später GitHub Action fürs monatliche Auto-Update.
- **Plattform:** Desktop-only (Alpha).

### 📖 Begriffe (so erklären wir sie auch auf der Seite)
- **Heißer Tag:** Tagesmaximum ≥ 30 °C.
- **Meteorologischer Sommer:** Juni, Juli, August.
- **Rekordtag:** höchster je an dieser Station gemessener Tageshöchstwert.
- **Vorläufige Daten:** die letzten Tage/Wochen sind evtl. noch nicht endgültig geprüft — werden markiert.

---

## 🚀 PHASE 0 — Setup & Werkzeuge  *(deine Aufgabe, einmalig)*

> Tipp: Alle Befehle kommen ins **Terminal**. Bei Windows nimm „PowerShell", bei Mac „Terminal".
> Wenn etwas hakt, kannst du Claude Code direkt fragen — er kann diese Schritte auch für dich erledigen.

- [ ] **Claude Code läuft** — prüfen mit:
  ```
  claude --version
  ```
- [ ] **Python vorhanden** — prüfen mit (eins von beiden gibt eine Versionsnummer):
  ```
  python3 --version
  python --version
  ```
  Falls nicht da: LTS-Version von https://www.python.org/ installieren.
- [ ] **Die zwei Pakete installieren:**
  ```
  pip install meteostat pandas
  ```
- [ ] **GitHub-Konto** vorhanden (kostenlos, für Code + Hosting): https://github.com/signup
- [ ] **Projektordner anlegen** (Name z. B. `hitze-check`) und darin ein Git-Repo starten:
  ```
  mkdir hitze-check
  cd hitze-check
  git init
  ```
- [ ] **Diese Datei** (`PROJEKTPLAN.md`) in den Projektordner legen.

Geplante Ordnerstruktur (richtet Claude Code in Phase 1 ein):
```
hitze-check/
├─ scripts/      → build_data.py (Python-Pipeline)
├─ data/         → die fertigen JSON-Dateien
├─ site/         → index.html, app.js, style.css (die Webseite)
├─ README.md
└─ PROJEKTPLAN.md
```

---

## 🐍 PHASE 1 — Datenpipeline  *(Code: ich / Claude Code · Ausführen: du)*

- [ ] **JSON-Schema festlegen** (die „Schnittstelle" zwischen Pipeline und Karte) — *machen wir als Erstes*
- [ ] `build_data.py`: lädt die 10 Stationen über Meteostat
- [ ] Kennzahlen je Jahr **und** je Sommer berechnen: heiße Tage, Durchschnitt, Höchstwert + Datum
- [ ] Metadaten je Station: Name, ID, Koordinaten, „Daten verfügbar ab", Stand, Quelle
- [ ] Ausgabe als JSON in `data/`
- [ ] Skript lokal ausführen und Ergebnis prüfen

## 🗺️ PHASE 2 — Grundkarte  *(Code: ich / Claude Code)*

- [ ] Leaflet-Karte mit Deutschland-Hintergrund
- [ ] Die 10 Stationspunkte aus dem JSON anzeigen
- [ ] Farbskala (≥ 30 °C rot) für ein festes Jahr

## 🎚️ PHASE 3 — Zeitregler + Farblogik

- [ ] Regler für das Jahr + Umschalter Sommer/Ganzjahr
- [ ] Punktfarben ändern sich live mit der Auswahl
- [ ] Beim Laden: letzte bekannte Daten
- [ ] Legende zur Farbskala

## 🔍 PHASE 4 — Detail-Panel (Klick auf eine Station)

- [ ] Klick öffnet Panel mit den 3 Kennzahlen
- [ ] Höchsttemperatur **mit Datum**
- [ ] Verlaufsdiagramm über alle Jahre + Rekord markiert
- [ ] Stationsinfos + Quelle/Lizenz
- [ ] **CSV-Rohdaten-Export-Button**

## ✨ PHASE 5 — Feinschliff & Transparenz

- [ ] Erklärtexte/Tooltips für jeden Begriff
- [ ] Disclaimer zu vorläufigen Daten
- [ ] Quellenangabe „Datenbasis: Meteostat / DWD" im Footer
- [ ] Kurzer „Was ist das hier?"-Text auf der Startseite

## 🌍 PHASE 6 — Online stellen  *(du, mit meiner Anleitung)*

- [ ] Repo auf GitHub hochladen (push)
- [ ] GitHub Pages aktivieren
- [ ] Öffentliche Seite testen
- [ ] 🎉 **Alpha (V1) ist live**

## 🔄 PHASE 7 — Auto-Update  *(Code: ich / Claude Code · Aktivieren: du)*

- [ ] GitHub Action: monatlicher Pipeline-Neulauf + automatischer Commit
- [ ] Testlauf erfolgreich → Monatsdaten erscheinen von allein

---

## 📦 V2-Backlog (nach der Alpha)

- Restliche Stationen ergänzen (Architektur ist schon vorbereitet)
- Vergleich zum langjährigen Mittel (z. B. 1961–1990): „+2,1 °C über dem Schnitt"
- „Nachweis teilen"-Link (Direktlink auf Station + Zeitraum)
- Flächige Heatmap statt nur Punkte
- Live-Schicht (Open-Meteo)
- Handy-Optimierung

---

## ❓ Offene Mini-Entscheidungen (blockieren nichts)

- [ ] Projektname / Titel der Seite
- [ ] Hosting: kostenlose GitHub-Pages-URL **oder** eigene Domain (~10 €/Jahr)

---

## 📚 Quellen & Lizenz

- Daten: Meteostat (Daten unter CC BY 4.0), bündelt u. a. Deutscher Wetterdienst (DWD).
- Pflicht: sichtbare Quellenangabe „Datenbasis: Meteostat / DWD".
