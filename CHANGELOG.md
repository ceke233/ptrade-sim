# 更新日志

本文件记录**用户可感知**的变更：新功能、行为变化、破坏性变更、缺陷修复。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

**当前状态**：17 个源码模块 / 8,183 行、489 项测试、覆盖率 83.1%。

---

## [Unreleased]

这一轮的主线是**工程化重构**与**修掉一批「静默出错」的缺陷**。

后者是重点：下面修复的 7 个缺陷里，有 4 个**不报错**，
只是让结果悄悄变错或让内存悄悄涨 —— 这类问题短区间回归发现不了，
所以本轮同时补了**长区间冒烟测试**与**取数失败的硬失败机制**。

### 新增

#### 长区间冒烟测试

`tests/test_long_horizon_smoke.py`：自建跨 **14 个月 / 2 个自然年**的合成库
（3 只股票 × 约 300 个交易日，日线模式），跑一次完整回测。**5.2 秒**，进 CI。

断言：跨 >12 个月、跨 ≥2 个年度、`kurt`/`skew` **真的被计算**（而非 0.0 兜底）、
与 pandas 逐位一致、核心指标无 NaN、按日缓存有界、年度收益齐全。

**为什么需要它**：项目历史上两次被同一类缺陷咬到 —— 代码路径只在
「区间够长 / 数据量够大」时才执行，而回归验证一律用 8 天区间。
两次都不是「没测到那行代码」，而是**没测到那个量级**。

#### 每模块覆盖率下限（ratchet）

`scripts/check_coverage.py` 按模块设下限（取实测值 −1%，**只升不许降**），
CI 在 pytest 之后跑它；全局门槛从 65 提到 **70**。

此前只有全局 `fail_under`，而全局达标会掩盖局部裸奔 ——
cache 92% / config 98% 的余量足以盖住 dbtools 58%。

`tests/test_coverage_floors.py` 用元测试锁住三件靠「跑覆盖率」发现不了的事：
新增模块忘了登记下限、有人为过 CI 调低下限、脚本与 `pyproject` 的门槛不一致。

#### 异常体系与分类退出码

此前全仓**0 个自定义异常**，错误类型是 `ValueError`×8 / `FileNotFoundError`×4 /
`OSError`×3 / `ImportError`×2 / `TimeoutError`×1 混用 —— 调用方**无法区分失败类别**
（「配置写错」与「库不完整」都是 `ValueError`），CLI 也只能一律退出 1。

现把 14 个 raise 点归入 5 类：`ConfigError` / `DataError` / `StrategyError` /
`QueueTimeoutError` / `DependencyError`（均继承 `PtradeSimError`）。

**向后兼容靠多重继承**：`StrategyPathError(ConfigError, FileNotFoundError)`
这类定义让既有的 `except FileNotFoundError` 与 `pytest.raises(ValueError)`
**继续有效**，因此 320 项既有测试**一行都不用改**。
这不是取巧 —— `json.JSONDecodeError(ValueError)`、`ssl.SSLError(OSError)`
都是标准库的同类做法。`tests/test_exceptions.py` 把这份兼容性写成了断言。

新增的 `EXIT_DATA = 3` 见下方「取数失败必须响亮失败」。

#### 本平台扩展 API

`get_strategy_params(key=None, default=None)` —— 读 `strategy_config.json` 的
`params` 段。官方 PTrade 无此 API。

#### 其他

- `--version` 打印版本号
- `ptrade-sim env --json` / `db verify --json`：纯 JSON 输出（stdout 只有 JSON）
- `db build` 的 `--start-year` / `--end-year`
- `data/` 目录约定（`.gitkeep` + `README.md`；库文件与源行情不入库）
- 示例策略改为**目录形态**（`examples/demo_rotation/`）

### 变更

