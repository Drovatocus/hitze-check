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

import calendar
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

# Referenzperiode fuer die Abweichungsberechnung des laufenden Jahres (offizielle
# Klimanormalperiode der WMO/DWD) - siehe compute_current_year_anomaly().
BASELINE_START_YEAR = 1991
BASELINE_END_YEAR = 2020

# Einmal pro Lauf bestimmt (nicht pro Station), damit alle Stationen exakt denselben
# Stichtag/dieselbe Definition von "laufendes Jahr" verwenden.
RUN_TIMESTAMP = datetime.now()
RUNNING_YEAR = RUN_TIMESTAMP.year
LAST_COMPLETE_YEAR = RUNNING_YEAR - 1
_RUNNING_YEAR_LENGTH = 366 if calendar.isleap(RUNNING_YEAR) else 365

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


SANITY_MAX_SPREAD_C = 20.0  # tmax darf an einem Tag nicht mehr als so viel ueber tavg liegen
_sanity_lock = threading.Lock()
_sanity_log: list = []  # (station_id, Datum, tmax, tavg, tmin) - fuer den Report

# Raeumliche Plausibilitaetspruefung (siehe find_spatial_outliers) - bewusst GETRENNT
# von SANITY_MAX_SPREAD_C und NICHT als globale Temperaturobergrenze umgesetzt: ein
# hoher Wert kann durchaus real sein (siehe deutscher Rekord 41,7 °C, 28.06.2026),
# verdaechtig ist er nur, wenn ihn an einem Tag mit guter Stationsdichte KEINE andere
# Station auch nur annaehernd stuetzt (siehe Chieming, 07.08.1991: 41,3 °C, naechst-
# hoechster Wert bundesweit an dem Tag nur 36,4 °C).
#
# WICHTIG (bei der ersten Version dieser Pruefung selbst gefunden): wird JEDER
# Wert ab 25 °C auf raeumliche Stuetzung geprueft, markiert dieselbe Pruefung auch
# mehrfach Freiburg (u. a. 1891, 35,7-36,0 °C) und Worms (u. a. 1995, 35,5 °C) als
# "Ausreisser" - beides bekannte, real waermere Regionen (Freiburg/Oberrheingraben
# war bereits in einem frueheren Datencheck als plausibel bestaetigt, siehe unten
# im Report), keine Fehler. Eine "sehr warme Region ist an einem warmen Tag noch
# waermer als der Rest" ist normal und kein Fehlersignal - deshalb wird nur
# GEFLAGGT, wenn der Wert selbst in die Naehe des nationalen Rekords kommt
# (SPATIAL_CHECK_FLAG_MIN_C), wie bei Chieming. Der niedrigere
# SPATIAL_CHECK_CANDIDATE_MIN_C bestimmt nur, welche Werte ueberhaupt als
# moegliche STUETZUNG/Vergleichswert eines anderen Tages einfliessen (muss
# niedriger sein, sonst wuerde z. B. Muehlackers echte 36,4 °C an Chiemings Tag
# nicht als Vergleichswert zaehlen, weil sie selbst unter der Flag-Schwelle liegt).
SPATIAL_OUTLIER_MARGIN_C = 3.5  # so viel Grad ueber dem naechsthoechsten Wert aller anderen Stationen gilt als verdaechtig
SPATIAL_CHECK_MIN_STATIONS = 20  # so viele andere Stationen brauchen an dem Tag mindestens einen Vergleichswert (>= SPATIAL_CHECK_CANDIDATE_MIN_C)
SPATIAL_CHECK_CANDIDATE_MIN_C = 25.0  # Aufnahmeschwelle in den Tages-Vergleichspool (Performance + Fokus auf warme Tage)
SPATIAL_CHECK_FLAG_MIN_C = 38.0  # nur Werte AB DIESER Hoehe werden ueberhaupt als moeglicher Ausreisser geflaggt (nahe am nationalen Rekord)


