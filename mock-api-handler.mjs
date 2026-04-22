/**
 * Offline demo API for local dev — mirrors Fuel Fault Lines FastAPI shapes without Zerve.
 * Session params (POST /model/params) are in-memory for the Node process.
 *
 * Keep in sync with demo-inline.js (browser bundled demo): same logic, but demo-inline
 * replaces `export function handleMockApi` with `function handleMockApi` and appends the fetch shim.
 */

const CANONICAL_COUNTIES = [
  'Carlow', 'Cavan', 'Clare', 'Cork', 'Donegal', 'Dublin',
  'Galway', 'Kerry', 'Kildare', 'Kilkenny', 'Laois', 'Leitrim',
  'Limerick', 'Longford', 'Louth', 'Mayo', 'Meath', 'Monaghan',
  'Offaly', 'Roscommon', 'Sligo', 'Tipperary', 'Waterford',
  'Westmeath', 'Wexford', 'Wicklow',
];

const COUNTY_HDD_MULT = {
  Carlow: 1.02, Cavan: 1.05, Clare: 1.06, Cork: 1.04, Donegal: 1.12, Dublin: 0.94,
  Galway: 1.1, Kerry: 1.08, Kildare: 0.96, Kilkenny: 1.01, Laois: 1.03, Leitrim: 1.11,
  Limerick: 1.05, Longford: 1.08, Louth: 1.0, Mayo: 1.12, Meath: 0.98, Monaghan: 1.06,
  Offaly: 1.04, Roscommon: 1.09, Sligo: 1.1, Tipperary: 1.03, Waterford: 1.02,
  Westmeath: 1.05, Wexford: 1.0, Wicklow: 0.97,
};

const COUNTY_PROVINCE = {
  Carlow: 'Leinster', Cavan: 'Ulster', Clare: 'Munster', Cork: 'Munster', Donegal: 'Ulster',
  Dublin: 'Leinster', Galway: 'Connacht', Kerry: 'Munster', Kildare: 'Leinster', Kilkenny: 'Leinster',
  Laois: 'Leinster', Leitrim: 'Connacht', Limerick: 'Munster', Longford: 'Leinster', Louth: 'Leinster',
  Mayo: 'Connacht', Meath: 'Leinster', Monaghan: 'Ulster', Offaly: 'Leinster', Roscommon: 'Connacht',
  Sligo: 'Connacht', Tipperary: 'Munster', Waterford: 'Munster', Westmeath: 'Leinster',
  Wexford: 'Leinster', Wicklow: 'Leinster',
};

function cloneParams(p) {
  return {
    litres_per_hh_pa: p.litres_per_hh_pa,
    poverty_threshold_pct: p.poverty_threshold_pct,
    weights: { ...p.weights },
    income_dep_min: p.income_dep_min,
    income_dep_max: p.income_dep_max,
    income_min_eur: p.income_min_eur,
    income_max_eur: p.income_max_eur,
    retrofit_grant_eur: p.retrofit_grant_eur,
    retrofit_saving_fraction: p.retrofit_saving_fraction,
    use_hdd_adjustment: p.use_hdd_adjustment,
    fuel_allowance_pa_eur: p.fuel_allowance_pa_eur,
  };
}

const DEFAULT_PARAMS = {
  litres_per_hh_pa: 1275,
  poverty_threshold_pct: 10,
  weights: {
    fuel_dependency_score: 0.3,
    building_inefficiency_score: 0.25,
    social_deprivation_score: 0.3,
    energy_intensity_score: 0.15,
  },
  income_dep_min: 20,
  income_dep_max: 40,
  income_min_eur: 28000,
  income_max_eur: 52000,
  retrofit_grant_eur: 25000,
  retrofit_saving_fraction: 0.5,
  use_hdd_adjustment: true,
  fuel_allowance_pa_eur: 33 * 28,
};

let sessionParams = cloneParams(DEFAULT_PARAMS);

function weightSum(w) {
  return (
    (w.fuel_dependency_score || 0) +
    (w.building_inefficiency_score || 0) +
    (w.social_deprivation_score || 0) +
    (w.energy_intensity_score || 0)
  );
}

/** Internal row: scores + derived fields for demo maths */
function buildInternalRows(p) {
  const w = p.weights;
  return CANONICAL_COUNTIES.map((county, i) => {
    const hdd = COUNTY_HDD_MULT[county] || 1;
    const fuel_dep = 22 + ((i * 17) % 45);
    const building = 20 + ((i * 11) % 42);
    const social = 21 + ((i * 13) % 44);
    const energy = 18 + ((i * 7) % 38);
    const vuln =
      fuel_dep * w.fuel_dependency_score +
      building * w.building_inefficiency_score +
      social * w.social_deprivation_score +
      energy * w.energy_intensity_score;
    const tier =
      vuln < 30 ? 'Low' : vuln < 50 ? 'Medium' : vuln < 70 ? 'High' : 'Critical';
    const deprivation = 21 + (i % 16);
    const depClamped = Math.min(p.income_dep_max, Math.max(p.income_dep_min, deprivation));
    const income =
      p.income_min_eur +
      ((depClamped - p.income_dep_min) / (p.income_dep_max - p.income_dep_min)) *
        (p.income_max_eur - p.income_min_eur);
    const litres =
      p.litres_per_hh_pa * (0.5 + fuel_dep / 100) * (p.use_hdd_adjustment ? hdd : 1);
    const pop = 42000 + i * 7500;
    const lowerSeg = (pop * deprivation) / 100;
    const estVuln = Math.max(800, Math.round((lowerSeg / 2.75) * 0.12));
    const primary_fuel = i % 3 === 0 ? 'oil' : 'gas';
    const pct_ber_defg = 30 + (i % 25);
    return {
      county,
      province: COUNTY_PROVINCE[county] || 'Leinster',
      fuel_dependency_score: Math.round(fuel_dep * 100) / 100,
      building_inefficiency_score: Math.round(building * 100) / 100,
      social_deprivation_score: Math.round(social * 100) / 100,
      energy_intensity_score: Math.round(energy * 100) / 100,
      vulnerability_score: Math.round(vuln * 100) / 100,
      risk_tier: tier,
      estimated_annual_income: Math.round(income),
      est_vulnerable_households: estVuln,
      model_litres_proxy_pa: Math.round(litres * 100) / 100,
      annual_oil_litres: Math.round(litres),
      primary_fuel,
      pct_ber_defg,
      hdd,
      deprivation_index: deprivation,
    };
  });
}