#### ⚠️ BREAKING CHANGE：CLI 退出码不再统一为 1

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 未分类（编程错误、用法错误） |
| 2 | 配置 · 策略定位 |
| 3 | **数据（含「取数失败 → 结果不可信」）** |
| 4 | 策略代码 |
| 5 | 资源 · 队列 |
| 6 | 缺依赖 |

`ptrade-sim backtest` 因配置/策略路径问题失败时，退出码由 `1` 变为 **`2`**。
依赖退出码的脚本需相应调整 —— 这也正是本次改动的目的：
让脚本能区分失败类别而不必解析错误文本。

#### 其他变更

- **引擎拆分为多模块**（见「重构」），`runtime.py` 从 3283 行降到 1991 行
- **结果目录名取策略目录名**（单文件策略取文件名），而不是父目录名
- **DuckDB 缓冲池默认上限 2GB**（`cache.duckdb_memory_limit`）——
  不设会让长区间回测内存无界增长，见「修复」

### 移除

- **`sample_data/`**：数据统一走 DuckDB，仓库不再附带样例行情
- **HTML 报告**（`render_report` + matplotlib 依赖）：已有 Web 看板，报告冗余
- **`docs/` 下的其余文档**：只保留 `ptrade_api.md`
- **`tools/` 目录**：功能已并入 `ptrade-sim db`

### 修复

按严重性排序。**加粗的是「不报错、结果悄悄变错」那一类**。

#### 🔴 `get_Ashares` 只返回主板，静默漏掉 40% 的股票

官方语义是「获取指定日期**沪深市场的所有A股**代码列表」，而实现硬过滤
``market == "主板"``（docstring 也写着「指定日主板 A 股列表」）：

```
2025-01-02  全部沪深 A 股 5088 只 -> 只返回 3149 只
            创业板 1358 / 科创板 581 / 北交所 262 全部不返回
2020-01-02  3553 只 -> 只返回 2729 只
```

任何用 ``get_Ashares()`` 建全市场池的策略，都拿到一个**静默缩水四成**的
股票池，且不报任何错 —— 典型的「跑完但结果错」。
连带后果：需要创业板的策略只能绕过引擎自己去读源数据（实测有策略因此
硬编码了本机 parquet 路径，一旦该路径失效就静默退化成只剩主板）。

> ⚠️ **BREAKING CHANGE**：修复后 ``get_Ashares()`` 返回沪深全部 A 股。
> 依赖旧行为的策略会看到**股票池变大**，历史结果不再可比。
> 实测某「主板 + 创业板」策略的池子变化：
>
> | 回测日期 | 旧池 | 新池 | 差异 |
> |---|---|---|---|
> | 2020-01-02 | 2729 | 2799 | +70（科创板）|
> | 2024-01-02 | 3125 | 3695 | +570 |
> | 2025-01-02 | 3149 | 3732 | +583 |
> | 2025-06-03 | 3149 | 3160 | +11 |
>
> 差异主要来自科创板：该策略靠 ``get_index_stocks("000680.SS")``
> 相减来排除科创板，而该指数成分**只从 2025-01-17 起**，之前相减为空。
> 修复后请重跑历史区间，或把「排除科创板」改成不依赖指数表的方式
> （如按代码前缀 ``68`` 判定）。

**修法**：市场过滤改为沪深 A 股（主板 + 创业板 + 科创板），
**排除北交所**（官方原文是「沪深市场」，北交所代码为 43/83/87/92 段且尾缀
``.BJ``，既非沪也非深）。代码前缀复核改用**两位号段**
（深 ``00``/``30``、沪 ``60``/``68``）而不是三位：实测反例 ``302132.SZ``
（中航成飞，创业板，在市）—— 只写 ``300/301`` 会把它漏掉。
前缀不认识时**告警而非静默丢弃**（要么是脏数据如 ``TS0018.SS``，
要么是交易所新开号段，两种都该让人知道）。

新增 ``tests/test_get_ashares.py``（8 项）锁住：纳入沪深三板块、
排除北交所、时点过滤、脏数据被排除并告警、新号段被暴露、告警去重。
已用变异检验确认有效（回退为「只主板」→ 4 项测试失败）。

