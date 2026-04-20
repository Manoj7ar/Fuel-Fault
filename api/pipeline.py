"""
Fuel Fault Lines — data pipeline (notebook logic consolidated for API use).
Sources: SEAI-style county energy (remote CSV), CSO FY068, AA Ireland fuel prices.
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
    raise RuntimeError(
        "Could not load SEAI county energy CSV from any configured URL. "
        "Check network access and SEAI_URLS in pipeline.py."
    )


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

    val = validation_payload(df, params, price_eur_l)
    ber_check = next((c for c in val.get("checks", []) if c.get("id") == "ber_bad_vs_fuel_share"), {})
    vuln_check = next((c for c in val.get("checks", []) if c.get("id") == "vulnerability_vs_fuel_share"), {})
    fp = flip_points_payload(df, params, reference_price_eur_l=price_eur_l)
    finite_breach = [
        x["breach_price_eur_l"]
        for x in fp.get("counties", [])
        if x.get("breach_price_eur_l") is not None
    ]
    breach_spread = (max(finite_breach) - min(finite_breach)) if len(finite_breach) > 1 else 0.0

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
        {
            "id": "ber_correlates_with_stress",
            "statement": "Poor BER stock (D–G %) is positively correlated with modelled fuel-income share (internal sanity check).",
            "holds": bool(ber_check.get("passes")),
            "detail": {"r": ber_check.get("r"), "n": ber_check.get("n")},
        },
        {
            "id": "vulnerability_aligns_with_fuel_share",
            "statement": "Composite vulnerability index is strongly aligned with modelled fuel-income share (internal consistency).",
            "holds": bool(vuln_check.get("passes")),
            "detail": {"r": vuln_check.get("r"), "n": vuln_check.get("n")},
        },
        {
            "id": "breach_price_spread",
            "statement": "Counties span a non-trivial range of €/L 'breach' prices where they cross the fuel-income threshold (≥ €0.50 spread).",
            "holds": breach_spread >= 0.5,
            "detail": {"eur_l_spread": round(breach_spread, 4), "n_with_finite_breach": len(finite_breach)},
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


def _pearsonr_xy(x: list[float], y: list[float]) -> tuple[float, int]:
    """Pearson r using numpy; returns (r, n_pairs). Empty or constant → (nan, 0)."""
    if len(x) != len(y) or len(x) < 2:
        return float("nan"), 0
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    if len(a) < 2:
        return float("nan"), int(len(a))
    if np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return float("nan"), int(len(a))
    r = float(np.corrcoef(a, b)[0, 1])
    return r, int(len(a))


def validation_payload(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> dict[str, Any]:
    """Internal consistency checks — not ground-truth validation against admin fuel poverty stats."""
    pct = df.apply(lambda r: poverty_pct_row_series(r, price_eur_l, params), axis=1)
    vuln = df["vulnerability_score"].astype(float)
    fds = df["fuel_dependency_score"].astype(float)
    ber_bad = df["pct_ber_defg"].astype(float)
    r_v, n1 = _pearsonr_xy(vuln.tolist(), pct.tolist())
    r_fd, n2 = _pearsonr_xy(fds.tolist(), pct.tolist())
    r_ber, n3 = _pearsonr_xy(ber_bad.tolist(), pct.tolist())
    cso_src = df.attrs.get("cso_source", "unknown")
    return {
        "price_eur_l": price_eur_l,
        "cso_deprivation_source": cso_src,
        "checks": [
            {
                "id": "vulnerability_vs_fuel_share",
                "description": "Pearson correlation: composite vulnerability index vs modelled fuel-income share (expect strong positive).",
                "r": round(r_v, 4) if np.isfinite(r_v) else None,
                "n": n1,
                "passes": bool(np.isfinite(r_v) and r_v > 0.55),
            },
            {
                "id": "fuel_driver_vs_fuel_share",
                "description": "Pearson correlation: fuel dependency sub-score vs modelled fuel-income share (expect positive).",
                "r": round(r_fd, 4) if np.isfinite(r_fd) else None,
                "n": n2,
                "passes": bool(np.isfinite(r_fd) and r_fd > 0.25),
            },
            {
                "id": "ber_bad_vs_fuel_share",
                "description": "Pearson correlation: % BER D–G vs modelled fuel-income share (expect positive).",
                "r": round(r_ber, 4) if np.isfinite(r_ber) else None,
                "n": n3,
                "passes": bool(np.isfinite(r_ber) and r_ber > 0.15),
            },
        ],
        "note": "Income is a synthetic function of deprivation in this model, so raw deprivation vs fuel % is not a clean sanity test; we use the composite index instead.",
    }


def breach_price_eur_l(r: pd.Series, params: ModelParams) -> float | None:
    """Minimum €/L at which modelled fuel share crosses poverty threshold (binary search)."""
    thr = params.poverty_threshold_pct

    def pct(p: float) -> float:
        return poverty_pct_row_series(r, p, params)

    lo, hi = 0.5, 8.0
    if pct(hi) <= thr:
        return None
    if pct(lo) > thr:
        return round(lo, 3)
    for _ in range(45):
        mid = (lo + hi) / 2.0
        if pct(mid) > thr:
            hi = mid
        else:
            lo = mid
    return round(hi, 4)


def flip_points_payload(
    df: pd.DataFrame, params: ModelParams, reference_price_eur_l: float = 2.14
) -> dict[str, Any]:
    """Per-county €/L where the model crosses the fuel-income threshold — 'fault line' prices."""
    ref = float(reference_price_eur_l)
    rows = []
    for _, r in df.iterrows():
        bp = breach_price_eur_l(r, params)
        rows.append(
            {
                "county": str(r["county"]),
                "province": str(r["province"]),
                "breach_price_eur_l": bp,
                "risk_tier": str(r["risk_tier"]),
                "vulnerability_score": round(float(r["vulnerability_score"]), 2),
            }
        )
    rows.sort(key=lambda x: (x["breach_price_eur_l"] is None, x["breach_price_eur_l"] or 99))
    already = sum(
        1 for x in rows if x["breach_price_eur_l"] is not None and x["breach_price_eur_l"] <= ref
    )
    return {
        "poverty_threshold_pct": params.poverty_threshold_pct,
        "reference_price_eur_l": ref,
        "counties_already_over_at_reference": already,
        "counties": rows,
    }


def distribution_payload(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> dict[str, Any]:
    pct = df.apply(lambda r: poverty_pct_row_series(r, price_eur_l, params), axis=1).astype(float)
    arr = np.sort(pct.values)
    n = len(arr)
    thr = params.poverty_threshold_pct

    def q(p: float) -> float:
        if n == 0:
            return float("nan")
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return float(arr[idx])

    deciles = {f"D{i}": round(q(i / 10), 2) for i in range(1, 10)}
    return {
        "price_eur_l": price_eur_l,
        "poverty_threshold_pct": thr,
        "n_counties": n,
        "mean": round(float(arr.mean()), 2) if n else None,
        "std": round(float(arr.std()), 2) if n > 1 else None,
        "min": round(float(arr.min()), 2) if n else None,
        "max": round(float(arr.max()), 2) if n else None,
        "deciles": deciles,
        "counties_over_threshold": int((pct > thr).sum()),
    }


def regional_summary_payload(df: pd.DataFrame, params: ModelParams, fuel_price: float = 2.14) -> dict[str, Any]:
    thr = params.poverty_threshold_pct
    out = []
    for prov, g in df.groupby("province"):
        pct = g.apply(lambda r: poverty_pct_row_series(r, fuel_price, params), axis=1)
        out.append(
            {
                "province": str(prov),
                "counties": int(len(g)),
                "mean_vulnerability": round(float(g["vulnerability_score"].mean()), 2),
                "mean_fuel_share_pct": round(float(pct.mean()), 2),
                "counties_over_threshold": int((pct > thr).sum()),
                "critical_counties": int((g["risk_tier"] == "Critical").sum()),
            }
        )
    out.sort(key=lambda x: -x["mean_vulnerability"])
    return {"fuel_price_eur_l": fuel_price, "poverty_threshold_pct": thr, "regions": out}


def national_snapshot_payload(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> dict[str, Any]:
    pct = df.apply(lambda r: poverty_pct_row_series(r, price_eur_l, params), axis=1)
    thr = params.poverty_threshold_pct
    over = df.loc[pct > thr]
    crit = df[df["risk_tier"] == "Critical"]
    worst = df.loc[pct.idxmax()] if len(df) else None
    best = df.loc[pct.idxmin()] if len(df) else None
    return {
        "headline_price_eur_l": price_eur_l,
        "poverty_threshold_pct": thr,
        "counties_over_threshold": int((pct > thr).sum()),
        "total_counties": int(len(df)),
        "vulnerable_households_modelled": int(df["est_vulnerable_households"].sum()),
        "vulnerable_households_in_stress_counties": int(over["est_vulnerable_households"].sum()) if len(over) else 0,
        "critical_tier_counties": int(len(crit)),
        "highest_fuel_share": (
            {
                "county": str(worst["county"]),
                "poverty_pct": round(float(pct.max()), 2),
            }
            if worst is not None
            else None
        ),
        "lowest_fuel_share": (
            {
                "county": str(best["county"]),
                "poverty_pct": round(float(pct.min()), 2),
            }
            if best is not None
            else None
        ),
        "mean_fuel_share_pct": round(float(pct.mean()), 2),
        "data_lineage": {
            "seai": df.attrs.get("seai_source", "unknown"),
            "cso": df.attrs.get("cso_source", "unknown"),
        },
    }


def scenario_curve_payload(
    df: pd.DataFrame,
    params: ModelParams,
    price_min: float = 1.5,
    price_max: float = 4.0,
    steps: int = 26,
) -> dict[str, Any]:
    """Counties over fuel-income threshold vs €/L (model curve for charts and video)."""
    thr = params.poverty_threshold_pct
    prices = np.linspace(price_min, price_max, max(5, int(steps))).round(4).tolist()
    series = []
    for p in prices:
        pct = df.apply(lambda r: poverty_pct_row_series(r, float(p), params), axis=1)
        over_mask = pct > thr
        n_over = int(over_mask.sum())
        names = sorted(df.loc[over_mask, "county"].astype(str).tolist())[:28]
        series.append(
            {
                "price_eur_l": float(p),
                "counties_over_threshold": n_over,
                "mean_fuel_share_pct": round(float(pct.mean()), 2),
                "counties_over_names": names,
            }
        )
    return {
        "poverty_threshold_pct": thr,
        "price_min": price_min,
        "price_max": price_max,
        "points": series,
    }


def ranking_stability_payload(df: pd.DataFrame, params: ModelParams, top_k: int = 10) -> dict[str, Any]:
    """How stable county vulnerability rankings are under alternative composite weights."""
    merged_base, _ = load_merged_energy_base()
    base_scored = score_vulnerability(merged_base, params)
    base_order = base_scored.sort_values("vulnerability_score", ascending=False)["county"].astype(str).tolist()
    k = min(top_k, len(base_order))
    top_set = set(base_order[:k])

    def norm_weights(w: dict[str, float]) -> dict[str, float]:
        s = sum(w.values())
        if s <= 0:
            return dict(w)
        return {kk: round(float(vv) / s, 4) for kk, vv in w.items()}

    def overlap(wn: dict[str, float]) -> int:
        p = ModelParams(**{**asdict(params), "weights": wn})
        alt = score_vulnerability(merged_base, p)
        ord2 = alt.sort_values("vulnerability_score", ascending=False)["county"].astype(str).tolist()
        return len(top_set & set(ord2[:k]))

    w0 = dict(params.weights)
    raw_variants = [
        ("baseline (current weights)", w0),
        (
            "tilt social (+0.10 from fuel)",
            {
                "fuel_dependency_score": max(0.12, w0.get("fuel_dependency_score", 0.3) - 0.1),
                "building_inefficiency_score": w0.get("building_inefficiency_score", 0.25),
                "social_deprivation_score": min(0.5, w0.get("social_deprivation_score", 0.3) + 0.1),
                "energy_intensity_score": w0.get("energy_intensity_score", 0.15),
            },
        ),
        (
            "tilt fuel (+0.10 from social)",
            {
                "fuel_dependency_score": min(0.5, w0.get("fuel_dependency_score", 0.3) + 0.1),
                "building_inefficiency_score": w0.get("building_inefficiency_score", 0.25),
                "social_deprivation_score": max(0.12, w0.get("social_deprivation_score", 0.3) - 0.1),
                "energy_intensity_score": w0.get("energy_intensity_score", 0.15),
            },
        ),
    ]
    out_variants = []
    for label, w in raw_variants:
        wn = norm_weights(w)
        ov = k if label.startswith("baseline") else overlap(wn)
        out_variants.append(
            {
                "label": label,
                "weights": wn,
                "top_k": k,
                "overlap_with_baseline_top_k": ov,
            }
        )
    return {
        "top_k": k,
        "baseline_top_counties": base_order[:k],
        "variants": out_variants,
        "note": "Overlap = how many of the baseline top-k counties remain in the top-k under alternative weights.",
    }


def narrative_insights_payload(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> dict[str, Any]:
    """Human-readable bullets for judges, Devpost, and video voiceover."""
    snap = national_snapshot_payload(df, params, price_eur_l)
    claims = evaluate_claims(df, params, price_eur_l)
    breach = flip_points_payload(df, params, reference_price_eur_l=price_eur_l)
    reg = regional_summary_payload(df, params, price_eur_l)
    curve = scenario_curve_payload(df, params, price_eur_l - 0.5, price_eur_l + 1.0, 16)
    thr = params.poverty_threshold_pct

    bullets: list[str] = []
    bullets.append(
        f"At €{price_eur_l:.2f}/L, {snap['counties_over_threshold']} of {snap['total_counties']} counties exceed the "
        f"{thr:g}% modelled fuel-income threshold — mean share {snap['mean_fuel_share_pct']}%."
    )
    if snap.get("highest_fuel_share"):
        hf = snap["highest_fuel_share"]
        bullets.append(f"Tightest squeeze: {hf['county']} at {hf['poverty_pct']}% of synthetic income on the liquid-fuel proxy.")
    if snap.get("lowest_fuel_share"):
        lf = snap["lowest_fuel_share"]
        bullets.append(f"Lowest proxy burden: {lf['county']} at {lf['poverty_pct']}%.")

    passed = sum(1 for c in claims.get("claims", []) if c.get("holds"))
    total = len(claims.get("claims", []))
    bullets.append(
        f"{total} registered consistency checks: {passed}/{total} pass on the live dataframe (see /model/claims)."
    )

    finite_b = [x["breach_price_eur_l"] for x in breach.get("counties", []) if x.get("breach_price_eur_l") is not None]
    if len(finite_b) > 1:
        bullets.append(
            f"Breach €/L (where counties cross the threshold) spans €{min(finite_b):.2f}–€{max(finite_b):.2f} — "
            "a wide geographic spread in price resilience."
        )

    if reg.get("regions"):
        top_r = reg["regions"][0]
        bullets.append(
            f"By province at this €/L, {top_r['province']} shows the highest mean vulnerability ({top_r['mean_vulnerability']}) "
            f"and {top_r['counties_over_threshold']}/{top_r['counties']} counties over the line."
        )

    # Price sensitivity one-liner from curve
    pts = curve.get("points", [])
    if len(pts) >= 2:
        n0 = pts[0]["counties_over_threshold"]
        n1 = pts[-1]["counties_over_threshold"]
        p0 = pts[0]["price_eur_l"]
        p1 = pts[-1]["price_eur_l"]
        bullets.append(
            f"Along the model curve from €{p0:.2f} to €{p1:.2f}/L, counties over threshold move from {n0} to {n1}."
        )

    return {
        "price_eur_l": price_eur_l,
        "bullets": bullets,
        "elevator_pitch": (
            "Fuel Fault Lines fuses SEAI county energy profiles with CSO deprivation, "
            "adds a heating-demand-aware liquid-fuel proxy, and exposes every assumption through a FastAPI hub "
            "so policymakers can stress-test price shocks, compare counties, and export briefings."
        ),
    }


def _word_trim(text: str, max_words: int = 300) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(".,;:") + "…"


def headline_insight_payload(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> dict[str, Any]:
    """Single-sentence + short context for dashboard and judges."""
    snap = national_snapshot_payload(df, params, price_eur_l)
    breach = flip_points_payload(df, params, reference_price_eur_l=price_eur_l)
    stab = ranking_stability_payload(df, params, top_k=10)
    thr = params.poverty_threshold_pct
    finite_b = [x["breach_price_eur_l"] for x in breach.get("counties", []) if x.get("breach_price_eur_l") is not None]

    parts: list[str] = [
        f"At €{price_eur_l:.2f}/L, {snap['counties_over_threshold']} of {snap['total_counties']} counties exceed the "
        f"{thr:g}% modelled fuel-income line (mean proxy burden {snap['mean_fuel_share_pct']}%)."
    ]
    if len(finite_b) > 1:
        parts.append(
            f"The €/L where counties first cross that line spans €{min(finite_b):.2f}–€{max(finite_b):.2f} "
            "— geographic price resilience is uneven, not uniform."
        )
    tilt_social = next((v for v in stab.get("variants", []) if "social" in v.get("label", "").lower()), None)
    if tilt_social and stab.get("top_k"):
        k = stab["top_k"]
        ov = tilt_social.get("overlap_with_baseline_top_k", 0)
        parts.append(
            f"Under a social-heavy composite tilt, {ov}/{k} of the baseline highest-vulnerability counties remain in the top {k} "
            "— the urgency ranking is partially robust to how we weight drivers."
        )

    headline = " ".join(parts)
    return {
        "price_eur_l": price_eur_l,
        "headline": headline,
        "supporting_bullets": narrative_insights_payload(df, params, price_eur_l).get("bullets", [])[:4],
    }


def submission_pack_payload(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> dict[str, Any]:
    """Devpost / video / social scaffolding aligned with ZerveHack requirements."""
    meta = model_meta_dict(df, params)
    narr = narrative_insights_payload(df, params, price_eur_l)
    head = headline_insight_payload(df, params, price_eur_l)
    snap = national_snapshot_payload(df, params, price_eur_l)

    why_zerve = [
        "Exploration stays in a notebook-first loop (Zerve): merge SEAI + CSO, score counties, iterate joins and columns without redeploying a separate analytics repo.",
        "The same dataframe ships as a documented FastAPI hub (/docs) with scenario curves, breach €/L, validation, and exports — analysis and production share one path.",
        "Assumption sliders and POST /model/params let judges stress-test the model live; a static PDF or one-off script would not.",
    ]

    zerve_video_checklist = [
        "State the question (who gets squeezed when liquid fuel prices move, and where?).",
        "Show Zerve: notebook or blocks that produce the county dataframe (e.g. warmer_homes_df / warmer_homes_roi).",
        "Show deploy: FastAPI hub live (health + version in /health or /meta).",
        "Demo the app: scenario €/L slider → Key finding card → scenario curve chart.",
        "Open Method & lab or /docs — mention OpenAPI tags and one analytical endpoint (e.g. /model/breach-prices).",
        "Optional: Gemini analyst with a key from Google AI Studio (client-side only).",
        "Close with limitations: synthetic income bands; proxy not CSO official fuel poverty.",
    ]

    hackathon_required = [
        "Public Zerve project — runs without errors (Devpost requirement).",
        "Project summary — max 300 words (use draft below as a starting point).",
        "Demo video — max 3 minutes (follow checklist above).",
        "Social post — tag @Zerve_AI (X) or Zerve on LinkedIn per Devpost.",
        "Deployed API/app — strongly encouraged for judging priority.",
    ]

    draft = f"""Fuel Fault Lines asks where Irish counties cross a modelled energy stress line when liquid fuel prices move, and whether vulnerability rankings hold up under different assumptions.

