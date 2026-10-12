"""
AEMO pre-dispatch (PD) risk forecast.

Pulls the latest public NEMweb reports (no API key needed) and scores price /
reserve / network risk for each NEM region over the PD horizon (~today + tomorrow):

    PredispatchIS_Reports     30-min PD: regional prices, demand, availability,
                              interconnector flows/limits, binding constraints
    Predispatch_Sensitivities 43 demand-scenario price sensitivities
    PDPASA                    LOR (lack of reserve) forecast
    Market_Notice             recent market notices (LOR, inter-regional transfer
                              limits, interventions, ...)

Outputs (in reports/):
    pd_risk_<run>.md      English Markdown report (also written to $GITHUB_STEP_SUMMARY)
    pd_risk_<run>.json    structured results
    pd_detail_<run>.csv   per-region, per-period detail
    *.png                 charts
"""
import csv
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

BASE = "https://nemweb.com.au"
PD_DIR = "/Reports/Current/PredispatchIS_Reports/"
PDPASA_DIR = "/Reports/Current/PDPASA/"
NOTICE_DIR = "/Reports/Current/Market_Notice/"
OUTDIR = "reports"
NEM_TZ = timezone(timedelta(hours=10))  # NEM time = AEST, no daylight saving
REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]
FOCUS = os.environ.get("PD_FOCUS_REGION", "NSW1")  # region that gets the deep-dive section
# Interconnectors into the focus region: (interconnector, sign that makes flow = import)
FOCUS_IMPORTS = {
    "NSW1": [("NSW1-QLD1", -1, "QLD"), ("N-Q-MNSP1", -1, "QLD (Terranora)"),
             ("VIC1-NSW1", +1, "VIC"), ("NSW1-SA1", -1, "SA")],
    "QLD1": [("NSW1-QLD1", +1, "NSW"), ("N-Q-MNSP1", +1, "NSW (Terranora)")],
    "VIC1": [("VIC1-NSW1", -1, "NSW"), ("V-SA", -1, "SA"), ("V-S-MNSP1", -1, "SA (Murraylink)"),
             ("T-V-MNSP1", +1, "TAS")],
    "SA1": [("V-SA", +1, "VIC"), ("V-S-MNSP1", +1, "VIC (Murraylink)"), ("NSW1-SA1", +1, "NSW")],
    "TAS1": [("T-V-MNSP1", -1, "VIC")],
}
REGION_NAMES = {"NSW1": "New South Wales", "QLD1": "Queensland", "VIC1": "Victoria",
                "SA1": "South Australia", "TAS1": "Tasmania"}
COLORS = {"NSW1": "#2a78d6", "QLD1": "#eb6834", "VIC1": "#1baf7a", "SA1": "#eda100",
          "TAS1": "#e87ba4"}

# Price thresholds ($/MWh)
P_HIGH, P_SPIKE, P_EXTREME = 300, 1000, 5000
# Marginal values this large come from constraints that pin a flow/unit to a fixed value
# (e.g. *_ZERO) and swamp the ranking; they are listed separately.
MV_PINNED = 1_000_000

# Interconnector ID -> (from, to) for positive flow
IC_DIRECTION = {
    "NSW1-QLD1": ("NSW", "QLD"), "N-Q-MNSP1": ("NSW", "QLD"),
    "VIC1-NSW1": ("VIC", "NSW"), "V-SA": ("VIC", "SA"), "V-S-MNSP1": ("VIC", "SA"),
    "T-V-MNSP1": ("TAS", "VIC"), "NSW1-SA1": ("NSW", "SA"),
}

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (pd-risk-forecast)"


def listing(path):
    r = S.get(BASE + path, timeout=60)
    r.raise_for_status()
    return re.findall(r'HREF="([^"]+)"', r.text, flags=re.I)


def latest(path, pattern):
    files = sorted(h for h in listing(path) if re.search(pattern, h, re.I))
    if not files:
        raise RuntimeError(f"no file matching {pattern} under {path}")
    return files[-1]