#### 🔴 取数失败被静默吞掉，回测**算错却报「完成」**

与下面一条同一次排查中撞见，但**更严重** —— 它直接让结果错。

| 情形 | 收益与成交 | 结果 |
|---|---|---|
| 有若干次 DuckDB 查询失败 | 与真实情况**量级级差异** | **错的** |
| 0 次失败 | 基准 | 对的 |

两次运行**都报告「回测完成」**，产出的 `summary.json` 也都格式完整 ——
差别只在那几行被淹没的 `WARNING`。

**根因**：`_q` 把查询异常吞成「无数据」（返回 `None`），只留一行 `WARNING`。
那 7 次是 `Out of Memory Error`，发生在分钟数据查询上 —— 对应的交易日
**行情根本没进来**，策略看到的是「无数据」而非真实行情，据此做出的决策与
真实行情无关。而 run 目录照常产出 `summary.json`、日志照常打印「回测完成」，
用户拿到的是一份**看起来很正常的错误结果**。一行 WARNING 埋在 28000 行日志里，
实际上不可能被发现。

**修法**：不再只靠日志。

1. `DuckDBSource` 累计查询失败（`query_error_count` + 有上限的样本），
   `data_errors()` 给出摘要，明确写出「结果不可信」而非「有告警」；
2. `pipeline` 收尾把明细写进 `summary.json` 的 `data_errors` 字段；
3. **非零退出（`EXIT_DATA = 3`）**，让脚本 / CI 无法忽略。

> 这也解释了为什么「取数失败」比「数据缺口」更需要硬失败：
> 数据缺口（回看窗口越界）至少会填 NaN 并在 `data_gaps` 留档；
> 而查询失败是**整日行情缺失**，且此前没有任何字段能反映它。

#### 🔴 长区间回测内存无界增长（DuckDB 缓冲池）

**现象**：一次多年期、分钟频率、全市场选股的策略回测中，进程 RSS 一路涨到
**数十 GB**，而引擎启动时的内存预估只有个位数 GB —— 相差近一个量级。

**定位过程**（逐层排除，每步都有实测）：

| 实验 | 结果 | 排除 |
|---|---|---|
| 深测引擎持有的**全部**对象 | 几乎不增长，而同期 RSS 涨了数百倍于此 | 不是引擎对象图 |
| 把分钟缓存预算压到很小 | 缓存稳定，RSS 仍按每交易日十几 MB 上涨 | 不是引擎缓存 |
| `estimate_bytes` 准确度 | 单日 `DayMinuteData` 估算 = 实测 | 不是计量偏差 |
| 查 `duckdb_memory()` 自报内存 | **按每交易日十几 MB 持续上涨，tag 全是 `BASE_TABLE`** | **命中** |

**根因**：DuckDB 的 `memory_limit` 默认是**系统内存的 80%**，而它的缓冲池
`BASE_TABLE` **只增不减** —— 读过的表页会一直被缓存。本平台的负载是
「每个交易日读不同日期的分区数据、几乎没有页复用」，这个缓冲池纯属浪费。

**修法**：连接建立时显式设 `memory_limit`，默认 **2GB**，
可用 `cache.duckdb_memory_limit` 配置。该值会被拼进 `SET` 语句，
故加了格式校验（仅数字与单位），挡掉 `"2GB; DROP TABLE ..."` 这类注入形态。
顺带把 `con.sql(...)` 换成 `con.execute(...)` —— `sql()` 会返回一个
未被消费的 relation 一直挂在连接上。

| | 修复前 | 修复后 |
|---|---|---|
| DuckDB 自报内存 | 持续上涨 | 封顶在 `memory_limit` 附近（默认约 1.9 GB）|
| 整机 RSS | 持续上涨、不收敛 | **进入平稳**，不再随区间增长 |
| 长区间趋势 | 越跑越高 | 基本不动 |

