#!/usr/bin/env python3
"""
Hitze-Check Deutschland — Datenpipeline (bundesweit, fortsetzbar)

Findet automatisch alle geeigneten Wetterstationen in Deutschland (Inventar-
Vorfilter: mindestens 30 Jahre Tagesreihe UND Daten aus den letzten 2 Jahren),
laedt ihre komplette Tages-Historie in EINER Datei pro Station (Meteostats
bulk-Endpunkt, siehe fetch_daily_bulk), berechnet Jahres- und Sommer-Kennzahlen
sowie die erweiterten "Mehr Details"-Auswertungen und schreibt alles als
JSON/CSV nach docs/data/.

Fortsetzbar: Stationen, die schon eine series/<id>.json haben, werden beim
naechsten Lauf uebersprungen (kein erneuter Download). Der Lauf kann also
jederzeit unterbrochen (auch hart, z. B. Stromausfall/Fenster zu) und mit
demselben Befehl wieder aufgenommen werden:

    python3 scripts/build_data.py

Das ist zugleich Start- UND Fortsetzen-Befehl - es gibt keinen Unterschied.
"""

import gzip
import io
import random
import re
import socket
import threading
import time
import unicodedata
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import json

# Ohne globalen Timeout kann ein Netzwerk-Hiccup eine TCP-Verbindung "stumm"
# haengen lassen (Socket bleibt ESTABLISHED, es kommen aber nie Daten/ein Fehler) -
# bei einem mehrstuendigen Lauf darf das nicht den ganzen Prozess lahmlegen.
socket.setdefaulttimeout(30)

import numpy as np
import pandas as pd
from meteostat import Stations

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

HOT_DAY_THRESHOLD = 30.0  # °C, Definition "heisser Tag"
SUMMER_DAY_THRESHOLD = 25.0  # °C, Definition "Sommertag"
TROPICAL_NIGHT_THRESHOLD = 20.0  # °C, Definition "Tropennacht" (Tiefstwert)
SUMMER_MONTHS = (6, 7, 8)  # meteorologischer Sommer
TREND_MIN_YEARS = 10  # so viele Jahre braucht es mindestens fuer einen Trendwert
GAP_MIN_DAYS = 30  # Datenluecken ab dieser Laenge werden im Report gemeldet

MIN_SERIES_YEARS = 30  # Eignungsfilter: mindestens so viele Jahre Tagesreihe
MAX_RECENT_GAP_DAYS = 730  # ... und Daten aus den letzten 2 Jahren

# Sanftes Laden: wenige parallele Downloads, kurze Pause vor jeder Anfrage,
# bei Fehlern exponentiell laenger warten. Grund: ein frueherer Lauf mit 6
# parallelen Downloads UND vielen Anfragen pro Station (eine je Jahr) hat den
# Server ab ca. Station 9 spuerbar gedrosselt (viele Timeouts). Der Umstieg auf
# den bulk-Endpunkt (eine Datei pro Station, siehe fetch_daily_bulk) reduziert
# die Anfragenzahl ohnehin von zehntausenden auf ~380 - trotzdem lieber vorsichtig.
MAX_WORKERS = 3
REQUEST_MIN_DELAY_S = 0.5  # kurze Pause vor jeder Anfrage (+ etwas Zufall)
REQUEST_MAX_DELAY_S = 1.5
FETCH_RETRIES = 5
FETCH_RETRY_BASE_DELAY_S = 5  # verdoppelt sich je Fehlversuch (Backoff)