def read_aemo_zip(href):
    """Parse an AEMO multi-table CSV (C/I/D rows) into {(group, table): DataFrame}."""
    r = S.get(BASE + href if href.startswith("/") else BASE + "/" + href, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    tables, headers = {}, {}
    for parts in csv.reader(io.StringIO(text)):
        if not parts:
            continue
        tag = parts[0]
        if tag == "I":
            key = (parts[1], parts[2])
            headers[key] = parts[4:]
            tables.setdefault(key, [])
        elif tag == "D":
            key = (parts[1], parts[2])
            tables.setdefault(key, []).append(parts[4:])
    out = {}
    for key, rows in tables.items():
        cols = headers.get(key)
        if not cols or not rows:
            continue
        df = pd.DataFrame([r[: len(cols)] for r in rows], columns=cols)
        for c in df.columns:
            if "DATETIME" in c or c in ("LASTCHANGED",):
                df[c] = pd.to_datetime(df[c], format="%Y/%m/%d %H:%M:%S", errors="coerce")
            else:
                conv = pd.to_numeric(df[c], errors="coerce")
                if conv.notna().sum() >= 0.8 * len(df):
                    df[c] = conv
        out[key] = df
    return out


def pick(tables, name):
    for (grp, tbl), df in tables.items():
        if tbl.upper() == name.upper():
            return df
    return pd.DataFrame()


def no_intervention(df):
    if "INTERVENTION" in df.columns and (df["INTERVENTION"] == 0).any():
        return df[df["INTERVENTION"] == 0]
    return df


def hhmm(ts):
    return pd.Timestamp(ts).strftime("%a %H:%M")


def windows(times, step=pd.Timedelta(minutes=30)):
    """Collapse a list of period-ending timestamps into 'Mon 15:30-18:30' ranges."""
    times = sorted(pd.to_datetime(pd.Series(times)).dropna().unique())
    if not times:
        return ""
    out, start, prev = [], times[0], times[0]
    for t in times[1:]:
        if t - prev > step:
            out.append((start, prev))
            start = t
        prev = t
    out.append((start, prev))
    return ", ".join(hhmm(a) if a == b else f"{hhmm(a)}-{pd.Timestamp(b):%H:%M}" for a, b in out)


# ---------------------------------------------------------------- risk rules
def price_level(p):
    if p >= P_EXTREME:
        return 4
    if p >= P_SPIKE:
        return 3
    if p >= P_HIGH:
        return 2
    return 0


LEVEL_NAME = {0: "Low", 1: "Watch", 2: "Medium", 3: "High", 4: "Extreme"}


def analyse_pd(tables):
    prices = no_intervention(pick(tables, "REGION_PRICES"))
    sol = no_intervention(pick(tables, "REGION_SOLUTION"))
    sens = no_intervention(pick(tables, "PRICESENSITIVITIES"))

    df = prices[["REGIONID", "DATETIME", "RRP"]].copy()
    keep = [c for c in ["TOTALDEMAND", "AVAILABLEGENERATION", "DISPATCHABLEGENERATION",
                        "NETINTERCHANGE", "SS_SOLAR_UIGF", "SS_WIND_UIGF", "UIGF"]
            if c in sol.columns]
    if keep:
        df = df.merge(sol[["REGIONID", "DATETIME"] + keep], on=["REGIONID", "DATETIME"], how="left")
    if not sens.empty:
        # Scenario columns: RRPEEP1..43 (or any "RRP<suffix><n>" in newer formats)
        sc = [c for c in sens.columns if re.fullmatch(r"RRP[A-Z_]*\d+", c)]
        print(f"PRICESENSITIVITIES: {len(sens)} rows, {len(sc)} scenario columns")
        s = sens[["REGIONID", "DATETIME"]].copy()
        s["SENS_MAX"] = sens[sc].max(axis=1)
        s["SENS_MAX_SCEN"] = sens[sc].idxmax(axis=1).str.extract(r"(\d+)$", expand=False)
        s["SENS_N_GE_1000"] = (sens[sc] >= P_SPIKE).sum(axis=1)
        s["SENS_N"] = len(sc)
        df = df.merge(s, on=["REGIONID", "DATETIME"], how="left")
    if {"AVAILABLEGENERATION", "DISPATCHABLEGENERATION"} <= set(df.columns):
        df["SPARE_MW"] = df["AVAILABLEGENERATION"] - df["DISPATCHABLEGENERATION"]
        df["SPARE_PCT"] = 100 * df["SPARE_MW"] / df["TOTALDEMAND"].where(df["TOTALDEMAND"] > 0)
    return df[df["REGIONID"].isin(REGIONS)].sort_values(["REGIONID", "DATETIME"])


def analyse_interconnectors(tables):
    """Per-period interconnector flow vs limits, binding flag and the limiting constraint."""
    ic = no_intervention(pick(tables, "INTERCONNECTOR_SOLN"))
    if ic.empty or not {"MWFLOW", "EXPORTLIMIT", "IMPORTLIMIT"} <= set(ic.columns):
        return pd.DataFrame(), pd.DataFrame()
    ic = ic.copy()
    ic["AT_EXPORT"] = (ic["MWFLOW"] >= ic["EXPORTLIMIT"] - 1) & (ic["EXPORTLIMIT"] != 0)
    ic["AT_IMPORT"] = (ic["MWFLOW"] <= ic["IMPORTLIMIT"] + 1) & (ic["IMPORTLIMIT"] != 0)
    ic["BOUND"] = ic["AT_EXPORT"] | ic["AT_IMPORT"]
    if "MARGINALVALUE" in ic.columns:
        ic["BOUND"] = ic["BOUND"] | (ic["MARGINALVALUE"].fillna(0) != 0)
    lim_con = pd.Series("", index=ic.index)
    if "EXPORTGENCONID" in ic.columns:
        lim_con = lim_con.mask(ic["AT_EXPORT"], ic["EXPORTGENCONID"].astype(str))
    if "IMPORTGENCONID" in ic.columns:
        lim_con = lim_con.mask(ic["AT_IMPORT"], ic["IMPORTGENCONID"].astype(str))
    ic["LIMIT_CONSTRAINT"] = lim_con.where(ic["BOUND"], "")

    rows = []
    for icid, g in ic.groupby("INTERCONNECTORID"):
        b = g[g["BOUND"]]
        a, z = IC_DIRECTION.get(icid, ("+", "-"))
        dirs = []
        if b["AT_EXPORT"].any():
            dirs.append(f"{a}->{z}")
        if b["AT_IMPORT"].any():
            dirs.append(f"{z}->{a}")
        cons = b["LIMIT_CONSTRAINT"].replace("", pd.NA).dropna().value_counts()
        rows.append({
            "interconnector": icid,
            "bound_periods": f"{len(b)}/{len(g)}",
            "bound_direction": "/".join(dirs),
            "bound_windows": windows(b["DATETIME"]),
            "flow_range_mw": f"{g['MWFLOW'].min():.0f} .. {g['MWFLOW'].max():.0f}",
            "export_limit_range": f"{g['EXPORTLIMIT'].min():.0f} .. {g['EXPORTLIMIT'].max():.0f}",
            "import_limit_range": f"{g['IMPORTLIMIT'].min():.0f} .. {g['IMPORTLIMIT'].max():.0f}",
            "limiting_constraints": "; ".join(f"{k} ({v})" for k, v in cons.head(3).items()),
        })
    summ = pd.DataFrame(rows)
    summ["_n"] = summ["bound_periods"].str.split("/").str[0].astype(int)
    summ = summ.sort_values("_n", ascending=False).drop(columns="_n")
    return ic, summ


def constraint_kind(cid):
    """Rough decode of the AEMO constraint naming convention (heuristic)."""
    cid = str(cid)
    region = {"N": "NSW", "Q": "QLD", "V": "VIC", "S": "SA", "T": "TAS",
              "I": "Inter-regional", "F": "FCAS"}.get(cid[:1], "Other")
    if cid.startswith("#"):
        kind = "Unit/manual"
    elif ">>" in cid or cid[1:2] == ">":
        kind = "Thermal"
    elif "^^" in cid or cid[1:2] == "^":
        kind = "Voltage stability"
    elif "::" in cid or cid[1:2] == ":":
        kind = "Transient stability"
    elif cid.startswith("F"):
        kind = "FCAS"
    else:
        kind = "Other"
    state = "System normal" if "NIL" in cid.upper() else "Outage/contingency-specific"
    return region, kind, state


def analyse_constraints(tables, hot_times):
    """Binding constraints across the PD horizon, and those binding in high-price periods."""
    con = no_intervention(pick(tables, "CONSTRAINT_SOLUTION"))
    if con.empty or "MARGINALVALUE" not in con.columns:
        return pd.DataFrame(), pd.DataFrame()
    b = con[con["MARGINALVALUE"].fillna(0) != 0].copy()
    if b.empty:
        return pd.DataFrame(), pd.DataFrame()
    b["ABS_MV"] = b["MARGINALVALUE"].abs()
    viol = "VIOLATIONDEGREE" in b.columns
    rows = []
    for cid, g in b.groupby("CONSTRAINTID"):
        region, kind, state = constraint_kind(cid)
        rows.append({
            "constraint": cid, "area": region, "type": kind, "network_state": state,
            "binding_periods": len(g),
            "windows": windows(g["DATETIME"]),
            "max_abs_marginal_value": round(float(g["ABS_MV"].max()), 0),
            "violated_periods": int((g["VIOLATIONDEGREE"] > 0).sum()) if viol else 0,
        })
    summ = (pd.DataFrame(rows)
            .sort_values(["max_abs_marginal_value", "binding_periods"], ascending=False))
    summ["pinned"] = summ["max_abs_marginal_value"] >= MV_PINNED
    hot = b[b["DATETIME"].isin(hot_times) & (b["ABS_MV"] < MV_PINNED)]
    hot_summ = (hot.groupby("CONSTRAINTID")
                .agg(periods=("DATETIME", "nunique"), max_abs_mv=("ABS_MV", "max"))
                .round(0).sort_values("max_abs_mv", ascending=False).head(15).reset_index())
    return summ, hot_summ


def find_sensitivity_tables(run):
    """PRICESENSITIVITIES is published in its own report; find the directory under
    /Reports/Current/ whose name contains 'sensitiv' and take the latest file."""
    dirs = [h for h in listing("/Reports/Current/") if re.search(r"sensitiv", h, re.I)]
    print("Sensitivity dirs:", dirs)
    for d in dirs:
        path = d if d.startswith("/") else "/Reports/Current/" + d
        zips = sorted(h for h in listing(path) if h.lower().endswith(".zip"))
        if not zips:
            continue
        same = [z for z in zips if run in z]
        href = (same or zips)[-1]
        print("Sensitivity file:", href)
        t = read_aemo_zip(href)
        for (grp, tbl), df in t.items():
            print(f"  table {grp}.{tbl}: {len(df)} rows")
        return t
    return {}


def scenario_defs(tables):
    """Scenario definitions (demand offset MW per region), if present in the file."""
    for (grp, tbl), df in tables.items():
        if "SCENARIO" in df.columns and "DELTAMW" in df.columns and "REGIONID" in df.columns:
            d = df.copy()
            if "EFFECTIVEDATE" in d.columns:
                d = d[d["EFFECTIVEDATE"] == d["EFFECTIVEDATE"].max()]
            if "VERSIONNO" in d.columns:
                d = d[d["VERSIONNO"] == d["VERSIONNO"].max()]
            return d.pivot_table(index="SCENARIO", columns="REGIONID", values="DELTAMW",
                                 aggfunc="first")
    return pd.DataFrame()


def analyse_pdpasa(tables):
    rs = pick(tables, "REGIONSOLUTION")
    if rs.empty:
        return pd.DataFrame()
    if "RUNTYPE" in rs.columns and (rs["RUNTYPE"] == "LOR").any():
        rs = rs[rs["RUNTYPE"] == "LOR"]
    cols = [c for c in ["REGIONID", "INTERVAL_DATETIME", "DEMAND50", "DEMAND10",
                        "AGGREGATECAPACITYAVAILABLE", "SURPLUSRESERVE", "LORSURPLUS",
                        "LORCONDITION"] if c in rs.columns]
    return rs[cols].sort_values(["REGIONID", "INTERVAL_DATETIME"])


def field(text, name):
    m = re.search(rf"^\s*{name}\s*:\s*(.+?)\s*$", text, re.I | re.M)
    return m.group(1).strip() if m else ""


def recent_notices(hours=24, max_files=150):
    """Recent market notices as dicts (time, type, subject, reason excerpt)."""
    try:
        hrefs = [h for h in listing(NOTICE_DIR) if re.search(r"MKTNOTICE|\.R\d+$", h, re.I)]
    except Exception as e:  # notices are supplementary
        print("Market notice listing failed:", e)
        return []
    num = lambda h: int(re.search(r"(\d+)$", h).group(1)) if re.search(r"(\d+)$", h) else 0
    hrefs = sorted(hrefs, key=num)[-max_files:]
    print(f"Market notice files: {len(hrefs)} (latest {hrefs[-1] if hrefs else None})")
    cutoff = datetime.now(NEM_TZ).replace(tzinfo=None) - timedelta(hours=hours)
    out = []
    for h in reversed(hrefs):
        try:
            t = S.get(BASE + h if h.startswith("/") else BASE + NOTICE_DIR + h, timeout=30).text
        except Exception:
            continue
        created = field(t, "Creation Date")
        m = re.match(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})", created)
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%Y %H:%M:%S") if m else None
        if dt and dt < cutoff:
            break  # files are in notice-number order, so everything older is out of range
        reason = t.split("Reason", 1)[-1] if "Reason" in t else ""
        reason = re.sub(r"[-=]{5,}", " ", reason)
        reason = re.sub(r"\s+", " ", reason.lstrip(" :\n")).strip()
        reason = re.sub(r"^AEMO ELECTRICITY MARKET NOTICE\.?\s*", "", reason, flags=re.I)
        out.append({
            "id": field(t, "Notice ID"),
            "time": dt.strftime("%Y-%m-%d %H:%M") if dt else created,
            "type": field(t, "Notice Type ID"),
            "subject": field(t, "External Reference"),
            "reason": reason[:400],
        })
    if not out and hrefs:  # help debugging format changes
        sample = S.get(BASE + hrefs[-1], timeout=30).text[:800]
        print("Market notice sample:\n" + sample)
    return out


