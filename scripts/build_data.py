#!/usr/bin/env python3
"""
Hitze-Check Deutschland — Datenpipeline

Laedt fuer jede Stadt aus STATIONS die Tages-Wetterhistorie ueber Meteostat,
berechnet Jahres- und Sommer-Kennzahlen (heisse Tage, Durchschnitt, Rekord)
und schreibt die Ergebnisse als JSON/CSV nach docs/data/.

Ausfuehren mit:
    python3 scripts/build_data.py
"""

from datetime import datetime
from pathlib import Path
import json
import time

import pandas as pd
from meteostat import Stations, Daily

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Neue Staedte koennen hier einfach ergaenzt werden (id, Anzeigename, lat, lon).
STATIONS = [
    {"id": "wuppertal", "name": "Wuppertal", "lat": 51.26, "lon": 7.18},
    {"id": "hamburg", "name": "Hamburg", "lat": 53.55, "lon": 9.99},
    {"id": "rostock-warnemuende", "name": "Rostock-Warnemünde", "lat": 54.18, "lon": 12.08},
    {"id": "hannover", "name": "Hannover", "lat": 52.37, "lon": 9.74},
    {"id": "berlin", "name": "Berlin", "lat": 52.52, "lon": 13.40},
    {"id": "dresden", "name": "Dresden", "lat": 51.05, "lon": 13.74},
    {"id": "frankfurt-am-main", "name": "Frankfurt am Main", "lat": 50.11, "lon": 8.68},
    {"id": "stuttgart", "name": "Stuttgart", "lat": 48.78, "lon": 9.18},
    {"id": "freiburg", "name": "Freiburg", "lat": 47.99, "lon": 7.85},
    {"id": "muenchen", "name": "München", "lat": 48.14, "lon": 11.58},
]

HOT_DAY_THRESHOLD = 30.0  # °C, Definition "heisser Tag"
SUMMER_MONTHS = (6, 7, 8)  # meteorologischer Sommer
NEARBY_CANDIDATES = 10  # so viele Kandidaten-Stationen pro Stadt pruefen
NEARBY_RADIUS_M = 50_000  # bevorzugter Umkreis (Meter) fuer die Stationswahl
FETCH_RETRIES = 3  # Meteostat-Downloads brechen gelegentlich mit einem Netzwerkfehler ab
FETCH_RETRY_DELAY_S = 5

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "docs" / "data"
SERIES_DIR = DATA_DIR / "series"
RAW_DIR = DATA_DIR / "raw"


def find_best_station(lat: float, lon: float) -> pd.Series:
    """Waehlt unter den naechstgelegenen Meteostat-Stationen die mit der
    laengsten verfuegbaren Tages-Historie (bevorzugt innerhalb NEARBY_RADIUS_M)."""
    candidates = Stations().nearby(lat, lon).fetch(NEARBY_CANDIDATES)
    candidates = candidates[candidates["daily_start"].notna()].copy()
    if candidates.empty:
        raise RuntimeError(f"Keine Station mit Tagesdaten in der Naehe von ({lat}, {lon}) gefunden.")

    candidates["span_days"] = (candidates["daily_end"] - candidates["daily_start"]).dt.days

    nearby = candidates[candidates["distance"] <= NEARBY_RADIUS_M]
    pool = nearby if not nearby.empty else candidates

    best_id = pool["span_days"].idxmax()
    return candidates.loc[best_id]