def apply_sanity_filter(df: pd.DataFrame, station_id: str) -> pd.DataFrame:
    """Verwirft physikalisch unplausible Tageswerte, BEVOR sie in irgendeine
    Berechnung einfliessen - z. B. tmax=50,0 °C bei tavg=10,6 °C (Hohn, 25.04.1980,
    ein echter, bei einer manuellen 40+-Kontrolle entdeckter Digitalisierungsfehler
    in Meteostats Quelldaten - kein Fehler in dieser Pipeline, siehe data_report.txt).
    Ein Tag gilt als unplausibel, wenn tmax weit ueber tavg liegt oder tavg
    ausserhalb von [tmin, tmax] faellt - das kommt bei echten Messungen praktisch
    nie vor (selbst am deutschen Rekordtag betrug der Abstand tmax-tavg nur ca.
    10 °C), aber die gefundenen Fehler zeigen 20-48 °C Abstand. Betroffene Tage
    werden komplett verworfen (alle drei Werte auf NaN), nicht nur der auffaellige
    Wert - wenn ein Feld offensichtlich falsch ist, ist der ganze Tag nicht
    vertrauenswuerdig."""
    suspect = (
        ((df["tmax"] - df["tavg"]) > SANITY_MAX_SPREAD_C)
        | (df["tavg"] < df["tmin"] - 2)
        | (df["tavg"] > df["tmax"] + 2)
    )
    if not suspect.any():
        return df

    df = df.copy()
    with _sanity_lock:
        for idx, row in df[suspect].iterrows():
            _sanity_log.append((station_id, idx.strftime("%Y-%m-%d"), row["tmax"], row["tavg"], row["tmin"]))
    df.loc[suspect, ["tavg", "tmin", "tmax"]] = float("nan")
    return df


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


def compute_years_out(df: pd.DataFrame) -> dict:
    """Baut aus der Tagesreihe einer Station die annual/summer-Kennzahlen je Jahr
    (siehe compute_period_stats) - als eigene, reine Funktion ausgelagert (unabhaengig
    von Download/Dateisystem), damit sie sich mit erfundenen Testdaten pruefen laesst."""
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
    return years_out


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


# Beobachtete Datenstaende (letztes Tagesdatum MIT Daten im laufenden Jahr) je
# Station - daraus leitet write_meta() den GLOBALEN Datenstand fuer Banner/
# meta.json ab. Bewusst NICHT das heutige Kalenderdatum: Meteostats Archivdaten
# hinken dem echten "heute" unterschiedlich stark hinterher (siehe
# compute_current_year_anomaly fuer denselben Grund je Station).
_running_year_last_dates_lock = threading.Lock()
_running_year_last_dates: list = []


def compute_current_year_anomaly(df: pd.DataFrame, data_from: int,
                                  running_year_last_date: "pd.Timestamp | None") -> float | None:
    """Temperatur-Abweichung des laufenden Jahres (1. Januar bis zum Datenstand
    DIESER Station im laufenden Jahr) gegenueber dem GLEICHEN Kalenderfenster in
    der Referenzperiode 1991-2020 - Tag-des-Jahres-basiert, damit Schaltjahre
    keine Rolle spielen. Verhindert, dass ein erst teilweise vergangenes Jahr
    (zwangslaeufig weniger heisse Tage als ein volles Jahr) faelschlich als
    "kuehl" erscheint: verglichen wird immer nur derselbe Zeitabschnitt.

    Der Stichtag wird bewusst PRO STATION aus deren eigenen Daten abgeleitet
    (nicht ein global einheitliches "heute") - Stationen werden unterschiedlich
    schnell aktualisiert. Ein einheitlicher globaler Stichtag wuerde sonst z. B.
    eine Station mit Datenstand Maerz gegen die Referenzperiode bis Juli
    vergleichen (asymmetrisches Fenster: Winterwerte vs. Halbjahresmittel inkl.
    Sommer) und eine stark verzerrte, falsche Abweichung liefern.

    None, wenn die Stationsreihe nicht bis zum Beginn der Referenzperiode
    zurueckreicht, es im laufenden Jahr noch gar keine Daten dieser Station gibt,
    oder in einem der beiden Fenster keine tavg-Werte vorliegen."""
    if data_from > BASELINE_START_YEAR or running_year_last_date is None:
        return None
    as_of_doy = running_year_last_date.dayofyear

    running_window = df[(df.index.year == running_year_last_date.year) & (df.index.dayofyear <= as_of_doy)]
    running_mean = running_window["tavg"].mean()
    if pd.isna(running_mean):
        return None

    baseline_window = df[
        (df.index.year >= BASELINE_START_YEAR)
        & (df.index.year <= BASELINE_END_YEAR)
        & (df.index.dayofyear <= as_of_doy)
    ]
    baseline_mean = baseline_window["tavg"].mean()
    if pd.isna(baseline_mean):
        return None

    return round(float(running_mean - baseline_mean), 1)


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
                   data_from: int, last_data: str, elevation_m: float | None, years_out: dict,
                   current_year_anomaly: float | None) -> dict:
    """Baut den oeffentlichen Stationseintrag (fuer stations.json/map_index.json/Report)."""
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
        "current_year_anomaly": current_year_anomaly,
        "_top_years": top_years,
        "_extreme_years": extreme_years,
        "_map_years": map_years,
    }


