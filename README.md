# ptrade-sim

把 **[恒生 PTrade](https://www.ptrade.com) 策略**在本地跑起来：分钟级撮合、
真实费率与 T+1、逐日净值与可视化看板。策略代码直接复用 PTrade API，不改一行就能回测。

- **分钟级撮合**：每交易日 241 个槽位（09:30 集合竞价成交 + 09:31–11:30 + 13:01–15:00）
- **同一份策略两种周期**：`minute`（默认）与 `daily`（每日 15:00 一次）
- **数据源唯一**：只连 DuckDB 物理库，单一取数路径、单一列名口径
- **资源自适应**：启动前探查 CPU/内存/磁盘，吃不下就排队而不是硬上
- **流式加载**：长区间回测内存平稳，不随区间线性增长
- **实时看板**：Vue 3 前端 + FastAPI 后端，回测中即可看净值、持仓、成交、日志

```
Python ≥ 3.10    DuckDB 物理库    PTrade API 兼容层（55 个 API）
```

---

## 安装

```bash
# 仅引擎
uv pip install "ptrade-sim[duckdb]"

# 含实时看板
uv pip install "ptrade-sim[duckdb,dashboard]"

# 本地开发（含测试与静态检查）
git clone https://github.com/ceke233/ptrade-sim.git && cd ptrade-sim
uv venv && uv pip install -e ".[dev,duckdb,dashboard]"
```

> 仓库**不含**前端构建产物（`web/dist` 与 `src/ptrade_sim/web/dist` 都在 .gitignore 里）。
> 想让看板显示界面，需先构建：`cd web && pnpm install && pnpm build`，
> 再用 `PTRADE_SIM_WEB_DIST` 指向它，或把 `web/dist` 拷到 `src/ptrade_sim/web/dist/`。
> 详见[构建前端](#构建前端看板)。

---

## 快速开始

### 1. 准备行情数据

引擎只读 DuckDB 物理库（默认 `data/quant.duckdb`）。库内表共 10 张，其中 **5 张必需**：

| 表 | 必需 | 说明 |
|---|---|---|
| `ashare_calendar` | ✅ | 交易日历 |
| `ashare_stock_basic` | ✅ | 证券基础信息（含 `list_date` / `delist_date` / `list_status`） |
| `ashare_1d_stock` | ✅ | 日线行情 |
| `ashare_1d_index` | ✅ | 指数日线（基准） |
| `ashare_1m_stock` | ✅ | 分钟行情（只跑 `daily` 周期可不灌） |
| `ashare_1m_index` | | 指数分钟 |
| `ashare_1d_feature` | | 特色因子 |
| `ashare_index_weight` | | 指数成分（**缺此表 `get_index_stocks` 返回空**） |
| `ashare_index_info_basic` | | 指数基础信息 |
| `ashare_l2_auction` | | L2 集合竞价（缺失时用 09:30 分钟 bar 近似） |

从 parquet 源构建：

```bash
ptrade-sim db build --db data/quant.duckdb --data-dir /path/to/data
ptrade-sim db build --db data/quant.duckdb --data-dir /path/to/data \
    --start-year 2019 --end-year 2025 --tables ashare_1d_stock,ashare_1m_stock

ptrade-sim db verify --db data/quant.duckdb --data-dir /path/to/data  # 契约 + 等价性校验
ptrade-sim db verify --list-contract                                 # 打印表契约
ptrade-sim db normalize --db data/quant.duckdb --dry-run              # 统一代码尾缀（.SH→.SS）
```

`db verify` 检查**契约**（列名/类型是否符合 `data_contract.py`）与**等价性**
（库内数据与 parquet 源是否一致）。契约是本项目字段口径的**唯一权威**：
PTrade 的字段命名与常见 A 股数据源不同，集中定义一处才能避免各取数路径各自翻译。

### 2. 编写策略

**推荐一个策略一个目录**：

```
strategies/my_momentum/
├── strategy.py             # 必需：策略代码
├── strategy_config.json    # 可选：该策略的配置
└── helper.py               # 可选：被 import 的模块（有 strategy.py 时不会被误选）
```

```python
# strategies/my_momentum/strategy.py
def initialize(context):
    set_benchmark("000300.SS")
    set_commission(commission_ratio=0.0003, min_commission=5.0)
    g.top_n = 3
    run_daily(context, rebalance, time="09:31")


def before_trading_start(context):
    g.pool = filter_stock_by_status(get_Ashares(), ["ST", "HALT", "DELISTING"])


def rebalance(context):
    df = get_history(20, "1d", g.pool, ["close"], fq="pre", is_dict=True)
    # …… 排序、下单
    for code in picks:
        order_target_value(code, context.portfolio.portfolio_value / g.top_n)
```

单文件也能跑（`--strategy path/to/x.py`），但**目录形态是推荐的**：
配置与代码放一起，换机器不必改策略目录。

### 3. 配置

优先级由低到高（后者覆盖前者）：

1. `config.example.json` — 公开模板，仅键结构与安全默认
2. `ptrade_config.json` / `.local_config.json` — 机器级私有（真实路径与库连接，**不入库**）
3. `<策略目录>/strategy_config.json` — 策略级（这个策略怎么跑）
4. 环境变量 `PT_SIM_*`
5. CLI 参数

```jsonc
// strategies/my_momentum/strategy_config.json —— 只写「这个策略怎么跑」
{
  "name": "我的动量策略",          // 看板展示名
  "frequency": "minute",
  "start_date": "2025-01-01",
  "end_date": "2025-12-31",
  "capital_base": 100000,
  "benchmark": "000300.SS",
  "params": { "top_n": 3 }        // 策略内用 get_strategy_params() 读
}
```

```jsonc
// ptrade_config.json —— 机器级设置（换台机器才需要改）
{ "db_path": "data/quant.duckdb" }
```

查看当前生效的配置来源与最终合并结果：

```bash
ptrade-sim env            # 人读格式
ptrade-sim env --json     # 纯 JSON（stdout 只有 JSON，便于脚本解析）
```

环境变量命名规则：`PT_SIM_` + 配置键的大写下划线形式（`.` → `_`）。

<details>
<summary>全部环境变量</summary>

| 环境变量 | 对应配置键 |
|---|---|
| `PT_SIM_DB_PATH` | `db_path` |
| `PT_SIM_STRATEGY` | `strategy` |
| `PT_SIM_START_DATE` / `PT_SIM_END_DATE` | `start_date` / `end_date` |
| `PT_SIM_FREQUENCY` | `frequency` |
| `PT_SIM_CAPITAL_BASE` / `PT_SIM_BENCHMARK` | `capital_base` / `benchmark` |
| `PT_SIM_OUTPUT_DIR` | `output_dir` |
| `PT_SIM_PRELOAD_MODE` / `PT_SIM_PRELOAD_THREADS` / `PT_SIM_PRELOAD_ROLLING_WINDOW_DAYS` | `preload.*` |
| `PT_SIM_QUEUE_ENABLED` / `PT_SIM_QUEUE_MAX_PARALLEL` / `PT_SIM_QUEUE_MAX_WAIT_SEC` / `PT_SIM_QUEUE_POLL_INTERVAL` | `queue.*` |
| `PT_SIM_CACHE_MINUTE_MEMORY_BUDGET` / `PT_SIM_CACHE_DAILY_CAPACITY` | `cache.*` |
| `PT_SIM_COST_COMMISSION_RATIO` / `PT_SIM_COST_MIN_COMMISSION` / `PT_SIM_COST_SLIPPAGE_RATIO` / `PT_SIM_COST_STAMP_TAX` | `cost.*` |

不走配置体系的进程级变量：

| 环境变量 | 作用 |
|---|---|
| `PTRADE_SIM_WEB_DIST` | 看板前端产物目录的绝对路径（优先级最高） |
| `PT_SIM_PORT` | `dashboard` 的默认端口（等价 `--port`，默认 8765） |

</details>

### 4. 运行回测

```bash
ptrade-sim backtest --strategy strategies/my_momentum
ptrade-sim backtest --strategy strategies/my_momentum \
    --start 2020-01-01 --end 2025-12-31 --frequency minute --no-dashboard
```

| 选项 | 说明 |
|---|---|
| `--config` | 配置文件路径（默认按分层自动查找） |
| `--strategy` | 策略文件或目录（覆盖配置） |
| `--start` / `--end` | 回测区间 `YYYY-MM-DD` |
| `--capital` / `--db-path` | 初始资金 / 库路径 |
| `--frequency {minute,daily}` | 分钟（默认）或日级 |
| `--output-dir` | 输出目录（默认 `backtest_results`） |
| `--port` | 实时看板端口（默认 8765） |
| `--no-dashboard` | 不自动拉起看板 |
| `--no-queue` / `--no-wait` | 跳过资源排队 / 资源不足时不等待 |
| `--threads` | 预读线程数 |

### 5. 看板

回测时默认自动拉起（`--no-dashboard` 关闭），也可单独启动：

```bash
ptrade-sim dashboard --port 8765 --root backtest_results
```

前端功能：run 列表、指标卡、资金曲线与回撤、月度收益矩阵、持仓与成交明细、
日志尾（可向前翻页）、策略源码查看。

### 6. 产出

输出到 `backtest_results/{策略名}-{时间戳}/`。目录名取**策略目录名**
（单文件策略取文件名）而不是展示名 —— 展示名可含中文与空格，不能直接做路径。

| 文件 | 内容 |
|---|---|
| `summary.json` | 全部指标与配置留档（看板读它，字段见下） |
| `daily_stats.csv` | 每日净值/现金/持仓市值/基准/回撤（**回测中逐日落盘**，看板实时读） |
| `trades.csv` | 每笔成交：时间/代码/方向/数量/价格/佣金/盈亏 |
| `run_config.json` | 本次实际生效的配置（分层合并后的结果） |
| `strategy_config.json` | 策略目录里那份的副本（展示名等留档，看历史无需外部配置） |
| `strategy_source.py` | 策略源码副本（策略文件日后被删/改名也能回看） |
| `progress.json` | 实时进度：状态/阶段/已完成天数/当前日期/耗时 |
| `output.log` | 运行日志 |

<details>
<summary><code>summary.json</code> 字段</summary>

```
total_return / annual_return / sharpe / max_drawdown / calmar   核心收益与风险
win_rate / profit_loss_ratio / final_value                      交易统计
trade_count / total_commission / benchmark_return               成交与基准
daily_returns[]                                                 逐日收益（看板画图）
monthly_returns{ "2025-06": 0.024 }                             月度收益（键 YYYY-MM）
annual_returns{ "2025": {strategy, benchmark} }                 年度收益
monthly_stats{ win_rate, best_month, worst_month,               月度分布
               mean, median, std, skew, kurt }                  skew/kurt 无偏，与 pandas 一致
alpha_beta{ beta, alpha }                                       对基准的回归
config{ strategy_name, start_date, end_date, frequency, ... }   本次配置留档
resources{ estimated, snapshot, cache_stats }                   事前估算与事后实测画像
data_gaps{ missing_days, missing_day_count, daily_coverage }    回看窗口越界（仅有缺口时出现）
data_errors[ ... ]                                              取数失败（仅有失败时出现）
```

</details>

---

## 回测周期

| | `minute`（默认） | `daily` |
|---|---|---|
| 频率 | 每交易日 241 次 | 每日 15:00 一次 |
| 槽位 | 09:30（集合竞价成交）+ 09:31–11:30 + 13:01–15:00 | 单点 |
| `handle_data` | 每槽位调用 | 每交易日 1 次 |
| `run_daily` 的 time | 按指定时刻 | **强制 15:00** |
| 数据依赖 | `ashare_1m_stock`（必需） | 可跳过分钟预热，仅按需读日线/估值 |

**09:30 是一个真实槽位，不是笔误。** 官方 PTrade 在 09:30 触发一次（集合竞价成交），
把它与 `09:31–11:30 + 13:01–15:00` 相加恰好 241。去掉它会让所有日内策略少一个 bar。

日级模式会自动跳过分钟数据预热（日志打印「日线回测模式」），长区间日线回测因此快得多。

## 撮合规则

- **T+1**：当日买入不可卖（`closeable_amount` 只含昨日及更早的持仓）
- **涨跌停**：按板块判定（主板 10%、创业板/科创板 20%、ST 5%），`set_limit_mode` 可调
- **费用**：佣金（默认 0.03%，最低 5 元）、过户费（0.00487%）、印花税（卖出 0.1%）、滑点（默认 0）
- **复权**：`fq` 支持 `pre` / `post` / `dypre` / `None`，按 `adj_factor` 处理除权除息
- **停牌**：填前收盘价、量为 0；窗口首日无前收盘时填 NaN
- **回看越界**：窗口越过库内覆盖时缺失日填 NaN，写入 `summary.json` 的 `data_gaps` 并告警

## 资源管理与队列

回测启动前会探查 CPU / 内存 / 磁盘，给出开销估算，再决定是否放行：

- **资源足够** → 直接开跑
- **资源不足** → 进入跨进程队列等待（`~/.ptrade-sim/queue`），轮询直至可跑
- **超时或被拒** → 退出码 5（加 `--no-wait` 则不等待直接退出）

并发受 `queue.max_parallel`（默认 `cpu_count // 2`）与
`queue.cpu_slots_limit`（默认 `cpu_count - 1`）双重约束。查看队列状态：

```bash
ptrade-sim queue            # 人读
ptrade-sim queue --json     # 纯 JSON
```

## 缓存与内存

- **统一缓存**：`CacheGroup` 按用途分类，容量上限 + 内存预算 + LRU
- **流式加载**（默认）：`preload.mode="rolling"` 时分钟数据按**内存预算**滚动 ——
  既不预载全区间，也不按固定天数；内存充裕多留几天，紧张自动少留
- **预算**：`cache.minute_memory_budget` 默认 `"auto"`（可用内存 × `minute_memory_ratio`，
  默认 25%）。实测单日全市场分钟约 **35 MB**

```jsonc
"cache": { "minute_memory_budget": "2GB" }   // 或 "512MB" / 字节数
```

> ⚠️ `preload.mode="all"` 会让全区间分钟数据常驻：1699 天约 **76 GB**，会被准入检查拦下。

### DuckDB 缓冲池上限

**`cache.duckdb_memory_limit` 默认 `"2GB"，不要设为 `null`。**

DuckDB 的 `memory_limit` 默认是**系统内存的 80%**，且它的缓冲池
（`duckdb_memory()` 里的 `BASE_TABLE`）**只增不减** —— 读过的表页会一直被缓存。

本平台的负载是「每个交易日读不同日期的分区数据、几乎没有页复用」，所以这个缓冲池
纯属浪费：实测按约 **18 MB/天**累积。一次 6 年分钟回测（1455 个交易日）中，
仅它自己就吃到 **26 GB+**，把整体 RSS 推到 43.7 GB（而引擎预估只有 7.1 GB）。
设 2GB 后它封顶在 1.9 GB，RSS 平稳在 **16.4 GB**。

```jsonc
"cache": { "duckdb_memory_limit": "2GB" }   // 也接受 "512MB" / 字节数
```

该值会被拼进 DuckDB 的 `SET` 语句，因此做了格式校验（仅数字与单位）；
`"2GB; DROP TABLE ..."` 这类形态会抛 `ConfigError` 而不是被执行。

> DuckDB 的 `memory_limit` 是**库实例级**而非连接级：进程内指向同一库文件的连接
> 共享它，后设者覆盖。不要指望「两条连接各有各的上限」。

---

## 退出码

调用脚本可据此区分失败类别，不必解析错误文本：

| 码 | 含义 | 典型场景 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 未分类 | 编程错误（`TypeError` 等缺陷）、用法错误 |
| 2 | 配置 · 策略定位 | 配置文件缺失/非法、策略路径不存在、策略目录有多个 `.py` |
| 3 | **数据** | 库不存在、缺必需表、**取数失败（结果不可信）**、区间无交易日 |
| 4 | 策略代码 | 策略导入失败、不符合约定 |
| 5 | 资源 · 队列 | 资源准入被拒、排队超时 |
| 6 | 缺依赖 | 未安装可选的 `dashboard` 依赖 |

```bash
ptrade-sim backtest --config x.json || case $? in
  2) echo "配置或策略路径有问题" ;;
  3) echo "数据有问题 —— 结果不可信，别用" ;;
  5) echo "资源不够，稍后重试" ;;
