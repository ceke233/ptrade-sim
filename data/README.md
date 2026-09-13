# data/ —— 本地数据目录（内容不入库）

这个目录本身进版本控制（靠 `.gitkeep`），但**里面的数据一律不入库**。
放三类东西：

| 放什么 | 典型路径 | 说明 |
|---|---|---|
| **DuckDB 库文件** | `data/quant.duckdb` | 引擎唯一数据源，由 `ptrade-sim db build` 构建 |
| **源行情 parquet** | `data/ashare_1d_stock/year=…/` | 建库的输入，hive 分区结构 |
| **可选增强数据** | `data/l2_auction.parquet` | 集合竞价；建库时灌入 `ashare_l2_auction` |

## 为什么必须不入库

DuckDB 库含 2019–2025 全部行情（约 19 亿行分钟数据），实测 **约 48 GB**。
它由源数据确定性构建，属于**派生物**，不该进 Git。

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
ptrade-sim db build --db data/quant.duckdb --data-dir G:/data \
    --start-year 2019 --end-year 2025

ptrade-sim env                                   # 确认库路径与覆盖范围
ptrade-sim db verify --db data/quant.duckdb --data-dir G:/data
```

`db_path` 也可以在 `ptrade_config.json` 里指定（例如库放在别的盘）：

```json
{ "db_path": "G:/quant.duckdb" }
```

## 磁盘与内存提示

- 构建 2019–2025 实测约 **48 GB**、约 **20 分钟**（8 线程）
- 回测本身是**流式**读库（`preload.mode=rolling`），不需要把这 48 GB 载入内存；
  实测分钟数据常驻约 35 MB/天，按可用内存的 25% 滚动
