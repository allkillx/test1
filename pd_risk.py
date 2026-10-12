"""
抓取 AEMO NEMweb 最新一期 Pre-dispatch (PD) + PD PASA，做今天/未来 ~40 小时的风险预测。

数据源（全部公开，无需 key）:
    PredispatchIS_Reports  30 分钟 PD：区域电价、需求、可用容量、价格敏感度、联络线
    PDPASA                 PD PASA：LOR（备用不足）情况、备用盈余
    Market_Notice          最近的市场通告（LOR / 干预 / 指令等）

输出:
    reports/pd_risk_<run>.md    Markdown 报告（同时写到 $GITHUB_STEP_SUMMARY）
    reports/pd_risk_<run>.json  结构化结果
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
NEM_TZ = timezone(timedelta(hours=10))  # NEM 时间 = AEST，全年不调夏令时
REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]

# 价格阈值 ($/MWh)
P_HIGH, P_SPIKE, P_EXTREME = 300, 1000, 5000

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (pd-risk-forecast)"


def listing(path):
    r = S.get(BASE + path, timeout=60)
    r.raise_for_status()
    return re.findall(r'HREF="([^"]+)"', r.text, flags=re.I)


def latest(path, pattern):
    files = sorted(h for h in listing(path) if re.search(pattern, h, re.I))
    if not files:
        raise RuntimeError(f"{path} 下没找到 {pattern}")
    return files[-1]


def read_aemo_zip(href):
    """解析 AEMO 多表 CSV（C/I/D 行），返回 {(组,表): DataFrame}。"""
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
                df[c] = pd.to_datetime(df[c], format="%Y/%m/%d %H:%M:%S",
                                       errors="coerce")
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


# ---------------------------------------------------------------- 风险规则
def price_level(p):
    if p >= P_EXTREME:
        return 4
    if p >= P_SPIKE:
        return 3
    if p >= P_HIGH:
        return 2
    return 0


LEVEL_NAME = {0: "低", 1: "关注", 2: "中", 3: "高", 4: "极高"}


def analyse_pd(tables):
    prices = no_intervention(pick(tables, "REGION_PRICES"))
    sol = no_intervention(pick(tables, "REGION_SOLUTION"))
    sens = no_intervention(pick(tables, "PRICESENSITIVITIES"))
    ic = no_intervention(pick(tables, "INTERCONNECTOR_SOLN"))

    df = prices[["REGIONID", "DATETIME", "RRP"]].copy()
    keep = [c for c in ["TOTALDEMAND", "AVAILABLEGENERATION", "DISPATCHABLEGENERATION",
                        "NETINTERCHANGE", "SS_SOLAR_UIGF", "SS_WIND_UIGF", "UIGF"]
            if c in sol.columns]
    if keep:
        df = df.merge(sol[["REGIONID", "DATETIME"] + keep], on=["REGIONID", "DATETIME"], how="left")
    if not sens.empty:
        # 场景列：老格式 RRPEEP1..43，新格式可能是 RRPSCENARIO1.. 等，统一按 "RRP*数字" 识别
        sc = [c for c in sens.columns if re.fullmatch(r"RRP[A-Z_]*\d+", c)]
        print(f"PRICESENSITIVITIES: {len(sens)} 行, 场景列 {len(sc)} 个: {sc[:3]}...")
        s = sens[["REGIONID", "DATETIME"]].copy()
        s["SENS_MAX"] = sens[sc].max(axis=1)
        s["SENS_MIN"] = sens[sc].min(axis=1)
        s["SENS_MAX_SCEN"] = sens[sc].idxmax(axis=1).str.extract(r"(\d+)$", expand=False)
        s["SENS_N_GE_1000"] = (sens[sc] >= P_SPIKE).sum(axis=1)
        df = df.merge(s, on=["REGIONID", "DATETIME"], how="left")
    if {"AVAILABLEGENERATION", "DISPATCHABLEGENERATION"} <= set(df.columns):
        df["SPARE_MW"] = df["AVAILABLEGENERATION"] - df["DISPATCHABLEGENERATION"]
        df["SPARE_PCT"] = 100 * df["SPARE_MW"] / df["TOTALDEMAND"].where(df["TOTALDEMAND"] > 0)
    df = df[df["REGIONID"].isin(REGIONS)].sort_values(["REGIONID", "DATETIME"])

    # 联络线：受限（|flow| 贴近限值）的时段
    ic_bind = pd.DataFrame()
    if not ic.empty and {"MWFLOW", "EXPORTLIMIT", "IMPORTLIMIT"} <= set(ic.columns):
        ic = ic.copy()
        ic["AT_EXPORT"] = (ic["MWFLOW"] >= ic["EXPORTLIMIT"] - 1) & (ic["EXPORTLIMIT"] > 0)
        ic["AT_IMPORT"] = (ic["MWFLOW"] <= ic["IMPORTLIMIT"] + 1) & (ic["IMPORTLIMIT"] < 0)
        ic["BOUND"] = ic["AT_EXPORT"] | ic["AT_IMPORT"]
        ic_bind = (ic.groupby("INTERCONNECTORID")
                   .agg(periods=("BOUND", "size"), bound=("BOUND", "sum"),
                        max_flow=("MWFLOW", "max"), min_flow=("MWFLOW", "min"))
                   .reset_index())
    return df, ic_bind


def find_sensitivity_tables(run):
    """PRICESENSITIVITIES 已不在 PredispatchIS 文件里；到 /Reports/Current/ 下找含
    "sensitiv" 的目录，取与 PD 同一期（或最新）的文件。"""
    dirs = [h for h in listing("/Reports/Current/") if re.search(r"sensitiv", h, re.I)]
    print("Sensitivity 目录:", dirs)
    for d in dirs:
        path = d if d.startswith("/") else "/Reports/Current/" + d
        zips = sorted(h for h in listing(path) if h.lower().endswith(".zip"))
        if not zips:
            continue
        same = [z for z in zips if run in z]
        href = (same or zips)[-1]
        print("Sensitivity 文件:", href)
        t = read_aemo_zip(href)
        for (grp, tbl), df in t.items():
            print(f"  表 {grp}.{tbl}: {len(df)} 行, 列 {list(df.columns)[:12]}...")
        return t
    return {}


def scenario_defs(tables):
    """场景定义（每个场景对各区域需求的偏移 MW），若文件里有的话。"""
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
                        "LORCONDITION", "RESERVEREQ"] if c in rs.columns]
    return rs[cols].sort_values(["REGIONID", "INTERVAL_DATETIME"])


def recent_notices(hours=24):
    try:
        hrefs = sorted(h for h in listing(NOTICE_DIR) if h.upper().endswith(".R"))[-60:]
    except Exception as e:  # 通告只是补充信息
        return [f"(拉取 Market Notice 失败: {e})"]
    out = []
    cutoff = datetime.now(NEM_TZ).replace(tzinfo=None) - timedelta(hours=hours)
    for h in hrefs:
        try:
            t = S.get(BASE + h, timeout=30).text
        except Exception:
            continue
        typ = re.search(r"Notice Type Description\s*:\s*(.+)", t)
        ref = re.search(r"Reason\s*:\s*\n?\s*(.+)", t)
        when = re.search(r"Creation Date\s*:\s*(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})", t)
        subj = re.search(r"External Reference\s*:\s*(.+)", t)
        if when:
            dt = datetime.strptime(when.group(1), "%d/%m/%Y %H:%M:%S")
            if dt < cutoff:
                continue
        line = " | ".join(x.group(1).strip() for x in (when, typ, subj) if x)
        if re.search(r"LOR|RESERVE|INTERVENTION|DIRECTION|RERT|PRICE|SUSPEN|ADMINISTERED|"
                     r"NON-CONFORMANCE|CONSTRAINT", line, re.I):
            out.append(line or (ref.group(1) if ref else h))
    return out


# ---------------------------------------------------------------- 汇总
def region_summary(df, pasa):
    rows = []
    for reg, g in df.groupby("REGIONID"):
        peak = g.loc[g["RRP"].idxmax()]
        low = g.loc[g["RRP"].idxmin()]
        lvl = int(g["RRP"].map(price_level).max())
        sens_max = g["SENS_MAX"].max() if "SENS_MAX" in g else float("nan")
        if pd.notna(sens_max) and price_level(sens_max) >= 3:
            lvl = max(lvl, 1)  # 敏感度场景有尖峰 → 至少"关注"
        spare_min = g["SPARE_PCT"].min() if "SPARE_PCT" in g else float("nan")
        lor = 0
        lor_first = None
        if not pasa.empty and "LORCONDITION" in pasa:
            p = pasa[pasa["REGIONID"] == reg]
            if not p.empty and p["LORCONDITION"].max() > 0:
                lor = int(p["LORCONDITION"].max())
                lor_first = p.loc[p["LORCONDITION"] > 0, "INTERVAL_DATETIME"].min()
        lvl = max(lvl, {0: 0, 1: 2, 2: 3, 3: 4}.get(lor, 4))
        if pd.notna(spare_min) and spare_min < 5:
            lvl = max(lvl, 2)
        neg = int((g["RRP"] < 0).sum())
        rows.append({
            "region": reg,
            "risk": LEVEL_NAME[lvl], "risk_level": lvl,
            "peak_rrp": round(float(peak["RRP"]), 2), "peak_time": str(peak["DATETIME"]),
            "min_rrp": round(float(low["RRP"]), 2), "min_time": str(low["DATETIME"]),
            "avg_rrp": round(float(g["RRP"].mean()), 2),
            "periods_ge_300": int((g["RRP"] >= P_HIGH).sum()),
            "periods_ge_1000": int((g["RRP"] >= P_SPIKE).sum()),
            "periods_negative": neg,
            "sens_max_rrp": None if pd.isna(sens_max) else round(float(sens_max), 2),
            "peak_demand_mw": round(float(g["TOTALDEMAND"].max()), 0) if "TOTALDEMAND" in g else None,
            "min_spare_pct": None if pd.isna(spare_min) else round(float(spare_min), 1),
            "pdpasa_max_lor": lor, "pdpasa_first_lor": None if lor_first is None else str(lor_first),
        })
    return pd.DataFrame(rows).sort_values("risk_level", ascending=False)


def fmt_table(df):
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r.values) + " |")
    return "\n".join(lines)


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    now = datetime.now(NEM_TZ)
    print(f"NEM 时间 {now:%Y-%m-%d %H:%M}")

    pd_href = latest(PD_DIR, r"PUBLIC_PREDISPATCHIS_\d{12}_.*\.zip")
    print("PD 文件:", pd_href)
    run = re.search(r"_(\d{12})_", pd_href).group(1)
    pd_tables = read_aemo_zip(pd_href)
    print("PD 表:", sorted(t for _, t in pd_tables))
    scen = pd.DataFrame()
    if pick(pd_tables, "PRICESENSITIVITIES").empty:
        try:
            sens_tables = find_sensitivity_tables(run)
            pd_tables.update(sens_tables)
            scen = scenario_defs(sens_tables)
        except Exception as e:
            print("Sensitivity 失败:", e)
    df, ic_bind = analyse_pd(pd_tables)

    try:
        pasa_href = latest(PDPASA_DIR, r"PUBLIC_PDPASA_\d{12}_.*\.zip")
        print("PDPASA 文件:", pasa_href)
        pasa = analyse_pdpasa(read_aemo_zip(pasa_href))
    except Exception as e:
        print("PDPASA 失败:", e)
        pasa = pd.DataFrame()

    notices = recent_notices()
    summary = region_summary(df, pasa)

    # 今天(NEM 日)的逐时段高风险清单
    today = now.date()
    hot = df[(df["RRP"] >= P_HIGH) | (df.get("SENS_MAX", df["RRP"]) >= P_SPIKE)].copy()
    hot_cols = [c for c in ["REGIONID", "DATETIME", "RRP", "SENS_MIN", "SENS_MAX",
                            "SENS_MAX_SCEN", "SENS_N_GE_1000", "TOTALDEMAND",
                            "SPARE_MW", "SPARE_PCT"] if c in hot.columns]
    hot = hot[hot_cols].round({c: 1 for c in hot_cols if c not in ("REGIONID", "DATETIME")})

    # 每区域按小时电价矩阵（今天 + 明天）
    piv = df.pivot_table(index="DATETIME", columns="REGIONID", values="RRP").round(0)

    run_dt = datetime.strptime(run, "%Y%m%d%H%M")
    md = [f"# AEMO PD 风险预测 — PD run {run_dt:%Y-%m-%d %H:%M} (NEM 时间)",
          "",
          f"生成时间 {now:%Y-%m-%d %H:%M} NEM；预测区间 {df['DATETIME'].min()} → {df['DATETIME'].max()}；"
          f"今天 = {today}",
          "",
          "风险等级：极高=价格≥$5000 或 LOR3；高=≥$1000 或 LOR2；中=≥$300 或 LOR1 或备用<5%；"
          "关注=仅敏感度场景出现尖峰",
          "",
          "## 区域汇总",
          fmt_table(summary),
          "",
          "## 高价 / 尖峰时段（RRP≥$300 或敏感度最大≥$1000）",
          fmt_table(hot) if not hot.empty else "无",
          "",
          "## 价格敏感度（各需求场景下的 RRP 范围）",
          fmt_table(df.groupby("REGIONID").agg(
              rrp_max=("RRP", "max"), sens_min=("SENS_MIN", "min"), sens_max=("SENS_MAX", "max"),
              periods_any_scen_ge_1000=("SENS_N_GE_1000", lambda x: int((x > 0).sum())),
          ).round(0).reset_index()) if "SENS_MAX" in df.columns else "未找到 PRICESENSITIVITIES 数据",
          "",
          "### 场景定义（需求偏移 MW）",
          fmt_table(scen.reset_index()) if not scen.empty else "文件中无场景定义表",
          "",
          "## 联络线受限时段数",
          fmt_table(ic_bind) if not ic_bind.empty else "无数据",
          "",
          "## PD PASA LOR",
          fmt_table(pasa[pasa["LORCONDITION"] > 0]) if (not pasa.empty and "LORCONDITION" in pasa
                                                        and (pasa["LORCONDITION"] > 0).any())
          else "PD PASA 未预测任何 LOR",
          "",
          "## 最近 24h 相关市场通告",
          "\n".join(f"- {n}" for n in notices) if notices else "无",
          "",
          "## 区域电价矩阵 ($/MWh)",
          fmt_table(piv.reset_index()),
          ]
    text = "\n".join(md)
    with open(f"{OUTDIR}/pd_risk_{run}.md", "w") as f:
        f.write(text)
    with open(f"{OUTDIR}/pd_risk_{run}.json", "w") as f:
        json.dump({"run": run, "summary": summary.to_dict("records"),
                   "hot": hot.astype(str).to_dict("records"),
                   "notices": notices}, f, ensure_ascii=False, indent=1)
    df.to_csv(f"{OUTDIR}/pd_detail_{run}.csv", index=False)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    print("\n===== REPORT BEGIN =====")
    print(text)
    print("===== REPORT END =====")


if __name__ == "__main__":
    sys.exit(main())
