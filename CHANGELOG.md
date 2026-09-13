# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 新增

- **策略目录形态**：一个策略一个目录，代码与配置放在一起::

      strategies/my_strategy/
      ├── strategy.py             # 必需：策略代码
      └── strategy_config.json    # 可选：该策略的配置

  `strategy_config.json` 可含 `name`（看板/报告展示名）、回测区间、初始资金、
  基准、周期，以及 `params`（策略入参）。旧的单文件 `--strategy x.py` 写法**仍兼容**。
  目录中若有多个 `.py` 且无 `strategy.py` 会**报错并列出候选**，而不是静默挑一个。
- **策略级配置层**：配置分层变为
  `DEFAULTS ← config.example.json ← ptrade_config.json（机器级）← strategy_config.json（策略级）← env ← CLI`。
  原则是"谁的特异性高谁在上面"：`db_path`/`queue`/`cache` 留在机器级（换机器不必改策略目录），
  回测区间与资金放策略级。
- **`get_strategy_params()`**：策略读取 `strategy_config.json` 的 `params` 段。
  **本平台扩展**（官方 `set_parameters` 仅交易模块可用，回测无等价机制）。
  返回副本，策略修改不影响配置。
- **run 目录留档**：除 `strategy_source.py` 外，新增留档 `strategy_config.json`
  （看板据此显示展示名，无需 `strategy_names.json` 反查）与 `run_config.json`
  （合并后的完整生效配置，便于复现）。
- **双回测周期**：`frequency` 可选 `minute`（默认，`handle_data` 每交易日 241 次）
  与 `daily`（每天 1 次，15:00）。`run_daily` 在日级模式下按官方语义统一到 15:00 触发；
  日级模式**完全不读分钟数据**（实测同区间快 6 倍）。- **DuckDB 数据源**：引擎只连 DuckDB 物理库，不再直接读 hive 分区 parquet。
  取数路径与字段口径统一（`volume`/`money`/`preclose`）。
- **`ptrade-sim db` 子命令**：`build`（灌库）/ `verify`（契约与等价性校验）/
  `normalize`（统一既有库的代码尾缀、删除冗余表）。
- **资源自适应与回测队列**：按 CPU/内存/磁盘探查做准入，资源不足自动排队，
  崩溃进程的占位自动清理；`ptrade-sim queue` 查看运行与排队状态。
- **统一缓存**：原先 9 个手写缓存合并为 `CacheGroup`（容量上限 + 内存预算 + LRU）。
- **流式加载**：分钟数据按内存预算滚动（`preload.mode="rolling"`，默认），
  长区间内存平稳（实测 2GB 预算下 300 天与 900 天占用都封顶在 ~2,040 MB）。
- **配置三级分层**：`config.example.json` ← `ptrade_config.json` ← 环境变量 `PT_SIM_*`。
- **持仓查询 API**：补齐官方 `get_position` / `get_positions` / `get_all_positions`。
- **周期相关 API**：补齐 `get_frequency` / `get_current_kline_count` / `get_business_type`
  / `is_trade` / `create_dir` / `get_user_name`。
- **证券简称**：更名历史合并进日线 `name` 列（实测 2,939,341 行一致率 100.0000%），
  ST 进出与更名均按回测日精确生效。
- **指数成分权重**：`ashare_index_weight` 表补回 `weight` 列（每期合计 ≈100）。
- **工程化**：pytest 测试套件（277 项）、ruff（lint + format）、mypy、GitHub Actions CI、
  pre-commit 钩子、覆盖率门槛。

### 变更

- **默认库路径改为 `data/quant.duckdb`**（原 `G:/quant.duckdb`）。
  `data/` 成为约定的本地数据目录（构建产物 + 源行情 + 可选增强数据，均不入库）。
  库很大（实测约 48 GB），想放别的盘在 `ptrade_config.json` 覆盖 `db_path` 即可。
- **行尾统一为 LF**，并新增 [.gitattributes](.gitattributes) 强制。
  背景：仓库曾出现 `runtime.py` 全 CRLF、其余源文件 LF —— 功能无影响，
  但任何改动都会把整文件标记为已修改，review 时看不出真实变更。
- **内部数据通路全面转向 polars**。pandas 现仅保留在 **PTrade API 边界**
  （`get_history` / `get_price` / `get_fundamentals` / `get_market_*`，官方即返回 pandas）。
  迁移范围：
  - `DataFeed` 全部内部结构（日线、估值、基本表、基准指数、L2 竞价）改 polars；
  - 热点路径 `daily_rows` 由「polars → pandas → dict」两次转换改为直接从 polars 列构建；
  - `get_Ashares` 由 `iterrows()` 逐行改为 polars 过滤；
  - `compute_metrics` / `render_report` / `daily_stats_frame` / `trades_frame` 全程 polars；
  - 看板 `server.py` 完全去除 pandas（CSV 读取、分组、分页、JSON 均用 polars）。
  实测回测结果逐项不变（分钟级 0.16%/夏普 0.37/5 笔/佣金 2416.49；日级 2.42%/夏普 9.54）。
- **表结构**：行情表字段改用 PTrade 口径（原 `vol`/`amount`/`pre_close`
  → `volume`/`money`/`preclose`）；估值表改用官方 valuation 字段名
  （`total_value`/`float_value`/`total_shares`/`a_floats`/`dividend_ratio`）。
- **代码尾缀统一为 `.SS`/`.SZ`/`.BJ`**：入库时即规范化（原库内混用 `.SH` 与 `.SS`，
  直接写 SQL join 会静默匹配不上；修复后跨表 join 命中率 100%）。
- `--backend` 参数移除（数据源已统一为 DuckDB）。
- `aux_data_dir` / `--data-dir`（回测侧）废弃：L2 竞价与更名历史均已入库。

### 修复

- **`get_history(..., 'preclose')` 静默返回空列**：内部列名原为 `pre_close`，
  与官方字段 `preclose` 不一致，字段过滤时被无声丢弃。
- **回测队列 `--no-wait` 永久挂起**：`max_wait_sec=0` 原被同时用作
  「无限等待」与「不等待」两种语义，导致 `--no-wait` 反而永不放弃。
  现改为 `None` = 无限等待、`0` = 不等待。