function povertyPctRow(r, price, p) {
  const income = r.estimated_annual_income;
  if (income <= 0) return 0;
  const litres =
    p.litres_per_hh_pa * (0.5 + r.fuel_dependency_score / 100) * (p.use_hdd_adjustment ? r.hdd : 1);
  return Math.round((litres * price * 100) / income) / 100;
}

function breachPriceEurL(r, p) {
  const thr = p.poverty_threshold_pct;
  const income = r.estimated_annual_income;
  if (income <= 0) return null;
  const litres =
    p.litres_per_hh_pa * (0.5 + r.fuel_dependency_score / 100) * (p.use_hdd_adjustment ? r.hdd : 1);
  if (litres <= 0) return null;
  const b = (thr / 100) * income / litres;
  if (b < 0.5) return Math.round(0.5 * 10000) / 10000;
  if (b > 8) return null;
  return Math.round(b * 10000) / 10000;
}

function countyToApi(r, fuelPrice, p) {
  const income = r.estimated_annual_income;
  const litres =
    p.litres_per_hh_pa * (0.5 + r.fuel_dependency_score / 100) * (p.use_hdd_adjustment ? r.hdd : 1);
  const poverty_pct = income > 0 ? Math.round((litres * fuelPrice * 100) / income) / 100 : 0;
  const oil_litres = r.annual_oil_litres;
  const annual_bill = Math.round(oil_litres * fuelPrice * 100) / 100;
  const saving = Math.round(annual_bill * p.retrofit_saving_fraction * 100) / 100;
  const roi_10 = Math.round((saving * 10 - p.retrofit_grant_eur) * 100) / 100;
  const be = saving > 0 ? Math.round((p.retrofit_grant_eur / saving) * 10) / 10 : null;
  const bp = breachPriceEurL(r, p);
  return {
    county: r.county,
    province: r.province,
    vulnerability_score: r.vulnerability_score,
    risk_tier: r.risk_tier,
    fuel_dependency_score: r.fuel_dependency_score,
    building_inefficiency_score: r.building_inefficiency_score,
    social_deprivation_score: r.social_deprivation_score,
    energy_intensity_score: r.energy_intensity_score,
    poverty_pct_at_price: poverty_pct,
    in_energy_poverty: poverty_pct > p.poverty_threshold_pct,
    cliff_price_eur: bp,
    estimated_annual_income: income,
    annual_oil_bill_eur: annual_bill,
    est_vulnerable_households: r.est_vulnerable_households,
    annual_saving_post_retrofit: saving,
    breakeven_years: be,
    retrofit_roi_saving_10yr: roi_10,
    model_litres_proxy_pa: Math.round(litres * 100) / 100,
    hdd_multiplier: r.hdd,
  };
}

function modelMetaDict(rows, p) {
  return {
    api_version: '1.3-demo',
    git_rev: 'mock',
    built_at_utc: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    zerve_notebook_block: 'warmer_homes_roi',
    zerve_notebook_var: 'warmer_homes_df',
    use_zerve_variable: false,
    params: {
      litres_per_hh_pa: p.litres_per_hh_pa,
      poverty_threshold_pct: p.poverty_threshold_pct,
      weights: { ...p.weights },
      income_dep_min: p.income_dep_min,
      income_dep_max: p.income_dep_max,
      income_min_eur: p.income_min_eur,
      income_max_eur: p.income_max_eur,
      retrofit_grant_eur: p.retrofit_grant_eur,
      retrofit_saving_fraction: p.retrofit_saving_fraction,
      use_hdd_adjustment: p.use_hdd_adjustment,
      fuel_allowance_pa_eur: p.fuel_allowance_pa_eur,
    },
    data_lineage: {
      seai: 'mock_demo_fixture',
      cso_deprivation: 'mock_demo_fixture',
      fuel_prices: 'mock_demo_fixture',
    },
    limitations:
      'Offline demo mode: synthetic county rows for UI testing. Not live SEAI/CSO data.',
    counties: rows.length,
    demo_script_for_judges: [
      '1. Demo mode uses bundled mock-api responses (no Zerve hub).',
      '2. Toggle in Settings; reload applies.',
      '3. Gemini (optional) still calls Google with your key.',
    ],
    key_endpoints: ['/meta', '/counties', '/national/snapshot', '/insights/headline'],
    recommended_demo: true,
  };
}

function nationalSnapshot(rows, p, price_eur_l) {
  const thr = p.poverty_threshold_pct;
  let maxPct = -1;
  let minPct = 999;
  let worst = null;
  let best = null;
  let sumPct = 0;
  let overN = 0;
  let overHh = 0;
  let critN = 0;
  let totalHh = 0;
  for (const r of rows) {
    const pct = povertyPctRow(r, price_eur_l, p);
    sumPct += pct;
    if (pct > thr) {
      overN += 1;
      overHh += r.est_vulnerable_households;
    }
    if (r.risk_tier === 'Critical') critN += 1;
    totalHh += r.est_vulnerable_households;
    if (pct > maxPct) {
      maxPct = pct;
      worst = r.county;
    }
    if (pct < minPct) {
      minPct = pct;
      best = r.county;
    }
  }
  return {
    headline_price_eur_l: price_eur_l,
    poverty_threshold_pct: thr,
    counties_over_threshold: overN,
    total_counties: rows.length,
    vulnerable_households_modelled: totalHh,
    vulnerable_households_in_stress_counties: overHh,
    critical_tier_counties: critN,
    highest_fuel_share: worst ? { county: worst, poverty_pct: maxPct } : null,
    lowest_fuel_share: best ? { county: best, poverty_pct: minPct } : null,
    mean_fuel_share_pct: Math.round((sumPct / rows.length) * 100) / 100,
    data_lineage: { seai: 'mock', cso: 'mock' },
  };
}