esac
```

> **看到 `data_errors` 就说明结果不可信。** 取数失败（如 DuckDB 内存不足）会被
> 记为「该日无数据」，那些交易日的行情根本没进来，策略据此做的决策与真实行情无关。
> 此时指标虽然照常写出，但不应作为结论，CLI 也会以退出码 3 结束。
> 实测同一策略、同一区间，仅因 7 次被吞掉的查询失败，结果就从 12620.69% 变成 5706.29%。

映射实现在 `exceptions.exit_code_for()`。异常体系与「新异常必须多重继承旧类型」
的兼容约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 策略展示名

看板与日志里显示的策略名，**唯一来源是策略目录的 `strategy_config.json`**：

```json
{ "name": "一进二·5x892" }
```

没写 `name` 时退回策略目录名（单文件策略为其文件名）。展示名会随
`strategy_config.json` 一起留档到 run 目录，所以看板读历史结果无需任何外部配置。

这是有意的：展示名属于「这个策略是谁」，就该和策略代码放一起，
而不是维护一份脱离策略的全局映射表（那样策略一改名映射就失效）。

---

## API 支持清单

### 已实现（55 个）

按 `api.py` 的注册分组列出 —— 这张表与 `build_api()` 的返回值逐一对应，
`tests/test_api_surface.py` 会校验。

| 分组 | 数量 | API |
|---|---|---|
| 环境与调度 | 11 | `log`、`g`、`context`、`run_daily`、`get_frequency`、`get_business_type`、`is_trade`、`create_dir`、`get_research_path`、`get_user_name`、`get_strategy_params` |
| 设置类 | 9 | `set_universe`、`set_benchmark`、`set_commission`、`set_slippage`、`set_fixed_slippage`、`set_volume_ratio`、`set_limit_mode`、`set_yesterday_position`、`set_parameters` |
| 行情 | 5 | `get_history`、`get_price`、`get_trend_data`、`get_snapshot`、`get_current_kline_count` |
| 证券信息 | 18 | `get_stock_name`、`get_stock_info`、`get_stock_status`、`filter_stock_by_status`、`get_Ashares`、`get_trade_days`、`get_all_trades_days`、`get_trading_day`、`get_trading_day_by_date`、`check_limit`、`get_fundamentals`、`get_index_stocks`、`get_stock_exrights`、`get_stock_blocks`、`get_industry_stocks`、`get_reits_list`、`get_market_list`、`get_market_detail` |
| 交易 | 9 | `order`、`order_value`、`order_target`、`order_target_value`、`cancel_order`、`get_open_orders`、`get_order`、`get_orders`、`get_trades` |
| 持仓 | 3 | `get_position`、`get_positions`、`get_all_positions` |

回调（引擎调用策略的函数）：`initialize`、`before_trading_start`、`handle_data`、
`after_trading_end`、`on_order_response`、`on_trade_response`。
`handle_data` 可选 —— 未定义时引擎会告警并跳过每 bar 调用（纯 `run_daily` 策略如此）。

### 本平台扩展

- `get_strategy_params(key=None, default=None)` — 读 `strategy_config.json` 的 `params` 段。
  不传参返回全部（**只读副本**）。官方 PTrade 无此 API。

### 受限与占位

- `get_fundamentals`：仅支持 `valuation`（市值），其余报表返回空 DataFrame
- `get_index_stocks`：需库内有 `ashare_index_weight`，否则返回空列表
- `get_market_detail`：返回空 DataFrame（无盘口明细）
- **占位接口**（可调用、返回空值并在首次调用告警，不会 `NameError`；完整实现需补数据表）：
  `get_stock_exrights`、`get_stock_blocks`、`get_industry_stocks`、`get_reits_list`
- **no-op 设置**：`set_volume_ratio`、`set_yesterday_position` 可调用但不改变本地回测行为
- 实盘相关（推送类 API、实盘模式）不支持

> 官方签名与语义以 PTrade 官方文档为准。本仓库的实现状态由
> `tests/test_api_surface.py`（调用与语义断言）与 `api.py::build_api()`（注册清单）
> 共同定义 —— 两者不符即测试失败，不存在「文档说实现了但代码没有」的中间态。

---

## 与真实 PTrade 的差异

差异主要来自**输入数据口径**，而非引擎逻辑：

1. **集合竞价**：优先用 `ashare_l2_auction` 的 9:25 竞价量价；缺失时用 09:30 分钟 bar 近似
   （部分数据源把竞价与首分钟合成一根，边缘股票判定可能不同）
2. **指数成分**：需库内有 `ashare_index_weight`。只有最新快照的来源（如部分 akshare 数据）
   **存在幸存者偏差**，历史日期成分会被低估；来自 baostock 的沪深300/上证50/中证500 时点精确
3. **财务数据**：仅 `valuation` 可用
4. **数据覆盖**：库内只含已灌入的年份，区间超出会告警并写入 `data_gaps`
5. **撮合细节**：按分钟 bar 撮合，不模拟逐笔委托队列与部分成交

---

## 开发

当前状态：**17 个源码模块 / 8,183 行**、**489 项测试**、覆盖率 **83.1%**
（全局门槛 70%，另按模块设 ratchet 下限）。

```bash
pytest                    # 全部测试（约 35s）
pytest -m unit            # 只跑纯逻辑单测（不连库、不起服务）
pytest -m integration     # 只跑需要合成库/引擎的用例
ruff check src tests scripts
ruff format src tests scripts
mypy                      # 类型检查