- **`check_limit` 签名与返回类型错误**：官方为 `check_limit(security, query_date=None)`，
  接受 str 或 list 并返回 `dict[str:int]`；原实现只收 str 且返回 bool，传列表直接
  `TypeError: unhashable type: 'list'`。
- **看板响应含纯 `date` 对象时 500**：`date.isoformat()` 不接受 `sep` 参数，
  原代码对 `date` 与 `datetime` 一视同仁地传了 `sep`。
- **`db build --tables X` 成功却返回非 0**：子集构建被全量契约校验误判为缺表。
- **`db normalize --dry-run` 真的删表**：删除逻辑漏了 dry_run 守卫。
- **非分区表建库静默失败**：`source_kind` 默认值写死 `hive`，导致
  `calendar`/`stock_basic`/`index_weight` 等非分区表被当成分区表而什么都没建。
- **`_check_limit` 使用错误的 bar 字段**：对元组调用 `.close` 属性（应为索引 3）。
- **队列并发语义**：`max_parallel` 原被当作 CPU 槽上限，单任务线程数一超上限
  即永久排队；现拆分为「并发数」与「CPU 槽」两个独立维度，线程数封顶到 `cpu_count-1`。
- **队列目录绑定 output-dir**：不同输出目录的回测各排各的队、完全不协调；
  现改为机器级 `~/.ptrade-sim/queue`。
- **`estimate_bytes` 对 `__slots__` 类失真**：退化为 1MB 兜底值，
  使内存预算严重偏离（实测单日分钟应为 ~35MB）。
- **`ashare_l2_auction` 一次性载入 571 万行**：约 850MB 常驻；改为按日懒加载 + LRU。
- **区间超出库覆盖时静默无成交**：现启动时显式告警。
- 策略加载失败时给出明确错误，而不是 `AttributeError: 'NoneType' has no attribute ...`。
- **`compute_metrics` 的 `monthly_returns` 键错乱**（polars 迁移引入的回归）：
  `daily_stats.date` 是 ISO（`2025-01-02`），而迁移时把 `strftime("%Y-%m")`
  换成了 `str.slice(0,6)` —— 取到 `"2025-0"`，键因此变成 `"2025--0"`，
  与看板 `_monthly_extended` 的 `"YYYY-MM"` 键对不上
  → 月度柱状图 X 轴错、月度明细的基准/超额/β/α 整列取不到。
  现先归一为紧凑串再切分，ISO 与紧凑两种输入都能吃。
  > 同一处严格格式串假设当时还让已删除的 `render_report` 直接抛
  > `InvalidOperationError`（见「移除」）。该缺陷此前未被发现，是因为
  > 报告路径**零测试覆盖**，而回归验证一律用静态报告开关跳过它。
- **`check_limit(security, query_date)` 传 `datetime.date` 时静默返回 0**：
  内联 `.replace("-", "")` 假设 `query_date` 是 str，传 `date`/`datetime` 抛
  `AttributeError` 被宽 except 吞掉 → 涨跌停状态变成"既不涨停也不跌停"。
  现统一走 `_norm_day()`（本文件其它日期入参本就走它）。
  > ⚠️ 行为变化：此前"静默返回 0"的场景现在会正确返回 ±1，
  > 进而可能改变 `filter_stock_by_status` 的过滤结果与新回测的成交。
- **`compute_metrics` 单月回测抛 `TypeError`**：polars 的 `std()` 在样本 <2 时返回
  **null**（pandas 返回 NaN），未兜底导致 `float(None)`。该缺陷仅在真实库上暴露，
  合成夹具未覆盖，已补测试固定。
- **`compute_metrics` 对空表抛 `ColumnNotFoundError`**：看板在回测刚启动、
  `daily_stats.csv` 尚空时会调用它，现返回零值摘要。
- **进度快照写 CSV 失败**：仍调用 pandas 的 `to_csv`；改用 polars 并保留
  UTF-8 BOM（Excel 打开中文不乱码，与原行为一致）。
- **`get_history` 回看窗口越过库内数据覆盖时静默返回 NaN**：
  缺失的交易日走"停牌填充"分支，而窗口首日没有前收盘可填 → NaN；
  但 `get_history` **照样返回非空结果**，从返回值上完全看不出缺口。
  策略里极常见的 `close.iloc[0]`（算区间涨幅）拿到 NaN → `dropna()` 清空
  → **静默跳过调仓**，用户只看到"回测完成"、成交笔数被低估。
  实测：库内日线自 2019-01-02 起，把回测起点设为 2019-01-02 取
  `get_history(20)`，得到 20/20 全 NaN、且无任何提示。
  现区分「个股停牌」与「该交易日整体缺失」，后者一次性告警 + 收尾汇总
  + 写入 `summary.json` 的 `data_gaps`。
  > 区分点：`daily_rows(ds) is None` 表示**整个交易日**不在库中（覆盖之外）；
  > 停牌则是该日 `rows` 非空、只是该 code 无行。
- **引擎不再调用 `logger.remove()`**：`BacktestEngine` 是可被嵌入的库
  （测试、notebook、看板服务都直接构造它），清空全局 handler 会连带干掉
  宿主的日志配置 —— 曾导致测试里的告警捕获全部失效。控制台输出归 CLI 负责。

### 重构

- **拆分 `runtime.py`**（2950 → 2415 行），新增两个模块：
  - **`history.py`（546 行）** —— 历史数据取数与组装：日线/分钟取数、停牌填充、
    复权、重采样、字典结构转换、价格区间与交易日查询、集合竞价款，以及数据缺口
    追踪。**不含**撮合、下单、涨跌停判定、指标计算，也不含 API 参数默认值处理。
  - **`conventions.py`（139 行）** —— 无状态的 A 股市场约定：241 槽位与 09:30
    集合竞价时点、涨跌停规则（科创板/创业板/北交所/主板 + ST）、证券代码与日期
    的表示形式转换。**纯函数，可被各层自由引用而不产生循环导入**。