function scenarioPayload(rows, p, price_a, price_b) {
  const thr = p.poverty_threshold_pct;
  const counties = rows.map((r) => {
    const pa = povertyPctRow(r, price_a, p);
    const pb = povertyPctRow(r, price_b, p);
    const in_a = pa > thr;
    const in_b = pb > thr;
    return {
      county: r.county,
      poverty_pct_a: pa,
      poverty_pct_b: pb,
      in_poverty_a: in_a,
      in_poverty_b: in_b,
      newly_at_risk: !in_a && in_b,
      households_a: in_a ? r.est_vulnerable_households : 0,
      households_b: in_b ? r.est_vulnerable_households : 0,
    };
  });
  return { counties, price_a, price_b };
}

function historyPayload(rows, p) {
  const historical = [
    { date: '2024-05-01', price: 1.87 },
    { date: '2024-06-01', price: 1.83 },
    { date: '2024-07-01', price: 1.8 },
    { date: '2024-08-01', price: 1.77 },
    { date: '2024-09-01', price: 1.74 },
    { date: '2024-10-01', price: 1.72 },
    { date: '2024-11-01', price: 1.7 },
    { date: '2024-12-01', price: 1.71 },
    { date: '2025-01-01', price: 1.73 },
    { date: '2025-02-01', price: 1.74 },
    { date: '2025-03-01', price: 1.76 },
    { date: '2025-04-01', price: 1.78 },
  ];
  const projected = [
    { date: '2025-05-01', price: 1.79 },
    { date: '2025-06-01', price: 1.81 },
    { date: '2025-07-01', price: 1.84 },
    { date: '2025-08-01', price: 1.86 },
    { date: '2025-09-01', price: 1.88 },
    { date: '2025-10-01', price: 1.9 },
  ];
  const julyP = 1.84;
  const octP = 1.9;
  const countOver = (price) =>
    rows.filter((r) => povertyPctRow(r, price, p) > p.poverty_threshold_pct).length;
  return {
    historical,
    projected,
    events: [
      { date: '2022-02-24', label: 'Russia invades Ukraine' },
      { date: '2025-06-01', label: 'Iran conflict' },
      { date: '2026-04-22', label: 'Today' },
    ],
    poverty_threshold_price: p.poverty_threshold_pct,
    projections: {
      july: { counties_in_poverty: countOver(julyP) },
      october: { counties_in_poverty: countOver(octP) },
    },
  };
}

function scenarioCurve(rows, p, price_min, price_max, steps) {
  const thr = p.poverty_threshold_pct;
  const n = Math.max(5, Math.min(80, Math.floor(steps)));
  const points = [];
  for (let i = 0; i < n; i++) {
    const t = n === 1 ? 0 : i / (n - 1);
    const price = price_min + t * (price_max - price_min);
    const pctList = rows.map((r) => povertyPctRow(r, price, p));
    const over = rows.filter((r, j) => pctList[j] > thr);
    points.push({
      price_eur_l: Math.round(price * 10000) / 10000,
      counties_over_threshold: over.length,
      mean_fuel_share_pct: Math.round((pctList.reduce((a, b) => a + b, 0) / pctList.length) * 100) / 100,
      counties_over_names: over.map((r) => r.county).sort((a, b) => a.localeCompare(b)).slice(0, 28),
    });
  }
  return { poverty_threshold_pct: thr, price_min, price_max, points };
}

function regionalSummary(rows, p, fuel_price) {
  const thr = p.poverty_threshold_pct;
  const byProv = {};
  for (const r of rows) {
    const pct = povertyPctRow(r, fuel_price, p);
    if (!byProv[r.province]) {
      byProv[r.province] = {
        province: r.province,
        counties: 0,
        sumV: 0,
        sumPct: 0,
        over: 0,
        crit: 0,
      };
    }
    const g = byProv[r.province];
    g.counties += 1;
    g.sumV += r.vulnerability_score;
    g.sumPct += pct;
    if (pct > thr) g.over += 1;
    if (r.risk_tier === 'Critical') g.crit += 1;
  }
  const regions = Object.values(byProv)
    .map((g) => ({
      province: g.province,
      counties: g.counties,
      mean_vulnerability: Math.round((g.sumV / g.counties) * 100) / 100,
      mean_fuel_share_pct: Math.round((g.sumPct / g.counties) * 100) / 100,
      counties_over_threshold: g.over,
      critical_counties: g.crit,
    }))
    .sort((a, b) => b.mean_vulnerability - a.mean_vulnerability);
  return { fuel_price_eur_l: fuel_price, poverty_threshold_pct: thr, regions };
}

function distributionPayload(rows, p, price_eur_l) {
  const thr = p.poverty_threshold_pct;
  const pct = rows.map((r) => povertyPctRow(r, price_eur_l, p)).sort((a, b) => a - b);
  const n = pct.length;
  const q = (p0) => pct[Math.min(n - 1, Math.max(0, Math.round((p0 / 10) * (n - 1))))];
  const deciles = {};
  for (let i = 1; i < 10; i++) deciles[`D${i}`] = Math.round(q(i) * 100) / 100;
  const mean = pct.reduce((a, b) => a + b, 0) / n;
  const variance = pct.reduce((a, x) => a + (x - mean) ** 2, 0) / (n > 1 ? n - 1 : 1);
  return {
    price_eur_l,
    poverty_threshold_pct: thr,
    n_counties: n,
    mean: Math.round(mean * 100) / 100,
    std: Math.round(Math.sqrt(variance) * 100) / 100,
    min: Math.round(pct[0] * 100) / 100,
    max: Math.round(pct[n - 1] * 100) / 100,
    deciles,
    counties_over_threshold: pct.filter((x) => x > thr).length,
  };
}

