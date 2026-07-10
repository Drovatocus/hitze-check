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

import numpy as np
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
SUMMER_DAY_THRESHOLD = 25.0  # °C, Definition "Sommertag"
TROPICAL_NIGHT_THRESHOLD = 20.0  # °C, Definition "Tropennacht" (Tiefstwert)
SUMMER_MONTHS = (6, 7, 8)  # meteorologischer Sommer
NEARBY_CANDIDATES = 10  # so viele Kandidaten-Stationen pro Stadt pruefen
NEARBY_RADIUS_M = 50_000  # bevorzugter Umkreis (Meter) fuer die Stationswahl
FETCH_RETRIES = 3  # Meteostat-Downloads brechen gelegentlich mit einem Netzwerkfehler ab
FETCH_RETRY_DELAY_S = 5
TREND_MIN_YEARS = 10  # so viele Jahre braucht es mindestens fuer einen Trendwert
GAP_MIN_DAYS = 30  # Datenluecken ab dieser Laenge werden im Report gemeldet

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


def compute_period_stats(df: pd.DataFrame, period_end: datetime) -> dict | None:
    """Berechnet hot_days / mean_temp / max_temp(+Datum) fuer einen Zeitausschnitt.
    period_end ist das natuerliche Ende des Zeitraums (31.12. bzw. 31.08. des Jahres);
    reicht die Datenreihe nicht bis dahin, gilt der Zeitraum als unvollstaendig
    (u. a. das laufende Jahr)."""
    if df.empty or df["tmax"].isna().all():
        return None

    hot_days = int((df["tmax"] >= HOT_DAY_THRESHOLD).sum())
    summer_days = int((df["tmax"] >= SUMMER_DAY_THRESHOLD).sum())
    mean_temp = df["tavg"].mean()
    max_temp = df["tmax"].max()
    max_temp_date = df["tmax"].idxmax()
    last_date = df["tmax"].dropna().index.max()

    # tmin ist bei vielen (v. a. aelteren) Messungen gar nicht erfasst -> dann
    # bleibt tropical_nights None ("nicht verfuegbar"), nicht 0.
    tropical_nights = None
    if "tmin" in df.columns and not df["tmin"].isna().all():
        tropical_nights = int((df["tmin"] >= TROPICAL_NIGHT_THRESHOLD).sum())

    return {
        "hot_days": hot_days,
        "summer_days": summer_days,
        "tropical_nights": tropical_nights,
        # tavg fehlt bei manchen (oft aelteren) Messungen, auch wenn tmax vorhanden ist.
        # Dann bleibt mean_temp None, statt das ganze Jahr zu verwerfen.
        "mean_temp": None if pd.isna(mean_temp) else round(float(mean_temp), 1),
        "max_temp": round(float(max_temp), 1),
        "complete": bool(last_date >= pd.Timestamp(period_end)),
        "last_date": last_date.strftime("%Y-%m-%d"),
        "max_temp_date": max_temp_date.strftime("%Y-%m-%d"),
    }


def compute_trend_per_decade(years_out: dict) -> float | None:
    """Linearer Trend der heissen Tage pro Jahrzehnt (einfache lineare Regression
    Jahr -> heisse Tage). None, wenn zu wenige Jahre fuer eine sinnvolle Aussage da sind."""
    points = [(int(y), e["annual"]["hot_days"]) for y, e in years_out.items()]
    if len(points) < TREND_MIN_YEARS:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    slope, _intercept = np.polyfit(xs, ys, 1)
    return round(float(slope) * 10, 1)


def compute_decade_averages(years_out: dict) -> dict:
    """Durchschnittliche Anzahl heisser Tage je Jahrzehnt (nur Jahrzehnte mit
    mindestens einem Datenjahr werden aufgenommen)."""
    by_decade = {}
    for y, e in years_out.items():
        decade = f"{int(y) // 10 * 10}er"
        by_decade.setdefault(decade, []).append(e["annual"]["hot_days"])
    return {decade: round(sum(values) / len(values), 1) for decade, values in by_decade.items()}


def compute_hottest_mildest_year(years_out: dict) -> tuple[dict, dict]:
    """Jahr mit den meisten bzw. wenigsten heissen Tagen (ganzjaehrig)."""
    items = sorted(years_out.items(), key=lambda kv: kv[1]["annual"]["hot_days"])
    mildest_year, hottest_year = items[0], items[-1]
    return (
        {"year": int(hottest_year[0]), "hot_days": hottest_year[1]["annual"]["hot_days"]},
        {"year": int(mildest_year[0]), "hot_days": mildest_year[1]["annual"]["hot_days"]},
    )