- 引擎与 `HistoryProvider` **共享同一个 `Clock`**（`_day_str` / `_slot_pos` 改为它
  的 property），避免「引擎已翻日、取数仍按上一日算」这类不报错但结果错的双份状态。
- 拆分的动机：这一簇逻辑与撮合几乎无耦合（实测只共享一个时钟和一个数据源），
  却占了 `runtime.py` 约六分之一，且它是最容易出错的部分（停牌填充、复权基准、
  跨日窗口、数据缺口），独立后便于单测与审查。
- **验证为零行为变更**：真实库回归分钟级 `0.16% / 夏普 0.37 / 5 笔 / 佣金 2416.49`
  与日级 `2.42% / 夏普 9.54 / 5 笔 / 65.81` 逐项一致；`daily_stats.csv` **逐字节相同**，
  `trades.csv` 仅随机 `order_id` 尾缀不同；319 项测试全过。
- `tests/test_architecture.py` 的 pandas 边界白名单新增**文件级防线**
  （`PANDAS_ALLOWED_FILES = {runtime.py, history.py}`），越界即失败。
- **拆分 CLI 与看板两层**（本轮第二刀，承接 `runtime.py` 的重构）：
  | 模块 | 前 | 后 | 职责 |
  |---|---|---|---|
  | `cli.py` | 568 | **323** | 参数解析与分发 |
  | `pipeline.py` | — | **262** | 回测流水线编排（配置合并→资源准入→执行→产出→拉看板） |
  | `server.py` | 798 | **313** | 看板 HTTP 路由 + 进程管理 + 静态资源 |
  | `runstore.py` | — | **422** | 看板数据层：读 run 产物 |
  | `derived.py` | — | **141** | 看板派生指标（月度矩阵 / β-α 回归 / 资金曲线） |
- **破除 `cli ↔ server` 循环依赖**：`server.py` 原先从 `cli.py` 延迟导入
  `DEFAULT_PORT` / `DEFAULT_RESULTS_DIR`（用函数内 import 掩盖反向依赖）。
  两个常量都是**应用级配置默认值**，已移入 `config.py` —— 现在
  `server` 完全不依赖 `cli`，延迟导入也删掉了。
- **看板层依赖单向**：`server → {derived, runstore}`、`derived → runstore`。
  拆分前用脚本验证过无环（`_slim_metrics` 原本会造成 runstore↔derived 环，
  已按职责归入 runstore）。
- **`scan_runs` 加指纹缓存**：看板每 3 秒轮询 `/api/runs`，原先每次全量重扫
  （124 个 run 实测 **98ms**，常驻约 3% 单核）。现按
  `(progress.json, daily_stats.csv, summary.json)` 的 mtime 指纹缓存，
  未变化直接复用 —— **98ms → 11ms（约 9 倍）**，结果与全量重算逐字节一致。
  缓存键带根目录，避免不同 `--root` 下的同名 run 互相串。
- 新增两条架构测试：`test_dashboard_layer_has_no_pandas`（覆盖看板三个模块）、
  `test_dashboard_layer_dependency_direction`（锁住单向依赖，防止再长出环）。

### 修复：取数失败被静默吞掉，回测**算错却报「完成」**

这是与内存问题同一次排查中撞见的、**更严重**的一个缺陷 —— 它直接让结果错。

- **现象**：同一次回测区间、同一策略，两次运行结果差异巨大：
  | | 总收益率 | 成交 |
  |---|---|---|
  | 有 7 次 DuckDB 查询失败 | **5706.29%** | 802 笔 | ← **错的** |
  | 0 次失败 | **12620.69%** | 1021 笔 | ← 对的 |
- **根因**：`_q` 把查询异常吞成「无数据」（返回 `None`），只留一行 `WARNING`。
  那 7 次是 `Out of Memory Error`，发生在分钟数据查询上 ——
  对应的交易日**行情根本没进来**，策略看到的是「无数据」而非真实行情，
  据此做出的决策与真实行情无关。而 run 目录照常产出 `summary.json`、
  日志照常打印「回测完成」，用户拿到的是一份**看起来很正常的错误结果**。
  一行 WARNING 埋在 28000 行日志里，实际上不可能被发现。
- **修法**：不再只靠日志。
  1. `DuckDBSource` 累计查询失败（`query_error_count` + 有上限的样本），
     提供 `data_errors()` 摘要，明确写出「结果不可信」而非「有告警」；
  2. `pipeline` 收尾时把明细写进 `summary.json` 的 `data_errors` 字段；
  3. **非零退出（`EXIT_DATA = 3`）**，让脚本/CI 无法忽略。

  > 这也顺带解释了为什么「取数失败」比「数据缺口」更需要硬失败：
  > 数据缺口（回看窗口越界）至少会填 NaN 并在 `data_gaps` 留档；
  > 而查询失败是**整日行情缺失**，且没有任何字段能反映它。

- 新增 `tests/test_data_errors.py`（7 项）+ `tests/test_pipeline.py` 的 2 项端到端用例，
  已用变异检验确认有效（把 `return EXIT_DATA` 改回 `return 0` → 测试失败并指出
  「返回 0 会让错误结果被当成正常产出」）。

### 修复：长区间回测内存无界增长（DuckDB 缓冲池）

**这是本项目最隐蔽的一类缺陷** —— 它不改任何数值结果、不报任何错、
日志里一行告警都没有，只是内存一路涨；而且**短区间完全看不出来**。

- **现象**：一进二策略 2020–2025（1455 个交易日，分钟频率，全市场选股）
  RSS 涨到 **43.7 GB**，而引擎启动时预估只有 **7,120 MB**（差 6 倍）。
- **定位过程**（逐层排除，每步都有实测）：
  | 实验 | 结果 | 排除 |
  |---|---|---|
  | 深测引擎持有的**全部**对象（day50→250） | 只涨 **67 MB**（0.34 MB/天），同期 RSS 涨 3,981 MB | 不是引擎对象图 |
  | 分钟预算压到 300 MB | 缓存稳定 11 条/296 MB，RSS 仍 19.9 MB/天 | 不是引擎缓存 |
  | `estimate_bytes` 准确度 | 单日 `DayMinuteData` 估算 = 实测 = 35.33 MB | 不是计量偏差 |
  | 查 `duckdb_memory()` 自报内存 | **1119 → 4781 MB，约 18 MB/天，tag 全是 `BASE_TABLE`** | **命中** |