function flipPoints(rows, p, reference_price_eur_l) {
  const ref = reference_price_eur_l;
  const list = rows
    .map((r) => ({
      county: r.county,
      province: r.province,
      breach_price_eur_l: breachPriceEurL(r, p),
      risk_tier: r.risk_tier,
      vulnerability_score: r.vulnerability_score,
    }))
    .sort((a, b) => {
      const an = a.breach_price_eur_l == null ? 99 : a.breach_price_eur_l;
      const bn = b.breach_price_eur_l == null ? 99 : b.breach_price_eur_l;
      return an - bn;
    });
  const already = list.filter(
    (x) => x.breach_price_eur_l != null && x.breach_price_eur_l <= ref
  ).length;
  return {
    poverty_threshold_pct: p.poverty_threshold_pct,
    reference_price_eur_l: ref,
    counties_already_over_at_reference: already,
    counties: list,
  };
}

function validationPayload(rows, p, price_eur_l) {
  const pct = rows.map((r) => povertyPctRow(r, price_eur_l, p));
  const vuln = rows.map((r) => r.vulnerability_score);
  const fds = rows.map((r) => r.fuel_dependency_score);
  const ber = rows.map((r) => r.pct_ber_defg);
  function pearson(x, y) {
    const n = x.length;
    if (n < 2) return [NaN, 0];
    const mx = x.reduce((a, b) => a + b, 0) / n;
    const my = y.reduce((a, b) => a + b, 0) / n;
    let num = 0;
    let dx = 0;
    let dy = 0;
    for (let i = 0; i < n; i++) {
      const vx = x[i] - mx;
      const vy = y[i] - my;
      num += vx * vy;
      dx += vx * vx;
      dy += vy * vy;
    }
    if (dx < 1e-12 || dy < 1e-12) return [NaN, n];
    return [num / Math.sqrt(dx * dy), n];
  }
  const [r_v, n1] = pearson(vuln, pct);
  const [r_fd, n2] = pearson(fds, pct);
  const [r_ber, n3] = pearson(ber, pct);
  return {
    price_eur_l,
    cso_deprivation_source: 'mock_demo_fixture',
    checks: [
      {
        id: 'vulnerability_vs_fuel_share',
        description: 'Pearson correlation: composite vulnerability index vs modelled fuel-income share.',
        r: Number.isFinite(r_v) ? Math.round(r_v * 10000) / 10000 : null,
        n: n1,
        passes: Number.isFinite(r_v) && r_v > 0.55,
      },
      {
        id: 'fuel_driver_vs_fuel_share',
        description: 'Pearson correlation: fuel dependency sub-score vs modelled fuel-income share.',
        r: Number.isFinite(r_fd) ? Math.round(r_fd * 10000) / 10000 : null,
        n: n2,
        passes: Number.isFinite(r_fd) && r_fd > 0.25,
      },
      {
        id: 'ber_bad_vs_fuel_share',
        description: 'Pearson correlation: % BER D–G vs modelled fuel-income share.',
        r: Number.isFinite(r_ber) ? Math.round(r_ber * 10000) / 10000 : null,
        n: n3,
        passes: Number.isFinite(r_ber) && r_ber > 0.15,
      },
    ],
    note: 'Mock demo: internal consistency checks on synthetic rows.',
  };
}

function evaluateClaims(rows, p, price_eur_l) {
  const pct = rows.map((r) => povertyPctRow(r, price_eur_l, p));
  const sorted = [...rows].sort((a, b) => b.vulnerability_score - a.vulnerability_score);
  const top5 = sorted.slice(0, 5).map((r) => r.county);
  const dublin = rows.find((r) => r.county === 'Dublin');
  const meanV = rows.reduce((a, r) => a + r.vulnerability_score, 0) / rows.length;
  const oilMask = rows.map((r) => r.primary_fuel === 'oil');
  const oilMean =
    pct.filter((_, i) => oilMask[i]).reduce((a, b) => a + b, 0) / Math.max(1, oilMask.filter(Boolean).length);
  const nonMean =
    pct.filter((_, i) => !oilMask[i]).reduce((a, b) => a + b, 0) / Math.max(1, oilMask.filter((x) => !x).length);
  const pNoHdd = { ...p, use_hdd_adjustment: false };
  const pctH = rows.map((r) => povertyPctRow(r, price_eur_l, p));
  const pctNo = rows.map((r) => povertyPctRow(r, price_eur_l, pNoHdd));
  function ranks(arr) {
    const idx = arr.map((_, i) => i).sort((i, j) => arr[j] - arr[i]);
    const out = new Array(arr.length);
    idx.forEach((ii, pos) => {
      out[ii] = pos;
    });
    return out;
  }
  const rankMoves = ranks(pctH).filter((ri, i) => ri !== ranks(pctNo)[i]).length;
  const val = validationPayload(rows, p, price_eur_l);
  const berCheck = val.checks.find((c) => c.id === 'ber_bad_vs_fuel_share') || {};
  const vulnCheck = val.checks.find((c) => c.id === 'vulnerability_vs_fuel_share') || {};
  const fp = flipPoints(rows, p, price_eur_l);
  const finiteBreach = fp.counties
    .map((x) => x.breach_price_eur_l)
    .filter((x) => x != null);
  const breachSpread =
    finiteBreach.length > 1 ? Math.max(...finiteBreach) - Math.min(...finiteBreach) : 0;
  return {
    price_eur_l,
    claims: [
      {
        id: 'longford_top5',
        statement: 'Longford is in the top 5 counties by composite vulnerability score.',
        holds: top5.includes('Longford'),
      },
      {
        id: 'dublin_below_mean',
        statement: "Dublin's vulnerability score is below the national mean.",
        holds: dublin ? dublin.vulnerability_score < meanV : false,
      },
      {
        id: 'oil_higher_burden',
        statement:
          'At the scenario €/L, mean modelled fuel-income share is higher in oil-primary counties than non-oil.',
        holds: oilMean > nonMean,
      },
      {
        id: 'hdd_moves_fuel_rank',
        statement:
          "Turning off the heating-demand (HDD) multiplier changes at least one county's rank by modelled fuel-income share.",
        holds: rankMoves > 0,
        detail: { counties_with_fuel_share_rank_change: rankMoves, hdd_adjustment_was_on: p.use_hdd_adjustment },
      },
      {
        id: 'ber_correlates_with_stress',
        statement:
          'Poor BER stock (D–G %) is positively correlated with modelled fuel-income share (internal sanity check).',
        holds: !!berCheck.passes,
        detail: { r: berCheck.r, n: berCheck.n },
      },
      {
        id: 'vulnerability_aligns_with_fuel_share',
        statement:
          'Composite vulnerability index is strongly aligned with modelled fuel-income share (internal consistency).',
        holds: !!vulnCheck.passes,
        detail: { r: vulnCheck.r, n: vulnCheck.n },
      },
      {
        id: 'breach_price_spread',
        statement:
          "Counties span a non-trivial range of €/L 'breach' prices where they cross the fuel-income threshold (≥ €0.50 spread).",
        holds: breachSpread >= 0.5,
        detail: { eur_l_spread: Math.round(breachSpread * 10000) / 10000, n_with_finite_breach: finiteBreach.length },
      },
    ],
  };
}

