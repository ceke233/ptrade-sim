# ptrade-sim

PTrade 量化策略的本地模拟回测平台（分钟级撮合 + 实时看板），支持 `uv tool install` 一键安装。

在本地复刻 [恒生 PTrade](https://www.ptrade.com) 的核心策略 API 与撮合行为，让 PTrade 策略无需修改即可在本地全市场数据上回测，用于策略开发、调参与验证。

> ⚠️ **免责声明**：本项目仅供学习与研究，不构成任何投资建议。回测结果不代表未来收益。

## 特性

- **PTrade API 兼容层**：40 个常用 API（清单见 `runtime.py` 的 `_build_api()`，契约测试见 [`tests/test_api_surface.py`](tests/test_api_surface.py)）
- **分钟级撮合**：241 根/交易日，含 09:30 集合竞价 bar；T+1、除权除息、佣金/印花税/过户费、涨跌停限制
- **双回测周期**：分钟级（241 次/日）与日级（15:00 一次），同一份策略代码均可运行
- **DuckDB 数据源**：引擎只连 DuckDB 物理库，取数路径与字段口径统一
- **资源自适应**：按 CPU/内存探查做准入，资源不足自动排队，长区间内存平稳（流式加载）
- **涨跌停板块规则**：主板 ±10%（ST ±5%）、创业板 2020-08-24 起 ±20%、科创板 ±20%、北交所 ±30%
- **实时看板**：回测逐日写入 `progress.json`，浏览器实时查看进度、净值曲线、回撤、月度收益、交易明细、策略源码
- **L2 集合竞价**：库内 `ashare_l2_auction` 表（9:25 竞价量价），`get_trend_data` 优先使用；
  缺表时回退 09:30 分钟 bar 近似
- **证券简称**：按回测日取生效简称（`ashare_1d_stock.name`），ST 进出与更名均精确

## 安装

```bash
# 仅引擎（回测 + HTML 报告）
uv tool install git+https://github.com/ceke233/ptrade-sim.git

# 含实时看板（FastAPI 依赖）
uv tool install "ptrade-sim[dashboard] @ git+https://github.com/ceke233/ptrade-sim.git"

# 本地开发
git clone https://github.com/ceke233/ptrade-sim.git
cd ptrade-sim
uv sync --extra dashboard          # 或 pip install -e ".[dashboard]"
```

安装后获得全局命令 `ptrade-sim`（子命令 `backtest` / `dashboard`）。

## 快速开始

### 1. 准备行情数据

引擎从 **DuckDB 物理库**取数。库由 hive 分区的 parquet 源数据构建，源目录结构如下
（**引擎不读它**，只在 `ptrade-sim db build` 时使用）：

```
data_dir/
├── ashare_calendar/data.parquet            # 交易日历: date(YYYYMMDD，字符串)
├── ashare_stock_basic/data.parquet         # 股票基础: code,name,market,list_status,list_date,delist_date
├── ashare_1d_stock/year=YYYY/month=MM/day=DD/data.parquet   # 日线（每天一个全市场文件）
├── ashare_1d_index/…                       # 指数日线（基准）
├── ashare_1d_feature/year=…                # 估值/股本（列名见下方契约）
├── ashare_1m_stock/year=…                  # 分钟线（每天一个全市场文件）
├── ashare_1m_index/year=…                  # 指数分钟线（可选）
└── ashare_index_weight/data.parquet        # 指数成分+权重（可选，支撑 get_index_stocks）
```

构建并指向数据库（**唯一必须的一步**）：

```bash
ptrade-sim db build --db data/quant.duckdb --start-year 2019 --end-year 2025
ptrade-sim env        # 确认 db_path、库内覆盖范围与资源
```

详见下文「[数据源：DuckDB 物理库](#数据源duckdb-物理库唯一)」。

### 回测周期：分钟级（默认）与日级

由 `config.frequency`（或 `--frequency`）切换，**同一份策略代码两种周期都能跑**：

```json
{ "frequency": "minute" }   // 默认：handle_data 每交易日 241 次
{ "frequency": "daily"  }   // 日级：handle_data 每天 1 次
```

语义严格对齐官方：

| | `minute`（分钟级） | `daily`（日级） |
|---|---|---|
| `handle_data` 触发 | 每交易日 **241 次**，09:30 ~ 15:00 | 每交易日 **1 次**，**15:00** |
| `run_daily` 任务 | 按设定时刻触发（09:30 前视为盘前任务） | **无论设定值都只在 15:00 触发** |
| `data[code]` 来源 | 当前分钟 bar | 当日**日线** bar（O/H/L/C/volume/money/preclose） |
| 撮合价 | 当前 bar 收盘价 | 当日**日线收盘价** |
| `get_frequency()` | `"minute"` | `"daily"` |
| `context.sim_params.data_frequency` | `"minute"` | `"daily"` |
| `get_current_kline_count()` | 已过槽位数（1…241） | `0`（无盘中分钟 bar） |
| 分钟数据 | 必需 | **完全不读**（省时省内存） |

> **分钟级为什么是 241 根而不是 240 根**：`09:30` 这一根对应**集合竞价的成交时点**——
> 实盘中集合竞价挂单就是在 9:30 撮合成交，开盘价即该次竞价的成交价。
> 因此 `handle_data` 必须在 09:30 触发一次，策略才能在该时点判断/下单；
> 若只从 09:31 起跑，等于丢掉开盘这个最重要的决策点。

实测同区间（2025-06-03~06-04）对比：

```
frequency=minute  handle_data 调用 241 次/日 | kline_count 1→241 | 耗时 0.6s
frequency=daily   handle_data 调用   1 次/日 | kline_count 0     | 耗时 0.1s
```

`get_history(frequency="1m"/"5m"/...)` 在日级模式下会明确告警并返回空
（官方日线回测同样无分钟数据）。

> ⚠️ **周期切换会显著改变结果，这是语义差异而非 bug**。依赖盘中时点的策略
> （打板 / 一进二 / 竞价买入）在日级模式下：`run_daily(09:26)` 之类的任务会被强制到 15:00、
> 成交价从盘中价变成**日线收盘价**，因此收益曲线会完全不同。实测同一打板策略同一区间：
> 分钟级 0.16%，日级 15.43%。**请用与策略设计意图相符的周期回测**；
> 日级模式适合以日线信号调仓的轮动/配置类策略。

### 数据源：DuckDB 物理库（唯一）

引擎**只连 DuckDB 物理库**，不再直接读 hive 分区 parquet。所有取数经统一数据源层
（`src/ptrade_sim/data_source.py`），列名一律 PTrade 口径，只有一条取数路径。

```bash
pip install "ptrade-sim[duckdb]"
ptrade-sim db build --db data/quant.duckdb --start-year 2019 --end-year 2025   # 从 parquet 灌库
ptrade-sim env                                                              # 查看库覆盖与资源
```

```json
{ "db_path": "data/quant.duckdb" }
```

> **库文件放哪：** 约定放在 `data/quant.duckdb`（`data/` 的内容已由 `.gitignore`
> 排除，`*.duckdb` 在任何位置都不会被提交）。库很大（2019–2025 实测约 48 GB），
> 想放别的盘就在 `ptrade_config.json` 里覆盖 `db_path`，例如
> `{ "db_path": "G:/quant.duckdb" }`。详见 [data/README.md](data/README.md)。

构建 2019-2025（1699 个交易日）实测约 47 GB / 约 20 分钟，含 19 亿行分钟数据：

| 表 | 行数 | 源 |
|---|---|---|
| `ashare_1m_stock` | 1,910,568,704 | hive 逐日 |
| `ashare_1m_index` | 209,501,300 | hive 逐日 |
| `ashare_1d_stock` | 7,989,350 | hive 逐年 |
| `ashare_1d_feature` | 7,931,512 | hive 逐年 |
| `ashare_l2_auction` | 5,711,800 | `l2_auction.parquet`（2021 起） |
| `ashare_1d_index` | 2,848,988 | hive 逐年 |
| `ashare_index_weight` | 409,134 | 成分+权重拉链（PTrade 7 个指数带权重） |
| `ashare_calendar` / `ashare_stock_basic` / `ashare_index_info_basic` | 5,343 / 5,829 / 732 | 单文件 |

> **代码尾缀统一为 PTrade 口径**：库内所有表的 ``code`` 一律 ``.SS`` / ``.SZ`` / ``.BJ``。
> 数据源原始为 ``.SH``，在**入库时**就统一（``ptrade-sim db build`` 内置规范化），
> 因此你可以直接对库写 SQL 做 join，不会因尾缀不一致而**静默**匹配不上。
> 既有旧库可用 ``ptrade-sim db normalize --db <库> --dry-run`` 检查、去掉 ``--dry-run`` 修复。

> **`ashare_1d_flag` 已删除**：其 ``name`` / ``is_st`` / ``is_delisted`` 三列与
> `ashare_1d_stock` **100.0000% 一致**（实测重叠 7,989,129 行全部相同），引擎也从未引用。

> **更名历史已合并进日线**（`ashare_1d_stock.name`），没有独立表。
> 实测源日线 `name` 列本身即**时点正确**的简称：对 2,939,341 行做全量比对，
> 与独立更名表推导结果**一致率 100.0000%**（0 处不一致），故独立表冗余已移除。
> 引擎改为从日线 `name` 派生「名称变化时点」（7,559 个），仅用于覆盖停牌等
> 无日线行情的日期 —— 否则会回退到基本表的**当前**简称，让历史日期显示未来才生效的名字。
> 该派生天然**时点安全**：只有日线中实际出现过的名称才会被记录。

> **所有可选增强数据都已并入库内表**，引擎不再依赖任何外挂文件
> （原先的 `l2_auction.parquet` / `name_change_df.csv` 与 `aux_data_dir` 配置均已废弃）。
> 引擎拿到的只有 `db_path` 一个数据入口。

其它命令：

```bash
ptrade-sim db verify --db data/quant.duckdb --data-dir G:/data   # 契约 + 与 parquet 逐字段对照
ptrade-sim db verify --list-contract                           # 打印表契约
```

> **表字段口径对齐 PTrade**：行情表用 `volume`(股) / `money`(元) / `preclose`，
> 估值表用 `total_value` / `float_value` / `total_shares` / `a_floats` / `dividend_ratio`
> 等官方 valuation 字段名。契约见 `src/ptrade_sim/data_contract.py`。
> 数据由用户直接管理（`CREATE TABLE` / `INSERT` / 导入），引擎只读消费。

> ⚠️ **回测区间须在库的覆盖范围内**。库只灌了部分年份时，区间超出覆盖会取不到行情；
> 引擎会在启动时显式告警（如「回测区间 20100104~20100108 超出库内日线数据覆盖
> 20190102~20251231」），不会静默给你一份"跑完但没交易"的空回测。
> 用 `ptrade-sim env` 可查看库内日线/分钟的实际覆盖范围。

### 配置三级分层

优先级由低到高（后者覆盖前者）：

1. `config.example.json` —— 公开模板，仅键结构与安全默认
2. `ptrade_config.json` / `.local_config.json` —— 本地私有，真实路径与库连接（**不入库**）
3. 环境变量 `PT_SIM_*` —— 临时覆盖

```bash
PT_SIM_DB_PATH=data/quant.duckdb PT_SIM_FREQUENCY=daily ptrade-sim backtest
ptrade-sim env      # 查看当前生效的配置来源、库内覆盖、资源快照与缓存策略
```

### 缓存与流式加载

- **统一缓存**：原先 9 个手写缓存合并为 `CacheGroup`（容量上限 + 内存预算 + LRU）。
- **流式加载（默认）**：`preload.mode="rolling"` 时分钟数据按**内存预算**滚动，
  既不预载全区间，也不按固定天数——内存充裕多留几天，紧张自动少留。
  实测 2GB 预算下 300 天与 900 天占用都封顶在 ~2,040 MB（长区间内存平稳）。
- **预算配置**：`cache.minute_memory_budget` 默认 `"auto"`（可用内存 × `minute_memory_ratio`，默认 25%），
  也可写 `"512MB"` / `"2GB"`。实测单日全市场分钟约 **35 MB**。

> ⚠️ `preload.mode="all"` 会让全区间分钟数据常驻内存：1699 天约 **76 GB**，会被准入检查拦下。

### 资源自适应与回测队列

回测启动前会探查 CPU/内存/磁盘，**吃不下就排队**，而不是硬上（硬上会 OOM 或互相拖慢）：

```
资源快照：CPU 16 核 | 内存可用 43,106 / 65,367 MB（已用 34%） | 磁盘可用 787 GB
本次回测：4 个交易日 / 2 线程 | 预计内存 1,076 MB | 占用 CPU 槽 2 | preload=rolling
WARNING 资源不足，进入等待队列：并发已达上限：正在运行 1 个，上限 1 个
排队 4s 后获得资源，开始回测（占 2 槽 / 1,076 MB）
```

- **两个独立维度**：`queue.max_parallel`（几个回测）× `queue.cpu_slots_limit`（线程数之和上限）
- **跨进程协调**：注册表在机器级目录 `~/.ptrade-sim/queue`（**不绑 `--output-dir`**，
  否则不同输出目录的回测各排各的队）；进程退出/崩溃自动注销（`atexit` + pid 存活探测清理陈旧项）
- **可观测**：`ptrade-sim queue` 查看运行/排队/资源视图（`--json` 供脚本消费）
- **参数**：`--no-queue` 跳过排队；`--no-wait` 资源不足直接退出；`--threads N` 降低占用
- 资源/缓存画像会写入 `summary.json` 的 `resources` 段，便于事后定位瓶颈
- 装了 `psutil` 能读到更准的系统 CPU 占用（可选依赖，不装也能跑）

表结构以 [src/ptrade_sim/data_contract.py](src/ptrade_sim/data_contract.py) 为**唯一权威**
（它同时驱动建库、引擎取数、测试夹具与文档），随时可打印：

```bash
ptrade-sim db verify --list-contract        # 列出每张表的列/主键/来源/派生列
```

主要表（字段为 PTrade 口径）：

| 表 | 关键列 |
|---|---|
| `ashare_1d_stock` | `code,date,open,high,low,close,volume,money,preclose,adj_factor,name,is_st,…` |
| `ashare_1d_feature` | `code,date,total_value,float_value,total_shares,a_floats,turnover_rate,dividend_ratio,…` |
| `ashare_1m_stock` | `code,date,trade_time,open,high,low,close,volume,money,preclose,…` |
| `ashare_calendar` / `ashare_stock_basic` | 交易日历 / 股票基础信息 |
| `ashare_l2_auction` | `code,date,hq_px,business_amount`（可选） |
| `ashare_index_weight` | `index_code,index_name,code,in_date,out_date,weight,source`（可选） |

> **dtype 契约**：`ashare_calendar.date` 与 `ashare_stock_basic.list_date/delist_date` 必须是
> 字符串 `YYYYMMDD`（非整数），否则 `get_Ashares` / 交易日历会报错。

**可选表 `ashare_index_weight`**（支撑 `get_index_stocks`）：

直接向 DuckDB 库导入即可，两种方式：

```bash
# 推荐：用 PTrade 权重拉链表（index_weight_link.parquet，含 weight/start_date/end_date）
#   源需先转成契约结构落到 <data_dir>/ashare_index_weight/data.parquet，再重建该表
ptrade-sim db build --db data/quant.duckdb --data-dir G:/data \
    --tables ashare_index_weight --overwrite

# 或直接 SQL 写入已建好的库
duckdb data/quant.duckdb -c "INSERT INTO ashare_index_weight VALUES (...)"
```

表结构（拉链/SCD，区间**左闭右开**：`in_date <= 交易日 < out_date`，`out_date=''` 表示至今）：

| 列 | 说明 |
|---|---|
| `index_code` | 指数代码（6 位，如 `000300`） |
| `index_name` | 指数名称 |
| `code` | 成分股（PTrade 尾缀 `.SS`/`.SZ`/`.BJ`） |
| `in_date` | 纳入生效日 `YYYYMMDD` |
| `out_date` | 调出生效日；空 = 截至抓取日仍在指数内 |
| `weight` | 成分权重（%，每期合计 ≈100）。**仅 PTrade 权重拉链表提供**；akshare/baostock 来源为 NULL |
| `source` | `ptrade`（权重拉链表，策略实盘口径）/ `baostock`（时点正确）/ `akshare`（仅最新快照，**有幸存者偏差**） |

查询语义为左闭右开 `in_date <= date < out_date`，回测中**无未来函数**。
PTrade 拉链表覆盖 7 个指数（2019 起）；baostock 覆盖沪深300/上证50/中证500（含历史调出）。
缺表时 `get_index_stocks` 降级为空列表并告警。

### 2. 编写策略

**推荐形态：一个策略一个目录**，代码与配置放在一起：

```
strategies/my_strategy/
├── strategy.py             # 必需：策略代码（PTrade 结构）
└── strategy_config.json    # 可选：该策略的配置
```

```json
// strategy_config.json
{
  "name": "我的动量策略",          // 看板/报告展示名
  "start_date": "2025-01-01",
  "end_date": "2025-12-31",
  "capital_base": 100000,
  "benchmark": "000300.SS",
  "frequency": "minute",
  "params": { "top_n": 3 }        // 策略入参，代码里 get_strategy_params() 读取
}
```

```python
# strategy.py —— 参数写在代码里做默认值，配置只覆盖要调的那几个
def initialize(context):
    g.top_n = int(get_strategy_params("top_n", 5))
    set_benchmark("000300.SS")
    run_daily(context, rebalance, time="09:31")
```

运行：`ptrade-sim backtest --strategy strategies/my_strategy`

也**兼容旧的单文件写法**：`--strategy path/to/x.py`（同目录若有
`strategy_config.json` 同样会被读取，便于渐进迁移）。

> **`get_strategy_params()` 是本平台扩展**，非 PTrade 官方 API ——
> 官方的 `set_parameters` 仅交易模块可用，回测里没有等价机制。
> 用法：`get_strategy_params()` 取全部（返回副本）、`get_strategy_params("k", 默认值)` 取单项。

完整的可运行示例见 [examples/demo_rotation/](examples/demo_rotation/)
（目录形态，含 `strategy_config.json`）。

### 3. 配置分层

优先级由低到高（后者覆盖前者）：

| 层 | 文件 | 放什么 |
|---|---|---|
| 1 | `config.example.json` | 公开模板：键结构与安全默认 |
| 2 | `ptrade_config.json` / `.local_config.json` | **机器级**私有：`db_path`、`queue`、`cache`（不入库） |
| 3 | **`strategy_config.json`** | **策略级**：回测区间、资金、基准、周期、`name`、`params` |
| 4 | 环境变量 `PT_SIM_*` | 临时覆盖 |
| 5 | CLI 参数 | 最高 |

分层的原则是**"谁的特异性高谁在上面"**：换台机器不该改策略目录，所以 `db_path`
留在机器级；而"这个策略跑哪段时间、多少资金"是策略自己的事，放在策略目录里。

```bash
PT_SIM_DB_PATH=data/quant.duckdb PT_SIM_FREQUENCY=daily ptrade-sim backtest
ptrade-sim env      # 查看当前生效的配置来源、库内覆盖、资源快照与缓存策略
```

```python
def initialize(context):
    set_benchmark("000300.XSHG")
    set_commission(commission_ratio=0.0002, min_commission=5.0, type="STOCK")
    run_daily(context, my_buy, time="09:30")

def my_buy(context):
    code = get_Ashares()[0]
    order_value(code, 10000)   # 引擎注入的 API 可直接调用
```

完整可运行示例见 [examples/demo_rotation/](examples/demo_rotation/)
（目录形态，含 `strategy_config.json`）。

模板文件：[config.example.json](config.example.json)（机器级）、
[strategy_config.example.json](strategy_config.example.json)（策略级）。

### 4. 运行回测

```bash
cd /path/to/workspace    # 配置、策略相对当前目录
ptrade-sim backtest                                  # 用配置里的 strategy
ptrade-sim backtest --strategy strategies/my_strategy
ptrade-sim backtest --strategy strategies/my_strategy --start 2024-01-01
```

参数：`--config`、`--strategy`（**目录**或 `.py`）、`--start`、`--end`、`--capital`、
`--db-path`、`--frequency`、`--output-dir`（默认 `./backtest_results`）、`--port`、
`--no-dashboard`、`--no-queue`、`--no-wait`。

回测会自动拉起本地看板（`http://127.0.0.1:8765`，已被占用则复用，失败不影响回测）。

### 5. 实时看板

```bash
ptrade-sim dashboard                 # 默认根目录 ./backtest_results，端口 8765
ptrade-sim dashboard --port 9000 --root /path/to/backtest_results
```

看板功能：run 列表（状态/进度/实时指标）、净值曲线与回撤、月度收益热力图、Beta/Alpha、
交易明细分页、日志尾（可向前翻页）、策略源码查看（回测时自动保存 `strategy_source.py` 副本）。

**前端需先构建一次**（构建产物 `web/dist` 由后端静态托管）：

```bash
cd web
pnpm install     # 需要 Node.js 20+ / pnpm
pnpm build
```

前端源码在 [web/](web/)（Vite + Vue3 + TypeScript + Tailwind + shadcn-vue + ECharts）。
开发模式：`pnpm dev`（vite dev server 会把 `/api` 代理到 8765 端口的后端）。

未构建时后端仍可用，`/` 返回 503 并提示构建命令，`/api/*` 全部正常。
若前端构建产物不在默认位置，可用环境变量 `PTRADE_SIM_WEB_DIST` 指定其绝对路径。

### 6. 结果

输出到 `backtest_results/{策略名}-{时间戳}/`：

- `summary.json` — 总收益/年化/夏普/最大回撤/胜率/盈亏比/月度收益等
- `daily_stats.csv` — 每日净值、回撤、基准（**回测过程中逐日落盘**，供看板实时读取）
- `trades.csv` — 每笔成交（时间/代码/方向/数量/价格/佣金/盈亏）
- `progress.json` — 实时进度（状态/阶段/已完成天数/耗时）
- `strategy_source.py` — 策略源码副本（策略文件日后被删/改名也能回看）
- `output.log` — 运行日志

## 策略展示名

看板与报告里显示的策略名，**唯一来源是策略目录的 `strategy_config.json`**：

```json
{ "name": "一进二·5x892" }
```

没写 `name` 时退回**策略目录名**（单文件策略则为其文件名）。展示名会随
`strategy_config.json` 一起留档到 run 目录，所以看板读取历史结果时无需任何外部配置。

这个设计是有意的：展示名属于「这个策略是谁」，就该和策略代码放在一起，
而不是维护一份脱离策略的全局文件名映射表（那样策略一改名映射就失效）。

## API 支持清单

### 已实现（40 个）

| 类别 | API |
|---|---|
| 初始化 | `initialize(context)`、`run_daily`、`set_benchmark`、`set_commission`、`set_slippage`、`set_fixed_slippage`、`set_limit_mode`、`set_universe` |
| 行情 | `get_price`、`get_history`、`get_trend_data`、`get_snapshot`、`get_trade_days`、`get_trading_day`、`get_trading_day_by_date`、`get_all_trades_days` |
| 交易 | `order`、`order_value`、`order_target`、`order_target_value`、`cancel_order`、`get_orders`、`get_open_orders`、`get_order`、`get_trades`、`check_limit` |
| 证券 | `get_Ashares`、`get_stock_name`、`get_stock_info`、`get_stock_status`、`get_index_stocks`、`get_fundamentals` |
| 其他 | `get_research_path`、`get_market_list`、`get_market_detail`、`filter_stock_by_status` |

回调：`before_trading_start`、`handle_data`、`after_trading_end`、`on_order_response`、`on_trade_response`。

### 未实现 / 受限

- `get_fundamentals`：仅支持 `valuation`（市值），其余报表返回空 DataFrame
- `get_index_stocks`：需先生成 `ashare_index_weight`（见上），否则返回空列表
- `get_market_detail`：返回空 DataFrame（无盘口明细）
- 以下为**占位接口**：已可调用（返回空值 + 首次调用告警），不抛 `NameError`，
  但完整实现需补数据表 —— `get_stock_exrights`（分红送配）、`get_stock_blocks`（板块码表）、
  `get_industry_stocks`（行业码表）、`get_reits_list`（REITs 清单）
- 实盘相关（`subscribe` / `set_universe` 实盘模式 / 推送类 API）不支持

> 每个 API 的官方签名与语义以 `ptrade-api` skill 为准；本仓库的实现状态由
> `tests/test_api_surface.py`（调用与语义断言）与 `runtime.py::_build_api()`（注册清单）
> 共同定义 —— 两者不符即测试失败，不存在"文档说实现了但代码没有"的中间态。

> 如果你需要某个未实现的 API，欢迎提 [Issue](https://github.com/ceke233/ptrade-sim/issues) 说明用途与调用方式。

## 数据构建与校验（`ptrade-sim db`）

数据灌库与校验已内置为 CLI 子命令（不需要额外脚本目录）：

| 命令 | 用途 |
|---|---|
| `ptrade-sim db build` | 从 hive 分区 parquet 构建 DuckDB 物理库（`--start-year/--end-year/--tables/--overwrite`） |
| `ptrade-sim db verify` | 契约校验（表/列/行数/日期范围）+ 与源 parquet 逐日行数与逐字段对照 |
| `ptrade-sim db verify --list-contract` | 打印表契约（列名/主键/是否分区/派生列） |
| `ptrade-sim db normalize` | 统一既有库的代码尾缀（`.SH`→`.SS`）；`--dry-run` 预演，`--drop` 顺带删表 |

```bash
ptrade-sim db build  --db data/quant.duckdb --data-dir G:/data --start-year 2019 --end-year 2025
ptrade-sim db verify --db data/quant.duckdb --data-dir G:/data
ptrade-sim db build  --db data/quant.duckdb --tables ashare_1m_stock --overwrite   # 只重建单表
ptrade-sim db normalize --db data/quant.duckdb --dry-run                            # 检查尾缀
```

> **指数成分数据现状**：公开源已实测均无「历史调出」能力（akshare `index_stock_hist`
> 已移除、金融界源下线、国证/中证仅单日快照），**只有 baostock 的 3 个指数**
> （沪深300/上证50/中证500）具备时点数据。
> **推荐用 PTrade 导出的 `index_weight_link.parquet`**（权重拉链表，含 `start_date`/`end_date`，
> 是策略实盘口径）。库内 `ashare_index_weight` 表结构可用
> `ptrade-sim db verify --list-contract` 打印，或直接 `INSERT` 导入。

> **L2 集合竞价（可选）**：数据来自 PostgreSQL + pg_duckdb 数据源，提取脚本**不在本仓库内**
> （属外部数据准备环节，故本包不提供相关依赖 extra）。产物 `l2_auction.parquet`
> 放进 `data/`，由 `ptrade-sim db build` 灌入 `ashare_l2_auction` 表。
> 若自己写提取脚本，凭据请通过环境变量传入（如 `PT_L2_DSN`），**不要写入文件**。

## 目录结构

```
ptrade-sim/
├── src/ptrade_sim/
│   ├── cli.py           命令行入口：参数解析与分发
│   ├── pipeline.py      回测流水线编排（配置合并→资源准入→执行→产出→拉看板）
│   ├── runtime.py       引擎核心：调度 + 撮合 + 指标计算
│   ├── api.py           PTrade API 适配层（55 个官方 API，按官方分类 6 组）
│   ├── history.py       历史数据取数与组装（日线/分钟/复权/重采样/数据缺口）
│   ├── conventions.py   市场约定（241 槽位、涨跌停规则）与代码/日期口径归一
│   ├── exceptions.py    异常体系与 CLI 退出码映射（PtradeSimError 及其子类）
│   ├── data_source.py   数据源层（DuckDBSource；ParquetSource 仅供 db build/verify）
│   ├── data_contract.py 表契约（PTrade 字段口径的唯一权威）
│   ├── dbtools.py       DuckDB 构建与校验实现
│   ├── cache.py         统一缓存（容量上限 + 内存预算 + LRU）
│   ├── resources.py     CPU/内存探查 + 回测开销估算 + 准入决策
│   ├── queue.py         资源感知回测队列（跨进程协调）
│   ├── config.py        配置分层（含策略级 strategy_config.json）
│   ├── server.py        看板后端（FastAPI 路由 + 看板进程 + 静态托管 web/dist）
│   ├── runstore.py      看板数据层：读 run 产物（progress/summary/csv/log）
│   └── derived.py       看板派生指标（月度矩阵 / β-α 回归 / 资金曲线）
├── tests/               pytest 套件（合成 DuckDB 夹具，不依赖真实库）
├── data/                本地数据目录（库文件/源行情；**内容不入库**，见 data/README.md）
├── web/                 看板前端（Vite + Vue3 + TS）
├── examples/            示例策略（目录形态：strategy.py + strategy_config.json）
└── docs/                PTrade API 覆盖评估、指数成分抓取、DataFeed 设计
```

## 与真实 PTrade 的差异

本地回测与真实 PTrade 的差异主要来自**输入数据口径**，而非引擎逻辑：

1. **集合竞价**：本地优先用 `l2_auction` 精确 9:25 竞价量价；缺失时用 09:30 分钟 bar 近似
   （Tushare 将竞价与首分钟合成一根，边缘股票判定可能不同）
2. **指数成分**：库内需有 `ashare_index_weight` 表；缺失时 `get_index_stocks` 返回空列表。
   用 akshare 来源的指数只有最新快照（**有幸存者偏差**），历史日期查询会低估成分；
   沪深300/上证50/中证500 来自 baostock，时点精确。
3. **财务数据**：仅 `valuation` 可用
4. **数据覆盖**：库内只含已灌入的年份（默认 2019-2025），区间超出会告警

## 开发

```bash
git clone https://github.com/ceke233/ptrade-sim.git
cd ptrade-sim
uv venv && uv pip install -e ".[dev,duckdb,dashboard]"
pre-commit install        # 安装提交前钩子

pytest                    # 全部测试（约 15s）
pytest -m unit            # 只跑纯逻辑单测
pytest --cov              # 带覆盖率（门槛 65%）
ruff check src tests      # lint
ruff format src tests     # 格式化
mypy                      # 类型检查
uv build                  # 构建 wheel / sdist
```

**测试不依赖真实行情库**：`tests/conftest.py` 用合成微型 DuckDB（3 只股票 × 5 个交易日，
毫秒级建好），表结构直接取自 `data_contract` —— 契约被改坏时夹具会立刻失败。

工程化配置（pytest / ruff / mypy / coverage）集中在 `pyproject.toml`；
CI 见 [.github/workflows/ci.yml](.github/workflows/ci.yml)（lint + typecheck +
Python 3.10–3.13 测试矩阵 + 构建校验）。

新增 API、改表结构、提交规范的约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，
版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## License

MIT
