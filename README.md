# Open Electricity 历史数据抓取

通过 [Open Electricity](https://platform.openelectricity.org.au)（前 OpenNEM）官方 API
抓取澳洲电网过去 2 年的 **demand** 和**各燃料类型 generation** 数据，跑在 GitHub Actions 上。

数据许可：CC BY-NC 4.0（非商用）。

## 一次性配置

1. 在 https://platform.openelectricity.org.au 注册并生成 API key。
2. 把这个仓库 push 到你的 GitHub。
3. 在仓库 **Settings → Secrets and variables → Actions → New repository secret** 新建：
   - Name: `OPENELECTRICITY_API_KEY`
   - Secret: 你的 key
   （绝不要把 key 写进代码或提交到仓库。）

## 运行

- 进 **Actions** 标签页 → 选 `fetch-openelectricity` → **Run workflow** 手动触发。
- 之后默认每周一自动跑一次（可在 `.github/workflows/fetch.yml` 改 `cron`）。
- 结果 CSV 写到 `data/` 目录并自动提交回仓库。

## 输出

| 文件 | 内容 |
|---|---|
| `data/demand_NEM_1d.csv` | 各州每日需求（MW） |
| `data/generation_NEM_1d.csv` | 各燃料类型每日发电（煤/气/风/光/水/电池…） |

## 调整

在 workflow 的 `env` 或本地环境变量里改：

- `OE_NETWORK`：`NEM`（东部）或 `WEM`（西澳）
- `OE_INTERVAL`：`1d`（默认）/ `1h` / `5m` / `1M` / `1y`
  - 粒度越细，分段请求越多。`5m` 拉满 2 年会有 ~100 段请求。

## 本地跑

```bash
pip install -r requirements.txt
export OPENELECTRICITY_API_KEY=xxxx
python fetch.py
```

## 注意

- Community（免费）账号只能查最近 **2 年**；要更早需 Academic/Enterprise 计划。
- 接近 2 年边界的请求脚本会自动跳过返回 400 的段。
"# 2yopenelectricity2" 
"# test1" 