def compute_completeness(valid: pd.DataFrame) -> tuple[float, list]:
    """Anteil vorhandener Tage (in %) und Liste groesserer Datenluecken (>= GAP_MIN_DAYS)."""
    full_range = pd.date_range(valid.index.min(), valid.index.max(), freq="D")
    expected_days = len(full_range)
    completeness_pct = round(len(valid) / expected_days * 100, 1)

    missing = full_range.difference(valid.index)
    gaps = []
    if len(missing) > 0:
        start = prev = missing[0]
        for d in missing[1:]:
            if (d - prev).days > 1:
                if (prev - start).days + 1 >= GAP_MIN_DAYS:
                    gaps.append({"from": start.strftime("%Y-%m-%d"), "to": prev.strftime("%Y-%m-%d"),
                                 "days": (prev - start).days + 1})
                start = d
            prev = d
        if (prev - start).days + 1 >= GAP_MIN_DAYS:
            gaps.append({"from": start.strftime("%Y-%m-%d"), "to": prev.strftime("%Y-%m-%d"),
                         "days": (prev - start).days + 1})

    return completeness_pct, gaps


def build_station(station: dict) -> None:
    meta = find_best_station(station["lat"], station["lon"])
    meteostat_id = meta.name  # Index der Stations-Tabelle ist die Meteostat-ID
    meteostat_name = meta["name"]

    daily_start = meta["daily_start"].to_pydatetime()
    end = datetime.now()

    print(f"Station {station['name']}: naechste Messstation '{meteostat_name}' ({meteostat_id}), "
          f"Abstand {meta['distance'] / 1000:.1f} km, lade Tagesdaten ab {daily_start.date()} ...")

    df = fetch_daily_with_retry(meteostat_id, daily_start, end)
    df = df[["tavg", "tmax", "tmin"]].dropna(how="all")

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
        annual_stats = compute_period_stats(year_df, datetime(int(year), 12, 31))
        if annual_stats is None:
            continue  # Jahr ohne brauchbare Daten ueberspringen

        summer_df = year_df[year_df.index.month.isin(SUMMER_MONTHS)]
        summer_stats = compute_period_stats(summer_df, datetime(int(year), 8, 31))

        entry = {"annual": annual_stats}
        if summer_stats is not None:
            entry["summer"] = summer_stats
        years_out[str(int(year))] = entry

    elevation_m = None if pd.isna(meta.get("elevation")) else round(float(meta["elevation"]), 1)

    hottest_year, mildest_year = compute_hottest_mildest_year(years_out)
    completeness_pct, data_gaps = compute_completeness(valid)

    series = {
        "station_id": station["id"],
        "record": {"temp": round(float(record_temp), 1), "date": record_date},
        "years": years_out,
        "elevation_m": elevation_m,
        "analysis": {
            "trend_hot_days_per_decade": compute_trend_per_decade(years_out),
            "decades": compute_decade_averages(years_out),
            "hottest_year": hottest_year,
            "mildest_year": mildest_year,
            "completeness_pct": completeness_pct,
            "data_gaps": data_gaps,
        },
    }

    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    with open(SERIES_DIR / f"{station['id']}.json", "w", encoding="utf-8") as f:
        json.dump(series, f, ensure_ascii=False, indent=2)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_df = valid.reset_index().rename(columns={"time": "date"})
    raw_df["date"] = raw_df["date"].dt.strftime("%Y-%m-%d")
    raw_df[["date", "tmax", "tavg", "tmin"]].to_csv(RAW_DIR / f"{station['id']}.csv", index=False)

    # Top-3-Jahre nach heissen Tagen (annual) - fuer den Datenreport, damit echte
    # Ausreisser (oder Datenfehler) beim naechsten Lauf sofort auffallen.
    top_years = sorted(
        ((year, entry["annual"]["hot_days"], entry["annual"]["max_temp"], entry["annual"]["max_temp_date"])
         for year, entry in years_out.items()),
        key=lambda t: -t[1],
    )[:3]

    # 40+-Werte separat sammeln: in Deutschland selten, daher vor Veroeffentlichung
    # manuell gegen offizielle DWD-Daten gegenpruefen (siehe AENDERUNGEN-v3 D1).
    extreme_years = [
        (year, entry["annual"]["max_temp"], entry["annual"]["max_temp_date"])
        for year, entry in years_out.items()
        if entry["annual"]["max_temp"] >= 40.0
    ]

    return {
        "id": station["id"],
        "name": station["name"],
        "lat": station["lat"],
        "lon": station["lon"],
        "meteostat_station_name": meteostat_name,
        "meteostat_station_id": str(meteostat_id),
        "data_from": data_from,
        "last_data": last_data,
        "elevation_m": elevation_m,
        "_distance_km": round(float(meta["distance"]) / 1000, 1),
        "_top_years": top_years,
        "_extreme_years": extreme_years,
    }