> 修复后的 RSS 基线以「分钟缓存预算（默认取可用内存的 25%）+ DuckDB 上限」为主，
> 二者都有界且可配置。

**一个必须知道的 DuckDB 语义**（已写成测试）：`memory_limit` 是
**数据库实例级**而非连接级 —— 进程内同路径的连接共享它、后设者覆盖、
全部关闭后重置。这既解释了本修复为何有效（缓冲管理器是实例级的），
也意味着测试之间会互相影响。

#### 🔴 `resample_1m` 频率解析少剥一位

`n = int(freq[:-2])`：

- `'5m'` → `int('')` 抛 `ValueError`
- `'15m'/'30m'/'60m'/'120m'` → **静默**按 1/3/6/12 分钟聚合

即策略请求 15 分钟均线，实际拿到的是 **1 分钟**数据 —— 周期悄悄变成 1/15，
信号全错但回测「正常完成」。现为 `int(freq[:-1])`。

#### 🔴 `today_partial_row` 对切片调 `float()`

`o = md.open[s]` 得到 1 元素 ndarray，随后 `float(o)` 在 **numpy ≥ 2.5** 上抛
`TypeError`（"only 0-dimensional arrays can be converted to Python scalars"）。

**后果不只是崩溃**：`include=True` 的分钟取数必失败，而引擎的 `_call_strategy`
**吞掉异常并把该策略函数永久加入跳过列表** —— 实测 5 个交易日只有第 1 天执行了
`run_daily`，回测仍报告「跑完」。

现取首分钟标量 `md.open[s.start]`（与 `close` 取末分钟对称）。

#### 🔴 `compute_metrics` 的 `mr.kurt()` 崩溃（polars 迁移遗留）

`compute_metrics` 的月度统计里有两个只有**回测跨度 > 3 个月**才会执行的代码路径
（守卫是 `len(mr) > 3`），而历史上的回归验证**一律用 8 天区间** ——
于是它们从 polars 迁移起就带病。

- **`mr.kurt()` 抛 `AttributeError`**：polars 的 `Series` **没有** `kurt()`
  （pandas 有）。它位于 `compute_metrics` 的**最后一步**，异常让整个 run 死掉、
  **不产出 `summary.json`**，产物只剩 `daily_stats.csv` / `trades.csv`，
  看板里该 run 显示为「数据缺失」。
  现自己实现 `_excess_kurtosis`，与 `pandas.Series.kurt()` **逐位一致**
  （10 组分布 × 样本量对照，Δ ≈ 1e-15）。
- **`mr.skew()` 静默给出错值**：polars 的 `skew()` 默认 `bias=True`（有偏），
  而 pandas 是**无偏**。同一份数据：polars 默认 `-0.07687` vs pandas `-0.07852`
  —— 不报错，只是指标悄悄变了。现显式 `bias=False`。

#### 🔴 `history._price_cache` 只写不清（内存无界增长）

`get_price` 在 `codes > 500` 时启用的批缓存，既不像同文件的
`_name_cache` / `_info_cache` 那样每日清理，也没有容量上限。

**为什么是真实风险**：项目的典型策略正是这条路径 ——
`pool = sorted(get_Ashares())` 得**数千只** >> 500，然后每个交易日
`get_price(pool, end_date=prev_ds, count=1)`。`end_date` 每天变 ⇒ `sel` 每天变
⇒ **每天一条新缓存，永不释放**。单条目约 0.3 MB（取 20 天则约 4.6 MB），
长区间可累积数 GB，且完全绕过 `cache.py` 的 LRU / 容量管理。

**修法**：在 `_run_day` 里与另两个按日缓存一起清空。
**零代价的论证**：缓存 key 含 `clock.day`，所以**跨日命中在构造上就不可能** ——
它的实际作用仅是「同一日内重复调用去重」。清掉不损失任何命中率。

> 这也解释了当初为什么会漏掉：既然跨日永不命中，缓存看起来「很小」，
> 但它恰恰因为从不命中而**从不被复用**，于是只增不减。