- **根因**：DuckDB 的 `memory_limit` 默认是**系统内存的 80%**（本机 50 GiB），
  而它的缓冲池 `BASE_TABLE` **只增不减** —— 读过的表页会一直被缓存。
  本平台的负载是「每个交易日读不同日期的分区数据、几乎没有页复用」，
  于是这个缓冲池纯粹是浪费，按 18 MB/天 累积（×1455 天 ≈ 26 GB）。
- **修法**：连接建立时显式设 `memory_limit`，默认 **2GB**，
  可用 `cache.duckdb_memory_limit` 配置（`"512MB"` / `"2GB"` / 字节数）。
  该值会被拼进 `SET` 语句，故加了**格式校验**（仅数字与单位），
  挡掉 `"2GB; DROP TABLE ..."` 这类注入形态（抛 `ConfigError`）。
  顺带把 `con.sql(...)` 换成 `con.execute(...)` —— `sql()` 会返回一个
  未被消费的 relation 一直挂在连接上。
- **效果**（同一策略、同一区间：
  | | 修复前 | 修复后 |
  |---|---|---|
  | DuckDB 自报内存 | 1119 → 4781 MB（持续涨） | 封顶 **1907 MB** |
  | 6 年区间 RSS | **43.7 GB** 且仍在涨 | **平稳 16.4 GB**（其中约 10 GB 是设计内的分钟缓存）|
  | 900 天处 RSS 趋势 | 30+ GB 且爬升 | 16.2 → 16.5 GB **基本不动** |

  > 16.4 GB 的构成：分钟缓存 ~10 GB（预算内、设计如此）+ DuckDB 1.9 GB + 其余约 4 GB。

- **一个必须知道的 DuckDB 语义**（已写成测试）：`memory_limit` 是
  **数据库实例级**而非连接级 —— 进程内同路径的连接共享它，后设者覆盖，
  全部关闭后重置。这既解释了本修复为何有效（缓冲管理器是实例级的），
  也意味着测试之间会互相影响。

新增 `tests/test_duckdb_memory_limit.py`（10 项）：默认值必须存在、配置能传到引擎、
`SET` 真的落到连接上、SQL 注入形态被拒、非正数被拒、`threads` 未被挤掉、
以及上述实例级语义。其中「引擎漏接线」一项已用变异检验确认有效
（删掉 `DataFlow.__init__` 里的传参 → 测试失败）。

### 新增：长区间冒烟测试（覆盖「只有大 N 才走到」的代码路径）

项目历史上出现过**两次**同一类缺陷 —— 代码路径只在「区间够长 / 数据量够大」时
才执行，而回归验证一律用 8 天区间：

| 缺陷 | 触发条件 | 后果 |
|---|---|---|
| `compute_metrics` 的 `mr.kurt()` | 区间 > 3 个月 | polars 无此方法 → run 死掉、不产出 `summary.json` |
| `history._price_cache` 只写不清 | codes > 500 | 每天一条、永不释放 → 内存无界增长 |

两次都不是「没测到那行代码」，而是**没测到那个量级**。

新增 `tests/test_long_horizon_smoke.py`（7 项，**5.2 秒**）：
自建跨 **14 个月 / 2 个自然年** 的合成库（3 只股票 × 约 300 个交易日，日线模式），
跑一次完整回测并断言：跨 >12 个月、跨 ≥2 个年度、`kurt`/`skew` **真的被计算**
（而非 0.0 兜底）、与 pandas 逐位一致、核心指标无 NaN、按日缓存有界、年度收益齐全。

速度足够进 CI，随主测试套件一起跑。

### 修复：单文件策略的结果目录名取了父目录

`StrategyBundle.stem` 原为 ``dir.name or py.stem``，而**单文件形态**的 ``dir``
是父目录 —— 于是 ``examples/yijin2_5x892.py`` 的结果目录叫 ``examples-<时间戳>``：

- 名不副实（用户在看板和文件系统里直接看到）；
- **同目录下多个单文件策略无法区分**（结果目录只差时间戳）。

改为**显式字段**（而不是从 `dir` 推导）：目录形态取目录名，单文件形态取文件名。
新增 `tests/test_bundle_stem.py`（5 项），已用变异检验确认有效
（改回 `p.parent.name` → 3 项测试失败）。

### 附：长区间内存问题的定位记录（已在下文修复）

实测一进二策略 2020–2025（1455 个交易日，分钟频率，全市场选股），
RSS 增长到 **43.7 GB**，而引擎启动时的预估是 **7,120 MB**（差 6 倍）。

**这不是缓存问题 —— 缓存管理本身是正确的**，证据如下：

| 实验 | 结果 |
|---|---|
| 默认预算（11,178 MB） | 分钟缓存第 450 天封顶于 11,180 MB，此后不再增长 ✓ |
| 把预算压到 300 MB | 缓存稳定在 11 条 / 296 MB，**RSS 仍按 19.9 MB/天增长** |
| 深测引擎持有的全部对象（day50 → day250） | 合计只增长 **67 MB**（0.34 MB/天），而同期 RSS 增长 **3,981 MB** |
| `estimate_bytes` 准确度 | 单日 `DayMinuteData` 估算 35.33 MB = 实测 35.33 MB（比率 1.000）✓ |

即：**内存不在引擎的对象图里**，是 Python/NumPy 堆的高水位爬升
（逐日分配/释放不同尺寸的大缓冲，内存未归还 OS）。
这与文档中「长区间回测内存占用平稳、不随区间线性增长」的表述**不符**。

**影响**：长区间分钟回测**能跑完**（结果正确），但实际占用约为预估的 6 倍，
在内存较小的机器上有 OOM 风险。**根因待进一步定位**（可能需在分配器层面处理），
当前建议：长区间分钟回测按「实际用量 ≈ 预估 + 20 MB × 交易日数」预留内存。

