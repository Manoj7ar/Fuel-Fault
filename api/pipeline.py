"""
Fuel Fault Lines — data pipeline (notebook logic consolidated for API use).
Sources: SEAI-style county energy (with CSV fallbacks), CSO FY068, AA Ireland fuel prices.
"""
from __future__ import annotations

import io
import itertools
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests

# --- Constants (match frontend county order where relevant) ---
CANONICAL_COUNTIES = [
    "Carlow", "Cavan", "Clare", "Cork", "Donegal", "Dublin",
    "Galway", "Kerry", "Kildare", "Kilkenny", "Laois", "Leitrim",
    "Limerick", "Longford", "Louth", "Mayo", "Meath", "Monaghan",
    "Offaly", "Roscommon", "Sligo", "Tipperary", "Waterford",
    "Westmeath", "Wexford", "Wicklow",
]

SEAI_URLS = [
    "https://data.gov.ie/dataset/64e79bdb-9aa8-46a6-8b95-cc9d4d5ea69b/resource/3aa6fb8f-2e9f-48b5-8a0e-70fa6de26dc9/download/seai-domestic-energy-profile.csv",
    "https://data.gov.ie/dataset/d1b3ab3c-b66a-4acf-be67-8c8a714daa4a/resource/3b01b0e1-fcad-4b51-b1a9-2e56c9e6ae8b/download/countyenergyprofiles.csv",
    "https://opendata-seai.hub.arcgis.com/datasets/seai::county-energy-profiles.csv",
    "https://ws.cso.ie/public/api.restful/PxStat.Data.Cube_API.ReadDataset/EIA01/CSV/1.0/en",
]

LITRES_PER_HH_PA = 1275
PRICE_POINTS = [1.74, 2.14, 2.50, 3.00, 3.50]
POVERTY_THRESHOLD_PCT = 10.0
DEP_MIN, DEP_MAX = 20.0, 40.0
INC_MIN, INC_MAX = 28_000.0, 52_000.0

TOE_TO_LITRES = 1163.0
FUEL_ALLOWANCE_PA = 33.0 * 28.0
RETROFIT_GRANT = 25_000.0
RETROFIT_SAVING = 0.50
AVG_HH_SIZE = 2.75

WEIGHTS = {
    "fuel_dependency_score": 0.30,
    "building_inefficiency_score": 0.25,
    "social_deprivation_score": 0.30,
    "energy_intensity_score": 0.15,
}

# Heating-demand proxy (1.0 = national blend). Inland / Atlantic counties typically higher.
COUNTY_HDD_MULT: dict[str, float] = {
    "Carlow": 1.02,
    "Cavan": 1.05,
    "Clare": 1.06,
    "Cork": 1.04,
    "Donegal": 1.12,
    "Dublin": 0.94,
    "Galway": 1.10,
    "Kerry": 1.08,
    "Kildare": 0.96,
    "Kilkenny": 1.01,
    "Laois": 1.03,
    "Leitrim": 1.11,
    "Limerick": 1.05,
    "Longford": 1.08,
    "Louth": 1.00,
    "Mayo": 1.12,
    "Meath": 0.98,
    "Monaghan": 1.06,
    "Offaly": 1.04,
    "Roscommon": 1.09,
    "Sligo": 1.10,
    "Tipperary": 1.03,
    "Waterford": 1.02,
    "Westmeath": 1.05,
    "Wexford": 1.00,
    "Wicklow": 0.97,
}


@dataclass
class ModelParams:
    """Tunable assumptions for stress tests and API `/model` endpoints."""

    litres_per_hh_pa: float = LITRES_PER_HH_PA
    poverty_threshold_pct: float = POVERTY_THRESHOLD_PCT
    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    income_dep_min: float = DEP_MIN
    income_dep_max: float = DEP_MAX
    income_min_eur: float = INC_MIN
    income_max_eur: float = INC_MAX
    retrofit_grant_eur: float = RETROFIT_GRANT
    retrofit_saving_fraction: float = RETROFIT_SAVING
    use_hdd_adjustment: bool = True
    fuel_allowance_pa_eur: float = FUEL_ALLOWANCE_PA

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "litres_per_hh_pa": self.litres_per_hh_pa,
            "poverty_threshold_pct": self.poverty_threshold_pct,
            "weights": dict(self.weights),
            "income_dep_min": self.income_dep_min,
            "income_dep_max": self.income_dep_max,
            "income_min_eur": self.income_min_eur,
            "income_max_eur": self.income_max_eur,
            "retrofit_grant_eur": self.retrofit_grant_eur,
            "retrofit_saving_fraction": self.retrofit_saving_fraction,
            "use_hdd_adjustment": self.use_hdd_adjustment,
            "fuel_allowance_pa_eur": self.fuel_allowance_pa_eur,
        }


def hdd_multiplier_for_county(county: str) -> float:
    return float(COUNTY_HDD_MULT.get(str(county).strip(), 1.0))