function sensitivityPayload(rows, p) {
  const price = 2.14;
  const baseN = rows.filter((r) => povertyPctRow(r, price, p) > p.poverty_threshold_pct).length;
  const p1 = { ...p, litres_per_hh_pa: p.litres_per_hh_pa * 0.85 };
  const n1 = rows.filter((r) => povertyPctRow(r, price, p1) > p1.poverty_threshold_pct).length;
  const p2 = { ...p, litres_per_hh_pa: p.litres_per_hh_pa * 1.15 };
  const n2 = rows.filter((r) => povertyPctRow(r, price, p2) > p2.poverty_threshold_pct).length;
  const wTilt = {
    fuel_dependency_score: Math.max(0.12, (p.weights.fuel_dependency_score || 0.3) - 0.1),
    building_inefficiency_score: p.weights.building_inefficiency_score || 0.25,
    social_deprivation_score: Math.min(0.5, (p.weights.social_deprivation_score || 0.3) + 0.1),
    energy_intensity_score: p.weights.energy_intensity_score || 0.15,
  };
  const s3 = weightSum(wTilt);
  const wn = {
    fuel_dependency_score: wTilt.fuel_dependency_score / s3,
    building_inefficiency_score: wTilt.building_inefficiency_score / s3,
    social_deprivation_score: wTilt.social_deprivation_score / s3,
    energy_intensity_score: wTilt.energy_intensity_score / s3,
  };
  const p3 = { ...p, weights: wn };
  const rows3 = buildInternalRows(p3);
  const n3 = rows3.filter((r) => povertyPctRow(r, price, p3) > p3.poverty_threshold_pct).length;
  const p4 = { ...p, use_hdd_adjustment: false };
  const n4 = rows.filter((r) => povertyPctRow(r, price, p4) > p4.poverty_threshold_pct).length;
  return {
    baseline: { counties_over_threshold: baseN, price_eur_l: price },
    variants: [
      { label: 'litres −15%', counties_over_threshold: n1, delta_vs_base: n1 - baseN },
      { label: 'litres +15%', counties_over_threshold: n2, delta_vs_base: n2 - baseN },
      {
        label: 'weight social +0.1 (fuel/building −0.05 ea.)',
        counties_over_threshold: n3,
        delta_vs_base: n3 - baseN,
      },
      { label: 'HDD multiplier off', counties_over_threshold: n4, delta_vs_base: n4 - baseN },
    ],
  };
}

function policyPayload(rows, p) {
  const g = p.retrofit_grant_eur;
  const crit = rows.filter((r) => r.risk_tier === 'Critical');
  const targetedHh = crit.reduce((a, r) => a + r.est_vulnerable_households, 0);
  const allHh = rows.reduce((a, r) => a + r.est_vulnerable_households, 0);
  const approxNet = crit.reduce((a, r) => {
    const bill = r.annual_oil_litres * 2.14;
    const saving = bill * p.retrofit_saving_fraction;
    return a + saving * 10 * r.est_vulnerable_households;
  }, 0);
  return {
    grant_eur_per_home: g,
    targeted_critical: {
      households: targetedHh,
      state_grant_outlay_eur: Math.round(targetedHh * g),
      approx_net_10yr_vs_grant_eur: Math.round(approxNet - targetedHh * g),
    },
    universal_all_modelled_vulnerable: {
      households: allHh,
      state_grant_outlay_eur: Math.round(allHh * g),
    },
    note: 'Mock demo: illustrative grant math only.',
  };
}

function rankingStabilityPayload(rows, p, top_k) {
  const k = Math.min(top_k, rows.length);
  const baseOrder = [...rows]
    .sort((a, b) => b.vulnerability_score - a.vulnerability_score)
    .map((r) => r.county);
  const topSet = new Set(baseOrder.slice(0, k));
  return {
    top_k: k,
    baseline_top_counties: baseOrder.slice(0, k),
    variants: [
      { label: 'baseline (current weights)', weights: { ...p.weights }, overlap_with_baseline_top_k: k },
      {
        label: 'tilt social (+0.10 from fuel)',
        weights: {
          fuel_dependency_score: 0.22,
          building_inefficiency_score: 0.25,
          social_deprivation_score: 0.38,
          energy_intensity_score: 0.15,
        },
        overlap_with_baseline_top_k: Math.max(4, k - 2),
      },
      {
        label: 'tilt fuel (+0.10 from social)',
        weights: {
          fuel_dependency_score: 0.38,
          building_inefficiency_score: 0.25,
          social_deprivation_score: 0.22,
          energy_intensity_score: 0.15,
        },
        overlap_with_baseline_top_k: Math.max(4, k - 1),
      },
    ],
    note: 'Mock demo: overlap numbers are illustrative.',
  };
}

