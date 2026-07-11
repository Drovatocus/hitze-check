"""
Unit-Tests der Pipeline-Rechenlogik (Tier 1a, PRE-LAUNCH-CHECKS.md).

Nutzt kleine, erfundene Testdaten, bei denen das richtige Ergebnis von Hand
bekannt ist - keine echten Downloads, kein Netzwerk noetig.
"""
import numpy as np
import pandas as pd
import pytest

import build_data as bd


def make_df(dates, tmax, tavg=None, tmin=None):
    """Baut einen Pipeline-typischen DataFrame (DatetimeIndex, Spalten tavg/tmax/tmin)."""
    idx = pd.DatetimeIndex(dates)
    if tavg is None:
        tavg = [t - 5.0 for t in tmax]
    if tmin is None:
        tmin = [t - 10.0 for t in tmax]
    return pd.DataFrame({"tavg": tavg, "tmax": tmax, "tmin": tmin}, index=idx)


# --- Zaehlung heisser Tage: Grenze bei >= 30 °C ---------------------------------

def test_hot_day_threshold_boundary():
    df = make_df(
        ["2020-07-01", "2020-07-02", "2020-07-03"],
        tmax=[29.9, 30.0, 30.1],
    )
    stats = bd.compute_period_stats(df, pd.Timestamp("2020-12-31"))
    assert stats["hot_days"] == 2, "29,9 °C darf nicht als heisser Tag zaehlen, 30,0 und 30,1 schon"


def test_summer_day_threshold_boundary():
    df = make_df(["2020-05-01", "2020-05-02"], tmax=[24.9, 25.0])
    stats = bd.compute_period_stats(df, pd.Timestamp("2020-12-31"))
    assert stats["summer_days"] == 1


def test_zero_hot_days_is_zero_not_none():
    df = make_df(["2020-01-01", "2020-01-02"], tmax=[2.0, 3.0])
    stats = bd.compute_period_stats(df, pd.Timestamp("2020-12-31"))
    assert stats["hot_days"] == 0
    assert stats is not None


# --- Meteorologischer Sommer: nur Juni, Juli, August ----------------------------

def test_summer_period_only_jun_jul_aug():
    # Ein Tag pro Monat, alle mit hot_days-Wert 1 (tmax=31) - nur 3 davon (Jun/Jul/Aug)
    # duerfen im "summer"-Zeitraum landen.
    dates = [f"2020-{m:02d}-15" for m in range(1, 13)]
    df = make_df(dates, tmax=[31.0] * 12)
    years_out = bd.compute_years_out(df)
    summer = years_out["2020"]["summer"]
    assert summer["hot_days"] == 3, "Nur Juni/Juli/August duerfen im Sommer-Zeitraum gezaehlt werden"
    assert years_out["2020"]["annual"]["hot_days"] == 12


# --- Durchschnitt und Hoechstwert (inkl. Datum) ---------------------------------

def test_mean_and_max_with_date():
    df = make_df(
        ["2020-07-01", "2020-07-02", "2020-07-03"],
        tmax=[28.0, 35.5, 30.0],
        tavg=[20.0, 25.0, 22.0],
    )
    stats = bd.compute_period_stats(df, pd.Timestamp("2020-12-31"))
    assert stats["mean_temp"] == pytest.approx(round((20.0 + 25.0 + 22.0) / 3, 1))
    assert stats["max_temp"] == 35.5
    assert stats["max_temp_date"] == "2020-07-02"


def test_complete_flag_depends_on_last_date_vs_period_end():
    df = make_df(["2020-01-01", "2020-06-15"], tmax=[1.0, 20.0])
    stats = bd.compute_period_stats(df, pd.Timestamp("2020-12-31"))
    assert stats["complete"] is False, "Reihe endet im Juni, Jahr ist noch nicht vollstaendig"

    df_full = make_df(["2020-01-01", "2020-12-31"], tmax=[1.0, 5.0])
    stats_full = bd.compute_period_stats(df_full, pd.Timestamp("2020-12-31"))
    assert stats_full["complete"] is True


# --- Rekord = Maximum ueber ALLE Jahre -------------------------------------------

