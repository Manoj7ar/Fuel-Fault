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
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from pipeline import (
    ModelParams,
    build_national_briefing_markdown,
    build_price_history_payload,
    build_warmer_homes_dataframe,
    county_row_to_api,
    distribution_payload,
    evaluate_claims,
    flip_points_payload,
    get_county_deep_dive_dict,
    model_meta_dict,
    national_snapshot_payload,
    policy_options_payload,
    poverty_pct_row_series,
    regional_summary_payload,
    sensitivity_payload,
    validation_payload,
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


def _rebuild_df(params: ModelParams) -> pd.DataFrame:
    if USE_ZERVE_VARIABLE:
        return _load_warmer_homes_df()
    return build_warmer_homes_dataframe(params)


class AppState:
    df: pd.DataFrame | None = None
    params: ModelParams = ModelParams()


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.params = ModelParams()
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


@app.get("/health")
def health() -> dict[str, Any]:
    meta = model_meta_dict(_df(), state.params) if state.df is not None else {}
    return {"status": "ok", **{k: meta[k] for k in ("api_version", "git_rev", "built_at_utc") if k in meta}}


@app.get("/meta")
def meta() -> dict[str, Any]:
    return model_meta_dict(_df(), state.params)


@app.get("/counties")
def list_counties() -> dict[str, Any]:
    df = _df()
    names = sorted(df["county"].astype(str).tolist(), key=str.casefold)
    return {"counties": names, "count": len(names)}


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
    return county_row_to_api(row.iloc[0], fuel_price, state.params)


@app.get("/scenario")
def scenario(
    price_a: float = Query(..., ge=0.5, le=8.0),
    price_b: float = Query(..., ge=0.5, le=8.0),
) -> dict[str, Any]:
    df = _df()
    thr = state.params.poverty_threshold_pct
    counties: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        pa = poverty_pct_row_series(r, price_a, state.params)
        pb = poverty_pct_row_series(r, price_b, state.params)
        in_a = pa > thr
        in_b = pb > thr
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


@app.get("/compare/counties")
def compare_counties(
    county_a: str = Query(..., min_length=2),
    county_b: str = Query(..., min_length=2),
    fuel_price: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    df = _df()
    thr = state.params.poverty_threshold_pct

    def row_for(name: str) -> pd.Series:
        m = df.loc[df["county"].str.casefold() == name.strip().casefold()]
        if m.empty:
            raise HTTPException(status_code=404, detail=f"Unknown county: {name}")
        return m.iloc[0]

    try:
        ra = row_for(county_a)
        rb = row_for(county_b)
    except HTTPException:
        raise
    pa = county_row_to_api(ra, fuel_price, state.params)
    pb = county_row_to_api(rb, fuel_price, state.params)
    take = (
        "vulnerability_score",
        "risk_tier",
        "poverty_pct_at_price",
        "in_energy_poverty",
        "estimated_annual_income",
        "annual_oil_bill_eur",
        "est_vulnerable_households",
        "cliff_price_eur",
        "model_litres_proxy_pa",
        "hdd_multiplier",
    )
    diff: dict[str, Any] = {"fuel_price": fuel_price, "threshold_pct": thr}
    for k in take:
        va = pa.get(k)
        vb = pb.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            diff[k] = round(float(va) - float(vb), 4 if k == "poverty_pct_at_price" else 3)
        elif va != vb:
            diff[k] = {"a": va, "b": vb}
    narrative = (
        f"At €{fuel_price:.2f}/L, {pa['county']} has {pa['poverty_pct_at_price']:.1f}% of income going to the modelled "
        f"liquid-fuel proxy vs {pb['poverty_pct_at_price']:.1f}% in {pb['county']} "
        f"(>{thr:.0f}% line = energy poverty in this dashboard)."
    )
    return {"a": pa, "b": pb, "delta_a_minus_b": diff, "takeaway": narrative}


@app.get("/export/county/{county}", response_class=PlainTextResponse)
def export_county_brief(
    county: str,
    fuel_price: float = Query(2.14, ge=0.5, le=8.0),
) -> PlainTextResponse:
    df = _df()
    key = county.strip()
    match = df.loc[df["county"].str.casefold() == key.casefold()]
    if match.empty:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county}")
    canon = str(match.iloc[0]["county"])
    row_api = county_row_to_api(match.iloc[0], fuel_price, state.params)
    meta = model_meta_dict(df, state.params)
    lines = [
        f"# Fuel Fault Lines — {canon}",
        "",
        f"- **Scenario:** €{fuel_price:.2f}/L (diesel / heating-oil proxy)",
        f"- **Vulnerability index:** {row_api['vulnerability_score']} ({row_api['risk_tier']})",
        f"- **Modelled fuel share of income:** {row_api['poverty_pct_at_price']}% (threshold {state.params.poverty_threshold_pct:g}%)",
        f"- **Synthetic income band (model):** €{row_api['estimated_annual_income']:,.0f}",
        f"- **Vulnerable households (model):** {row_api['est_vulnerable_households']:,}",
        f"- **HDD multiplier:** {row_api.get('hdd_multiplier', '—')}",
        "",
        "## Limitations",
        meta.get("limitations", ""),
        "",
        f"Data: SEAI ({meta['data_lineage']['seai']}), CSO ({meta['data_lineage']['cso_deprivation']}).",
        f"API git `{meta.get('git_rev', '?')}` · built {meta.get('built_at_utc', '')}",
    ]
    return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8")


@app.get("/model/claims")
def model_claims(
    price_eur_l: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    return evaluate_claims(_df(), state.params, price_eur_l)


@app.get("/model/sensitivity")
def model_sensitivity() -> dict[str, Any]:
    return sensitivity_payload(_df(), state.params)


@app.get("/model/policy")
def model_policy() -> dict[str, Any]:
    return policy_options_payload(_df(), state.params)


@app.get("/model/validation")
def model_validation(
    price_eur_l: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    return validation_payload(_df(), state.params, price_eur_l)


@app.get("/model/distribution")
def model_distribution(
    price_eur_l: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    return distribution_payload(_df(), state.params, price_eur_l)


@app.get("/model/breach-prices")
def model_breach_prices(
    reference_price_eur_l: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    return flip_points_payload(_df(), state.params, reference_price_eur_l=reference_price_eur_l)


@app.get("/national/snapshot")
def national_snapshot(
    price_eur_l: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    return national_snapshot_payload(_df(), state.params, price_eur_l)


@app.get("/insights/regional")
def insights_regional(
    fuel_price: float = Query(2.14, ge=0.5, le=8.0),
) -> dict[str, Any]:
    return regional_summary_payload(_df(), state.params, fuel_price)


@app.get("/export/briefing", response_class=PlainTextResponse)
def export_national_briefing(
    price_eur_l: float = Query(2.14, ge=0.5, le=8.0),
) -> PlainTextResponse:
    body = build_national_briefing_markdown(_df(), state.params, price_eur_l)
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8")


@app.post("/model/params")
def model_params_update(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Adjust tunable assumptions (session-local); rebuilds in-memory model unless using Zerve variable."""
    cur = state.params
    w = dict(cur.weights)
    if "weights" in body and isinstance(body["weights"], dict):
        for k, v in body["weights"].items():
            if k in w and isinstance(v, (int, float)):
                w[k] = float(v)
    if abs(sum(w.values()) - 1.0) > 0.02:
        raise HTTPException(status_code=400, detail="weights must sum to ~1.0")
    try:
        new_p = ModelParams(
            litres_per_hh_pa=float(body.get("litres_per_hh_pa", cur.litres_per_hh_pa)),
            poverty_threshold_pct=float(body.get("poverty_threshold_pct", cur.poverty_threshold_pct)),
            weights=w,
            income_dep_min=float(body.get("income_dep_min", cur.income_dep_min)),
            income_dep_max=float(body.get("income_dep_max", cur.income_dep_max)),
            income_min_eur=float(body.get("income_min_eur", cur.income_min_eur)),
            income_max_eur=float(body.get("income_max_eur", cur.income_max_eur)),
            retrofit_grant_eur=float(body.get("retrofit_grant_eur", cur.retrofit_grant_eur)),
            retrofit_saving_fraction=float(body.get("retrofit_saving_fraction", cur.retrofit_saving_fraction)),
            use_hdd_adjustment=bool(body.get("use_hdd_adjustment", cur.use_hdd_adjustment)),
            fuel_allowance_pa_eur=float(body.get("fuel_allowance_pa_eur", cur.fuel_allowance_pa_eur)),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    state.params = new_p
    state.df = _rebuild_df(new_p)
    return {"ok": True, "params": new_p.to_public_dict(), "meta": model_meta_dict(state.df, new_p)}


@app.get("/history")
def history() -> dict[str, Any]:
    return build_price_history_payload(_df(), state.params)


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
        return get_county_deep_dive_dict(df, canon, fuel_price, state.params)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# Zerve / platforms often expect `app` at module level (above).
