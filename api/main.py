"""
Fuel Fault Lines — FastAPI hub for Zerve deployment.

Zerve (see https://docs.zerve.ai/guide/notebook-view/deployment/fast-api):
  - Run your notebook blocks first so dataframes exist, then either:
    (a) Keep this file self-contained (default): model rebuilds on startup via pipeline.py, or
    (b) Uncomment ZERVE_BLOCK / ZERVE_VAR and set them to your block title + variable name, e.g.
        variable("warmer_homes_roi", "warmer_homes_df")

Run locally:
  cd api && pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from pipeline import (
    POVERTY_THRESHOLD_PCT,
    build_price_history_payload,
    build_warmer_homes_dataframe,
    county_row_to_api,
    get_county_deep_dive_dict,
)

# --- Optional: Zerve notebook injection (block title must match your notebook UI) ---
ZERVE_BLOCK = os.environ.get("ZERVE_DATA_BLOCK", "warmer_homes_roi")
ZERVE_VAR = os.environ.get("ZERVE_DATA_VAR", "warmer_homes_df")
USE_ZERVE_VARIABLE = os.environ.get("USE_ZERVE_VARIABLE", "").lower() in ("1", "true", "yes")


def _load_warmer_homes_df() -> pd.DataFrame:
    if USE_ZERVE_VARIABLE:
        try:
            from zerve import variable  # type: ignore

            df = variable(ZERVE_BLOCK, ZERVE_VAR)
            if df is None or not hasattr(df, "columns"):
                raise TypeError("Zerve variable is not a DataFrame")
            return df
        except Exception as e:
            raise RuntimeError(
                f"USE_ZERVE_VARIABLE=1 but could not load variable({ZERVE_BLOCK!r}, {ZERVE_VAR!r}): {e}"
            ) from e
    return build_warmer_homes_dataframe()


class AppState:
    df: pd.DataFrame | None = None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.df = _load_warmer_homes_df()
    yield
    state.df = None


app = FastAPI(
    title="Fuel Fault Lines API",
    description="County energy vulnerability, scenarios, history, deep-dive (ZerveHack / SEAI / CSO / AA)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _df() -> pd.DataFrame:
    if state.df is None:
        raise HTTPException(status_code=503, detail="Model not initialised")
    return state.df


def _poverty_pct_row(r: pd.Series, price: float) -> float:
    from pipeline import LITRES_PER_HH_PA

    income = float(r["estimated_annual_income"])
    if income <= 0:
        return 0.0
    litres = LITRES_PER_HH_PA * (0.5 + float(r["fuel_dependency_score"]) / 100.0)
    return round(litres * price / income * 100, 2)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/county/{county}")
def get_county(
    county: str,
    fuel_price: float = Query(2.14, ge=0.5, le=8.0, description="Diesel/heating-oil proxy €/L"),
) -> dict[str, Any]:
    df = _df()
    key = county.strip()
    row = df.loc[df["county"].str.casefold() == key.casefold()]
    if row.empty:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county}")
    return county_row_to_api(row.iloc[0], fuel_price)


@app.get("/scenario")
def scenario(
    price_a: float = Query(..., ge=0.5, le=8.0),
    price_b: float = Query(..., ge=0.5, le=8.0),
) -> dict[str, Any]:
    df = _df()
    counties: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        pa = _poverty_pct_row(r, price_a)
        pb = _poverty_pct_row(r, price_b)
        in_a = pa > POVERTY_THRESHOLD_PCT
        in_b = pb > POVERTY_THRESHOLD_PCT
        counties.append(
            {
                "county": str(r["county"]),
                "poverty_pct_a": pa,
                "poverty_pct_b": pb,
                "in_poverty_a": in_a,
                "in_poverty_b": in_b,
                "newly_at_risk": (not in_a) and in_b,
                "households_a": int(r["est_vulnerable_households"]) if in_a else 0,
                "households_b": int(r["est_vulnerable_households"]) if in_b else 0,
            }
        )
    return {"counties": counties, "price_a": price_a, "price_b": price_b}


@app.get("/history")
def history() -> dict[str, Any]:
    return build_price_history_payload(_df())


@app.get("/deep-dive/{county}")
def deep_dive(
    county: str,
    fuel_price: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    df = _df()
    key = county.strip()
    match = df.loc[df["county"].str.casefold() == key.casefold()]
    if match.empty:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county}")
    canon = str(match.iloc[0]["county"])
    try:
        return get_county_deep_dive_dict(df, canon, fuel_price)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# Zerve / platforms often expect `app` at module level (above).