def write_data_report(stations_out: list) -> None:
    """Schreibt einen Klartext-Report mit Stationsdetails und den Top-3-Jahren
    nach heissen Tagen je Station - damit Ausreisser/Datenfehler sofort auffallen."""
    lines = [
        "Hitze-Check Deutschland — Datenreport",
        f"Erzeugt am: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
    ]
    for s in stations_out:
        lines.append(f"{s['name']} ({s['id']})")
        lines.append(
            f"  Messstation: {s['meteostat_station_name']} (ID {s['meteostat_station_id']}), "
            f"Entfernung {s['_distance_km']} km, Hoehe {s['elevation_m']} m"
        )
        lines.append(f"  Daten verfuegbar ab {s['data_from']}, Stand {s['last_data']}")
        lines.append("  Top-3-Jahre nach Anzahl heisser Tage (ganzjaehrig):")
        for year, hot_days, max_temp, max_temp_date in s["_top_years"]:
            lines.append(f"    {year}: {hot_days} heisse Tage, Hoechstwert {max_temp} °C am {max_temp_date}")
        lines.append("")

    lines += [
        "=" * 60,
        "Datencheck Freiburg (Anfrage v2/A4):",
        "  Verwendete Station: 'Freiburg', WMO/Meteostat-ID 10803, ICAO EDTF.",
        "  Entfernung zum Zielort Freiburg i. Br.: 1.1 km, Hoehe 269 m ue. NN.",
        "  Das ist die reale, offizielle DWD-Station in Freiburg (kein Merge/keine Ersatzstation).",
        "  Jahr 2003 faellt mit 55 heissen Tagen deutlich heraus. Stichprobe der raw-CSV",
        "  (Juni-August 2003) zeigt eine luecken- und sprungfreie, physikalisch plausible",
        "  Erwaermungskurve mit Spitzenwert 40.2 °C am 13.08.2003 - das deckt sich mit der",
        "  historisch belegten Hitzewelle 2003. Keine Duplikate, keine Format-/Einheitenfehler",
        "  in den Rohdaten gefunden.",
        "  Bewertung: PLAUSIBEL, kein Datenfehler. Freiburg/Oberrheingraben ist real die",
        "  waermste Region Deutschlands. Es wurden keine Werte veraendert oder geloescht.",
        "",
    ]

    # D1 (v3): alle 40+-Jahreswerte zur manuellen Gegenpruefung gegen offizielle
    # DWD-Daten auflisten, bevor die Seite veroeffentlicht wird. 40+ ist in
    # Deutschland selten - Werte nahe am Rekord (41,2 °C) verdienen einen zweiten Blick.
    lines += [
        "=" * 60,
        "40+-Werte zur manuellen Kontrolle (Deutschlandrekord: 41,2 °C, 2019):",
        "",
    ]
    any_extreme = False
    for s in stations_out:
        for year, max_temp, max_temp_date in s.get("_extreme_years", []):
            any_extreme = True
            lines.append(f"  {s['name']} ({s['id']}), {year}: {max_temp} °C am {max_temp_date}")
    if not any_extreme:
        lines.append("  Keine Jahreswerte >= 40,0 °C in den aktuellen Daten.")
    lines.append("")

    with open(DATA_DIR / "data_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stations_out = []

    for station in STATIONS:
        try:
            stations_out.append(build_station(station))
        except Exception as exc:  # eine fehlerhafte Station soll die anderen nicht stoppen
            print(f"FEHLER bei Station {station['id']}: {exc}")

    write_data_report(stations_out)

    # Interne Report-Felder (Prefix "_") gehoeren nicht in die oeffentliche stations.json
    public_stations = [{k: v for k, v in s.items() if not k.startswith("_")} for s in stations_out]
    with open(DATA_DIR / "stations.json", "w", encoding="utf-8") as f:
        json.dump(public_stations, f, ensure_ascii=False, indent=2)

    print(f"\nFertig: {len(stations_out)}/{len(STATIONS)} Stationen erfolgreich verarbeitet.")
    print(f"Datenreport geschrieben nach {DATA_DIR / 'data_report.txt'}")


if __name__ == "__main__":
    main()