### 修复：`get_price` 全市场批缓存无界增长

`history._price_cache`（`get_price` 在 `codes > 500` 时启用的批缓存）**只写不清**：
既不像同文件的 `_name_cache` / `_info_cache` 那样每日清理，也没有容量上限。

**为什么是真实风险**：项目的典型策略正是这条路径 ——
`pool = sorted(get_Ashares())` 得 **5380 只** >> 500，
然后每个交易日 `get_price(pool, end_date=prev_ds, count=1)`。
`end_date` 每天变 ⇒ `sel` 每天变 ⇒ **每天一条新缓存，永不释放**。
实测单条目规模：5000 码 × 1 天 × 6 字段 ≈ 0.3 MB；取 20 天则约 4.6 MB。
长区间可累积数 GB，且完全绕过 `cache.py` 的 LRU / 容量管理
（属于「分钟内存预算」之外的额外常驻）。

**修法**：在 `_run_day` 里与另两个按日缓存一起清空。
**零代价的论证**：缓存 key 含 `clock.day`，所以**跨日命中在构造上就不可能** ——
它的实际作用仅是「同一日内重复调用去重」。清掉不损失任何命中率。
> 这也解释了为什么当初会漏掉：既然跨日不可能命中，缓存看起来"很小"，
> 但它恰恰因为从不命中而**从不被复用**，于是只增不减。

新增 `tests/test_price_cache.py`（3 项）锁住「每交易日清空」这一不变量，
并已用变异检验确认有效（去掉 `clear()` → 测试失败并指出「未按日清理，长回测会无界增长」）。

### 修复：polars 迁移遗留的两处月度统计缺陷（**长回测必崩**）

`compute_metrics` 的月度统计里有两个只有**回测跨度 > 3 个月**才会执行的代码路径
（守卫是 `len(mr) > 3`），而项目历史上的回归验证**一律用 8 天区间** ——
于是它们从 polars 迁移起就带病，直到这次用 6 年区间实测才暴露。

- **🔴 `mr.kurt()` 抛 `AttributeError`** —— polars 的 `Series` **没有** `kurt()`
  （pandas 有）。它位于 `compute_metrics` 的**最后一步**，异常让整个 run 死掉、
  **不产出 `summary.json`**，产物只剩 `daily_stats.csv` / `trades.csv`，
  看板里该 run 显示为「数据缺失」。
  实测：6 年区间（72 个月）触发，`Traceback: 'Series' object has no attribute 'kurt'`。
  现自己实现 `_excess_kurtosis`，与 `pandas.Series.kurt()` **逐位一致**
  （10 组分布 × 样本量对照，Δ ≈ 1e-15）。
- **🟠 `mr.skew()` 静默给出错值** —— polars 的 `skew()` 默认 `bias=True`（有偏），
  而 pandas 是**无偏**。同一份数据：polars 默认 `-0.07687` vs pandas `-0.07852` ——
  差 0.0017，不会报错，只是指标悄悄变了。现显式 `bias=False`。

> **影响面核查**：现有 124 个历史 run **无一受影响** —— 它们全部产生于
> 09-02~09-10，而此缺陷是本次会话的 polars 迁移（09-12 起）引入的；
> 会话中的回归又恰好都用 8 天区间。也就是说它**尚未污染任何一次真实回测**，
> 但会打中下一次长区间运行。

新增 `tests/test_multimonth_metrics.py`（5 项）专门守这两条路径，
并已用**变异检验**确认有效：把 `kurt` 改回 `mr.kurt()` → 测试失败（AttributeError）；
去掉 `bias=False` → 测试失败（数值偏差）。

**长区间回测实测**（2020-01-01 ~ 2025-12-31，6 年 / 1455 个交易日 / 557 笔）：
```
总收益率 119.71% | 年化 14.61% | 夏普 0.81 | 最大回撤 -37.05%
月度统计：skew 1.0214 | kurt 1.8335 | 胜率 50.0%
summary.json 8.2 KB ✓   72 个月 / 6 个年度 / 月度键格式 YYYY-MM ✓
```
看板亦验证正常：`/api/runs`、`/detail`（72 个月度扩展键、1455 点资金曲线、β/α）、
`/trades`、`/log`、`/source` 全部 200，404 边界正确，前端 HTML 与静态资源正常加载。

### 重构：拆分 `_build_api`（501 行 / 52 闭包）

把 55 个 PTrade API 从 `runtime.BacktestEngine._build_api` 一个 **501 行的函数**里
拆到新模块 `api.py`（591 行），按**官方分类**分成 6 个工厂：

| 工厂 | 键数 | 内容 |
|---|---|---|
| `_api_env` | 11 | `log` / `g` / `context` / `run_daily` / 周期与杂项 |
| `_api_settings` | 9 | `set_*` 设置类 |
| `_api_market` | 5 | `get_history` / `get_price` / 竞价 / 快照 |
| `_api_info` | 18 | 证券名称/信息/涨跌停/交易日/估值/指数成分（含占位） |
| `_api_trading` | 9 | 下单四件套 + 撤单 + 委托/成交查询 |
| `_api_position` | 3 | 持仓查询三件套 |

`runtime._build_api` 现为 3 行转发方法。`runtime.py` **2423 → 1939 行**。

拆分前用脚本验证了三个前提，避免拍脑袋切：
1. **闭包之间几乎没有互相调用** —— 52 个闭包里只有 4 处用共享的 `_stub`、
   1 处 `_code_aliases`、1 处 `filter_stock_by_status` 调 `get_stock_status`
   （后两者本就同属「证券状态」，分组没有割裂依赖）；
2. **依赖方向单向** —— `runtime → api`。唯一需要 `Position` 的地方
   （无持仓时返回空 `Position`）改由引擎的 `_empty_position(code)` 承担，
   对象在哪定义就在哪构造，`api.py` 因此**完全不 import runtime**（已验证）；
3. **依赖的模块级符号很少** —— 7 个 `conventions`/`loguru` + 5 个 stdlib/三方，
   无隐藏耦合。