def is_network_notice(n):
    s = f"{n['type']} {n['subject']} {n['reason']}"
    return bool(re.search(r"INTER-REGIONAL|TRANSFER|CONSTRAINT|INTERCONNECTOR|OUTAGE|"
                          r"RECLASSIF|NETWORK|LIMIT", s, re.I))


# ---------------------------------------------------------------- focus region
def focus_periods(df, ic, pasa, tables, reg=FOCUS):
    """One row per PD period for the focus region: price, scenarios, demand, LOR,
    imports and whether each import path is at its limit, top binding constraint."""
    g = df[df["REGIONID"] == reg].copy()
    out = pd.DataFrame({"period_ending": g["DATETIME"].values})
    out["pd_rrp"] = g["RRP"].round(0).values
    if "SENS_MAX" in g:
        out["scen_>=$1k"] = (g["SENS_N_GE_1000"].fillna(0).astype(int).astype(str)
                             + "/" + g["SENS_N"].fillna(0).astype(int).astype(str)).values
        out["scen_max"] = g["SENS_MAX"].round(0).values
    if "TOTALDEMAND" in g:
        out["demand"] = g["TOTALDEMAND"].round(0).values
    if "SPARE_MW" in g:
        out["spare_mw"] = g["SPARE_MW"].round(0).values
    if "NETINTERCHANGE" in g:
        out["net_import"] = (-g["NETINTERCHANGE"]).round(0).values
    lor = {}
    if not pasa.empty and "LORCONDITION" in pasa:
        p = pasa[(pasa["REGIONID"] == reg) & (pasa["LORCONDITION"] > 0)]
        lor = dict(zip(p["INTERVAL_DATETIME"], p["LORCONDITION"]))
    out["lor"] = [f"LOR{int(lor[t])}" if t in lor else "" for t in out["period_ending"]]
    if not ic.empty:
        for icid, sign, label in FOCUS_IMPORTS.get(reg, []):
            x = ic[ic["INTERCONNECTORID"] == icid].set_index("DATETIME")
            if x.empty:
                continue
            imp = (sign * x["MWFLOW"]).reindex(out["period_ending"]).round(0)
            # "at limit" in the import direction for this region
            at = (x["AT_EXPORT"] if sign > 0 else x["AT_IMPORT"]).reindex(out["period_ending"])
            key = label.replace(" (", "-").replace(")", "")  # "QLD (Terranora)" -> "QLD-Terranora"
            out[f"from_{key}"] = [
                "" if pd.isna(v) else f"{v:.0f}{' ●' if (not pd.isna(a) and bool(a)) else ''}"
                for v, a in zip(imp.values, at.values)]
    con = no_intervention(pick(tables, "CONSTRAINT_SOLUTION"))
    if not con.empty and "MARGINALVALUE" in con.columns:
        b = con[con["MARGINALVALUE"].fillna(0) != 0].copy()
        b["ABS_MV"] = b["MARGINALVALUE"].abs()
        b = b[b["CONSTRAINTID"].astype(str).map(lambda c: relevant_constraint(c, reg))
              & (b["ABS_MV"] < MV_PINNED)]
        top = (b.sort_values("ABS_MV", ascending=False).groupby("DATETIME")
               .agg(top_constraint=("CONSTRAINTID", "first"), mv=("ABS_MV", "first"),
                    n_binding=("CONSTRAINTID", "nunique")))
        out = out.merge(top, left_on="period_ending", right_index=True, how="left")
        out["top_constraint"] = (out["top_constraint"].fillna("") +
                                 out["mv"].map(lambda v: "" if pd.isna(v) else f" (${v:,.0f})"))
        out["n_binding"] = out["n_binding"].fillna(0).astype(int)
        out = out.drop(columns="mv")
    return out