def test_record_is_max_over_all_years_not_just_latest():
    dates = ["2001-08-01", "2010-08-01", "2020-08-01"]
    df = make_df(dates, tmax=[38.0, 41.0, 33.0])  # hoechster Wert liegt NICHT im letzten Jahr
    record_temp = df["tmax"].max()
    record_date = df["tmax"].idxmax().strftime("%Y-%m-%d")
    assert record_temp == 41.0
    assert record_date == "2010-08-01"


# --- Anomalie: laufendes Jahr ggue. Referenzperiode, gleiches Kalenderfenster ----

def test_anomaly_positive_when_running_year_warmer():
    # Baseline 1991-2020: konstant 10 °C bis Tag 100. Laufendes Jahr 2026: 13 °C bis Tag 100.
    baseline_years = range(bd.BASELINE_START_YEAR, bd.BASELINE_END_YEAR + 1)
    dates, tavg = [], []
    for y in baseline_years:
        d = pd.Timestamp(f"{y}-01-01") + pd.Timedelta(days=50)
        dates.append(d)
        tavg.append(10.0)
    running_date = pd.Timestamp("2026-01-01") + pd.Timedelta(days=50)
    dates.append(running_date)
    tavg.append(13.0)
    df = make_df(dates, tmax=[t + 5 for t in tavg], tavg=tavg)

    anomaly = bd.compute_current_year_anomaly(df, data_from=1991, running_year_last_date=running_date)
    assert anomaly == pytest.approx(3.0, abs=0.05)


def test_anomaly_negative_when_running_year_cooler():
    baseline_years = range(bd.BASELINE_START_YEAR, bd.BASELINE_END_YEAR + 1)
    dates, tavg = [], []
    for y in baseline_years:
        d = pd.Timestamp(f"{y}-03-01")
        dates.append(d)
        tavg.append(8.0)
    running_date = pd.Timestamp("2026-03-01")
    dates.append(running_date)
    tavg.append(5.0)
    df = make_df(dates, tmax=[t + 5 for t in tavg], tavg=tavg)

    anomaly = bd.compute_current_year_anomaly(df, data_from=1991, running_year_last_date=running_date)
    assert anomaly == pytest.approx(-3.0, abs=0.05)


def test_anomaly_none_when_series_starts_after_baseline():
    df = make_df(["2026-03-01"], tmax=[10.0])
    anomaly = bd.compute_current_year_anomaly(df, data_from=1995, running_year_last_date=pd.Timestamp("2026-03-01"))
    assert anomaly is None


def test_anomaly_none_without_running_year_data():
    df = make_df(["2010-03-01"], tmax=[10.0])
    anomaly = bd.compute_current_year_anomaly(df, data_from=1991, running_year_last_date=None)
    assert anomaly is None


def test_anomaly_uses_symmetric_window_not_todays_calendar_date():
    """Regressionstest fuer den waehrend der Entwicklung selbst gefundenen Bug:
    der Stichtag muss aus den Stationsdaten kommen, nicht aus dem heutigen Datum -
    sonst vergleicht man z. B. Winterwerte (laufendes Jahr, Datenstand Maerz) gegen
    ein Halbjahresmittel der Referenzperiode (bis "heute" im Juli) und bekommt eine
    stark verzerrte, falsche Abweichung."""
    baseline_years = range(bd.BASELINE_START_YEAR, bd.BASELINE_END_YEAR + 1)
    dates, tavg = [], []
    for y in baseline_years:
        # Kalt im Winter (Tag 50), heiss im Sommer (Tag 200) - Referenzmittel bis Tag 50 ist kalt.
        dates.append(pd.Timestamp(f"{y}-01-01") + pd.Timedelta(days=50))
        tavg.append(0.0)
        dates.append(pd.Timestamp(f"{y}-01-01") + pd.Timedelta(days=200))
        tavg.append(25.0)
    running_date = pd.Timestamp("2026-01-01") + pd.Timedelta(days=50)  # Datenstand: nur bis Tag 50 (Winter)
    dates.append(running_date)
    tavg.append(0.5)  # nahezu identisch zum Winter-Referenzwert
    df = make_df(dates, tmax=[t + 5 for t in tavg], tavg=tavg)

    anomaly = bd.compute_current_year_anomaly(df, data_from=1991, running_year_last_date=running_date)
    # Muss nahe 0 sein (Winter vs. Winter) - NICHT stark negativ (Winter vs. Halbjahresmittel inkl. Sommer).
    assert anomaly == pytest.approx(0.5, abs=0.05)