- **验证 55 个 API 键逐一对齐**（分组规模 11+9+5+18+9+3 = 55，无缺失无多出），
  并由既有的 `tests/test_api_surface.py` 55 项契约断言把关。
- 真实库回归**逐项一致**：分钟级 `0.16% / 7.07% / 0.37 / -2.45% / 5 笔 / 2,416.49`，
  日级 `2.42% / 112.26% / 9.54 / -0.16% / 5 笔 / 65.81`。
- 拆分过程中架构守卫**正确拦下两处遗漏**（这正是它们存在的意义）：
  `api.py` 未登记进 pandas 文件白名单（它确实是 API 边界的一部分）、
  未登记覆盖率下限。均已补上，`FLOORS` 与 `WATERMARK` 现为 17 个模块。

### 修复：三个真实缺陷（由本轮新增测试发现）

这三个缺陷有共同特征 —— **错得不明显**：要么被引擎吞掉、要么静默给出错数据，
回测照跑照出数字，用户不会收到任何提示。

- **🔴 `resample_1m` 频率解析少剥一位**（`history.py`）：`n = int(freq[:-2])`。
  - `'5m'` → `int('')` 抛 `ValueError`
  - `'15m'/'30m'/'60m'/'120m'` → **静默**按 1/3/6/12 分钟聚合

  即策略请求 15 分钟均线，实际拿到的是 **1 分钟**数据 —— 周期悄悄变成 1/15，
  信号全错但回测"正常完成"。现为 `int(freq[:-1])`。
- **🔴 `today_partial_row` 对切片调 `float()`**（`history.py`）：
  `o = md.open[s]` 得到 1 元素 ndarray，随后 `float(o)` 在 **numpy ≥ 2.5** 上
  抛 `TypeError`（"only 0-dimensional arrays can be converted to Python scalars"）。
  后果不只是崩溃：`include=True` 的分钟取数必失败，而引擎的 `_call_strategy`
  **吞掉异常并把该策略函数永久加入跳过列表** —— 实测 5 个交易日只有第 1 天执行了
  `run_daily`，回测仍报告"跑完"。
  现取首分钟标量 `md.open[s.start]`（与 `close` 取末分钟对称）。
- **🟠 `DataFeed.adj_factor` 对 NULL 因子抛 `TypeError`**（`runtime.py`）：
  原写法 `float(row["adj_factor"]) if row else None` 只判断了**行**是否存在，
  没判断**值**是否为 NULL（契约未标 NOT NULL）→ `float(None)`。
  现显式判空返回 `None`，`fq='pre'/'post'` 的取数不再整段失败。

> 两个缺陷原先以 `xfail(strict=True)` 登记在 `tests/test_history_resources_extra.py`，
> **修复后已摘掉标记**，现由普通断言持续守护。

### 测试

- 新增 5 个测试文件、共 **110 项**用例，覆盖率 **71.9% → 82.3%**：
  | 文件 | 用例 | 目标 |
  |---|---|---|
  | `test_runstore_cache.py` | 10 | `scan_runs` 指纹缓存的行为契约（此前 **0 处测试引用**）|
  | `test_pipeline.py` | 14 | 回测主路径：47% → 96% |
  | `test_dbtools_extra.py` | 19 | 建库/校验分支：58% → 72% |
  | `test_cli_server_extra.py` | 35 | `--json` 纯 JSON、404 边界、进程分支 |
  | `test_history_resources_extra.py` | 32 | 复权/重采样/停牌填充/平台内存探查 |
- **验证测试有效性用变异检验**，而非"跑得通"：对 6 个核心机制做故意破坏
  （指纹失效、缓存键去掉根目录、缓存污染、复权比例反转、重采样聚合取错、
  缺口告警静音），**全部被测试捕获**。另有 44 项变异，42 项被捕获。
- 修复 2 处**恒真断言**（`cpu_count >= 1` 由 `os.cpu_count() or 1` 保证恒真；
  `"jobs" in q or isinstance(q, dict)` 后半恒真）—— 它们零鉴别力，
  实测把 `run_queue` 的降级分支改成 `max_parallel=-999` 仍全过。改为结构契约断言。

### 覆盖率下限的两处漏洞（对抗验证发现并修复）

- **`WATERMARK` 漏登记 3 个模块**：`conventions.py` / `data_source.py` / `derived.py`
  （恰好是当时覆盖率最低的三个）。实测把它们的下限从 72/68/72 改成 **1**，
  `check_coverage.py`（exit 0）与全部元测试**一起放行** —— ratchet 形同虚设。
  现补齐为 16 个模块，并新增 `test_watermark_covers_every_floor` 强制两者的
  **键集合相等**（防止新增模块只登记一处）。
- **fail-open 改为 fail-closed**：`check_coverage.py` 原先对「登记了下限但未出现在
  `coverage.json`」的模块只打印提示、不判失败。实测在 `pyproject.toml` 的
  `[tool.coverage.run] omit` 里加一行，就能让任意模块从统计中消失而门禁全绿。
  现改为计入 violations 并失败。

### 异常体系与覆盖率下限

- **新增异常体系** `exceptions.py`。此前全仓 **0 个自定义异常**，错误类型是
  `ValueError`×8 / `FileNotFoundError`×4 / `OSError`×3 / `ImportError`×2 /
  `TimeoutError`×1 混用 —— 调用方**无法区分失败类别**（"配置写错"与"库不完整"
  都是 `ValueError`），CLI 也只能一律退出 1。
  现已把 14 个 raise 点归入 5 类：`ConfigError` / `DataError` / `StrategyError` /
  `QueueTimeoutError` / `DependencyError`（均继承 `PtradeSimError`）。
  - **向后兼容靠多重继承**：`StrategyPathError(ConfigError, FileNotFoundError)`
    这类定义让既有的 `except FileNotFoundError` 与 `pytest.raises(ValueError)`
    **继续有效**，因此 320 项既有测试**一行都不用改**。
    这不是取巧 —— `json.JSONDecodeError(ValueError)`、`ssl.SSLError(OSError)`
    都是标准库的同类做法。`tests/test_exceptions.py` 把这份兼容性写成了断言。
  - **CLI 分类退出码**（`exit_code_for`）：0 成功 / 1 未分类 / 2 配置·策略定位 /
    3 数据 / 4 策略代码 / 5 资源·队列 / 6 缺依赖。`main()` 增加顶层兜底，
    保证任何漏出的可预期错误都能拿到分类码而不是冒泡成一律 1。
    > ⚠️ **破坏性变更**：`ptrade-sim backtest` 因配置/策略路径问题失败时，
    > 退出码由 `1` 变为 **`2`**。依赖退出码的脚本需相应调整
    > （这也正是本次改动的目的 —— 让脚本能区分失败类别）。
  - **不进体系的两类**（有意）：`resources.py` 的 3 处 `OSError` 是平台内存探测的
    内部实现细节，被 `probe_memory` 的 try 立即捕获并进入下一级回退，对外不可见；
    `server.py` 的 4 处 `HTTPException` 是 FastAPI 的响应契约。
    编程错误（`TypeError` / `AttributeError`）同样不进 —— 那些是缺陷，
    不该被业务逻辑捕获，退出码落到 1。