def relevant_constraint(cid, reg=FOCUS):
    """Constraint touches the focus region: same region letter, or an inter-regional
    constraint, or one whose leading token names the region letter (heuristic)."""
    cid = str(cid)
    head = re.split(r"[-_+]", cid)[0]  # e.g. "N>>N", "V^^N", "I"
    return cid.startswith("I") or reg[0] in re.sub(r"[^A-Z]", "", head)


def focus_headline(fp, reg=FOCUS):
    lines = []
    cap = fp[fp["pd_rrp"] >= P_EXTREME]
    if not cap.empty:
        lines.append(f"- PD price ≥ $5,000 in **{len(cap)} periods**: {windows(cap['period_ending'])} "
                     f"(peak ${fp['pd_rrp'].max():,.0f}/MWh).")
    mid = fp[(fp["pd_rrp"] >= P_HIGH) & (fp["pd_rrp"] < P_EXTREME)]
    if not mid.empty:
        lines.append(f"- PD price $300–5,000 in {len(mid)} periods: {windows(mid['period_ending'])}.")
    if "scen_>=$1k" in fp:
        n = fp["scen_>=$1k"].str.split("/").str[0].astype(int)
        tail = fp[(n > 0) & (fp["pd_rrp"] < P_SPIKE)]
        if not tail.empty:
            lines.append(f"- Tail risk (base < $1,000 but ≥ $1,000 in some demand scenarios): "
                         f"{windows(tail['period_ending'])}.")
    lor = fp[fp["lor"] != ""]
    if not lor.empty:
        lines.append(f"- PD PASA forecasts **{lor['lor'].max()}** at {windows(lor['period_ending'])}.")
    if "demand" in fp:
        pk = fp.loc[fp["demand"].idxmax()]
        lines.append(f"- Peak demand {pk['demand']:,.0f} MW at {hhmm(pk['period_ending'])}.")
    imp_cols = [c for c in fp.columns if c.startswith("from_")]
    for c in imp_cols:
        bound = fp[fp[c].str.endswith("●")]
        if not bound.empty:
            lines.append(f"- Import from {c[5:]} at its limit in {len(bound)} periods: "
                         f"{windows(bound['period_ending'])}.")
    return "\n".join(lines) if lines else "- No elevated risk signals."


