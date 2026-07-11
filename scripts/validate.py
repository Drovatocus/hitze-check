#!/usr/bin/env python3
"""
Validierung der erzeugten Dateien in docs/data/ gegen die Rohdaten und gegen
Plausibilitaet (Tier 1b, PRE-LAUNCH-CHECKS.md). Kein Netzwerk noetig, laeuft
gegen die bereits vorhandenen Dateien.

Aufruf: python3 scripts/validate.py
Exit-Code 0 = keine kritischen Verstoesse (Warnungen moeglich), 1 = Fehler gefunden.
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "docs" / "data"
SERIES_DIR = DATA_DIR / "series"
RAW_DIR = DATA_DIR / "raw"

PLAUSIBLE_MAX_C = 42.0
PLAUSIBLE_MIN_C = -40.0
# Grobe Bounding Box Deutschlands (inkl. Puffer) - faengt Vorzeichen-/Vertauschungsfehler
# bei lat/lon ab. Ersetzt die im Pre-Launch-Dokument vorgeschlagene "Distanz zum
# gelabelten Ort"-Pruefung: in dieser bundesweiten Architektur IST jede Station ihr
# eigener Ort (keine separate Stadt->Station-Zuordnung wie in der urspruenglichen
# 10-Staedte-Version), daher ist eine Geo-Plausibilitaetspruefung der Koordinaten
# selbst die sinnvollere Entsprechung.
GERMANY_BBOX = {"lat_min": 47.0, "lat_max": 55.5, "lon_min": 5.5, "lon_max": 15.5}


def load_raw_rows(station_id: str) -> list[dict]:
    with open(RAW_DIR / f"{station_id}.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    errors: list[str] = []
    warnings: list[str] = []

    stations = json.loads((DATA_DIR / "stations.json").read_text(encoding="utf-8"))
    today = datetime.now().date()

    warming_recent, warming_total = 0, 0

    for s in stations:
        sid = s["id"]
        series_path = SERIES_DIR / f"{sid}.json"
        raw_path = RAW_DIR / f"{sid}.csv"

        if not series_path.exists():
            errors.append(f"{sid}: series/-Datei fehlt")
            continue
        if not raw_path.exists():
            errors.append(f"{sid}: raw/-CSV fehlt")
            continue

        series = json.loads(series_path.read_text(encoding="utf-8"))

        if not (GERMANY_BBOX["lat_min"] <= s["lat"] <= GERMANY_BBOX["lat_max"]):
            errors.append(f"{sid}: Breitengrad {s['lat']} ausserhalb Deutschlands")
        if not (GERMANY_BBOX["lon_min"] <= s["lon"] <= GERMANY_BBOX["lon_max"]):
            errors.append(f"{sid}: Laengengrad {s['lon']} ausserhalb Deutschlands")

        raw_by_year: dict[str, list[tuple[float, str]]] = {}
        overall_max, overall_max_date = None, None
        for row in load_raw_rows(sid):
            tmax_raw = row.get("tmax", "")
            if not tmax_raw:
                continue
            tmax_val = float(tmax_raw)
            date_str = row["date"]
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()

            if date_obj > today:
                errors.append(f"{sid}: Datum in der Zukunft in raw-CSV: {date_str}")
            if tmax_val > PLAUSIBLE_MAX_C or tmax_val < PLAUSIBLE_MIN_C:
                errors.append(f"{sid}: unplausibler tmax={tmax_val} am {date_str} in raw-CSV")

            raw_by_year.setdefault(date_str[:4], []).append((tmax_val, date_str))
            if overall_max is None or tmax_val > overall_max:
                overall_max, overall_max_date = tmax_val, date_str

        for year, entry in series["years"].items():
            annual = entry["annual"]
            year_rows = raw_by_year.get(year, [])
            if not year_rows:
                continue
            csv_max = max(t for t, _ in year_rows)
            csv_hot_days = sum(1 for t, _ in year_rows if t >= 30.0)
            if abs(csv_max - annual["max_temp"]) > 0.05:
                errors.append(f"{sid} {year}: max_temp {annual['max_temp']} != Roh-CSV-Maximum {csv_max}")
            if csv_hot_days != annual["hot_days"]:
                errors.append(f"{sid} {year}: hot_days {annual['hot_days']} != Roh-CSV-Zaehlung {csv_hot_days}")

        if overall_max is not None:
            if abs(overall_max - series["record"]["temp"]) > 0.05:
                errors.append(f"{sid}: record.temp {series['record']['temp']} != Roh-CSV-Gesamtmaximum {overall_max}")
            if overall_max_date != series["record"]["date"]:
                warnings.append(
                    f"{sid}: record.date {series['record']['date']} != erstes Roh-CSV-Maximaldatum "
                    f"{overall_max_date} (evtl. mehrere Tage mit demselben Hoechstwert)"
                )

        if int(s["last_data"][:4]) < s["data_from"]:
            errors.append(f"{sid}: last_data {s['last_data']} liegt vor data_from {s['data_from']}")

        summary = series["analysis"].get("trend_summary")
        if summary:
            warming_total += 1
            if summary["recent"]["avg_hot_days"] > summary["earliest"]["avg_hot_days"]:
                warming_recent += 1

    if warming_total > 0:
        share = warming_recent / warming_total * 100
        print(
            f"Erwaermungssignal: {warming_recent}/{warming_total} Stationen ({share:.1f} %) haben in den "
            f"juengsten 10 Jahren ihrer Reihe im Schnitt mehr heisse Tage als in den aeltesten 10 Jahren."
        )
        if share < 70:
            warnings.append(
                f"Nur {share:.1f} % der Stationen zeigen ein Erwaermungssignal - erwartet waere eine "
                "deutliche Mehrheit, keine zufaellige ~50/50-Verteilung."
            )

    print(f"\n{len(stations)} Stationen geprueft.")
    print(f"{len(errors)} Fehler, {len(warnings)} Warnungen.\n")

    if warnings:
        print("WARNUNGEN:")
        for w in warnings:
            print(f"  - {w}")
        print()

    if errors:
        print("FEHLER:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("Keine kritischen Verstoesse gefunden.")


if __name__ == "__main__":
    main()