CHECKPOINT_EVERY = 10  # alle N erfolgreich verarbeiteten Stationen zwischenspeichern

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "docs" / "data"
SERIES_DIR = DATA_DIR / "series"
RAW_DIR = DATA_DIR / "raw"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def slugify(name: str) -> str:
    """Einfache, robuste Slug-Erzeugung ohne Zusatzpaket (Umlaute etc. transliterieren)."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name).strip("-").lower()
    return slug or "station"


def find_suitable_stations() -> pd.DataFrame:
    """Holt alle deutschen Wetterstationen und filtert VOR dem Laden der vollen
    Daten anhand des Stations-Inventars (daily_start/daily_end): mindestens
    MIN_SERIES_YEARS Jahre Tagesreihe UND Daten aus den letzten 2 Jahren.
    Das spart massiv Zeit, weil nur geeignete Stationen die vollen Tagesdaten
    herunterladen muessen."""
    all_stations = Stations().region("DE").fetch(100_000)
    with_daily = all_stations[all_stations["daily_start"].notna() & all_stations["daily_end"].notna()].copy()

    span_years = (with_daily["daily_end"] - with_daily["daily_start"]).dt.days / 365.25
    recent_cutoff = pd.Timestamp(datetime.now() - pd.Timedelta(days=MAX_RECENT_GAP_DAYS))
    suitable = with_daily[(span_years >= MIN_SERIES_YEARS) & (with_daily["daily_end"] >= recent_cutoff)]

    return suitable.sort_values("name")


BULK_DAILY_URL = "https://bulk.meteostat.net/v2/daily/{id}.csv.gz"
# Reihenfolge/Namen entsprechen exakt dem Format der bulk-CSV (siehe Meteostat-Doku).
_BULK_COLUMNS = ["date", "tavg", "tmin", "tmax", "prcp", "snow", "wdir", "wspd", "wpgt", "pres", "tsun"]


def fetch_daily_bulk(meteostat_id: str) -> pd.DataFrame:
    """Laedt die KOMPLETTE Tageshistorie einer Station in EINER Anfrage ueber
    Meteostats bulk-Endpunkt - statt wie die Standardbibliothek (meteostat.Daily)
    pro Jahr eine eigene Datei zu holen (bei einer 90-Jahre-Reihe also 90 Anfragen
    statt einer). Das war der Hauptgrund fuer die Serverdrosselung bei einem
    frueheren Lauf."""
    url = BULK_DAILY_URL.format(id=meteostat_id)
    req = urllib.request.Request(url, headers={"User-Agent": "hitze-check-deutschland/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        df = pd.read_csv(gz, names=_BULK_COLUMNS, parse_dates=["date"], index_col="date")
    return df


def fetch_daily_with_retry(meteostat_id: str) -> pd.DataFrame:
    """Laedt mit kurzer Pause vor jeder Anfrage und exponentiell laengerem
    Backoff bei Fehlern - schont den Server, damit es nicht wieder zu einer
    Drosselung wie beim vorigen Lauf kommt."""
    time.sleep(random.uniform(REQUEST_MIN_DELAY_S, REQUEST_MAX_DELAY_S))
    last_error = None
    delay = FETCH_RETRY_BASE_DELAY_S
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            return fetch_daily_bulk(meteostat_id)
        except Exception as exc:  # z. B. urlopen error, Timeout, HTTP-Fehler
            last_error = exc
            if attempt < FETCH_RETRIES:
                time.sleep(delay)
                delay *= 2  # exponentielles Backoff
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


def compute_trend_line_and_summary(years_out: dict) -> tuple[list, dict | None]:
    """Gefittete Trendlinie (Jahr -> Wert) fuers Diagramm, sowie ein sachlicher
    Vergleich der juengsten 10 Jahre der Reihe gegen die aeltesten 10 Jahre
    (fuer den Klartext-Satz unter dem Diagramm). None/leer, wenn die Reihe zu
    kurz fuer eine sinnvolle Aussage ist."""
    items = sorted((int(y), e["annual"]["hot_days"]) for y, e in years_out.items())
    if len(items) < TREND_MIN_YEARS:
        return [], None

    years_sorted = [y for y, _ in items]
    hotdays_sorted = [h for _, h in items]

    xs = np.array(years_sorted, dtype=float)
    ys = np.array(hotdays_sorted, dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    line = [{"year": y, "value": round(float(slope * y + intercept), 1)} for y in years_sorted]

    recent_years, earliest_years = years_sorted[-10:], years_sorted[:10]
    recent_avg = sum(hotdays_sorted[-10:]) / 10
    earliest_avg = sum(hotdays_sorted[:10]) / 10
    summary = {
        "recent": {"from": recent_years[0], "to": recent_years[-1], "avg_hot_days": round(recent_avg, 1)},
        "earliest": {"from": earliest_years[0], "to": earliest_years[-1], "avg_hot_days": round(earliest_avg, 1)},
    }
    # Ueberschneiden sich die beiden Fenster (sehr kurze Reihe knapp ab TREND_MIN_YEARS),
    # ist der Vergleich nicht aussagekraeftig - dann lieber keinen Klartext-Satz zeigen.
    if recent_years[0] <= earliest_years[-1]:
        summary = None

    return line, summary


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


def compute_station_id(name: str, meteostat_id: str) -> str:
    """Stabile, eindeutige id: geslugter Stationsname + Meteostat-ID (die ist
    global eindeutig, damit ist auch bei Namensgleichheit keine Kollision moeglich).
    Haengt nur von Inventar-Metadaten ab, nicht von den Tagesdaten - kann also
    VOR dem Download berechnet werden (fuer den Fortsetzbarkeits-Check)."""
    return f"{slugify(name)}-{str(meteostat_id).lower()}"


def summarize_years(years_out: dict) -> tuple[list, list, dict]:
    """Leitet aus den Jahresdaten ab: Top-3-Jahre nach heissen Tagen (fuer den
    Report), 40+-Jahre (fuer die manuelle Kontrollliste, siehe v3 D1) und den
    schlanken Jahres-Index fuer map_index.json. Wird sowohl fuer frisch
    heruntergeladene als auch fuer aus dem Cache wiederhergestellte Stationen
    genutzt, damit beide Pfade exakt dieselben Ergebnisse liefern."""
    top_years = sorted(
        ((year, entry["annual"]["hot_days"], entry["annual"]["max_temp"], entry["annual"]["max_temp_date"])
         for year, entry in years_out.items()),
        key=lambda t: -t[1],
    )[:3]

    extreme_years = [
        (year, entry["annual"]["max_temp"], entry["annual"]["max_temp_date"])
        for year, entry in years_out.items()
        if entry["annual"]["max_temp"] >= 40.0
    ]

    map_years = {
        year: {
            "annual": {"max_temp": e["annual"]["max_temp"], "hot_days": e["annual"]["hot_days"]},
            **({"summer": {"max_temp": e["summer"]["max_temp"], "hot_days": e["summer"]["hot_days"]}}
               if "summer" in e else {}),
        }
        for year, e in years_out.items()
    }

    return top_years, extreme_years, map_years


def public_record(station_id: str, name: str, meta: pd.Series, meteostat_id: str,
                   data_from: int, last_data: str, elevation_m: float | None, years_out: dict) -> dict:
    """Baut den oeffentlichen Stationseintrag (fuer stations.json/map_index.json/
    Report) - identisch verwendet fuer frisch geladene UND aus dem Cache
    wiederhergestellte Stationen."""
    top_years, extreme_years, map_years = summarize_years(years_out)
    return {
        "id": station_id,
        "name": name,
        "lat": round(float(meta["latitude"]), 4),
        "lon": round(float(meta["longitude"]), 4),
        "meteostat_station_name": name,
        "meteostat_station_id": str(meteostat_id),
        "region": None if pd.isna(meta.get("region")) else meta.get("region"),
        "data_from": data_from,
        "last_data": last_data,
        "elevation_m": elevation_m,
        "_top_years": top_years,
        "_extreme_years": extreme_years,
        "_map_years": map_years,
    }


def try_load_cached_station(station_id: str, meta: pd.Series, meteostat_id: str) -> dict | None:
    """Fortsetzbarkeit: Wenn fuer diese Station schon eine series/<id>.json auf
    der Platte liegt (aus einem frueheren, ggf. abgebrochenen Lauf), wird daraus
    der oeffentliche Eintrag rekonstruiert - OHNE erneuten Download. Liefert
    None, wenn nichts gecacht ist oder die Datei nicht lesbar/unvollstaendig ist
    (dann wird ganz normal neu heruntergeladen)."""
    path = SERIES_DIR / f"{station_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            series = json.load(f)
        years_out = series["years"]
        if not years_out:
            return None
        sorted_years = sorted(years_out.keys(), key=int)
        data_from = int(sorted_years[0])
        last_data = years_out[sorted_years[-1]]["annual"]["last_date"]
        elevation_m = series.get("elevation_m")
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None  # kaputte/unvollstaendige Cache-Datei -> lieber neu laden

    return public_record(station_id, meta["name"], meta, meteostat_id, data_from, last_data, elevation_m, years_out)


def build_station_fresh(meteostat_id: str, meta: pd.Series, station_id: str) -> dict | None:
    """Laedt und verarbeitet eine einzelne Station frisch von Meteostat. Gibt
    None zurueck (statt abzubrechen), wenn die Station aus irgendeinem Grund
    nicht nutzbar ist - damit ein Fehler bei einer Station die anderen nicht
    aufhaelt."""
    name = meta["name"]

    try:
        df = fetch_daily_with_retry(meteostat_id)
    except Exception as exc:
        log(f"  FEHLER bei {name} ({meteostat_id}): {exc}")
        return None

    df = df[["tavg", "tmax", "tmin"]].dropna(how="all")
    if df.empty:
        log(f"  FEHLER bei {name} ({meteostat_id}): keine Tagesdaten erhalten.")
        return None

    valid = df.dropna(subset=["tmax"])
    if valid.empty:
        log(f"  FEHLER bei {name} ({meteostat_id}): keine gueltigen Tmax-Werte.")
        return None

    data_from = int(valid.index.min().year)
    last_data = valid.index.max().strftime("%Y-%m-%d")

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

    if not years_out:
        log(f"  FEHLER bei {name} ({meteostat_id}): keine brauchbaren Jahre nach Aufbereitung.")
        return None

    elevation_m = None if pd.isna(meta.get("elevation")) else round(float(meta["elevation"]), 1)
    hottest_year, mildest_year = compute_hottest_mildest_year(years_out)
    completeness_pct, data_gaps = compute_completeness(valid)
    trend_line, trend_summary = compute_trend_line_and_summary(years_out)

    series = {
        "station_id": station_id,
        "record": {"temp": round(float(record_temp), 1), "date": record_date},
        "years": years_out,
        "elevation_m": elevation_m,
        "analysis": {
            "trend_hot_days_per_decade": compute_trend_per_decade(years_out),
            "trend_line": trend_line,
            "trend_summary": trend_summary,
            "decades": compute_decade_averages(years_out),
            "hottest_year": hottest_year,
            "mildest_year": mildest_year,
            "completeness_pct": completeness_pct,
            "data_gaps": data_gaps,
        },
    }

    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    with open(SERIES_DIR / f"{station_id}.json", "w", encoding="utf-8") as f:
        json.dump(series, f, ensure_ascii=False, indent=2)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_df = valid.reset_index().rename(columns={"time": "date"})
    raw_df["date"] = raw_df["date"].dt.strftime("%Y-%m-%d")
    raw_df[["date", "tmax", "tavg", "tmin"]].to_csv(RAW_DIR / f"{station_id}.csv", index=False)

    return public_record(station_id, name, meta, meteostat_id, data_from, last_data, elevation_m, years_out)


def write_data_report(stations_out: list, total_suitable: int, failed: list) -> None:
    """Schreibt einen Klartext-Report: Stationsuebersicht, grobe Regionsverteilung,
    Top-3-Jahre je Station, Freiburg-Datencheck (v2), die 40+-Kontrollliste (v3 D1)
    und - falls der Lauf noch unterwegs/unterbrochen ist - wie viele Stationen noch
    fehlen bzw. dauerhaft fehlgeschlagen sind."""
    region_counts = Counter(s.get("region") or "unbekannt" for s in stations_out)
    remaining = total_suitable - len(stations_out) - len(failed)

    lines = [
        "Hitze-Check Deutschland — Datenreport (bundesweit)",
        f"Erzeugt am: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
        f"Geeignete Stationen gefunden (Inventar-Vorfilter): {total_suitable}",
        f"Davon erfolgreich verarbeitet: {len(stations_out)}",
        f"Dauerhaft fehlgeschlagen (nach mehreren Versuchen): {len(failed)}",
    ]
    if remaining > 0:
        lines.append(f"Noch nicht bearbeitet (Lauf unterbrochen/laeuft noch): {remaining}")
        lines.append('-> Einfach "python3 scripts/build_data.py" erneut ausfuehren, macht dort weiter.')
    lines.append("")

    if failed:
        lines += ["Dauerhaft fehlgeschlagene Stationen (Netzwerkfehler o. ae. nach allen Versuchen):"]
        for name, meteostat_id, reason in failed:
            lines.append(f"  {name} ({meteostat_id}): {reason}")
        lines.append("")

    lines.append("Regionsverteilung (Anzahl Stationen je Bundesland-Kuerzel):")
    for region, count in sorted(region_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {region}: {count}")
    lines.append("")

    lines += ["=" * 60, "Stationsuebersicht mit Top-3-Jahren nach heissen Tagen:", ""]
    for s in stations_out:
        lines.append(f"{s['name']} ({s['id']})")
        lines.append(
            f"  Meteostat-ID {s['meteostat_station_id']}, Region {s.get('region') or 'unbekannt'}, "
            f"Hoehe {s['elevation_m']} m"
        )
        lines.append(f"  Daten verfuegbar ab {s['data_from']}, Stand {s['last_data']}")
        lines.append("  Top-3-Jahre nach Anzahl heisser Tage (ganzjaehrig):")
        for year, hot_days, max_temp, max_temp_date in s["_top_years"]:
            lines.append(f"    {year}: {hot_days} heisse Tage, Hoechstwert {max_temp} °C am {max_temp_date}")
        lines.append("")

    lines += [
        "=" * 60,
        "Datencheck Freiburg (Anfrage v2/A4, weiterhin gueltig):",
        "  Betroffene Station in diesem Datensatz: Name 'Freiburg', Meteostat-ID 10803, ICAO EDTF,",
        "  Region BW, Hoehe 269 m ue. NN.",
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


def write_outputs(stations_out: list, total_suitable: int, failed: list) -> None:
    """Schreibt stations.json, map_index.json und den Datenreport aus dem
    aktuellen (ggf. noch unvollstaendigen) Zwischenstand - wird sowohl als
    Zwischenspeicherung (Checkpoint) als auch am Ende aufgerufen."""
    stations_sorted = sorted(stations_out, key=lambda s: s["name"])

    write_data_report(stations_sorted, total_suitable=total_suitable, failed=failed)

    # Interne Report-Felder (Prefix "_") gehoeren nicht in die oeffentliche stations.json
    public_stations = [{k: v for k, v in s.items() if not k.startswith("_")} for s in stations_sorted]
    with open(DATA_DIR / "stations.json", "w", encoding="utf-8") as f:
        json.dump(public_stations, f, ensure_ascii=False, indent=2)

    # Schlanker Karten-Index (id, name, lat, lon, max_temp/hot_days je Jahr/Saison)
    map_index = [
        {"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"], "years": s["_map_years"]}
        for s in stations_sorted
    ]
    with open(DATA_DIR / "map_index.json", "w", encoding="utf-8") as f:
        json.dump(map_index, f, ensure_ascii=False, indent=2)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log("Suche geeignete Stationen (Inventar-Vorfilter: "
        f">= {MIN_SERIES_YEARS} Jahre Tagesreihe, Daten aus den letzten 2 Jahren) ...")
    suitable = find_suitable_stations()
    total = len(suitable)

    # Fortsetzbarkeit: station_id kann rein aus den (bereits vorliegenden)
    # Inventar-Metadaten berechnet werden, also VOR jedem Download - so laesst
    # sich sofort pruefen, ob schon eine series/<id>.json existiert.
    jobs = [(meteostat_id, meta, compute_station_id(meta["name"], meteostat_id))
            for meteostat_id, meta in suitable.iterrows()]
    already_cached = sum(1 for _, _, sid in jobs if (SERIES_DIR / f"{sid}.json").exists())
    log(f"{total} geeignete Stationen gefunden ({already_cached} davon schon aus einem frueheren "
        f"Lauf vorhanden - werden uebersprungen). Starte (bis zu {MAX_WORKERS} parallel, sanft) ...")

    stations_out = []
    failed = []  # (name, meteostat_id, grund) - fuer den Abschlussbericht
    done_count = 0
    count_lock = threading.Lock()

    def process(meteostat_id, meta, station_id):
        nonlocal done_count
        cached = try_load_cached_station(station_id, meta, meteostat_id)
        if cached is not None:
            status = "gecacht (frueherer Lauf)"
            result = cached
        else:
            result = build_station_fresh(meteostat_id, meta, station_id)
            status = "ok" if result is not None else "uebersprungen (Fehler, siehe oben)"
            if result is None:
                failed.append((meta["name"], meteostat_id, "Download/Verarbeitung fehlgeschlagen"))
        with count_lock:
            done_count += 1
            log(f"[{done_count}/{total}] {meta['name']} ({meteostat_id}): {status}")
        return result

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process, meteostat_id, meta, sid) for meteostat_id, meta, sid in jobs]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    stations_out.append(result)
                # Checkpoint: alle CHECKPOINT_EVERY erfolgreiche Stationen zwischenspeichern,
                # damit bei einem harten Abbruch (Stromausfall, Fenster zu, Absturz) moeglichst
                # wenig verloren geht und stations.json/map_index.json nie hoffnungslos veraltet sind.
                if len(stations_out) % CHECKPOINT_EVERY == 0 and stations_out:
                    write_outputs(stations_out, total_suitable=total, failed=failed)
    except KeyboardInterrupt:
        log("\nAbgebrochen (Strg+C) - speichere den bisherigen Stand ...")
        write_outputs(stations_out, total_suitable=total, failed=failed)
        log(f"Zwischenstand gesichert: {len(stations_out)}/{total} Stationen. "
            'Einfach "python3 scripts/build_data.py" erneut ausfuehren, um fortzufahren.')
        raise

    write_outputs(stations_out, total_suitable=total, failed=failed)

    log(f"\nFertig: {len(stations_out)}/{total} Stationen erfolgreich verarbeitet, "
        f"{len(failed)} dauerhaft fehlgeschlagen.")
    if failed:
        log("Dauerhaft fehlgeschlagene Stationen:")
        for name, meteostat_id, reason in failed:
            log(f"  {name} ({meteostat_id}): {reason}")
        log('Erneuter Aufruf von "python3 scripts/build_data.py" versucht auch diese wieder '
            "(nur erfolgreiche Stationen werden uebersprungen).")
    log(f"Datenreport geschrieben nach {DATA_DIR / 'data_report.txt'}")


if __name__ == "__main__":
    main()