# ---------------------------------------------------------------- summary
def region_summary(df, pasa):
    rows = []
    for reg, g in df.groupby("REGIONID"):
        peak = g.loc[g["RRP"].idxmax()]
        low = g.loc[g["RRP"].idxmin()]
        lvl = int(g["RRP"].map(price_level).max())
        sens_max = g["SENS_MAX"].max() if "SENS_MAX" in g else float("nan")
        if pd.notna(sens_max) and price_level(sens_max) >= 3:
            lvl = max(lvl, 1)  # spike only in sensitivity scenarios -> at least "Watch"
        spare_min = g["SPARE_PCT"].min() if "SPARE_PCT" in g else float("nan")
        lor, lor_win = 0, ""
        if not pasa.empty and "LORCONDITION" in pasa:
            p = pasa[pasa["REGIONID"] == reg]
            if not p.empty and p["LORCONDITION"].max() > 0:
                lor = int(p["LORCONDITION"].max())
                lor_win = windows(p.loc[p["LORCONDITION"] > 0, "INTERVAL_DATETIME"])
        lvl = max(lvl, {0: 0, 1: 2, 2: 3, 3: 4}.get(lor, 4))
        if pd.notna(spare_min) and spare_min < 5:
            lvl = max(lvl, 2)
        rows.append({
            "region": reg,
            "risk": LEVEL_NAME[lvl], "risk_level": lvl,
            "peak_rrp": round(float(peak["RRP"]), 0), "peak_time": hhmm(peak["DATETIME"]),
            "min_rrp": round(float(low["RRP"]), 0), "min_time": hhmm(low["DATETIME"]),
            "periods_>=$300": int((g["RRP"] >= P_HIGH).sum()),
            "periods_>=$1000": int((g["RRP"] >= P_SPIKE).sum()),
            "negative_periods": int((g["RRP"] < 0).sum()),
            "sens_max_rrp": None if pd.isna(sens_max) else round(float(sens_max), 0),
            "peak_demand_mw": round(float(g["TOTALDEMAND"].max()), 0) if "TOTALDEMAND" in g else None,
            "min_spare_%": None if pd.isna(spare_min) else round(float(spare_min), 1),
            "pdpasa_lor": f"LOR{lor} {lor_win}" if lor else "-",
        })
    return pd.DataFrame(rows).sort_values("risk_level", ascending=False)


def fmt_table(df):
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v).replace("|", "/")
                                       for v in r.values) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------- charts