def litres_proxy_row(r: pd.Series, params: ModelParams) -> float:
    fds = float(r["fuel_dependency_score"])
    base = params.litres_per_hh_pa * (0.5 + fds / 100.0)
    if params.use_hdd_adjustment:
        base *= hdd_multiplier_for_county(str(r["county"]))
    return base


def poverty_pct_row_series(r: pd.Series, price: float, params: ModelParams) -> float:
    income = float(r["estimated_annual_income"])
    if income <= 0:
        return 0.0
    litres = litres_proxy_row(r, params)
    return round(litres * price / income * 100, 2)


def git_short_hash(fallback: str = "unknown") -> str:
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return fallback


def _seai_fallback_df() -> pd.DataFrame:
    counties_data = {
        "county": [
            "Carlow", "Cavan", "Clare", "Cork", "Donegal", "Dublin",
            "Galway", "Kerry", "Kildare", "Kilkenny", "Laois", "Leitrim",
            "Limerick", "Longford", "Louth", "Mayo", "Meath", "Monaghan",
            "Offaly", "Roscommon", "Sligo", "Tipperary", "Waterford",
            "Westmeath", "Wexford", "Wicklow",
        ],
        "province": [
            "Leinster", "Ulster", "Munster", "Munster", "Ulster", "Leinster",
            "Connacht", "Munster", "Leinster", "Leinster", "Leinster", "Connacht",
            "Munster", "Leinster", "Leinster", "Connacht", "Leinster", "Ulster",
            "Leinster", "Connacht", "Connacht", "Munster", "Munster",
            "Leinster", "Leinster", "Leinster",
        ],
        "population_2022": [
            61927, 82950, 129592, 570700, 168997, 1450358,
            284322, 158268, 246977, 102085, 92015, 34950,
            204666, 46464, 146389, 136872, 220248, 63363,
            82668, 72183, 72987, 181316, 125450,
            95419, 162540, 155258,
        ],
        "residential_energy_ktoe": [
            38.2, 56.4, 92.3, 387.5, 131.8, 784.2,
            195.8, 115.7, 161.4, 69.2, 62.8, 26.3,
            141.5, 33.1, 98.7, 104.3, 148.7, 47.6,
            58.4, 54.2, 53.9, 127.6, 87.3,
            67.1, 113.4, 104.7,
        ],
        "dwellings_count": [
            24801, 33456, 52184, 231960, 70245, 575423,
            116178, 65894, 97823, 41678, 37264, 15234,
            83567, 19456, 58934, 57123, 89234, 26345,
            34567, 30123, 30456, 74523, 51234,
            39456, 66123, 62345,
        ],
        "pct_ber_ab": [
            12.3, 10.1, 11.8, 14.2, 9.7, 18.6,
            13.5, 10.9, 15.8, 11.4, 10.8, 8.4,
            13.2, 9.3, 14.7, 9.8, 16.2, 9.5,
            10.4, 9.1, 10.3, 11.7, 13.1,
            11.9, 12.6, 14.8,
        ],
        "pct_ber_defg": [
            41.2, 46.8, 42.3, 38.7, 49.2, 29.8,
            40.5, 45.8, 35.6, 43.2, 44.7, 52.3,
            40.1, 47.6, 37.4, 47.3, 36.8, 48.2,
            44.3, 49.7, 46.8, 43.2, 39.8,
            42.7, 40.9, 36.5,
        ],
        "primary_fuel": [
            "Oil", "Oil", "Oil", "Gas", "Oil", "Gas",
            "Oil", "Oil", "Gas", "Oil", "Oil", "Oil",
            "Gas", "Oil", "Gas", "Oil", "Gas", "Oil",
            "Oil", "Oil", "Oil", "Oil", "Gas",
            "Oil", "Oil", "Gas",
        ],
        "energy_per_dwelling_toe": [
            1.54, 1.69, 1.77, 1.67, 1.88, 1.36,
            1.69, 1.76, 1.65, 1.66, 1.69, 1.73,
            1.69, 1.70, 1.67, 1.83, 1.67, 1.81,
            1.69, 1.80, 1.77, 1.71, 1.70,
            1.70, 1.72, 1.68,
        ],
        "residential_co2_kt": [
            102.3, 151.2, 247.5, 1021.4, 361.8, 1824.6,
            524.3, 313.7, 422.8, 185.6, 168.4, 71.8,
            378.3, 89.7, 263.5, 285.6, 397.5, 129.8,
            157.3, 147.9, 147.1, 342.8, 233.4,
            180.3, 304.7, 278.9,
        ],
    }
    return pd.DataFrame(counties_data)


