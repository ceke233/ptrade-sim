# data/ —— 本地数据目录（内容不入库）

这个目录本身进版本控制（靠 `.gitkeep`），但**里面的数据一律不入库**。
放三类东西：

| 放什么 | 典型路径 | 说明 |
|---|---|---|
| **DuckDB 库文件** | `data/quant.duckdb` | 引擎唯一数据源，由 `ptrade-sim db build` 构建 |
| **源行情 parquet** | `data/ashare_1d_stock/year=…/` | 建库的输入，hive 分区结构 |
| **可选增强数据** | `data/l2_auction.parquet` | 集合竞价；建库时灌入 `ashare_l2_auction` |

## 为什么必须不入库

DuckDB 库含多年全市场分钟与日线行情，规模可达**数十 GB**。
它由源数据确定性构建，属于**派生物**，不该进 Git（仓库也放不下）。

`.gitignore` 里的保护：

```
data/*            # 目录内容全部忽略…
!data/.gitkeep    # …但保留目录本身
!data/README.md   # …以及这份说明
*.duckdb          # DuckDB 库文件在任何位置都忽略
*.duckdb.wal      # 预写日志
```

注意 `*.duckdb` 是**全局**规则 —— 即使你把库放在仓库根目录或别处，也不会被误提交。

## 构建

```bash
# 源行情目录（hive 分区 parquet）→ 库
ptrade-sim db build --db data/quant.duckdb --data-dir data/ \
    --start-year 2019 --end-year 2025

ptrade-sim env                                   # 确认库路径与覆盖范围
ptrade-sim db verify --db data/quant.duckdb --data-dir data/
```

`db_path` 也可以在 `ptrade_config.json` 里指定（例如库放在别的盘）：

```json
{ "db_path": "data/quant.duckdb" }
```

## 磁盘与内存提示

- 建库耗时与体积取决于源数据的年份跨度与股票池规模
- 回测本身是**流式**读库（`preload.mode=rolling`），不需要把整个库载入内存；
  分钟数据常驻约 35 MB/天，按可用内存的 25% 滚动
- **但要注意 DuckDB 自己的缓冲池**：它的 `memory_limit` 默认是系统内存的 80%，
  且缓冲池（`duckdb_memory()` 里的 `BASE_TABLE`）**只增不减** —— 读过的表页会
  一直被缓存。在「每天读不同日期、几乎没有页复用」的回测负载下这纯属浪费，
  实测按**每交易日十几 MB** 稳定累积，长区间会累积到数十 GB。

  所以 `cache.duckdb_memory_limit` 默认设为 `"2GB"`，**不要改成 `null`**。
  详见 [README 的「缓存与内存」](../README.md#缓存与内存)。