def build_station(meteostat_id: str, meta: pd.Series, station_id: str) -> dict | None:
    """Laedt und verarbeitet eine einzelne Station. Gibt None zurueck (statt
    abzubrechen), wenn die Station aus irgendeinem Grund nicht nutzbar ist -
    damit ein Fehler bei einer Station die anderen nicht aufhaelt.

    Fortsetzbarkeit/Effizienz: Liegt schon eine raw/<id>.csv von einem frueheren
    Lauf vor, wird NICHT erneut heruntergeladen, sondern direkt von der Platte
    gelesen (Sanity-Filter wurde darauf schon angewendet, bevor sie geschrieben
    wurde). So bleibt ein erneuter Lauf schnell UND wendet trotzdem automatisch
    jede Pipeline-Aenderung (z. B. eine neue Kennzahl) auf alle Stationen an,
    ohne dass die Rohdaten-Caches geloescht werden muessten."""
    name = meta["name"]
    raw_path = RAW_DIR / f"{station_id}.csv"
    raw_cached = raw_path.exists()

    if raw_cached:
        df = pd.read_csv(raw_path, parse_dates=["date"], index_col="date")[["tavg", "tmax", "tmin"]]
    else:
        try:
            df = fetch_daily_with_retry(meteostat_id)
        except Exception as exc:
            log(f"  FEHLER bei {name} ({meteostat_id}): {exc}")
            return None

        df = df[["tavg", "tmax", "tmin"]].dropna(how="all")
        if df.empty:
            log(f"  FEHLER bei {name} ({meteostat_id}): keine Tagesdaten erhalten.")
            return None

        df = apply_sanity_filter(df, station_id)

    valid = df.dropna(subset=["tmax"])
    if valid.empty:
        log(f"  FEHLER bei {name} ({meteostat_id}): keine gueltigen Tmax-Werte.")
        return None

    data_from = int(valid.index.min().year)
    last_data = valid.index.max().strftime("%Y-%m-%d")

    record_temp = valid["tmax"].max()
    record_date = valid["tmax"].idxmax().strftime("%Y-%m-%d")

    years_out = compute_years_out(df)
    if not years_out:
        log(f"  FEHLER bei {name} ({meteostat_id}): keine brauchbaren Jahre nach Aufbereitung.")
        return None

    elevation_m = None if pd.isna(meta.get("elevation")) else round(float(meta["elevation"]), 1)
    hottest_year, mildest_year = compute_hottest_mildest_year(years_out)
    completeness_pct, data_gaps = compute_completeness(valid)
    trend_line, trend_summary = compute_trend_line_and_summary(years_out)

    running_year_entry = years_out.get(str(RUNNING_YEAR))
    running_year_last_date = (
        pd.Timestamp(running_year_entry["annual"]["last_date"]) if running_year_entry else None
    )
    if running_year_last_date is not None:
        with _running_year_last_dates_lock:
            _running_year_last_dates.append(running_year_last_date)
    current_year_anomaly = compute_current_year_anomaly(df, data_from, running_year_last_date)

    series = {
        "station_id": station_id,
        "record": {"temp": round(float(record_temp), 1), "date": record_date},
        "years": years_out,
        "elevation_m": elevation_m,
        "current_year_anomaly": current_year_anomaly,
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

    if not raw_cached:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        raw_df = valid.reset_index().rename(columns={"time": "date"})
        raw_df["date"] = raw_df["date"].dt.strftime("%Y-%m-%d")
        raw_df[["date", "tmax", "tavg", "tmin"]].to_csv(raw_path, index=False)

    return public_record(station_id, name, meta, meteostat_id, data_from, last_data, elevation_m, years_out,
                          current_year_anomaly)


def compute_running_year_meta() -> dict:
    """Leitet aus den bisher beobachteten Datenstaenden des laufenden Jahres
    (ueber alle bereits verarbeiteten Stationen, siehe _running_year_last_dates)
    den GLOBALEN Datenstand fuer Banner/meta.json ab - das juengste tatsaechlich
    beobachtete Datum, NICHT das heutige Kalenderdatum (Meteostats Archivdaten
    hinken dem echten "heute" unterschiedlich stark hinterher)."""
    with _running_year_last_dates_lock:
        dates = list(_running_year_last_dates)
    overall_data_stand = max(dates) if dates else None
    day_of_year = overall_data_stand.dayofyear if overall_data_stand is not None else 0
    coverage_pct = round(day_of_year / _RUNNING_YEAR_LENGTH * 100, 1)
    data_stand_str = (
        overall_data_stand.strftime("%Y-%m-%d") if overall_data_stand is not None else f"{RUNNING_YEAR}-01-01"
    )
    return {
        "running_year": RUNNING_YEAR,
        "data_stand": data_stand_str,
        "last_complete_year": LAST_COMPLETE_YEAR,
        "running_year_coverage_pct": coverage_pct,
        "baseline_start_year": BASELINE_START_YEAR,
        "baseline_end_year": BASELINE_END_YEAR,
    }


def write_data_report(stations_out: list, total_suitable: int, failed: list) -> None:
    """Schreibt einen Klartext-Report: Stationsuebersicht, grobe Regionsverteilung,
    Top-3-Jahre je Station, Freiburg-Datencheck (v2), die 40+-Kontrollliste (v3 D1)
    und - falls der Lauf noch unterwegs/unterbrochen ist - wie viele Stationen noch
    fehlen bzw. dauerhaft fehlgeschlagen sind."""
    region_counts = Counter(s.get("region") or "unbekannt" for s in stations_out)
    remaining = total_suitable - len(stations_out) - len(failed)
    run_meta = compute_running_year_meta()

    lines = [
        "Hitze-Check Deutschland — Datenreport (bundesweit)",
        f"Erzeugt am: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Laufendes Jahr: {RUNNING_YEAR} (Datenstand {run_meta['data_stand']}, "
        f"{run_meta['running_year_coverage_pct']} % des Jahres erfasst, jeweils juengstes "
        f"beobachtetes Datum ueber alle Stationen) - Startansicht zeigt stattdessen "
        f"das letzte vollstaendige Jahr {LAST_COMPLETE_YEAR}.",
        f"Referenzperiode fuer die Abweichungsberechnung des laufenden Jahres: "
        f"{BASELINE_START_YEAR}-{BASELINE_END_YEAR}.",
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
        "Datencheck Chieming (Pre-Launch-Check, Abschluss-vor-Launch Schritt 1):",
        "  Chieming zeigte 41,3 °C am 07.08.1991 - das haette 28 Jahre lang der deutsche",
        "  Allzeitrekord sein muessen, ist aber historisch nirgends belegt. Bundesweiter",
        "  Vergleich fuer genau diesen Tag: naechsthoechster Wert ueberall sonst 36,4 °C",
        "  (Muehlacker) - kein Nachbarstation stuetzt den Chieming-Wert auch nur annaehernd.",
        "  Bewertung: unplausibel, sehr wahrscheinlich ein Mess-/Digitalisierungsfehler in",
        "  Meteostats Quelldaten (kein Fehler dieser Pipeline). Ausgeschlossen ueber die",
        "  raeumliche Plausibilitaetspruefung (siehe find_spatial_outliers, NICHT ueber einen",
        "  verschaerften globalen Grenzwert - das haette auch echte 41+-Werte wie den",
        "  neuen deutschen Rekord vom 28.06.2026 faelschlich herausgefiltert). Siehe",
        "  docs/data/ausgeschlossene_werte.txt fuer alle so ausgeschlossenen Tageswerte.",
        "",
    ]

    # D1 (v3): alle 40+-Jahreswerte zur manuellen Gegenpruefung gegen offizielle
    # DWD-Daten auflisten, bevor die Seite veroeffentlicht wird. 40+ ist in
    # Deutschland selten - Werte nahe am Rekord verdienen einen zweiten Blick.
    # Rekord seit der historischen Hitzewelle Ende Juni 2026: 41,7 °C (28.06.2026,
    # Neissemuende-Coschen/Brandenburg, laut DWD vorlaeufig) - zuvor 41,2 °C (25.07.2019,
    # Lingen/Emsland). Unser Datensatz reicht (Stand dieser Pipeline-Version) nur bis
    # Maerz 2026, enthaelt die Juni-2026-Hitzewelle also noch NICHT (Meteostats
    # bulk-Archiv war zum Zeitpunkt des Pipeline-Laufs noch nicht so weit aktualisiert).
    lines += [
        "=" * 60,
        "40+-Werte zur manuellen Kontrolle (Deutschlandrekord: 41,7 °C, 28.06.2026, "
        "Neissemuende-Coschen/Brandenburg, DWD-Angabe vorlaeufig; zuvor 41,2 °C, 2019):",
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

    # Bugfix nach der ersten bundesweiten Kontrolle: die 40+-Liste hatte mehrere
    # physikalisch unmoegliche Werte offengelegt (z. B. 50 °C im April), die sich
    # als echte Fehler in Meteostats Quelldaten herausstellten (direkt gegen die
    # Rohdaten geprueft, keine Fehler in dieser Pipeline). apply_sanity_filter()
    # verwirft solche Tage jetzt automatisch - hier zur Transparenz aufgelistet.
    lines += [
        "=" * 60,
        "Automatisch verworfene, physikalisch unplausible Tageswerte:",
        f"(tmax > tavg + {SANITY_MAX_SPREAD_C:g} °C oder tavg ausserhalb [tmin, tmax] - "
        "das kommt bei echten Messungen praktisch nie vor)",
        "",
    ]
    if _sanity_log:
        for station_id, date, tmax, tavg, tmin in sorted(_sanity_log):
            lines.append(f"  {station_id}, {date}: tmax={tmax} tavg={tavg} tmin={tmin} -> verworfen")
    else:
        lines.append("  Keine.")
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

    # Schlanker Karten-Index (id, name, lat, lon, max_temp/hot_days je Jahr/Saison,
    # current_year_anomaly fuer die Abweichungs-Einfaerbung des laufenden Jahres)
    map_index = [
        {
            "id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
            "years": s["_map_years"], "current_year_anomaly": s["current_year_anomaly"],
        }
        for s in stations_sorted
    ]
    with open(DATA_DIR / "map_index.json", "w", encoding="utf-8") as f:
        json.dump(map_index, f, ensure_ascii=False, indent=2)

    write_meta()


def write_meta() -> None:
    """Schreibt meta.json: laufendes Jahr, Datenstand, letztes vollstaendiges Jahr
    (Startansicht der Karte) und die Referenzperiode fuer die Abweichungsberechnung."""
    with open(DATA_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump(compute_running_year_meta(), f, ensure_ascii=False, indent=2)


def find_spatial_outliers() -> list[tuple[str, str, float, float, int]]:
    """Raeumliche Plausibilitaetspruefung UEBER alle Stationen hinweg (im Unterschied zu
    apply_sanity_filter, die nur INNERHALB einer einzelnen Station prueft): ein Tages-
    Hoechstwert gilt als verdaechtig, wenn
      (a) er selbst mindestens SPATIAL_CHECK_FLAG_MIN_C erreicht (nahe am nationalen
          Rekord - eine sehr warme Region an einem warmen Tag, z. B. Freiburg mit
          36 °C, ist normal und wird NICHT geprueft, siehe Kommentar bei den
          Konstanten oben),
      (b) er den naechsthoechsten Wert ALLER ANDEREN Stationen am selben Tag um mehr
          als SPATIAL_OUTLIER_MARGIN_C uebersteigt, UND
      (c) an dem Tag mindestens SPATIAL_CHECK_MIN_STATIONS andere Stationen ueberhaupt
          einen Vergleichswert (>= SPATIAL_CHECK_CANDIDATE_MIN_C) hatten (sonst ist die
          Datenlage zu duenn fuer eine raeumliche Aussage, z. B. in sehr alten Jahren).
    Laeuft als Nachbearbeitungsschritt ueber die bereits geschriebenen raw/-CSVs (nicht
    pro Station waehrend des Downloads, weil der Vergleich alle Stationen braucht).
    Gibt (station_id, date, tmax, naechsthoechster_wert, anzahl_vergleichsstationen) je
    Fund zurueck."""
    frames = []
    for raw_path in sorted(RAW_DIR.glob("*.csv")):
        day_df = pd.read_csv(raw_path, usecols=["date", "tmax"])
        day_df = day_df[day_df["tmax"] >= SPATIAL_CHECK_CANDIDATE_MIN_C]
        if day_df.empty:
            continue
        day_df = day_df.copy()
        day_df["station_id"] = raw_path.stem
        frames.append(day_df)
    if not frames:
        return []

    candidates = pd.concat(frames, ignore_index=True)

    outliers = []
    for date, group in candidates.groupby("date"):
        if len(group) < SPATIAL_CHECK_MIN_STATIONS:
            continue
        top2 = group.nlargest(2, "tmax")
        highest, second = top2.iloc[0], top2.iloc[1]
        if highest["tmax"] < SPATIAL_CHECK_FLAG_MIN_C:
            continue  # normale warme Region/Tag, keine (nahezu) rekordverdaechtige Behauptung
        margin = highest["tmax"] - second["tmax"]
        if margin > SPATIAL_OUTLIER_MARGIN_C:
            outliers.append((highest["station_id"], date, float(highest["tmax"]), float(second["tmax"]), len(group)))
    return outliers


def blank_raw_day(station_id: str, date: str) -> None:
    """Loescht einen einzelnen Tag (alle drei Werte) aus einer raw/<id>.csv - genutzt
    von der raeumlichen Pruefung, um einen als verdaechtig erkannten Tag dauerhaft
    auszunehmen, BEVOR die Station neu berechnet wird. dtype=str, damit unveraenderte
    Zeilen beim Zurueckschreiben nicht in der Formatierung driften."""
    path = RAW_DIR / f"{station_id}.csv"
    df = pd.read_csv(path, dtype=str)
    df.loc[df["date"] == date, ["tmax", "tavg", "tmin"]] = ""
    df.to_csv(path, index=False)


def write_spatial_exclusion_report(outliers: list) -> None:
    """Schreibt die transparente Ausschlussliste der raeumlichen Pruefung nach
    docs/data/ausgeschlossene_werte.txt - unabhaengig vom Sanity-Filter-Log in
    data_report.txt, weil dieser Mechanismus einen anderen Fehlertyp abdeckt
    (siehe find_spatial_outliers)."""
    lines = [
        "Hitze-Check Deutschland — raeumlich ausgeschlossene Tageswerte",
        f"Erzeugt am: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
        f"Diese Tageswerte wurden automatisch ausgeschlossen, weil ihr Tages-Hoechstwert",
        f"(a) mindestens {SPATIAL_CHECK_FLAG_MIN_C:g} °C erreicht (nahe am nationalen Rekord),",
        f"(b) den naechsthoechsten Wert ALLER ANDEREN Stationen am selben Tag um mehr als",
        f"    {SPATIAL_OUTLIER_MARGIN_C:g} °C uebersteigt, UND",
        f"(c) mindestens {SPATIAL_CHECK_MIN_STATIONS} andere Stationen an dem Tag einen Vergleichswert",
        f"    (>= {SPATIAL_CHECK_CANDIDATE_MIN_C:g} °C) hatten.",
        "Begruendung: Bei einer echten Hitzewelle stuetzen Nachbarstationen einen hohen",
        "Wert - ein einzelner Ausreisser weit darueber, den niemand in der Umgebung auch",
        "nur annaehernd erreicht, ist typischerweise ein Mess-/Digitalisierungsfehler in",
        "Meteostats Quelldaten (kein Fehler dieser Pipeline). Bewusst KEIN globaler",
        "Temperatur-Grenzwert und bewusst nur nahe am Rekord (Kriterium a) - eine sehr",
        "warme Region (z. B. Freiburg) an einem warmen Tag deutlich ueber dem Rest des",
        "Landes ist normal und real, das wird NICHT ausgeschlossen. Ein Wert kann auch",
        "nahe am Rekord real sein, wenn ihn andere Stationen stuetzen (siehe deutscher",
        "Rekord 41,7 °C, 28.06.2026, DWD vorlaeufig, an drei Tagen von mehreren",
        "Stationen gemeinsam erreicht/uebertroffen).",
        "",
    ]
    if not outliers:
        lines.append("Keine.")
    else:
        for station_id, date, tmax, next_highest, n in sorted(outliers, key=lambda o: o[1]):
            lines.append(
                f"  {station_id}, {date}: {tmax:.1f} °C -> ausgeschlossen "
                f"(naechsthoechster Wert bundesweit an dem Tag: {next_highest:.1f} °C, "
                f"Differenz {tmax - next_highest:.1f} °C, {n} Stationen mit Vergleichswert an dem Tag)"
            )
    lines.append("")
    with open(DATA_DIR / "ausgeschlossene_werte.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log("Suche geeignete Stationen (Inventar-Vorfilter: "
        f">= {MIN_SERIES_YEARS} Jahre Tagesreihe, Daten aus den letzten 2 Jahren) ...")
    suitable = find_suitable_stations()
    total = len(suitable)

    # Fortsetzbarkeit: station_id kann rein aus den (bereits vorliegenden)
    # Inventar-Metadaten berechnet werden, also VOR jedem Download - so laesst
    # sich sofort pruefen, ob schon eine raw/<id>.csv existiert (dann kein
    # erneuter Download noetig, siehe build_station()).
    jobs = [(meteostat_id, meta, compute_station_id(meta["name"], meteostat_id))
            for meteostat_id, meta in suitable.iterrows()]
    already_cached = sum(1 for _, _, sid in jobs if (RAW_DIR / f"{sid}.csv").exists())
    log(f"{total} geeignete Stationen gefunden ({already_cached} davon mit bereits vorhandenen "
        f"Rohdaten - kein erneuter Download noetig, nur neu berechnet). "
        f"Starte (bis zu {MAX_WORKERS} parallel, sanft) ...")

    stations_out = []
    failed = []  # (name, meteostat_id, grund) - fuer den Abschlussbericht
    done_count = 0
    count_lock = threading.Lock()

    def process(meteostat_id, meta, station_id):
        nonlocal done_count
        result = build_station(meteostat_id, meta, station_id)
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

    # Raeumliche Plausibilitaetspruefung (siehe find_spatial_outliers) - laeuft erst HIER,
    # weil sie die Rohdaten ALLER Stationen braucht, um einen Tag als isolierten Ausreisser
    # zu erkennen. Betroffene Tage werden aus der raw-CSV entfernt und NUR die betroffenen
    # Stationen anschliessend neu berechnet (schnell, da die restlichen Rohdaten schon lokal
    # vorliegen).
    log("\nRaeumliche Pruefung: suche Tageswerte ohne Stuetzung durch Nachbarstationen ...")
    outliers = find_spatial_outliers()
    write_spatial_exclusion_report(outliers)
    if outliers:
        log(f"{len(outliers)} verdaechtige Tageswerte gefunden, werden ausgeschlossen:")
        jobs_by_id = {sid: (meteostat_id, meta) for meteostat_id, meta, sid in jobs}
        stations_by_id = {s["id"]: s for s in stations_out}
        for station_id, date, tmax, next_highest, n in outliers:
            log(f"  {station_id}, {date}: {tmax:.1f} °C (naechsthoechster Wert bundesweit: {next_highest:.1f} °C)")
            blank_raw_day(station_id, date)
        for station_id in {o[0] for o in outliers}:
            meteostat_id, meta = jobs_by_id[station_id]
            updated = build_station(meteostat_id, meta, station_id)
            if updated:
                stations_by_id[station_id] = updated
            else:
                log(f"  WARNUNG: {station_id} liess sich nach dem Ausschluss nicht neu berechnen.")
        stations_out = list(stations_by_id.values())
        log(f"Ausschlussliste geschrieben nach {DATA_DIR / 'ausgeschlossene_werte.txt'}")
    else:
        log("Keine gefunden.")

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