- **每模块覆盖率下限（ratchet）**。此前只有**全局** `fail_under`，而全局达标会
  掩盖局部裸奔 —— cache 92% / config 98% 的余量足以盖住 dbtools 58%。
  现新增 `scripts/check_coverage.py`，按模块设下限（取当前实测值 −1%，
  **只许升不许降**），CI 在 pytest 之后跑它；全局门槛从 65 提到 **70**。
  另加 `tests/test_coverage_floors.py` 元测试，锁住三件靠"跑覆盖率"发现不了的事：
  新增模块忘了登记下限、有人为过 CI 调低下限、脚本与 `pyproject` 的全局门槛不一致。
  当前水位（见 `scripts/check_coverage.py` 的 FLOORS，由 ratchet 只升不降）：
  `exceptions` 99% / `resources` 99% / `config` 97% / `data_contract` 96% /
  `pipeline` 94% / `server` 93% / `cache` 91% / `queue` 90% / `cli` 85% /
  `runstore` 78% / `history` 78% / `runtime` 75% / `conventions` 72% /
  `derived` 72% / `dbtools` 70% / `data_source` 68%；全局 82.3%（门槛 70）。

### 清理与发布缺口修复（workflow 并行执行）

- **前端纳入 CI 与发布**（此前的发布级缺口）：
  - `ci.yml` 新增 `frontend` job：`pnpm install --frozen-lockfile` → `eslint` →
    `vue-tsc -b && vite build` → 上传 `web/dist`（`if-no-files-found: error`）。
    此前 `ci.yml` 提到 `web/` / `pnpm` / `vite` / `vue-tsc` / `eslint` / `node`
    的次数**全是 0**，TS 与 Vue 模板错误可畅通进主干。
  - 新增 `web/eslint.config.js` 与 `lint` script（web/ 此前无任何 lint 配置）。
  - **wheel 现在带前端**：此前实测 wheel 只有 18 个条目、**0 个前端文件**，
    `pip install ptrade-sim` 后 `ptrade-sim dashboard` 打不开看板。
    现将 `web/dist` 复制进 `src/ptrade_sim/web/dist`（正好命中 `server.py`
    `_web_dist_candidates()` 的第 2 条候选），wheel 变为 25 个条目、含 4 个前端文件；
    已实测解包后可 `resolve_web_dist()` 找到 `index.html`。
  - 过程中发现并规避两个隐蔽陷阱（已写入 `pyproject.toml` 注释，勿改回去）：
    1. **不能用 hatchling 的 `force-include`** —— 源路径不存在时直接抛
       `FileNotFoundError`，而 **editable 安装也走这条路径**，
       全新检出（`web/dist` 不存在）时 `pip install -e .` 会失败。
    2. **必须 `ignore-vcs = true`** —— 根 `.gitignore` 的 `dist/` 因「末尾斜杠不锚定」
       会匹配任意层级的 dist 目录，包括复制进来的 `src/ptrade_sim/web/dist/`，
       导致 hatchling 静默丢弃前端（这正是「wheel 里 0 个前端文件」的成因）。
       另注意 sdist 目标**不**关 VCS 排除（否则 `web/**` 会把 node_modules 打进 sdist）。
    3. **必须 `python -m build --wheel` 单独构建** —— 默认的 `python -m build`
       会先从 sdist 再构建 wheel，而 sdist 不含前端，产物又变回没有看板的 wheel。

- **删除死代码**（逐项核实过官方规范，避免误删 API 字段）：
  - `DataFeed.days_upto`：全仓零引用；其「保留」理由记在**已删除的**
    `docs/datafeed_redesign.md`，理由不再存在。相邻的 `days_between` 未动。
  - `DataFeed.aux_data_dir` 参数与赋值：接收但从不使用（恒被 `= None` 覆盖），
    无任何调用方 —— Parquet `data_dir` 时代残留。
  - `ParquetSource` 的 `exists` / `minute_day` / `feature` / `reference` / `describe`
    与 `RENAME_FEATURE`（107 行 → 56 行）：8 个方法只有 `daily` 被
    `dbtools.py` 调用一次。类 docstring 原本就写着「引擎已不再使用」。
  - `Admission.__bool__`（零引用）、`INDEX_WEIGHT` 的 `unsupported=()`（等于默认值）。
  - `resample_1m` 末尾的 `rename(columns={"time": "ts"})` —— 实测为 no-op
    （上游 `reset_index()` 后列名已是 `ts`）。
  > ⚠️ **未删**这些「零引用但属官方 API」的成员：`Position.last_sale_price`、
  > `Portfolio.portfolio_value` / `capital_used` / `returns`
  > （依据 `ptrade-api/references/objects.md:229-231`）。
  > 在 API 兼容层里，静态扫描看不到真正的消费者（用户策略），
  > **「零引用」不等于「可删」**。