pytest --cov --cov-report=json:coverage.json   # 覆盖率：全局门槛 70%
python scripts/check_coverage.py               # **每模块**下限（ratchet）
```

**测试不依赖真实行情库**：`tests/conftest.py` 用合成微型 DuckDB
（3 只股票 × 5 个交易日，毫秒级建好），表结构直接取自 `data_contract` ——
契约被改坏时夹具会立刻失败。`tests/test_long_horizon_smoke.py` 另建一个跨
14 个月的合成库（约 5 秒），专门覆盖「只有区间够长才走到」的代码路径。

### 覆盖率是 ratchet

`scripts/check_coverage.py` 按模块设下限、**只升不降**。全局门槛会掩盖局部裸奔 ——
cache 92% 的余量足以盖住 dbtools 58%。该脚本对「登记了下限但未出现在
`coverage.json`」的模块**判失败**，否则往 omit 里加一行就能让任意模块从统计中消失。

### 改完要跑长区间

项目两次被同一类缺陷咬到：`compute_metrics` 的月度统计有 `len(mr) > 3` 守卫、
`_price_cache` 有 `codes > 500` 守卫 —— **8 天区间会把它们整个绕过，且不报任何错**。

改动以下任一位置后，除 `pytest` 外还要跑一次长区间：

- 指标计算（`compute_metrics`）
- 任何带 `len(...) > N` / `count > N` 守卫的分支
- 缓存（`cache.py` 与各 `Cache` 的 put/evict）

```bash
pytest tests/test_long_horizon_smoke.py -q       # 快速：跨 14 个月的合成库
ptrade-sim backtest --strategy <策略> --start 2020-01-01 --end 2025-12-31   # 完整
```

<a id="构建前端看板"></a>

### 构建前端（看板）

仓库不含前端产物，本地装包后需自行构建：

```bash
cd web && pnpm install && pnpm build      # 产出 web/dist
# 让包内副本生效：
python -c "import shutil,pathlib; d=pathlib.Path('src/ptrade_sim/web/dist'); shutil.rmtree(d,ignore_errors=True); shutil.copytree('web/dist', d)"
```

也可用 `PTRADE_SIM_WEB_DIST` 指向任意目录（优先级最高）。

CI 见 [.github/workflows/ci.yml](.github/workflows/ci.yml)：lint + typecheck +
frontend（eslint + vue-tsc + vite build）+ Python 3.10–3.13 测试矩阵与每模块覆盖率下限
+ 构建校验（含 wheel 内看板产物的可服务性验证）。

工程化配置（pytest / ruff / mypy / coverage）集中在 `pyproject.toml`。
新增 API、改表结构、提交规范的约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，
版本变更见 [CHANGELOG.md](CHANGELOG.md)。

---

## 目录结构

```
ptrade-sim/
├── src/ptrade_sim/
│   ├── cli.py           命令行入口：参数解析与分发
│   ├── pipeline.py      回测流水线编排（配置合并→资源准入→执行→产出→拉看板）
│   ├── runtime.py       引擎核心：调度 + 撮合 + 指标计算
│   ├── api.py           PTrade API 适配层（55 个 API，按官方分类 6 组）
│   ├── history.py       历史数据取数与组装（日线/分钟/复权/重采样/数据缺口）
│   ├── conventions.py   市场约定（241 槽位、涨跌停规则）与代码/日期口径归一
│   ├── exceptions.py    异常体系与 CLI 退出码映射
│   ├── data_source.py   数据源层（DuckDBSource；ParquetSource 仅供 db build/verify）
│   ├── data_contract.py 表契约（PTrade 字段口径的唯一权威）
│   ├── dbtools.py       DuckDB 构建与校验
│   ├── cache.py         统一缓存（容量上限 + 内存预算 + LRU）
│   ├── resources.py     CPU/内存探查 + 开销估算 + 准入决策
│   ├── queue.py         资源感知回测队列（跨进程协调）
│   ├── config.py        配置分层（含策略级 strategy_config.json）
│   ├── server.py        看板后端（FastAPI 路由 + 静态托管 web/dist）
│   ├── runstore.py      看板数据层：读 run 产物（progress/summary/csv/log）
│   └── derived.py       看板派生指标（月度矩阵 / β-α 回归 / 资金曲线）
├── tests/               pytest 套件（合成 DuckDB 夹具，不依赖真实库）
├── scripts/             dev 工具（check_coverage.py：每模块覆盖率下限）
├── data/                本地数据目录（库文件/源行情；**内容不入库**，见 data/README.md）
├── web/                 看板前端（Vite + Vue 3 + TS；产物 dist/ 不入库）
├── examples/            示例策略（目录形态：strategy.py + strategy_config.json）
├── docs/                PTrade API 参考（ptrade_api.md）
└── .github/workflows/   CI：lint / typecheck / frontend / test / build
```

## License

MIT