function narrativePayload(rows, p, price_eur_l) {
  const snap = nationalSnapshot(rows, p, price_eur_l);
  const bullets = [
    `At €${price_eur_l.toFixed(2)}/L, ${snap.counties_over_threshold} of ${snap.total_counties} counties exceed the ${snap.poverty_threshold_pct}% modelled fuel-income threshold — mean share ${snap.mean_fuel_share_pct}%.`,
  ];
  if (snap.highest_fuel_share) {
    bullets.push(
      `Tightest squeeze: ${snap.highest_fuel_share.county} at ${snap.highest_fuel_share.poverty_pct}% of synthetic income on the liquid-fuel proxy.`
    );
  }
  return {
    price_eur_l,
    bullets,
    elevator_pitch:
      'Fuel Fault Lines (demo) fuses synthetic county profiles with a liquid-fuel proxy for offline UI testing.',
  };
}

function headlinePayload(rows, p, price_eur_l) {
  const snap = nationalSnapshot(rows, p, price_eur_l);
  const headline = `At €${price_eur_l.toFixed(2)}/L, ${snap.counties_over_threshold} of ${snap.total_counties} counties exceed the ${snap.poverty_threshold_pct}% modelled fuel-income line (mean proxy burden ${snap.mean_fuel_share_pct}%).`;
  return {
    price_eur_l,
    headline,
    supporting_bullets: narrativePayload(rows, p, price_eur_l).bullets.slice(0, 4),
  };
}

function submissionPackPayload(rows, p, price_eur_l) {
  const meta = modelMetaDict(rows, p);
  const head = headlinePayload(rows, p, price_eur_l);
  const snap = nationalSnapshot(rows, p, price_eur_l);
  const draft = `At €${price_eur_l.toFixed(2)}/L, ${snap.counties_over_threshold} of ${snap.total_counties} counties exceed a ${snap.poverty_threshold_pct}% fuel-income proxy threshold, with mean burden ${snap.mean_fuel_share_pct}%. ${head.headline}

The dashboard turns these outputs into copy-ready narrative for policymakers. This offline demo uses synthetic data for UI testing without the live Zerve hub.`;

  return {
    price_eur_l,
    headline_insight: head.headline,
    elevator_pitch: narrativePayload(rows, p, price_eur_l).elevator_pitch,
    why_zerve_not_spreadsheet: [
      'Live mode: notebook-first iteration on Zerve with the same dataframe as FastAPI.',
      'Demo mode: bundled mock responses for judges without network access to the hub.',
    ],
    zerve_video_checklist: [
      'State the question (who gets squeezed when liquid fuel prices move?).',
      'Show Zerve notebook and deployed API in production demo.',
      'Show this dashboard with live hub or Settings → offline demo.',
    ],
    devpost_required_checklist: [
      'Public Zerve project — runs without errors.',
      'Project summary — max 300 words.',
      'Demo video — max 3 minutes.',
    ],
    devpost_summary_draft: draft.slice(0, 2800),
    devpost_summary_word_count: draft.split(/\s+/).filter(Boolean).length,
    social_post_draft_x: 'Fuel Fault Lines — Irish county fuel stress (demo mode available). #ZerveHack',
    rubric_talking_points: {
      analytical_depth: 'Scenario curves, breach prices, validation — mock preserves UI wiring.',
      end_to_end_workflow: 'Zerve dataframe → FastAPI → dashboard; demo bypasses hub only.',
      storytelling: 'Key finding card and narrative endpoints work against mock-api.',
      creativity: 'Offline demo mode in Settings for hackathon booths.',
    },
    links: {
      zervehack_devpost: 'https://zervehack.devpost.com',
      google_ai_studio_key: 'https://aistudio.google.com/apikey',
    },
    api_meta: {
      api_version: meta.api_version,
      git_rev: meta.git_rev,
      zerve_notebook_block: meta.zerve_notebook_block,
      zerve_notebook_var: meta.zerve_notebook_var,
    },
  };
}

function deepDiveDict(r, price, p) {
  const api = countyToApi(r, price, p);
  const povCols = {};
  for (const pt of [1.74, 2.14, 2.5, 3.0, 3.5]) {
    povCols[`${pt.toFixed(2)}`] = povertyPctRow(r, pt, p);
  }
  return {
    county: r.county,
    price_queried_eur_l: price,
    vulnerability: {
      score: api.vulnerability_score,
      risk_tier: api.risk_tier,
      fuel_dependency_score: api.fuel_dependency_score,
      building_inefficiency_score: api.building_inefficiency_score,
      social_deprivation_score: api.social_deprivation_score,
      energy_intensity_score: api.energy_intensity_score,
    },
    poverty: {
      estimated_annual_income_eur: api.estimated_annual_income,
      poverty_pct_at_price: api.poverty_pct_at_price,
      in_energy_poverty: api.in_energy_poverty,
      cliff_price_eur_l: api.cliff_price_eur,
      poverty_pct_by_price_point: povCols,
    },
    retrofit_roi: {
      annual_oil_litres: r.annual_oil_litres,
      annual_oil_bill_eur: api.annual_oil_bill_eur,
      annual_saving_post_retrofit: api.annual_saving_post_retrofit,
      breakeven_years: api.breakeven_years,
      retrofit_roi_saving_10yr_eur: api.retrofit_roi_saving_10yr,
      est_vulnerable_households: api.est_vulnerable_households,
    },
    td_contacts: { tds: [], note: 'Mock demo: no TD directory.' },
    td_name: null,
    td_party: null,
    td_email: null,
    minister_email_template: `Dear TD,\n\nI am writing regarding energy costs in ${r.county}.\n`,
    tweet_text: `${r.county}: demo vulnerability score ${api.vulnerability_score} #FuelFaultLines`,
  };
}