def fetch_daily_with_retry(meteostat_id: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Laedt Tagesdaten; Meteostat bricht bei einzelnen Jahresdateien gelegentlich mit
    einem transienten Netzwerkfehler ab, daher hier ein paar Versuche mit Pause."""
    last_error = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            return Daily(meteostat_id, start, end).fetch()
        except Exception as exc:  # z. B. urlopen error, abgebrochene Verbindung
            last_error = exc
            if attempt < FETCH_RETRIES:
                print(f"  Netzwerkfehler ({exc}), Versuch {attempt}/{FETCH_RETRIES} - warte {FETCH_RETRY_DELAY_S}s ...")
                time.sleep(FETCH_RETRY_DELAY_S)
    raise last_error


def compute_period_stats(df: pd.DataFrame) -> dict | None:
    """Berechnet hot_days / mean_temp / max_temp(+Datum) fuer einen Zeitausschnitt."""
    if df.empty or df["tmax"].isna().all():
        return None

    hot_days = int((df["tmax"] >= HOT_DAY_THRESHOLD).sum())
    mean_temp = df["tavg"].mean()
    max_temp = df["tmax"].max()
    max_temp_date = df["tmax"].idxmax()

    return {
        "hot_days": hot_days,
        # tavg fehlt bei manchen (oft aelteren) Messungen, auch wenn tmax vorhanden ist.
        # Dann bleibt mean_temp None, statt das ganze Jahr zu verwerfen.
        "mean_temp": None if pd.isna(mean_temp) else round(float(mean_temp), 1),
        "max_temp": round(float(max_temp), 1),
        "max_temp_date": max_temp_date.strftime("%Y-%m-%d"),
    }


def build_station(station: dict) -> None:
    meta = find_best_station(station["lat"], station["lon"])
    meteostat_id = meta.name  # Index der Stations-Tabelle ist die Meteostat-ID
    meteostat_name = meta["name"]

    daily_start = meta["daily_start"].to_pydatetime()
    end = datetime.now()

    print(f"Station {station['name']}: naechste Messstation '{meteostat_name}' ({meteostat_id}), "
          f"Abstand {meta['distance'] / 1000:.1f} km, lade Tagesdaten ab {daily_start.date()} ...")

    df = fetch_daily_with_retry(meteostat_id, daily_start, end)
    df = df[["tavg", "tmax"]].dropna(how="all")

    if df.empty:
        raise RuntimeError(f"Keine Tagesdaten fuer Station {station['id']} erhalten.")

    valid = df.dropna(subset=["tmax"])
    data_from = int(valid.index.min().year)
    last_data = valid.index.max().strftime("%Y-%m-%d")
    print(f"  -> Jahre {data_from}-{valid.index.max().year} geladen, letzter Datenpunkt {last_data}.")

    # Absoluter Rekord ueber die gesamte Historie
    record_temp = valid["tmax"].max()
    record_date = valid["tmax"].idxmax().strftime("%Y-%m-%d")

    years_out = {}
    for year, year_df in df.groupby(df.index.year):
        annual_stats = compute_period_stats(year_df)
        if annual_stats is None:
            continue  # Jahr ohne brauchbare Daten ueberspringen

        summer_df = year_df[year_df.index.month.isin(SUMMER_MONTHS)]
        summer_stats = compute_period_stats(summer_df)

        entry = {"annual": annual_stats}
        if summer_stats is not None:
            entry["summer"] = summer_stats
        years_out[str(int(year))] = entry

    series = {
        "station_id": station["id"],
        "record": {"temp": round(float(record_temp), 1), "date": record_date},
        "years": years_out,
    }

    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    with open(SERIES_DIR / f"{station['id']}.json", "w", encoding="utf-8") as f:
        json.dump(series, f, ensure_ascii=False, indent=2)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_df = valid.reset_index().rename(columns={"time": "date"})
    raw_df["date"] = raw_df["date"].dt.strftime("%Y-%m-%d")
    raw_df[["date", "tmax", "tavg"]].to_csv(RAW_DIR / f"{station['id']}.csv", index=False)

    return {
        "id": station["id"],
        "name": station["name"],
        "lat": station["lat"],
        "lon": station["lon"],
        "meteostat_station_name": meteostat_name,
        "meteostat_station_id": str(meteostat_id),
        "data_from": data_from,
        "last_data": last_data,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stations_out = []

    for station in STATIONS:
        try:
            stations_out.append(build_station(station))
        except Exception as exc:  # eine fehlerhafte Station soll die anderen nicht stoppen
            print(f"FEHLER bei Station {station['id']}: {exc}")

    with open(DATA_DIR / "stations.json", "w", encoding="utf-8") as f:
        json.dump(stations_out, f, ensure_ascii=False, indent=2)

    print(f"\nFertig: {len(stations_out)}/{len(STATIONS)} Stationen erfolgreich verarbeitet.")


if __name__ == "__main__":
    main()