We built an end-to-end pipeline in Zerve: county energy profiles (SEAI-style), deprivation (CSO FY068 when available), and a heating-demand-aware litres proxy combined with a FastAPI hub so the same model powers both exploration and a public API. The hub exposes scenario curves, per-county breach €/L, internal consistency checks, regional roll-ups, and markdown exports for briefings.

At €{price_eur_l:.2f}/L, {snap['counties_over_threshold']} of {snap['total_counties']} counties exceed a {snap.get('poverty_threshold_pct', params.poverty_threshold_pct):g}% fuel-income proxy threshold, with mean burden {snap['mean_fuel_share_pct']}%. {head['headline']}

The dashboard and /export/briefing turn these outputs into copy-ready narrative for policymakers and hackathon judges. Limitations are explicit: income is a synthetic band from deprivation, not survey microdata; fuel use is a proxy, not metered bills.

Stack: Zerve notebook → optional zerve.variable injection → FastAPI (OpenAPI at /docs) → single-page dashboard; optional Google Gemini chat in-browser for Q&A. This submission demonstrates that question-driven iteration plus deployment without leaving the analytical environment is the fastest path from data to usable policy tooling."""

    draft = _word_trim(draft, 300)
    wc = len(draft.split())

    social = (
        "Built Fuel Fault Lines on @Zerve_AI — Irish county fuel stress, breach €/L, scenario API + dashboard. "
        "Zerve notebook → FastAPI → live stress tests. #ZerveHack"
    )

    return {
        "price_eur_l": price_eur_l,
        "headline_insight": head["headline"],
        "elevator_pitch": narr.get("elevator_pitch", ""),
        "why_zerve_not_spreadsheet": why_zerve,
        "zerve_video_checklist": zerve_video_checklist,
        "devpost_required_checklist": hackathon_required,
        "devpost_summary_draft": draft,
        "devpost_summary_word_count": wc,
        "social_post_draft_x": social,
        "rubric_talking_points": {
            "analytical_depth": "Scenario curve, breach prices, sensitivity, ranking stability, validation correlations — stress-tested assumptions, not a single chart.",
            "end_to_end_workflow": "Zerve dataframe → FastAPI hub (/docs) → deployed app; demo script in /meta.",
            "storytelling": "Key finding card, narrative endpoint, national briefing export, Method lab human summaries.",
            "creativity": "Fault-line €/L + HDD proxy + optional Gemini analyst; Ireland-specific policy framing.",
        },
        "links": {
            "zervehack_devpost": "https://zervehack.devpost.com",
            "google_ai_studio_key": "https://aistudio.google.com/apikey",
        },
        "api_meta": {
            "api_version": meta.get("api_version"),
            "git_rev": meta.get("git_rev"),
            "zerve_notebook_block": meta.get("zerve_notebook_block"),
            "zerve_notebook_var": meta.get("zerve_notebook_var"),
        },
    }


def build_national_briefing_markdown(df: pd.DataFrame, params: ModelParams, price_eur_l: float = 2.14) -> str:
    snap = national_snapshot_payload(df, params, price_eur_l)
    val = validation_payload(df, params, price_eur_l)
    meta = model_meta_dict(df, params)
    head_line = headline_insight_payload(df, params, price_eur_l)["headline"]
    lines = [
        "# Fuel Fault Lines — national briefing (auto-generated)",
        "",
        f"**Scenario:** €{price_eur_l:.2f}/L · **Threshold:** {snap['poverty_threshold_pct']}% modelled fuel-income share",
        "",
        "## Key finding (one take for judges)",
        "",
        head_line,
        "",
        "*Full submission scaffolding: `GET /insights/submission-pack` (Devpost draft, video checklist, social).*",
        "",
        "## Headline numbers",
        "",
        f"- Counties over threshold: **{snap['counties_over_threshold']}** / {snap['total_counties']}",
        f"- Critical-tier counties (composite index): **{snap['critical_tier_counties']}**",
        f"- Modelled vulnerable households (national): **{snap['vulnerable_households_modelled']:,}**",
        f"- Mean fuel share: **{snap['mean_fuel_share_pct']}%**",
    ]
    if snap.get("highest_fuel_share"):
        lines.append(
            f"- Highest stress: **{snap['highest_fuel_share']['county']}** ({snap['highest_fuel_share']['poverty_pct']}%)"
        )
    if snap.get("lowest_fuel_share"):
        lines.append(
            f"- Lowest stress: **{snap['lowest_fuel_share']['county']}** ({snap['lowest_fuel_share']['poverty_pct']}%)"
        )
    lines.extend(
        [
            "",
            "## Internal consistency (model)",
            "",
        ]
    )
    for c in val.get("checks", []):
        st = "✓" if c.get("passes") else "✗"
        lines.append(
            f"- {st} **{c.get('id')}** — r={c.get('r')}, n={c.get('n')}"
        )
    narr = narrative_insights_payload(df, params, price_eur_l)
    lines.extend(["", "## Narrative (auto)", ""])
    for b in narr.get("bullets", []):
        lines.append(f"- {b}")
    lines.append("")
    lines.append(f"*Elevator:* {narr.get('elevator_pitch', '')}")
    lines.extend(
        [
            "",
            "## Data lineage",
            "",
            f"- SEAI: {snap['data_lineage']['seai']}",
            f"- CSO deprivation: {snap['data_lineage']['cso']}",
            "",
            "## Limitations",
            "",
            meta.get("limitations", ""),
            "",
            f"_API {meta.get('api_version', '')} · `{meta.get('git_rev', '')}` · {meta.get('built_at_utc', '')}_",
        ]
    )
    return "\n".join(lines)


def model_meta_dict(df: pd.DataFrame, params: ModelParams) -> dict[str, Any]:
    cso_src = df.attrs.get("cso_source", "unknown")
    seai_src = df.attrs.get("seai_source", "unknown")
    return {
        "api_version": "1.3",
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
        "demo_script_for_judges": [
            "1. Zerve: question → notebook blocks → dataframe warmer_homes_df (block warmer_homes_roi).",
            "2. Deploy this FastAPI hub; optional USE_ZERVE_VARIABLE=1 to inject the notebook df.",
            "3. Dashboard: Key finding card + scenario curve + thesis banner (live €/L).",
            "4. Demo & submit page: copy Devpost draft, video checklist, social post; link to ZerveHack on Devpost.",
            "5. Method & lab: human-readable summaries + raw JSON; /insights/submission-pack for full pack.",
            "6. Open /docs — OpenAPI contract (tagged).",
        ],
        "key_endpoints": [
            "/health",
            "/meta",
            "/docs",
            "/national/snapshot",
            "/insights/headline",
            "/insights/submission-pack",
            "/model/scenario-curve",
            "/model/validation",
            "/model/distribution",
            "/model/breach-prices",
            "/model/ranking-stability",
            "/insights/narrative",
            "/insights/regional",
            "/export/briefing",
            "/model/params",
        ],
    }