def make_charts(df, ic, pasa, run, fp=None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping charts")
        return []
    colors = COLORS
    fmt = mdates.DateFormatter("%a %H:%M")
    files = []

    # 1. Regional prices (symlog so the price cap and negatives both stay readable)
    fig, ax = plt.subplots(figsize=(12, 5))
    for reg, g in df.groupby("REGIONID"):
        ax.step(g["DATETIME"], g["RRP"], where="pre", label=reg, color=colors.get(reg), lw=1.8)
    ax.set_yscale("symlog", linthresh=100)
    for y in (P_HIGH, P_SPIKE):
        ax.axhline(y, color="grey", ls=":", lw=0.8)
    ax.set_title(f"PD regional prices — run {run}")
    ax.set_ylabel("$/MWh (symlog)")
    ax.xaxis.set_major_formatter(fmt)
    ax.legend(ncol=5)
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    files.append(f"{OUTDIR}/chart_prices.png")
    fig.savefig(files[-1], dpi=120, bbox_inches="tight")
    plt.close(fig)

    # 2. Per-region risk panel: base price, sensitivity max, scenarios >= $1000, LOR shading
    regs = [r for r in REGIONS if r in set(df["REGIONID"])]
    fig, axes = plt.subplots(len(regs), 1, figsize=(12, 2.6 * len(regs)), sharex=True)
    for ax, reg in zip(axes, regs):
        g = df[df["REGIONID"] == reg]
        ax.step(g["DATETIME"], g["RRP"], where="pre", color=colors[reg], lw=1.8, label="PD RRP")
        if "SENS_MAX" in g:
            ax.step(g["DATETIME"], g["SENS_MAX"], where="pre", color=colors[reg], lw=1,
                    ls="--", alpha=0.6, label="Max across scenarios")
        if not pasa.empty and "LORCONDITION" in pasa:
            for _, r in pasa[(pasa["REGIONID"] == reg) & (pasa["LORCONDITION"] > 0)].iterrows():
                ax.axvspan(r["INTERVAL_DATETIME"] - pd.Timedelta(minutes=30),
                           r["INTERVAL_DATETIME"], color="red", alpha=0.12)
        ax.set_yscale("symlog", linthresh=100)
        ax.set_ylim(-1000, 40000)  # same scale for every region: negatives up to the price cap
        ax.set_ylabel(reg)
        ax.grid(alpha=0.3)
    axes[0].legend(loc="upper left", fontsize=8)
    axes[0].set_title("Price risk by region (red shading = PD PASA LOR)")
    axes[-1].xaxis.set_major_formatter(fmt)
    fig.autofmt_xdate()
    files.append(f"{OUTDIR}/chart_region_risk.png")
    fig.savefig(files[-1], dpi=110, bbox_inches="tight")
    plt.close(fig)

    # Focus region deep-dive: price, scenarios, demand/availability, imports (small multiples,
    # one y-scale per panel)
    if fp is not None and not fp.empty:
        g = df[df["REGIONID"] == FOCUS]
        c = colors.get(FOCUS, "#2a78d6")
        fig, axes = plt.subplots(4, 1, figsize=(12, 11), sharex=True,
                                 gridspec_kw={"height_ratios": [3, 1.3, 2, 2]})
        ax = axes[0]
        ax.step(g["DATETIME"], g["RRP"], where="pre", color=c, lw=2, label="PD RRP")
        if "SENS_MAX" in g:
            ax.step(g["DATETIME"], g["SENS_MAX"], where="pre", color=c, lw=1, ls="--",
                    alpha=0.6, label="Max across demand scenarios")
        if not pasa.empty and "LORCONDITION" in pasa:
            for _, r in pasa[(pasa["REGIONID"] == FOCUS) & (pasa["LORCONDITION"] > 0)].iterrows():
                for a in axes:
                    a.axvspan(r["INTERVAL_DATETIME"] - pd.Timedelta(minutes=30),
                              r["INTERVAL_DATETIME"], color="#d03b3b", alpha=0.10, lw=0)
        ax.set_yscale("symlog", linthresh=100)
        ax.set_ylim(-1000, 40000)
        ax.set_ylabel("$/MWh")
        ax.legend(loc="upper left", fontsize=8)
        ax.set_title(f"{FOCUS} deep-dive — PD run {run} (red shading = PD PASA LOR)")
        if "SENS_N_GE_1000" in g:
            axes[1].bar(g["DATETIME"] - pd.Timedelta(minutes=15), g["SENS_N_GE_1000"],
                        width=0.018, color=c, alpha=0.7)
            axes[1].set_ylim(0, int(g["SENS_N"].max()))
            axes[1].set_ylabel("scenarios\n≥ $1,000")
        if "TOTALDEMAND" in g:
            axes[2].step(g["DATETIME"], g["TOTALDEMAND"], where="pre", color="#0b0b0b",
                         lw=1.5, label="Demand")
        if "AVAILABLEGENERATION" in g:
            axes[2].step(g["DATETIME"], g["AVAILABLEGENERATION"], where="pre", color=c,
                         lw=1.5, ls="--", label="Available generation")
        axes[2].set_ylabel("MW")
        axes[2].legend(loc="upper left", fontsize=8)
        pal = ["#eb6834", "#1baf7a", "#6250d6", "#e87ba4"]
        for k, (icid, sign, label) in enumerate(FOCUS_IMPORTS.get(FOCUS, [])):
            x = ic[ic["INTERCONNECTORID"] == icid].sort_values("DATETIME") if not ic.empty else ic
            if x.empty or x["MWFLOW"].abs().max() == 0:
                continue
            imp = sign * x["MWFLOW"]
            lim = sign * (x["EXPORTLIMIT"] if sign > 0 else x["IMPORTLIMIT"])
            axes[3].step(x["DATETIME"], imp, where="pre", color=pal[k % 4], lw=1.8,
                         label=f"Import from {label}")
            axes[3].step(x["DATETIME"], lim, where="pre", color=pal[k % 4], lw=1, ls=":",
                         label=f"{label} import limit")
        axes[3].axhline(0, color="#c3c2b7", lw=0.8)
        axes[3].set_ylabel("MW")
        axes[3].legend(loc="upper left", fontsize=7, ncol=2)
        for a in axes:
            a.grid(alpha=0.3)
        axes[-1].xaxis.set_major_formatter(fmt)
        fig.autofmt_xdate()
        files.append(f"{OUTDIR}/chart_{FOCUS.lower()}_focus.png")
        fig.savefig(files[-1], dpi=110, bbox_inches="tight")
        plt.close(fig)

    # 3. Interconnector flows vs limits
    if not ic.empty:
        ids = sorted(ic["INTERCONNECTORID"].unique())
        n = len(ids)
        fig, axes = plt.subplots((n + 1) // 2, 2, figsize=(13, 2.6 * ((n + 1) // 2)), sharex=True)
        axes = axes.ravel()
        for ax, icid in zip(axes, ids):
            g = ic[ic["INTERCONNECTORID"] == icid].sort_values("DATETIME")
            ax.fill_between(g["DATETIME"], g["IMPORTLIMIT"], g["EXPORTLIMIT"], color="#ccd",
                            alpha=0.5, step="pre", label="limits")
            ax.step(g["DATETIME"], g["MWFLOW"], where="pre", color="k", lw=1.4, label="flow")
            bnd = g[g["BOUND"]]
            ax.scatter(bnd["DATETIME"], bnd["MWFLOW"], color="red", s=10, zorder=3, label="at limit")
            a, z = IC_DIRECTION.get(icid, ("+", "-"))
            ax.set_title(f"{icid}  (+ = {a}->{z})", fontsize=9)
            ax.axhline(0, color="grey", lw=0.6)
            ax.grid(alpha=0.3)
            ax.xaxis.set_major_formatter(fmt)
        for ax in axes[n:]:
            ax.axis("off")
        axes[0].legend(fontsize=7)
        fig.suptitle("Interconnector flow vs PD limits (MW)")
        fig.autofmt_xdate()
        files.append(f"{OUTDIR}/chart_interconnectors.png")
        fig.savefig(files[-1], dpi=110, bbox_inches="tight")
        plt.close(fig)
    return files


# ---------------------------------------------------------------- main
def main():
    os.makedirs(OUTDIR, exist_ok=True)
    now = datetime.now(NEM_TZ)
    print(f"NEM time {now:%Y-%m-%d %H:%M}")

    pd_href = latest(PD_DIR, r"PUBLIC_PREDISPATCHIS_\d{12}_.*\.zip")
    print("PD file:", pd_href)
    run = re.search(r"_(\d{12})_", pd_href).group(1)
    pd_tables = read_aemo_zip(pd_href)
    print("PD tables:", sorted(t for _, t in pd_tables))
    scen = pd.DataFrame()
    if pick(pd_tables, "PRICESENSITIVITIES").empty:
        try:
            sens_tables = find_sensitivity_tables(run)
            pd_tables.update(sens_tables)
            scen = scenario_defs(sens_tables)
        except Exception as e:
            print("Sensitivity fetch failed:", e)
    df = analyse_pd(pd_tables)
    ic, ic_summ = analyse_interconnectors(pd_tables)

    try:
        pasa_href = latest(PDPASA_DIR, r"PUBLIC_PDPASA_\d{12}_.*\.zip")
        print("PDPASA file:", pasa_href)
        pasa = analyse_pdpasa(read_aemo_zip(pasa_href))
    except Exception as e:
        print("PDPASA fetch failed:", e)
        pasa = pd.DataFrame()

    notices = recent_notices()
    net_notices = [n for n in notices if is_network_notice(n)]
    other_notices = [n for n in notices if not is_network_notice(n)]
    summary = region_summary(df, pasa)

    has_sens = "SENS_MAX" in df.columns
    hot_mask = df["RRP"] >= P_HIGH
    if has_sens:
        hot_mask |= df["SENS_MAX"] >= P_SPIKE
    hot_cols = [c for c in ["REGIONID", "DATETIME", "RRP", "SENS_MAX", "SENS_N_GE_1000",
                            "SENS_MAX_SCEN", "TOTALDEMAND", "SPARE_MW", "SPARE_PCT"]
                if c in df.columns]
    hot = df.loc[hot_mask, hot_cols].copy()
    hot = hot.round({c: 0 for c in hot_cols if c not in ("REGIONID", "DATETIME", "SPARE_PCT")})
    hot = hot.round({"SPARE_PCT": 1}).rename(columns={
        "REGIONID": "region", "DATETIME": "period_ending", "RRP": "pd_rrp",
        "SENS_MAX": "scenario_max_rrp", "SENS_N_GE_1000": "scenarios_>=$1000",
        "SENS_MAX_SCEN": "max_scenario_#", "TOTALDEMAND": "demand_mw",
        "SPARE_MW": "spare_mw", "SPARE_PCT": "spare_%"})

    price_spike_times = df.loc[df["RRP"] >= P_SPIKE, "DATETIME"].unique()
    con_summ, con_hot = analyse_constraints(pd_tables, price_spike_times)

    piv = df.pivot_table(index="DATETIME", columns="REGIONID", values="RRP").round(0)
    fp = focus_periods(df, ic, pasa, pd_tables)
    pinned = con_summ[con_summ["pinned"]] if not con_summ.empty else con_summ
    con_summ = con_summ[~con_summ["pinned"]].drop(columns="pinned") if not con_summ.empty else con_summ
    focus_cons = (con_summ[con_summ["constraint"].map(relevant_constraint)]
                  if not con_summ.empty else con_summ)
    focus_notices = [n for n in notices
                     if re.search(rf"\b{FOCUS[:-1]}1?\b|{REGION_NAMES.get(FOCUS, '^$')}",
                                  f"{n['subject']} {n['reason']}", re.I)]
    charts = make_charts(df, ic, pasa, run, fp)

    def notice_lines(ns):
        if not ns:
            return "None in the last 24 h."
        return "\n".join(f"- **{n['time']}** `{n['type']}` — {n['subject']}"
                         + (f"\n  > {n['reason'][:300]}" if n["reason"] else "") for n in ns)

    run_dt = datetime.strptime(run, "%Y%m%d%H%M")
    md = [f"# AEMO Pre-dispatch Risk Forecast — PD run {run_dt:%Y-%m-%d %H:%M} (NEM time)",
          "",
          f"Generated {now:%Y-%m-%d %H:%M} NEM time. Horizon: {df['DATETIME'].min()} → "
          f"{df['DATETIME'].max()} (30-min periods, period-ending).",
          "",
          "**Risk levels:** Extreme = PD price ≥ $5,000 or LOR3 · High = ≥ $1,000 or LOR2 · "
          "Medium = ≥ $300, LOR1 or spare capacity < 5% · Watch = spike only in demand-sensitivity "
          "scenarios · Low = none of the above.",
          "",
          f"## 1. {FOCUS} focus",
          focus_headline(fp),
          "",
          f"### {FOCUS} period by period",
          "`scen_>=$1k` = demand-sensitivity scenarios with price ≥ $1,000. `from_X` = import "
          "from region X in MW (negative = export); ● = at the PD limit in the import direction. "
          f"`top_constraint` = highest-marginal-value binding constraint touching {FOCUS} "
          "(region letter match, heuristic) with its marginal value.",
          "",
          fmt_table(fp.assign(period_ending=fp["period_ending"].map(hhmm))),
          "",
          f"### Binding constraints touching {FOCUS}",
          fmt_table(focus_cons.head(20)) if not focus_cons.empty else "None.",
          "",
          f"### {FOCUS} market notices (last 24 h)",
          "NOTICES_FOCUS_PLACEHOLDER",
          "",
          "## 2. Regional summary (all regions)",
          fmt_table(summary.drop(columns="risk_level")),
          "",
          "## 3. High-price / spike-risk periods",
          "PD price ≥ $300, or ≥ $1,000 in at least one of the demand-sensitivity scenarios.",
          "",
          fmt_table(hot) if not hot.empty else "None.",
          "",
          "## 4. Price sensitivity",
          (fmt_table(df.groupby("REGIONID").agg(
              pd_max_rrp=("RRP", "max"), scenario_max_rrp=("SENS_MAX", "max"),
              periods_any_scenario_ge_1000=("SENS_N_GE_1000", lambda x: int((x > 0).sum())),
              max_scenarios_ge_1000_in_a_period=("SENS_N_GE_1000", "max"),
          ).round(0).reset_index()) if has_sens else "PRICESENSITIVITIES not found."),
          "",
          ("Scenario definitions (demand offset, MW):\n\n" + fmt_table(scen.reset_index()))
          if not scen.empty else
          "_Scenario definitions are not included in the sensitivity file, so scenario numbers "
          "are reported as-is._",
          "",
          "## 5. Interconnector limits",
          "A period counts as *at limit* when flow is within 1 MW of the PD export/import limit "
          "(or the interconnector has a non-zero marginal value). `limiting_constraints` is the "
          "constraint AEMO reports as setting that limit (EXPORTGENCONID / IMPORTGENCONID).",
          "",
          fmt_table(ic_summ) if not ic_summ.empty else "No interconnector data.",
          "",
          "## 6. Binding network constraints",
          "Constraint type / area / network state are decoded heuristically from AEMO's "
          "constraint naming convention.",
          "",
          "### Binding during PD price ≥ $1,000 periods",
          fmt_table(con_hot) if not con_hot.empty else "None.",
          "",
          "### Top binding constraints across the horizon",
          fmt_table(con_summ.head(25)) if not con_summ.empty else "No binding constraints reported.",
          "",
          f"Excluded from the rankings (marginal value ≥ ${MV_PINNED:,}, typically constraints that "
          "pin a flow or unit to a fixed value): " +
          (", ".join(f"`{r.constraint}` ({r.windows})" for r in pinned.itertuples())
           if not pinned.empty else "none."),
          "",
          "## 7. PD PASA lack-of-reserve forecast",
          fmt_table(pasa[pasa["LORCONDITION"] > 0]) if (not pasa.empty and "LORCONDITION" in pasa
                                                        and (pasa["LORCONDITION"] > 0).any())
          else "No LOR forecast by PD PASA.",
          "",
          "## 8. Market notices (last 24 h)",
          "### Network / interconnector / constraint related",
          notice_lines(net_notices),
          "",
          "### Other",
          notice_lines(other_notices),
          "",
          "## 9. Regional price matrix ($/MWh)",
          fmt_table(piv.reset_index()),
          "",
          "Charts: " + (", ".join(os.path.basename(c) for c in charts) or "none") +
          " (in the `pd-risk-report` workflow artifact).",
          ]
    text = "\n".join(md).replace("NOTICES_FOCUS_PLACEHOLDER", notice_lines(focus_notices))
    with open(f"{OUTDIR}/pd_risk_{run}.md", "w") as f:
        f.write(text)
    df.to_csv(f"{OUTDIR}/pd_detail_{run}.csv", index=False)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)

    # Compact machine-readable dump (used to build dashboards from the job log)
    t = lambda s: pd.to_datetime(s).dt.strftime("%Y-%m-%d %H:%M").tolist()
    data = {
        "run": run, "generated": f"{now:%Y-%m-%d %H:%M}",
        "summary": json.loads(summary.to_json(orient="records")),
        "focus": FOCUS,
        "focus_periods": json.loads(fp.assign(period_ending=fp["period_ending"].dt.strftime(
            "%Y-%m-%d %H:%M")).to_json(orient="records")),
        "focus_constraints": json.loads(focus_cons.head(20).to_json(orient="records"))
        if not focus_cons.empty else [],
        "regions": {reg: {
            "t": t(g["DATETIME"]), "rrp": g["RRP"].round(1).tolist(),
            "sens_max": g["SENS_MAX"].round(1).tolist() if has_sens else [],
            "sens_n": g["SENS_N_GE_1000"].fillna(0).astype(int).tolist() if has_sens else [],
            "demand": g["TOTALDEMAND"].round(0).tolist() if "TOTALDEMAND" in g else [],
            "spare_pct": g["SPARE_PCT"].round(1).tolist() if "SPARE_PCT" in g else [],
        } for reg, g in df.groupby("REGIONID")},
        "interconnectors": {icid: {
            "t": t(g["DATETIME"]), "flow": g["MWFLOW"].round(0).tolist(),
            "exp": g["EXPORTLIMIT"].round(0).tolist(), "imp": g["IMPORTLIMIT"].round(0).tolist(),
            "bound": g["BOUND"].astype(int).tolist(), "con": g["LIMIT_CONSTRAINT"].tolist(),
        } for icid, g in (ic.sort_values("DATETIME").groupby("INTERCONNECTORID")
                          if not ic.empty else [])},
        "ic_summary": json.loads(ic_summ.to_json(orient="records")) if not ic_summ.empty else [],
        "constraints_hot": json.loads(con_hot.to_json(orient="records")) if not con_hot.empty else [],
        "constraints_top": json.loads(con_summ.head(25).to_json(orient="records"))
        if not con_summ.empty else [],
        "lor": json.loads(pasa[pasa["LORCONDITION"] > 0].assign(
            INTERVAL_DATETIME=lambda x: x["INTERVAL_DATETIME"].dt.strftime("%Y-%m-%d %H:%M"))
            .to_json(orient="records")) if (not pasa.empty and "LORCONDITION" in pasa) else [],
        "notices": notices,
    }
    with open(f"{OUTDIR}/pd_risk_{run}.json", "w") as f:
        json.dump(data, f, indent=1)

    print("\n===== REPORT BEGIN =====")
    print(text)
    print("===== REPORT END =====")
    print("===== DATA JSON BEGIN =====")
    print(json.dumps(data, separators=(",", ":")))
    print("===== DATA JSON END =====")


if __name__ == "__main__":
    sys.exit(main())