function compareCounties(rows, p, county_a, county_b, fuel_price) {
  const ra = rows.find((r) => r.county.toLowerCase() === String(county_a).trim().toLowerCase());
  const rb = rows.find((r) => r.county.toLowerCase() === String(county_b).trim().toLowerCase());
  if (!ra || !rb) return null;
  const pa = countyToApi(ra, fuel_price, p);
  const pb = countyToApi(rb, fuel_price, p);
  const thr = p.poverty_threshold_pct;
  const take = [
    'vulnerability_score',
    'risk_tier',
    'poverty_pct_at_price',
    'in_energy_poverty',
    'estimated_annual_income',
    'annual_oil_bill_eur',
    'est_vulnerable_households',
    'cliff_price_eur',
    'model_litres_proxy_pa',
    'hdd_multiplier',
  ];
  const diff = { fuel_price, threshold_pct: thr };
  for (const k of take) {
    const va = pa[k];
    const vb = pb[k];
    if (typeof va === 'number' && typeof vb === 'number') {
      const dec = k === 'poverty_pct_at_price' ? 4 : 3;
      diff[k] = Math.round((va - vb) * 10 ** dec) / 10 ** dec;
    } else if (va !== vb) diff[k] = { a: va, b: vb };
  }
  const narrative = `At €${fuel_price.toFixed(2)}/L, ${pa.county} has ${pa.poverty_pct_at_price}% of income going to the modelled liquid-fuel proxy vs ${pb.poverty_pct_at_price}% in ${pb.county} (>${thr}% line = energy poverty in this dashboard).`;
  return { a: pa, b: pb, delta_a_minus_b: diff, takeaway: narrative };
}

function exportCountyMarkdown(r, price, p) {
  const api = countyToApi(r, price, p);
  const meta = modelMetaDict([r], p);
  return [
    `# Fuel Fault Lines — ${api.county}`,
    '',
    `- **Scenario:** €${price.toFixed(2)}/L (diesel / heating-oil proxy)`,
    `- **Vulnerability index:** ${api.vulnerability_score} (${api.risk_tier})`,
    `- **Modelled fuel share of income:** ${api.poverty_pct_at_price}% (threshold ${p.poverty_threshold_pct}%)`,
    `- **Synthetic income band (model):** €${api.estimated_annual_income.toLocaleString('en-IE')}`,
    `- **Vulnerable households (model):** ${api.est_vulnerable_households.toLocaleString('en-IE')}`,
    `- **HDD multiplier:** ${api.hdd_multiplier}`,
    '',
    '## Limitations',
    meta.limitations,
    '',
    '_Mock demo mode — not live SEAI/CSO._',
  ].join('\n');
}

function exportBriefingMarkdown(rows, p, price_eur_l) {
  const snap = nationalSnapshot(rows, p, price_eur_l);
  const head = headlinePayload(rows, p, price_eur_l).headline;
  const val = validationPayload(rows, p, price_eur_l);
  const lines = [
    '# Fuel Fault Lines — national briefing (mock demo)',
    '',
    `**Scenario:** €${price_eur_l.toFixed(2)}/L · **Threshold:** ${snap.poverty_threshold_pct}% modelled fuel-income share`,
    '',
    '## Key finding',
    '',
    head,
    '',
    '## Headline numbers',
    '',
    `- Counties over threshold: **${snap.counties_over_threshold}** / ${snap.total_counties}`,
    `- Critical-tier counties: **${snap.critical_tier_counties}**`,
    `- Modelled vulnerable households: **${snap.vulnerable_households_modelled.toLocaleString('en-IE')}**`,
    `- Mean fuel share: **${snap.mean_fuel_share_pct}%**`,
    '',
    '## Internal consistency',
    '',
    ...val.checks.map((c) => `- ${c.passes ? '✓' : '✗'} **${c.id}** — r=${c.r}, n=${c.n}`),
    '',
    '_Mock demo mode._',
  ];
  return lines.join('\n');
}

function sendJson(res, code, obj) {
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(JSON.stringify(obj));
}

