"""
抓取 Open Electricity 过去 2 年的 demand + 各燃料类型 generation。

用官方 SDK（openelectricity），自动分段、自动解析。
- demand 走市场接口 get_market（MarketMetric.DEMAND）
- generation 走发电接口 get_network_data（DataMetric.POWER + fueltech_group）

环境变量:
    OPENELECTRICITY_API_KEY  (必填，SDK 自动读取)
    OE_NETWORK   默认 NEM   (NEM / WEM)
    OE_INTERVAL  默认 1d    (5m / 1h / 1d / 1M / 1y ...)
"""
import os
import sys
import time
import pandas as pd
from datetime import datetime, timedelta

from openelectricity import OEClient
from openelectricity.types import DataMetric, MarketMetric

NETWORK = os.environ.get("OE_NETWORK", "NEM")
INTERVAL = os.environ.get("OE_INTERVAL", "1d")
OUTDIR = "data"

# 每个 interval 单次允许的最大天数（官方限制，留余量）
MAX_DAYS = {"5m": 7, "1h": 30, "1d": 365, "7d": 365, "1M": 730, "1y": 3650}


def chunked(client, kind, metrics, start, end, **extra):
    """按 interval 限制分段抓取，返回合并后的长表 DataFrame。
    kind: 'network'(发电) 或 'market'(需求)
    """
    max_days = MAX_DAYS[INTERVAL]
    frames = []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        print(f"    {cur.date()} -> {chunk_end.date()}")
        if kind == "network":
            resp = client.get_network_data(
                network_code=NETWORK, metrics=metrics, interval=INTERVAL,
                date_start=cur, date_end=chunk_end, **extra,
            )
        else:
            resp = client.get_market(
                network_code=NETWORK, metrics=metrics, interval=INTERVAL,
                date_start=cur, date_end=chunk_end, **extra,
            )
        df = resp.to_pandas()
        if not df.empty:
            frames.append(df)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def to_wide(long_df, metric_col, group_col):
    """长表 -> 宽表：每个分组(燃料/州)一列，行索引为时间。"""
    if long_df.empty:
        return long_df
    if group_col not in long_df.columns:
        return long_df.set_index("interval").sort_index()
    wide = long_df.pivot_table(
        index="interval", columns=group_col, values=metric_col, aggfunc="first"
    )
    return wide.sort_index()


def main():
    if not os.environ.get("OPENELECTRICITY_API_KEY"):
        sys.exit("缺少 OPENELECTRICITY_API_KEY（在 GitHub Secrets 里配置）")

    os.makedirs(OUTDIR, exist_ok=True)
    end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=730 - 2)  # 往里收 2 天避开 2 年历史边界

    print(f"网络={NETWORK} 粒度={INTERVAL}  区间 {start.date()} -> {end.date()}\n")

    with OEClient() as client:
        print("[1/2] demand（按州）")
        demand_long = chunked(
            client, "market", [MarketMetric.DEMAND], start, end,
            primary_grouping="network_region",
        )
        demand = to_wide(demand_long, "demand", "network_region")
        demand.to_csv(f"{OUTDIR}/demand_{NETWORK}_{INTERVAL}.csv")
        print(f"  -> {len(demand)} 行, 列: {list(demand.columns)}\n")

        print("[2/2] generation（按燃料类型）")
        gen_long = chunked(
            client, "network", [DataMetric.POWER], start, end,
            secondary_grouping="fueltech_group",
        )
        gen = to_wide(gen_long, "power", "fueltech_group")
        gen.to_csv(f"{OUTDIR}/generation_{NETWORK}_{INTERVAL}.csv")
        print(f"  -> {len(gen)} 行, 列: {list(gen.columns)}\n")

    print("完成。")


if __name__ == "__main__":
    main()