def load_seai_df() -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FuelFaultLines/1.0)"}
    for url in SEAI_URLS:
        try:
            r = requests.get(url, timeout=25, headers=headers, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 200:
                df = pd.read_csv(io.StringIO(r.text))
                df.columns = (
                    df.columns.str.strip()
                    .str.lower()
                    .str.replace(r"[\s/()]+", "_", regex=True)
                    .str.replace(r"[^a-z0-9_]", "", regex=True)
                    .str.strip("_")
                )
                for col in df.columns:
                    if df[col].dtype in ("float64", "int64"):
                        df[col] = df[col].fillna(df[col].median())
                    else:
                        df[col] = df[col].fillna("Unknown")
                df.attrs["ffl_seai_source"] = "remote_csv"
                return df
        except Exception:
            continue
    fb = _seai_fallback_df()
    fb.attrs["ffl_seai_source"] = "embedded_fallback"
    return fb


def _build_pos_to_label(cats: dict) -> dict:
    idx = cats.get("index", [])
    lbl = cats.get("label", {})
    if isinstance(idx, list):
        return {pos: lbl.get(code, code) for pos, code in enumerate(idx)}
    return {pos: lbl.get(code, code) for code, pos in idx.items()}


def load_cso_deprivation_df() -> pd.DataFrame:
    base = "https://ws.cso.ie/public/api.restful/PxStat.Data.Cube_API.ReadDataset/{}/JSON-stat/2.0/en"
    resp = requests.get(base.format("FY068"), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    dimension_ids = data["id"]
    dimensions = data["dimension"]
    dim_sizes = data["size"]
    values = data["value"]
    dim_label = {
        dim: _build_pos_to_label(dimensions[dim]["category"]) for dim in dimension_ids
    }
    records = []
    for combo, val in zip(itertools.product(*[range(s) for s in dim_sizes]), values):
        record = {
            dimension_ids[k]: dim_label[dimension_ids[k]][combo[k]] for k in range(len(dimension_ids))
        }
        record["population"] = val
        records.append(record)
    raw = pd.DataFrame(records)
    raw.rename(
        columns={
            "C03789V04537": "county_council",
            "C02199V02655": "sex",
            "C02728V03296": "socioeconomic_group",
            "TLIST(A1)": "year",
            "STATISTIC": "statistic",
        },
        inplace=True,
    )
    fy = raw[(raw["sex"] == "Both sexes") & (raw["county_council"] != "Ireland")].copy()

    def clean_county(name: str) -> str:
        name = re.sub(
            r"\s*(County Council|City Council|City and County Council)\s*$",
            "",
            str(name),
            flags=re.IGNORECASE,
        )
        return name.strip()

    fy["county"] = fy["county_council"].apply(clean_county)
    dublin_areas = ["Dublin City", "Fingal", "Dún Laoghaire Rathdown", "South Dublin"]
    fy.loc[fy["county"].isin(dublin_areas), "county"] = "Dublin"
    pivot = fy.groupby(["county", "socioeconomic_group"])["population"].sum().reset_index()
    wide = pivot.pivot(index="county", columns="socioeconomic_group", values="population").reset_index()
    wide.columns.name = None
    total_col = "All socio-economic groups"
    wide["total_pop"] = wide[total_col]
    lower_seg_cols = [
        c
        for c in wide.columns
        if any(g in c for g in ["F. Semi-skilled", "G. Unskilled", "Z. All others"])
    ]
    wide["lower_seg_pop"] = wide[lower_seg_cols].sum(axis=1)
    wide["deprivation_index"] = (wide["lower_seg_pop"] / wide["total_pop"] * 100).round(2)
    out = wide[["county", "total_pop", "lower_seg_pop", "deprivation_index"]].copy()
    return out.sort_values("deprivation_index", ascending=False).reset_index(drop=True)


_COUNTY_ALIASES = {
    "limerick city &": "Limerick",
    "waterford city &": "Waterford",
    "galway city &": "Galway",
    "limerick city and county": "Limerick",
    "waterford city and county": "Waterford",
    "galway city and county": "Galway",
    "cork city": "Cork",
    "dublin city": "Dublin",
    "dún laoghaire rathdown": "Dublin",
    "fingal": "Dublin",
    "south dublin": "Dublin",
}


def normalise_county(name: str) -> str:
    if pd.isna(name):
        return name
    cleaned = re.sub(
        r"\s*(county council|city council|city and county council|county|co\.?)\s*$",
        "",
        str(name).strip(),
        flags=re.IGNORECASE,
    ).strip()
    lower = cleaned.lower()
    if lower in _COUNTY_ALIASES:
        return _COUNTY_ALIASES[lower]
    for alias, canon in _COUNTY_ALIASES.items():
        if lower.startswith(alias):
            return canon
    titled = cleaned.title()
    return titled if titled in CANONICAL_COUNTIES else titled


def load_fuel_prices_df() -> pd.DataFrame:
    """AA Ireland national averages; skip live scrape on cold start for reliability."""
    return pd.DataFrame(
        [
            {
                "label": "Republic of Ireland — National Average",
                "fuel_type": "Petrol",
                "price_raw": "182.9c",
                "price_per_litre": 1.829,
                "unit": "€/litre",
                "source": "AA Ireland survey (March 2025)",
                "as_of": "2025-03-01",
            },
            {
                "label": "Republic of Ireland — National Average",
                "fuel_type": "Diesel",
                "price_raw": "173.5c",
                "price_per_litre": 1.735,
                "unit": "€/litre",
                "source": "AA Ireland survey (March 2025)",
                "as_of": "2025-03-01",
            },
        ]
    )


def synthetic_cso_from_seai(seai_df: pd.DataFrame) -> pd.DataFrame:
    """If CSO PxStat is unreachable (eg restricted egress), approximate deprivation from BER mix."""
    out = seai_df[["county", "population_2022"]].copy()
    out = out.rename(columns={"population_2022": "total_pop"})
    ber_bad = seai_df["pct_ber_defg"].astype(float)
    out["deprivation_index"] = (26 + (ber_bad / ber_bad.max()) * 10).round(2).clip(20, 40)
    out["lower_seg_pop"] = (out["total_pop"] * out["deprivation_index"] / 100).round(0)
    return out[["county", "total_pop", "lower_seg_pop", "deprivation_index"]]


def merge_energy_vulnerability(seai_df: pd.DataFrame, cso_df: pd.DataFrame, fuel_df: pd.DataFrame) -> pd.DataFrame:
    seai_clean = seai_df.copy()
    seai_clean["county"] = seai_clean["county"].apply(normalise_county)
    cso_clean = cso_df.copy()
    cso_clean["county"] = cso_clean["county"].apply(normalise_county)
    merged = pd.merge(seai_clean, cso_clean, on="county", how="outer", suffixes=("_seai", "_cso"))
    roi_fuel = fuel_df[fuel_df["unit"] == "€/litre"].copy()
    petrol_price = roi_fuel.loc[roi_fuel["fuel_type"] == "Petrol", "price_per_litre"].values
    diesel_price = roi_fuel.loc[roi_fuel["fuel_type"] == "Diesel", "price_per_litre"].values
    merged["fuel_petrol_eur_per_l"] = float(petrol_price[0]) if len(petrol_price) else None
    merged["fuel_diesel_eur_per_l"] = float(diesel_price[0]) if len(diesel_price) else None
    merged["fuel_price_source"] = fuel_df["source"].iloc[0]
    merged["fuel_price_as_of"] = fuel_df["as_of"].iloc[0]
    return merged.sort_values("county").reset_index(drop=True)


def norm_0_100(series: pd.Series) -> pd.Series:
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series(50.0, index=series.index)
    return (series - mn) / (mx - mn) * 100.0


def score_vulnerability(df: pd.DataFrame, params: ModelParams | None = None) -> pd.DataFrame:
    params = params or ModelParams()
    w = params.weights
    out = df.copy()
    fuel_dep_raw = out["pct_ber_defg"].copy()
    oil_mask = out["primary_fuel"].str.strip().str.lower() == "oil"
    fuel_dep_raw = fuel_dep_raw + oil_mask.astype(float) * 10.0
    bldg_ineff_raw = (100.0 - out["pct_ber_ab"]) * 0.5 + out["energy_per_dwelling_toe"] * 0.5
    lower_seg_share = out["lower_seg_pop"] / out["total_pop"]
    social_dep_raw = out["deprivation_index"] * 0.6 + lower_seg_share * 100.0 * 0.4
    energy_per_cap = out["residential_energy_ktoe"] / out["population_2022"] * 1000
    co2_per_cap = out["residential_co2_kt"] / out["population_2022"] * 1000
    energy_int_raw = energy_per_cap * 0.5 + co2_per_cap * 0.5
    out["fuel_dependency_score"] = norm_0_100(fuel_dep_raw).round(2)
    out["building_inefficiency_score"] = norm_0_100(bldg_ineff_raw).round(2)
    out["social_deprivation_score"] = norm_0_100(social_dep_raw).round(2)
    out["energy_intensity_score"] = norm_0_100(energy_int_raw).round(2)
    out["hdd_multiplier"] = out["county"].map(hdd_multiplier_for_county).astype(float)
    out["vulnerability_score"] = (
        out["fuel_dependency_score"] * w["fuel_dependency_score"]
        + out["building_inefficiency_score"] * w["building_inefficiency_score"]
        + out["social_deprivation_score"] * w["social_deprivation_score"]
        + out["energy_intensity_score"] * w["energy_intensity_score"]
    ).round(2)
    out["risk_tier"] = pd.cut(
        out["vulnerability_score"],
        bins=[0, 30, 50, 70, 100],
        labels=["Low", "Medium", "High", "Critical"],
        include_lowest=True,
    ).astype(str)
    return out


def add_price_shock_and_warmer_homes(df: pd.DataFrame, params: ModelParams | None = None) -> pd.DataFrame:
    params = params or ModelParams()
    out = df.copy()
    dmin, dmax = params.income_dep_min, params.income_dep_max
    dep = out["deprivation_index"].clip(lower=dmin, upper=dmax)
    out["estimated_annual_income"] = (
        params.income_min_eur
        + (dep - dmin) / (dmax - dmin) * (params.income_max_eur - params.income_min_eur)
    ).round(0)

    def row_litres(r: pd.Series) -> float:
        return litres_proxy_row(r, params)

    litres_pa = out.apply(row_litres, axis=1)
    out["model_litres_proxy_pa"] = litres_pa.round(1)
    thr = params.poverty_threshold_pct
    for p in PRICE_POINTS:
        out[f"annual_fuel_spend_{p:.2f}"] = (litres_pa * p).round(2)
        out[f"poverty_pct_{p:.2f}"] = (
            out[f"annual_fuel_spend_{p:.2f}"] / out["estimated_annual_income"] * 100
        ).round(2)

    def find_cliff(row: pd.Series) -> float | None:
        for p in PRICE_POINTS:
            if row[f"poverty_pct_{p:.2f}"] > thr:
                return float(p)
        return None

    out["cliff_price"] = out.apply(find_cliff, axis=1)
    out["annual_oil_litres"] = (out["energy_per_dwelling_toe"] * TOE_TO_LITRES).round(0)
    out["annual_oil_bill"] = (out["annual_oil_litres"] * out["fuel_diesel_eur_per_l"]).round(2)
    out["cost_10yr_keep_allowance"] = float(params.fuel_allowance_pa_eur * 10)
    out["annual_saving_post_retrofit"] = (out["annual_oil_bill"] * params.retrofit_saving_fraction).round(2)
    out["cost_10yr_retrofit_grant"] = float(params.retrofit_grant_eur)
    g = params.retrofit_grant_eur
    out["retrofit_roi_saving"] = (out["annual_saving_post_retrofit"] * 10 - g).round(2)
    sav = out["annual_saving_post_retrofit"].astype(float)
    out["breakeven_years"] = np.where(sav > 0, g / sav, np.nan)
    out["breakeven_years"] = pd.Series(out["breakeven_years"], index=out.index).round(1)
    out["est_vulnerable_households"] = (out["lower_seg_pop"] / AVG_HH_SIZE).round(0).astype(int)
    out["total_state_saving_10yr"] = (
        (out["cost_10yr_keep_allowance"] - g) * out["est_vulnerable_households"]
    ).round(0).astype(int)
    return out


def compare_scenarios(df: pd.DataFrame, price_a: float, price_b: float) -> dict[str, Any]:
    col_a = f"poverty_pct_{price_a:.2f}"
    col_b = f"poverty_pct_{price_b:.2f}"
    if col_a not in df.columns or col_b not in df.columns:
        raise ValueError("Price points not precomputed; use 1.74, 2.14, 2.50, 3.00, or 3.50")
    poor_a = df.loc[df[col_a] > POVERTY_THRESHOLD_PCT, "county"].tolist()
    poor_b = df.loc[df[col_b] > POVERTY_THRESHOLD_PCT, "county"].tolist()
    newly = [c for c in poor_b if c not in poor_a]
    return {
        "price_a": price_a,
        "price_b": price_b,
        "counties_poor_a": poor_a,
        "counties_poor_b": poor_b,
        "newly_poor": newly,
        "count_a": len(poor_a),
        "count_b": len(poor_b),
        "newly_poor_count": len(newly),
    }


def build_price_history_payload(df: pd.DataFrame, params: ModelParams | None = None) -> dict[str, Any]:
    params = params or ModelParams()
    hist_dates = pd.date_range(start="2024-05-01", periods=12, freq="MS")
    hist_prices = [1.87, 1.83, 1.80, 1.77, 1.74, 1.72, 1.70, 1.71, 1.73, 1.74, 1.76, 1.78]
    x = np.arange(len(hist_prices))
    slope, intercept = np.polyfit(x, np.array(hist_prices), 1)
    future = pd.date_range(start="2025-05-01", periods=6, freq="MS")
    future_nums = np.arange(len(hist_prices), len(hist_prices) + 6)
    proj_prices = np.round(intercept + slope * future_nums, 4)
    thr = params.poverty_threshold_pct

    def poverty_count_at_price(price: float) -> int:
        pct = df.apply(lambda r: poverty_pct_row_series(r, price, params), axis=1)
        return int((pct > thr).sum())

    historical = [{"date": str(hist_dates[i].date()), "price": float(hist_prices[i])} for i in range(len(hist_prices))]
    projected = [{"date": str(future[i].date()), "price": float(proj_prices[i])} for i in range(len(future))]
    july_p = float(proj_prices[2]) if len(proj_prices) > 2 else float(proj_prices[-1])
    oct_p = float(proj_prices[5]) if len(proj_prices) > 5 else float(proj_prices[-1])
    return {
        "historical": historical,
        "projected": projected,
        "events": [
            {"date": "2022-02-24", "label": "Russia invades Ukraine"},
            {"date": "2025-06-01", "label": "Iran conflict"},
            {"date": "2026-04-08", "label": "Today"},
        ],
        "poverty_threshold_price": thr,
        "projections": {
            "july": {"counties_in_poverty": poverty_count_at_price(july_p)},
            "october": {"counties_in_poverty": poverty_count_at_price(oct_p)},
        },
    }


# Minimal TD directory — extend in notebook / JSON as needed
TD_DATA: dict[str, dict[str, Any]] = {
    "Donegal": {
        "tds": [
            {"name": "Pearse Doherty", "party": "Sinn Féin", "email": "pearse.doherty@oireachtas.ie"},
            {"name": "Charlie McConalogue", "party": "Fianna Fáil", "email": "charlie.mcconalogue@oireachtas.ie"},
        ],
        "constituency": "Donegal",
    },
    "Longford": {
        "tds": [
            {"name": "Joe Flaherty", "party": "Fianna Fáil", "email": "joe.flaherty@oireachtas.ie"},
        ],
        "constituency": "Longford-Westmeath",
    },
}


def get_county_deep_dive_dict(
    df: pd.DataFrame,
    county_name: str,
    price_per_l: float,
    params: ModelParams | None = None,
) -> dict[str, Any]:
    params = params or ModelParams()
    row = df.loc[df["county"] == county_name]
    if row.empty:
        raise ValueError(f"County '{county_name}' not found")
    r = row.iloc[0]
    income = float(r["estimated_annual_income"])
    litres_pa = litres_proxy_row(r, params)
    spend = litres_pa * price_per_l
    poverty_pct_at_price = round(spend / income * 100, 2) if income else 0.0
    pov_cols = {f"{p:.2f}": poverty_pct_row_series(r, float(p), params) for p in PRICE_POINTS}
    td_info = TD_DATA.get(county_name, {"tds": [], "note": "No TD sample for this county"})
    first = (td_info.get("tds") or [{}])[0]
    email_body = (
        f"Dear {first.get('name', 'TD')},\n\n"
        f"I am writing regarding the energy crisis in {county_name}. "
        f"At €{price_per_l:.2f}/L, households are under severe pressure.\n\n"
        f"Please raise fuel poverty and retrofit funding with the Minister.\n"
    )
    tweet = (
        f"{county_name}: energy vulnerability score {float(r['vulnerability_score']):.1f} "
        f"— we need action on fuel poverty and Warmer Homes. #FuelFaultLines"
    )
    return {
        "county": county_name,
        "price_queried_eur_l": price_per_l,
        "vulnerability": {
            "score": round(float(r["vulnerability_score"]), 2),
            "risk_tier": str(r["risk_tier"]),
            "fuel_dependency_score": round(float(r["fuel_dependency_score"]), 2),
            "building_inefficiency_score": round(float(r["building_inefficiency_score"]), 2),
            "social_deprivation_score": round(float(r["social_deprivation_score"]), 2),
            "energy_intensity_score": round(float(r["energy_intensity_score"]), 2),
        },
        "poverty": {
            "estimated_annual_income_eur": int(income),
            "poverty_pct_at_price": poverty_pct_at_price,
            "in_energy_poverty": poverty_pct_at_price > params.poverty_threshold_pct,
            "cliff_price_eur_l": float(r["cliff_price"]) if pd.notna(r["cliff_price"]) else None,
            "poverty_pct_by_price_point": pov_cols,
        },
        "retrofit_roi": {
            "annual_oil_litres": int(r["annual_oil_litres"]),
            "annual_oil_bill_eur": round(float(r["annual_oil_bill"]), 2),
            "annual_saving_post_retrofit": round(float(r["annual_saving_post_retrofit"]), 2),
            "breakeven_years": round(float(r["breakeven_years"]), 1),
            "retrofit_roi_saving_10yr_eur": round(float(r["retrofit_roi_saving"]), 2),
            "est_vulnerable_households": int(r["est_vulnerable_households"]),
        },
        "td_contacts": td_info,
        "td_name": first.get("name"),
        "td_party": first.get("party"),
        "td_email": first.get("email"),
        "minister_email_template": email_body,
        "tweet_text": tweet,
    }


def county_row_to_api(r: pd.Series, fuel_price: float, params: ModelParams | None = None) -> dict[str, Any]:
    """Flatten row to match fuel-fault-lines-app/index.html expectations."""
    params = params or ModelParams()
    income = float(r["estimated_annual_income"])
    litres_pa = litres_proxy_row(r, params)
    spend = litres_pa * fuel_price
    poverty_pct = round(spend / income * 100, 2) if income else 0.0
    oil_litres = float(r["annual_oil_litres"])
    annual_bill = round(oil_litres * fuel_price, 2)
    rs = params.retrofit_saving_fraction
    g = params.retrofit_grant_eur
    saving = round(annual_bill * rs, 2)
    roi_10 = round(saving * 10 - g, 2)
    be = round(g / saving, 1) if saving > 0 else None
    cliff = float(r["cliff_price"]) if pd.notna(r["cliff_price"]) else None
    hdd = float(r["hdd_multiplier"]) if "hdd_multiplier" in r.index else hdd_multiplier_for_county(str(r["county"]))
    return {
        "county": str(r["county"]),
        "province": str(r["province"]),
        "vulnerability_score": float(r["vulnerability_score"]),
        "risk_tier": str(r["risk_tier"]),
        "fuel_dependency_score": float(r["fuel_dependency_score"]),
        "building_inefficiency_score": float(r["building_inefficiency_score"]),
        "social_deprivation_score": float(r["social_deprivation_score"]),
        "energy_intensity_score": float(r["energy_intensity_score"]),
        "poverty_pct_at_price": poverty_pct,
        "in_energy_poverty": poverty_pct > params.poverty_threshold_pct,
        "cliff_price_eur": cliff,
        "estimated_annual_income": income,
        "annual_oil_bill_eur": annual_bill,
        "est_vulnerable_households": int(r["est_vulnerable_households"]),
        "annual_saving_post_retrofit": saving,
        "breakeven_years": be,
        "retrofit_roi_saving_10yr": roi_10,
        "model_litres_proxy_pa": round(litres_pa, 2),
        "hdd_multiplier": hdd,
    }


def enrich_provenance(merged: pd.DataFrame, cso_was_synthetic: bool, seai: pd.DataFrame) -> None:
    merged.attrs["cso_source"] = "synthetic_from_seai_ber" if cso_was_synthetic else "cso_fy068_pxstat"
    merged.attrs["seai_source"] = seai.attrs.get("ffl_seai_source", "unknown")
    merged.attrs["model_note"] = (
        "Income is a synthetic band from deprivation index, not measured household income. "
        "Fuel share uses a litres proxy (dependency score + optional HDD multiplier), not metered use."
    )


def load_merged_energy_base() -> tuple[pd.DataFrame, bool]:
    """SEAI+CSO+fuel merge before scoring (for sensitivity / rebuilds)."""
    seai = load_seai_df()
    cso_was_synthetic = False
    try:
        cso = load_cso_deprivation_df()
    except Exception:
        cso = synthetic_cso_from_seai(seai)
        cso_was_synthetic = True
    fuel = load_fuel_prices_df()
    merged = merge_energy_vulnerability(seai, cso, fuel)
    enrich_provenance(merged, cso_was_synthetic, seai)
    return merged, cso_was_synthetic


def build_warmer_homes_dataframe(params: ModelParams | None = None) -> pd.DataFrame:
    params = params or ModelParams()
    merged, _cso_syn = load_merged_energy_base()
    scored = score_vulnerability(merged, params)
    return add_price_shock_and_warmer_homes(scored, params)


def model_meta_dict(df: pd.DataFrame, params: ModelParams) -> dict[str, Any]:
    cso_src = df.attrs.get("cso_source", "unknown")
    seai_src = df.attrs.get("seai_source", "unknown")
    return {
        "api_version": "1.1",
        "git_rev": git_short_hash(),
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zerve_notebook_block": os.environ.get("ZERVE_DATA_BLOCK", "warmer_homes_roi"),
        "zerve_notebook_var": os.environ.get("ZERVE_DATA_VAR", "warmer_homes_df"),
        "use_zerve_variable": os.environ.get("USE_ZERVE_VARIABLE", "").lower() in ("1", "true", "yes"),
        "params": params.to_public_dict(),
        "data_lineage": {
            "seai": seai_src,
            "cso_deprivation": cso_src,
            "fuel_prices": "aa_ireland_static_snapshot",
        },
        "limitations": df.attrs.get("model_note", ""),
        "counties": int(len(df)),
    }


def evaluate_claims(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> dict[str, Any]:
    thr = params.poverty_threshold_pct
    pct_at = df.apply(lambda r: poverty_pct_row_series(r, price_eur_l, params), axis=1)
    top5 = df.nlargest(5, "vulnerability_score")
    claim_longford_top = "Longford" in top5["county"].astype(str).tolist()
    dublin_row = df.loc[df["county"].str.casefold() == "dublin"]
    dublin_below_mean = False
    if not dublin_row.empty:
        dublin_below_mean = float(dublin_row.iloc[0]["vulnerability_score"]) < float(df["vulnerability_score"].mean())
    oil_counties = df["primary_fuel"].str.strip().str.lower() == "oil"
    claim_oil_higher = float(pct_at[oil_counties].mean()) > float(pct_at[~oil_counties].mean()) if oil_counties.any() else False
    hdd_on = params.use_hdd_adjustment
    params_off = ModelParams(**{**asdict(params), "use_hdd_adjustment": False})
    pct_no_hdd = df.apply(lambda r: poverty_pct_row_series(r, price_eur_l, params_off), axis=1)
    rank_moves = int((pct_at.rank(ascending=False) != pct_no_hdd.rank(ascending=False)).sum())

    claims = [
        {
            "id": "longford_top5",
            "statement": "Longford is in the top 5 counties by composite vulnerability score.",
            "holds": claim_longford_top,
        },
        {
            "id": "dublin_below_mean",
            "statement": "Dublin's vulnerability score is below the national mean.",
            "holds": dublin_below_mean,
        },
        {
            "id": "oil_higher_burden",
            "statement": "At the scenario €/L, mean modelled fuel-income share is higher in oil-primary counties than non-oil.",
            "holds": claim_oil_higher,
        },
        {
            "id": "hdd_moves_fuel_rank",
            "statement": "Turning off the heating-demand (HDD) multiplier changes at least one county's rank by modelled fuel-income share.",
            "holds": rank_moves > 0,
            "detail": {"counties_with_fuel_share_rank_change": rank_moves, "hdd_adjustment_was_on": hdd_on},
        },
    ]
    return {"price_eur_l": price_eur_l, "claims": claims}


def policy_options_payload(df: pd.DataFrame, params: ModelParams) -> dict[str, Any]:
    """Universal vs targeted retrofit: rough state cost / household reach."""
    g = params.retrofit_grant_eur
    crit = df[df["risk_tier"] == "Critical"]
    targeted_hh = int(crit["est_vulnerable_households"].sum())
    targeted_cost = targeted_hh * g
    all_hh = int(df["est_vulnerable_households"].sum())
    universal_cost = all_hh * g
    saving_per_hh = df["annual_saving_post_retrofit"].astype(float)
    ten_yr_saving = saving_per_hh * 10
    net_targeted = (ten_yr_saving * df["est_vulnerable_households"]).where(df["risk_tier"] == "Critical").sum() - targeted_cost
    return {
        "grant_eur_per_home": g,
        "targeted_critical": {
            "households": targeted_hh,
            "state_grant_outlay_eur": round(targeted_cost, 0),
            "approx_net_10yr_vs_grant_eur": round(float(net_targeted), 0),
        },
        "universal_all_modelled_vulnerable": {
            "households": all_hh,
            "state_grant_outlay_eur": round(universal_cost, 0),
        },
        "note": "Household counts are modelled from CSO lower-seg population ÷ avg household size, not administrative lists.",
    }


def sensitivity_payload(df: pd.DataFrame, base_params: ModelParams) -> dict[str, Any]:
    price = 2.14
    base_n = int((df.apply(lambda r: poverty_pct_row_series(r, price, base_params), axis=1) > base_params.poverty_threshold_pct).sum())
    variants: list[dict[str, Any]] = []

    p1 = ModelParams(**{**asdict(base_params), "litres_per_hh_pa": base_params.litres_per_hh_pa * 0.85})
    n1 = int((df.apply(lambda r: poverty_pct_row_series(r, price, p1), axis=1) > p1.poverty_threshold_pct).sum())
    variants.append({"label": "litres −15%", "counties_over_threshold": n1, "delta_vs_base": n1 - base_n})

    p2 = ModelParams(**{**asdict(base_params), "litres_per_hh_pa": base_params.litres_per_hh_pa * 1.15})
    n2 = int((df.apply(lambda r: poverty_pct_row_series(r, price, p2), axis=1) > p2.poverty_threshold_pct).sum())
    variants.append({"label": "litres +15%", "counties_over_threshold": n2, "delta_vs_base": n2 - base_n})

    w = dict(base_params.weights)
    w["social_deprivation_score"] = min(0.5, w.get("social_deprivation_score", 0.3) + 0.1)
    w["fuel_dependency_score"] = max(0.15, w.get("fuel_dependency_score", 0.3) - 0.05)
    w["building_inefficiency_score"] = max(0.15, w.get("building_inefficiency_score", 0.25) - 0.05)
    p3 = ModelParams(**{**asdict(base_params), "weights": w})
    merged_base, _ = load_merged_energy_base()
    rescored = add_price_shock_and_warmer_homes(score_vulnerability(merged_base, p3), p3)
    n3 = int(
        (rescored.apply(lambda r: poverty_pct_row_series(r, price, p3), axis=1) > p3.poverty_threshold_pct).sum()
    )
    variants.append(
        {
            "label": "weight social +0.1 (fuel/building −0.05 ea.)",
            "counties_over_threshold": n3,
            "delta_vs_base": n3 - base_n,
        }
    )

    p4 = ModelParams(**{**asdict(base_params), "use_hdd_adjustment": False})
    n4 = int((df.apply(lambda r: poverty_pct_row_series(r, price, p4), axis=1) > p4.poverty_threshold_pct).sum())
    variants.append({"label": "HDD multiplier off", "counties_over_threshold": n4, "delta_vs_base": n4 - base_n})

    return {"baseline": {"counties_over_threshold": base_n, "price_eur_l": price}, "variants": variants}