#### 🟠 `DataFeed.adj_factor` 对 NULL 因子抛 `TypeError`

原写法 `float(row["adj_factor"]) if row else None` 只判断了**行**是否存在，
没判断**值**是否为 NULL（契约未标 NOT NULL）→ `float(None)`。
现显式判空返回 `None`，`fq='pre'/'post'` 的取数不再整段失败。

#### 🟠 单文件策略的结果目录名取了父目录

`StrategyBundle.stem` 原为 `dir.name or py.stem`，而**单文件形态**的 `dir`
是父目录 —— 于是 `examples/demo_momentum.py` 的结果目录叫 `examples-<时间戳>`：
名不副实（用户在看板和文件系统里直接看到），且**同目录下多个单文件策略无法区分**。

改为**显式字段**（而不是从 `dir` 推导）：目录形态取目录名，单文件形态取文件名。

#### 🟠 覆盖率下限的两处漏洞（对抗验证发现）

- **`WATERMARK` 漏登记 3 个模块**：`conventions.py` / `data_source.py` /
  `derived.py`（恰好是当时覆盖率最低的三个）。实测把它们的下限从 72/68/72
  改成 **1**，`check_coverage.py`（exit 0）与全部元测试**一起放行** ——
  ratchet 形同虚设。现补齐为 17 个模块，并新增
  `test_watermark_covers_every_floor` 强制两者的**键集合相等**。
- **fail-open 改为 fail-closed**：原先对「登记了下限但未出现在 `coverage.json`」
  的模块只打印提示、不判失败。实测在 `[tool.coverage.run] omit` 里加一行，
  就能让任意模块从统计中消失而门禁全绿。现计入 violations 并失败。

#### 🟠 `check_limit` 静默返回 0（polars NULL）

`int(is_st)` 对 polars NULL 抛 `TypeError`，被吞后返回 0（= 未涨跌停）。
`limit_pct` 增加 NULL 守卫。

#### 🟡 `get_history` 回看越界静默填 NaN

窗口越过库内覆盖时缺失交易日被填成 NaN 却仍返回满 count 行 ——
依赖窗口起点（如 `close.iloc[0]`）的策略逻辑会失效，成交笔数被低估。
现记录到 `summary.json` 的 `data_gaps` 并显式告警。

#### 🟡 `_count_trade_days` 静默回退

取交易日数失败时静默回退到估算值 → 现记录 warning。
（另 8 处 `except` 是合理的最佳努力回退链，保持不变。）

### 重构

#### 引擎拆分为多模块

`runtime.py` 从 **3283 行降到 1991 行**，按职责拆出：

| 模块 | 职责 |
|---|---|
| `history.py` | 历史数据取数与组装（日线/分钟/复权/重采样/停牌填充/数据缺口） |
| `conventions.py` | 市场约定（241 槽位、涨跌停规则）与代码/日期口径归一 |
| `api.py` | PTrade API 适配层（55 个 API 按官方分类分 6 个工厂） |
| `pipeline.py` | 回测编排（配置 → 资源 → 队列 → 引擎 → 产出 → 看板） |
| `runstore.py` | 结果目录读取（看板数据源；含 mtime 指纹缓存） |
| `derived.py` | 看板派生指标（月度扩展 / β-α 回归） |
| `exceptions.py` | 异常体系与 CLI 退出码映射 |

依赖方向单向：`cli → pipeline → runtime → {api, cache, conventions, data_source, history}`；
`server → {runstore, derived}`。`tests/test_architecture.py` 强制。

#### 拆分 `_build_api`（501 行 / 52 闭包）

把 55 个 API 从 `runtime.BacktestEngine._build_api` 一个 **501 行的函数**里拆到
`api.py`，按官方分类分成 6 个工厂（环境与调度 11 / 设置类 9 / 行情 5 /
证券信息 18 / 交易 9 / 持仓 3）。`runtime._build_api` 现为 3 行转发方法。