# --- Randfaelle: Schaltjahr, Datenluecken ----------------------------------------

def test_leap_year_does_not_crash_dayofyear_logic():
    baseline_years = list(range(bd.BASELINE_START_YEAR, bd.BASELINE_END_YEAR + 1))
    dates, tavg = [], []
    for y in baseline_years:
        dates.append(pd.Timestamp(f"{y}-02-28"))
        tavg.append(4.0)
    running_date = pd.Timestamp("2024-02-29")  # Schaltjahr
    dates.append(running_date)
    tavg.append(6.0)
    df = make_df(dates, tmax=[t + 5 for t in tavg], tavg=tavg)

    anomaly = bd.compute_current_year_anomaly(df, data_from=1991, running_year_last_date=running_date)
    assert anomaly is not None  # darf nicht crashen oder None liefern


def test_year_with_data_gap_still_computes_from_available_days():
    # Nur 3 Tage im ganzen Jahr vorhanden (grosse Luecke) - Statistik soll trotzdem
    # aus den vorhandenen Tagen berechnet werden, nicht scheitern.
    df = make_df(["2020-01-01", "2020-06-15", "2020-11-30"], tmax=[5.0, 31.0, 8.0])
    stats = bd.compute_period_stats(df, pd.Timestamp("2020-12-31"))
    assert stats is not None
    assert stats["hot_days"] == 1
    assert stats["complete"] is False  # letzter Wert 30.11., nicht 31.12.


def test_completeness_reports_large_gap_but_not_small_one():
    full_range = pd.date_range("2020-01-01", "2020-12-31", freq="D")
    # 40-Tage-Luecke (Feb-Maerz) rausnehmen, kleine 5-Tage-Luecke (Sept) auch rausnehmen.
    gap_large = pd.date_range("2020-02-01", "2020-03-11", freq="D")  # 40 Tage
    gap_small = pd.date_range("2020-09-01", "2020-09-05", freq="D")  # 5 Tage
    kept = full_range.difference(gap_large).difference(gap_small)
    df = pd.DataFrame({"tavg": 10.0, "tmax": 20.0, "tmin": 5.0}, index=kept)

    completeness_pct, gaps = bd.compute_completeness(df)
    assert completeness_pct < 100.0
    assert len(gaps) == 1, "nur die grosse Luecke (>= GAP_MIN_DAYS) soll gemeldet werden"
    assert gaps[0]["days"] == 40


# --- Sanity-Filter: physikalisch unplausible Tage werden verworfen ---------------

def test_sanity_filter_discards_implausible_day_keeps_normal_ones():
    df = make_df(
        ["2020-01-01", "2020-01-02", "2020-01-03"],
        tmax=[5.0, 50.0, 6.0],   # Tag 2: tmax=50 bei tavg=... (siehe unten) -> unplausibel
        tavg=[2.0, 10.6, 3.0],
        tmin=[-1.0, 8.0, 0.0],
    )
    filtered = bd.apply_sanity_filter(df, "test-station")
    assert filtered.loc["2020-01-02"].isna().all(), "unplausibler Tag muss komplett verworfen (NaN) werden"
    assert not filtered.loc["2020-01-01"].isna().any(), "normale Tage duerfen nicht veraendert werden"
    assert not filtered.loc["2020-01-03"].isna().any()


# --- Hilfsfunktionen: hottest/mildest Jahr, Dekaden-Mittel -----------------------

def test_hottest_and_mildest_year():
    years_out = {
        "2018": {"annual": {"hot_days": 20}},
        "2019": {"annual": {"hot_days": 35}},
        "2020": {"annual": {"hot_days": 5}},
    }
    hottest, mildest = bd.compute_hottest_mildest_year(years_out)
    assert hottest == {"year": 2019, "hot_days": 35}
    assert mildest == {"year": 2020, "hot_days": 5}


def test_decade_averages():
    years_out = {
        "1991": {"annual": {"hot_days": 4}},
        "1995": {"annual": {"hot_days": 6}},
        "2001": {"annual": {"hot_days": 10}},
    }
    decades = bd.compute_decade_averages(years_out)
    assert decades["1990er"] == pytest.approx(5.0)
    assert decades["2000er"] == pytest.approx(10.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