- **修复不一致与静默行为**：
  - `capital_base` 默认值三处不一致（`config.py` 100000 vs `runtime.py` /
    `runstore.py` 1_000_000）→ 统一为 `config.DEFAULT_CAPITAL_BASE = 100000`。
  - `self.frequency` 在 `__init__` 里赋值 4 次 → 归一为局部变量后赋值一次。
  - `pipeline._count_trade_days` 查库失败时**静默**退回日历估算 →
    改为告警（这是全仓 9 处静默 `except` 里唯一应当修改的一处；
    其余 8 处是有意的「尽力而为」回退链，已逐处复核后保留）。
  - `limit_pct` 的 `int(is_st)` 遇 polars NULL（→ `None`）抛 `TypeError` →
    加安全取值，NULL 按非 ST 处理。
  - `DuckDBSource._q` 裸 `except Exception` 把 SQL 错误/表名拼错/库损坏一律变成
    「无数据」→ 保留不抛异常的行为（调用方依赖它做存在性探测）但补上 warning 日志。
  - `db verify` 硬编码 `range(2019, 2026)` → 新增 `--start-year` / `--end-year`。
  - `runstore` 排序键类型混用（命中时间戳目录为 str、未命中为 float mtime）→
    统一为可比较的数值，修复「按开始时间倒序」对混合目录不成立的问题。
  - `_tail_lines` 翻页越界时返回文件**开头** n 行而非空列表 → 改为返回空列表。

- **CLI 可用性**：新增 `--version`（输出 `ptrade-sim 0.2.0`）；
  `env` 与 `db verify` 新增 `--json`（此前只有 `queue` 有，`env` 有 21 处 `print`
  只能人读）。JSON 输出为**纯 JSON**（不含日志行），人读模式文案完全不变。

### 移除

- **`strategy_names.json` 策略名映射机制**（`runtime.STRATEGY_CN_NAMES` /
  `strategy_cn_name()` 等 49 行 + `.gitignore` 条目 + README 整节 +
  `strategy_names.example.json`）。展示名的唯一来源改为策略目录的
  `strategy_config.json` 的 `name`，缺失时退回策略目录名/文件名。
  > 为什么删：该机制在策略目录形态下**结构性失效** —— `strategy_cn_name()` 取
  > `Path(p).stem`，而目录形态里 `strategy` 字段指向 `.../dir/strategy.py`，
  > `stem` 恒为 `"strategy"`（即所有目录策略都会显示同一个名字）。
  > 实测确认删除无信息损失：全仓不存在 `strategy_names.json`；内置映射唯一条目
  > `demo_rotation` 对应 0 个 run；已有 run 的中文名早已固化在各自
  > `summary.json`，不依赖映射表。对现有 124 个 run 逐条比对，展示名**零变化**。
  > 这是上一版「策略目录」改动只做加法、未删旧机制的遗留。
- **`strategy_config.example.json`**：同一份 `strategy_config.json` 结构说明
  当时散落在 7 处（README §2 内联 JSON、`config.example.json` 的 `_comments`、
  `CONTRIBUTING.md`、`config.py` docstring、`examples/demo_rotation/strategy_config.json`
  等）。保留**可运行的真实示例** `examples/demo_rotation/`，删掉纯占位模板。
- **`report.html` 静态报告**：已有 Web 看板（`ptrade-sim backtest` 会自动拉起，
  `http://127.0.0.1:8765`），自包含 HTML 报告属重复能力，且它把 `matplotlib`
  这个重依赖拖进了运行时。一并删除 `runtime.render_report()` / `_frame_to_html()` /
  `setup_matplotlib_cn_font()`（共约 280 行）、CLI 的 `--no-report` 参数、
  `matplotlib` 依赖声明与其 mypy 覆盖项。
  > ⚠️ **破坏性变更**：命令行**不再接受 `--no-report`**，带了会报
  > `unrecognized arguments`。删掉该参数即可（行为与原来加它时一致）。
  > 回测产物不再包含 `report.html`；指标与图表全部由看板提供，
  > `summary.json` / `daily_stats.csv` / `trades.csv` 不受影响。
- **孤儿 optional-extra `tools` / `index`**：二者的唯一消费者（`tools/` 下的 L2 竞价提取
  与指数成分抓取脚本）已随该目录删除，全仓已无 `import psycopg2` / `import baostock` /
  `import akshare`。留着会让用户按 README 装了依赖却找不到脚本。README 相应段落已改写。
- **`examples/demo_rotation.py`**：单文件形态示例，已被目录形态
  `examples/demo_rotation/`（`strategy.py` + `strategy_config.json`）取代。
- **`sample_data/`**：8 个 CSV 样例。表结构以 `data_contract.py` 为唯一权威，
  随时可用 `ptrade-sim db verify --list-contract` 打印，无需再维护一份会过期的样例
  （实际上它引用的还是旧字段名 `vol`/`amount`/`pre_close`/`total_mv`）。
- **`ashare_1d_flag`**：其 `name`/`is_st`/`is_delisted` 与 `ashare_1d_stock`
  100.0000% 重复（实测重叠 7,989,129 行全部相同），引擎亦从未引用。
- **`ashare_name_change`**：更名历史已合并进日线 `name` 列。
- **死代码**（代码清理）：`to_data_code`、`cli.DEFAULT_CONFIG`、`config.REQUIRED`、
  `runtime.SLOT_INDEX`、`DayMinuteData.has_volume()`、`BacktestEngine._callbacks` /
  `_initialized`、`queue` 里恒空的 `*dead[:0]`、`server` 内重复的 `import numpy`。
- **重复代码**：`data_source` 6 处内联 `.SH→.SS` 收口到已有的 `_to_ss()`；
  `runtime` 10 处 8 位日期格式化收口到新 `_day_iso()`；`get_trading_day` 复用 `day_dt_date()`。

### 工程约定

- **`data/` 为本地数据目录**：目录本身入库（`.gitkeep`），内容一律忽略。
  `.gitignore` 新增 `*.duckdb` / `*.duckdb.wal` 全局规则 —— 库文件约 48 GB，
  **任何位置**都不会被误提交。详见 [data/README.md](data/README.md)。

## [0.2.0]

早期版本：PTrade API 适配层、分钟级撮合（T+1、除权除息、涨跌停、费用）、
实时看板、HTML 报告、示例策略。