拆分前用脚本验证了三个前提，避免拍脑袋切：

1. **闭包之间几乎没有互相调用** —— 52 个闭包里只有 4 处用共享的 `_stub`、
   1 处 `_code_aliases`、1 处 `filter_stock_by_status` 调 `get_stock_status`
   （后两者本就同属「证券状态」，分组没有割裂依赖）；
2. **依赖方向单向** —— 唯一需要 `Position` 的地方（无持仓时返回空 `Position`）
   改由引擎的 `_empty_position(code)` 承担，对象在哪定义就在哪构造，
   `api.py` 因此**完全不 import runtime**；
3. **依赖的模块级符号很少** —— 7 个 `conventions`/`loguru` + 5 个 stdlib/三方。

#### 清理与发布缺口修复

- 架构约束写成断言：pandas 只允许出现在 API 边界、看板层不得依赖引擎、
  已删表不得复活
- `pyproject.toml` 打包修正：wheel 目标用 `ignore-vcs = true`（**不能用
  `force-include`**，后者会破坏 editable 安装，且会因根 `.gitignore` 的
  `dist/` 规则未锚定而静默丢掉 `src/ptrade_sim/web/dist`）
- CI 增加 `frontend` job（eslint + vue-tsc + vite build）与 wheel 内看板产物的可服务性验证

### 测试

本轮新增 5 个测试文件、共 **110 项**用例，覆盖率 **71.9% → 83.1%**：

| 文件 | 用例 | 目标 |
|---|---|---|
| `test_runstore_cache.py` | 10 | `scan_runs` 指纹缓存的行为契约（此前 **0 处测试引用**）|
| `test_pipeline.py` | 16 | 回测主路径：47% → 96% |
| `test_dbtools_extra.py` | 14 | 建库/校验分支：58% → 72% |
| `test_cli_server_extra.py` | 33 | `--json` 纯 JSON、404 边界、进程分支 |
| `test_history_resources_extra.py` | 32 | 复权/重采样/停牌填充/平台内存探查 |
| `test_multimonth_metrics.py` | 5 | >3 个月才执行的统计路径 |
| `test_duckdb_memory_limit.py` | 10 | 缓冲池上限与实例级语义 |
| `test_data_errors.py` | 7 | 取数失败必须累计、落盘、非零退出 |
| `test_long_horizon_smoke.py` | 7 | 跨 14 个月的端到端冒烟 |
| `test_exceptions.py` | 9 | 异常层级与向后兼容 |
| `test_coverage_floors.py` | 6 | 覆盖率下限策略的元测试 |
| `test_bundle_stem.py` | 5 | 结果目录命名 |

**验证测试有效性用变异检验**，而非「跑得通」：对核心机制做故意破坏
（指纹失效、缓存键去掉根目录、缓存污染、复权比例反转、重采样聚合取错、
缺口告警静音、`return EXIT_DATA` 改回 `return 0`、删掉配置传参……），
确认对应测试会失败并指出原因。

同时修掉 2 处**恒真断言**（`cpu_count >= 1` 由 `os.cpu_count() or 1` 保证恒真；
`"jobs" in q or isinstance(q, dict)` 后半恒真）—— 它们零鉴别力，
实测把 `run_queue` 的降级分支改成 `max_parallel=-999` 仍全过。

### 工程约定

- `.gitattributes` 强制 LF（避免 Windows 上编辑器把源码转成 CRLF）
- `.pre-commit-config.yaml` 与 GitHub Actions（lint / typecheck / frontend / test / build）
- `data/` 内容不入库；`*.duckdb` 任意位置均不入库
- `CONTRIBUTING.md` 新增「验证要求：改完要跑长区间」与「错误与退出码」两节

---

## [0.2.0]

早期版本：PTrade API 适配层、分钟级撮合（T+1、除权除息、涨跌停、费用）、
实时看板、HTML 报告、示例策略。
