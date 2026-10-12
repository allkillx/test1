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

# Price thresholds ($/MWh)
P_HIGH, P_SPIKE, P_EXTREME = 300, 1000, 5000

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
    hot = b[b["DATETIME"].isin(hot_times)]
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
def make_charts(df, ic, pasa, run):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping charts")
        return []
    colors = {"NSW1": "#2a6fdb", "QLD1": "#a23b72", "VIC1": "#1b998b", "SA1": "#e07a1f",
              "TAS1": "#6c757d"}
    fmt = mdates.DateFormatter("%a %H:%M")
    files = []

    # 1. Regional prices (symlog so the price cap and negatives both stay readable)
    fig, ax = plt.subplots(figsize=(12, 5))
    for reg, g in df.groupby("REGIONID"):
        ax.step(g["DATETIME"], g["RRP"], where="post", label=reg, color=colors.get(reg), lw=1.8)
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
        ax.step(g["DATETIME"], g["RRP"], where="post", color=colors[reg], lw=1.8, label="PD RRP")
        if "SENS_MAX" in g:
            ax.step(g["DATETIME"], g["SENS_MAX"], where="post", color=colors[reg], lw=1,
                    ls="--", alpha=0.6, label="Max across scenarios")
            ax2 = ax.twinx()
            ax2.bar(g["DATETIME"], g["SENS_N_GE_1000"], width=0.018, color="grey", alpha=0.3)
            ax2.set_ylim(0, max(43, g["SENS_N"].max() if "SENS_N" in g else 43))
            ax2.set_ylabel("# scen ≥$1k", fontsize=8)
        if not pasa.empty and "LORCONDITION" in pasa:
            for _, r in pasa[(pasa["REGIONID"] == reg) & (pasa["LORCONDITION"] > 0)].iterrows():
                ax.axvspan(r["INTERVAL_DATETIME"] - pd.Timedelta(minutes=30),
                           r["INTERVAL_DATETIME"], color="red", alpha=0.12)
        ax.set_yscale("symlog", linthresh=100)
        ax.set_ylim(-1000, 40000)  # same scale for every region: negatives up to the price cap
        ax.set_ylabel(reg)
        ax.grid(alpha=0.3)
    axes[0].legend(loc="upper left", fontsize=8)
    axes[0].set_title("Price risk by region (red shading = PD PASA LOR; bars = scenarios ≥ $1000)")
    axes[-1].xaxis.set_major_formatter(fmt)
    fig.autofmt_xdate()
    files.append(f"{OUTDIR}/chart_region_risk.png")
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
                            alpha=0.5, step="post", label="limits")
            ax.step(g["DATETIME"], g["MWFLOW"], where="post", color="k", lw=1.4, label="flow")
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
    charts = make_charts(df, ic, pasa, run)

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
          "## 1. Regional summary",
          fmt_table(summary.drop(columns="risk_level")),
          "",
          "## 2. High-price / spike-risk periods",
          "PD price ≥ $300, or ≥ $1,000 in at least one of the demand-sensitivity scenarios.",
          "",
          fmt_table(hot) if not hot.empty else "None.",
          "",
          "## 3. Price sensitivity",
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
          "## 4. Interconnector limits",
          "A period counts as *at limit* when flow is within 1 MW of the PD export/import limit "
          "(or the interconnector has a non-zero marginal value). `limiting_constraints` is the "
          "constraint AEMO reports as setting that limit (EXPORTGENCONID / IMPORTGENCONID).",
          "",
          fmt_table(ic_summ) if not ic_summ.empty else "No interconnector data.",
          "",
          "## 5. Binding network constraints",
          "Constraint type / area / network state are decoded heuristically from AEMO's "
          "constraint naming convention.",
          "",
          "### Binding during PD price ≥ $1,000 periods",
          fmt_table(con_hot) if not con_hot.empty else "None.",
          "",
          "### Top binding constraints across the horizon",
          fmt_table(con_summ.head(25)) if not con_summ.empty else "No binding constraints reported.",
          "",
          "## 6. PD PASA lack-of-reserve forecast",
          fmt_table(pasa[pasa["LORCONDITION"] > 0]) if (not pasa.empty and "LORCONDITION" in pasa
                                                        and (pasa["LORCONDITION"] > 0).any())
          else "No LOR forecast by PD PASA.",
          "",
          "## 7. Market notices (last 24 h)",
          "### Network / interconnector / constraint related",
          notice_lines(net_notices),
          "",
          "### Other",
          notice_lines(other_notices),
          "",
          "## 8. Regional price matrix ($/MWh)",
          fmt_table(piv.reset_index()),
          "",
          "Charts: " + (", ".join(os.path.basename(c) for c in charts) or "none") +
          " (in the `pd-risk-report` workflow artifact).",
          ]
    text = "\n".join(md)
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