function sendText(res, code, body, contentType) {
  res.writeHead(code, {
    'Content-Type': contentType || 'text/plain; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(body);
}

function mergeParamsUpdate(body, cur) {
  const next = cloneParams(cur);
  if (body.weights && typeof body.weights === 'object') {
    for (const k of Object.keys(next.weights)) {
      if (typeof body.weights[k] === 'number') next.weights[k] = body.weights[k];
    }
  }
  const numFields = [
    'litres_per_hh_pa',
    'poverty_threshold_pct',
    'income_dep_min',
    'income_dep_max',
    'income_min_eur',
    'income_max_eur',
    'retrofit_grant_eur',
    'retrofit_saving_fraction',
    'fuel_allowance_pa_eur',
  ];
  for (const f of numFields) {
    if (typeof body[f] === 'number' && !Number.isNaN(body[f])) next[f] = body[f];
  }
  if (typeof body.use_hdd_adjustment === 'boolean') next.use_hdd_adjustment = body.use_hdd_adjustment;
  const ws = weightSum(next.weights);
  if (Math.abs(ws - 1) > 0.02) {
    const err = new Error('weights must sum to ~1.0');
    err.status = 400;
    throw err;
  }
  return next;
}

/**
 * Handle /mock-api/* requests. For POST, body must be read by caller and passed as bodyStr.
 */
export function handleMockApi(req, res, url, bodyStr) {
  const pathname = url.pathname.replace(/^\/mock-api(\/|$)/, '/') || '/';
  const search = url.searchParams;
  const rows = buildInternalRows(sessionParams);
  const p = sessionParams;

  try {
    if (pathname === '/docs') {
      return sendText(
        res,
        200,
        'OpenAPI is not bundled in offline demo mode. Use live hub or local FastAPI at /docs.',
        'text/plain; charset=utf-8'
      );
    }

    if (pathname === '/health') {
      const meta = modelMetaDict(rows, p);
      return sendJson(res, 200, {
        status: 'ok',
        api_version: meta.api_version,
        git_rev: meta.git_rev,
        built_at_utc: meta.built_at_utc,
        mock_demo: true,
      });
    }

    if (pathname === '/meta') {
      return sendJson(res, 200, modelMetaDict(rows, p));
    }

    if (pathname === '/counties') {
      const fuelPrice = parseFloat(search.get('fuel_price') || '2.14');
      const list = rows.map((r) => countyToApi(r, fuelPrice, p));
      const over = list.filter((x) => x.in_energy_poverty).length;
      const totalHh = list.reduce((a, x) => a + x.est_vulnerable_households, 0);
      return sendJson(res, 200, {
        counties: list,
        count: list.length,
        counties_in_energy_poverty: over,
        total_vulnerable_households: totalHh,
      });
    }

    const countyMatch = pathname.match(/^\/county\/([^/]+)$/);
    if (countyMatch) {
      const name = decodeURIComponent(countyMatch[1]);
      const r = rows.find((x) => x.county.toLowerCase() === name.toLowerCase());
      if (!r) return sendJson(res, 404, { detail: `Unknown county: ${name}` });
      const fuelPrice = parseFloat(search.get('fuel_price') || '2.14');
      return sendJson(res, 200, countyToApi(r, fuelPrice, p));
    }

    const deepMatch = pathname.match(/^\/deep-dive\/([^/]+)$/);
    if (deepMatch) {
      const name = decodeURIComponent(deepMatch[1]);
      const r = rows.find((x) => x.county.toLowerCase() === name.toLowerCase());
      if (!r) return sendJson(res, 404, { detail: `Unknown county: ${name}` });
      const fuelPrice = parseFloat(search.get('fuel_price') || '2.14');
      return sendJson(res, 200, deepDiveDict(r, fuelPrice, p));
    }

    if (pathname === '/scenario') {
      const price_a = parseFloat(search.get('price_a') || '2.14');
      const price_b = parseFloat(search.get('price_b') || '3');
      return sendJson(res, 200, scenarioPayload(rows, p, price_a, price_b));
    }

    if (pathname === '/compare/counties') {
      const ca = search.get('county_a') || '';
      const cb = search.get('county_b') || '';
      const fuelPrice = parseFloat(search.get('fuel_price') || '2.14');
      const out = compareCounties(rows, p, ca, cb, fuelPrice);
      if (!out) return sendJson(res, 404, { detail: 'Unknown county' });
      return sendJson(res, 200, out);
    }

    if (pathname === '/history') {
      return sendJson(res, 200, historyPayload(rows, p));
    }

    if (pathname === '/national/snapshot') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendJson(res, 200, nationalSnapshot(rows, p, price));
    }

    if (pathname === '/insights/headline') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendJson(res, 200, headlinePayload(rows, p, price));
    }

    if (pathname === '/insights/narrative') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendJson(res, 200, narrativePayload(rows, p, price));
    }

    if (pathname === '/insights/regional') {
      const price = parseFloat(search.get('fuel_price') || '2.14');
      return sendJson(res, 200, regionalSummary(rows, p, price));
    }

    if (pathname === '/insights/submission-pack') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendJson(res, 200, submissionPackPayload(rows, p, price));
    }

    if (pathname === '/model/claims') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendJson(res, 200, evaluateClaims(rows, p, price));
    }

    if (pathname === '/model/sensitivity') {
      return sendJson(res, 200, sensitivityPayload(rows, p));
    }

    if (pathname === '/model/policy') {
      return sendJson(res, 200, policyPayload(rows, p));
    }

    if (pathname === '/model/validation') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendJson(res, 200, validationPayload(rows, p, price));
    }

    if (pathname === '/model/distribution') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendJson(res, 200, distributionPayload(rows, p, price));
    }

    if (pathname === '/model/breach-prices') {
      const ref = parseFloat(search.get('reference_price_eur_l') || '2.14');
      return sendJson(res, 200, flipPoints(rows, p, ref));
    }

    if (pathname === '/model/scenario-curve') {
      const price_min = parseFloat(search.get('price_min') || '1.5');
      const price_max = parseFloat(search.get('price_max') || '4');
      const steps = parseInt(search.get('steps') || '26', 10);
      return sendJson(res, 200, scenarioCurve(rows, p, price_min, price_max, steps));
    }

    if (pathname === '/model/ranking-stability') {
      const top_k = parseInt(search.get('top_k') || '10', 10);
      return sendJson(res, 200, rankingStabilityPayload(rows, p, top_k));
    }

    const exportCountyMatch = pathname.match(/^\/export\/county\/([^/]+)$/);
    if (exportCountyMatch) {
      const name = decodeURIComponent(exportCountyMatch[1]);
      const r = rows.find((x) => x.county.toLowerCase() === name.toLowerCase());
      if (!r) return sendJson(res, 404, { detail: `Unknown county: ${name}` });
      const fuelPrice = parseFloat(search.get('fuel_price') || '2.14');
      return sendText(
        res,
        200,
        exportCountyMarkdown(r, fuelPrice, p),
        'text/markdown; charset=utf-8'
      );
    }

    if (pathname === '/export/briefing') {
      const price = parseFloat(search.get('price_eur_l') || '2.14');
      return sendText(res, 200, exportBriefingMarkdown(rows, p, price), 'text/markdown; charset=utf-8');
    }

    if (pathname === '/model/params' && req.method === 'POST') {
      let body = {};
      try {
        body = bodyStr ? JSON.parse(bodyStr) : {};
      } catch {
        return sendJson(res, 400, { detail: 'Invalid JSON body' });
      }
      try {
        sessionParams = mergeParamsUpdate(body, sessionParams);
      } catch (e) {
        return sendJson(res, e.status || 400, { detail: e.message || 'Bad request' });
      }
      const rows2 = buildInternalRows(sessionParams);
      return sendJson(res, 200, {
        ok: true,
        params: modelMetaDict(rows2, sessionParams).params,
        meta: modelMetaDict(rows2, sessionParams),
      });
    }

    return sendJson(res, 404, { detail: `No mock route for ${pathname}` });
  } catch (e) {
    return sendJson(res, 500, { detail: String(e && e.message ? e.message : e) });
  }
}
