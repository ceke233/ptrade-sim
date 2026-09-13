# 贡献指南

感谢参与。本文说明本地开发、测试与提交规范。

## 环境准备

运行下限是 **Python 3.10**，但类型检查目标为 3.12（原因见下）。

```bash
git clone https://github.com/ceke233/ptrade-sim.git
cd ptrade-sim

# 方式一：uv（推荐，快）
uv venv && uv pip install -e ".[dev,duckdb,dashboard]"

# 方式二：pip
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,duckdb,dashboard]"

# 安装提交前钩子
pre-commit install
```

> **为什么类型检查目标是 3.12 而运行下限是 3.10**：numpy 2.x 的 `.pyi` 使用
> PEP 695 `type` 语句（需 Python ≥3.12）。若 mypy 按 3.10 解析会直接语法报错并
> 中止整个检查。故 `[tool.mypy] python_version = "3.12"`，
> 而包本身仍可在 3.10 上运行（CI 的测试矩阵覆盖 3.10–3.13）。

## 测试

```bash
pytest                                  # 全量
pytest -m unit                          # 只跑纯逻辑单测（快）
pytest -m integration                   # 只跑引擎/看板集成测试
pytest --cov --cov-report=term-missing  # 带覆盖率
pytest tests/test_cache.py -k lru -q    # 单文件/单用例
```

### 测试不依赖真实行情库

`tests/conftest.py` 用**合成微型 DuckDB 夹具**（`tiny_db`，3 只股票 × 5 个交易日），
毫秒级建好。这样测试在 CI 上也能跑，且断言可确定 —— 而不是 `skip` 掉或依赖
几十 GB 的真实库。

夹具的表结构**直接取自 `data_contract`**，所以契约一旦被改坏，夹具构建就会失败。

### 写测试的约定

- 新增/修复缺陷时**先写一条会失败的测试**，并注明它防的是什么回归。
  例如：

  ```python
  def test_check_limit_accepts_list_and_returns_status_dict(api_results):
      """官方 check_limit：接受 str 或 list[str]，返回 dict[str:int]。

      回归防护：此前只接受单个 str 且返回 bool，传列表直接
      TypeError: unhashable type: 'list'。
      """
  ```

- `pytestmark = pytest.mark.unit` / `mark.integration` 标明类型（`--strict-markers`
  会让拼错的 marker 直接报错）。
- 不要在测试里静默 `skip` 掉核心路径；缺依赖时用
  `pytest.importorskip("duckdb", reason=...)` 并**说明原因**。

## 静态检查

```bash
ruff check src tests          # lint
ruff check src tests --fix    # 自动修
ruff format src tests         # 格式化
mypy                          # 类型检查（只查 src）
```

三者在 CI 中都会跑，必须全绿才能合入。

## 提交规范

提交信息用**祈使句**，首行 ≤ 72 字符，说明「为什么」而不只是「做了什么」：

```
修复 check_limit 返回类型不符官方签名

官方签名为 check_limit(security, query_date=None)，接受 str 或 list[str]
并返回 dict[str:int]；原实现只收 str 且返回 bool，传列表直接
TypeError: unhashable type: 'list'。

同时补上下方 query_date 的历史日期语义（以该日收盘价判断）。
```

## 策略形态

**推荐：一个策略一个目录。**

```
strategies/my_strategy/
├── strategy.py             # 必需：策略代码
├── strategy_config.json    # 可选：该策略的配置
└── helper.py               # 可选：被 import 的模块（有 strategy.py 时不会被误选）
```

`strategy_config.json` 只写「**这个策略怎么跑**」：

```json
{
  "name": "我的动量策略",
  "start_date": "2025-01-01",
  "end_date": "2025-12-31",
  "capital_base": 100000,
  "benchmark": "000300.SS",
  "frequency": "minute",
  "params": { "top_n": 3 }
}
```

而 `db_path` / `queue` / `cache` 这类**机器级**设置留在 `ptrade_config.json`
—— 换台机器不该改策略目录。以 `_` 开头的键视为注释，会被忽略。

策略里通过 `get_strategy_params()` 读入参（**本平台扩展**，官方 `set_parameters`
仅交易模块可用）：

```python
def initialize(context):
    g.top_n = int(get_strategy_params("top_n", 5))   # 默认值写在代码里
```

`get_strategy_params()` 返回**副本**，策略修改不会影响配置。

也兼容旧的单文件写法（`--strategy path/to/x.py`）；此时同目录若有
`strategy_config.json` 同样会被读取，便于渐进迁移。

## 数据与契约

**`src/ptrade_sim/data_contract.py` 是数据层的唯一权威**，同时驱动：

1. 建库（`dbtools` 依据它生成投影 SQL）
2. 引擎取数（`data_source` 依据它确定列名）
3. 测试夹具（`conftest` 依据它建表）
4. 文档（`db verify --list-contract` 输出）

因此**改表结构必须先改契约**，其余三处会自动跟随并在测试中暴露不一致。

几点约定：

- 列名一律用 **PTrade 口径**：`volume`(股) / `money`(元) / `preclose`；
  估值用官方 valuation 字段名（`total_value`/`float_value`/`a_floats`/...）。
- 代码尾缀统一 `.SS`/`.SZ`/`.BJ`（入库时由 `_select_expr` 规范化）。
- 日期统一 8 位 `YYYYMMDD` 字符串。
- `derive` 的 SQL 表达式引用**源列名**（rename 尚未生效）。
- 官方有、本地数据源无的字段，写进 `unsupported` 显式声明，
  由 API 层明确报错而不是静默返回 NaN。

## polars 与 pandas 的边界（重要）

**内部数据通路一律用 polars；pandas 只允许出现在 PTrade API 边界。**

| 位置 | 用什么 | 原因 |
|---|---|---|
| `data_source.py` / `dbtools.py` / `cache.py` | polars | 取数与建库 |
| `DataFeed` 内部（日线/估值/基本表/基准/L2） | polars | 引擎内部通路 |
| `compute_metrics` / `daily_stats_frame` / `trades_frame` | polars | 本项目自有接口 |
| `conventions.py` | 无 | 纯函数，不碰 DataFrame |
| `server.py` / `runstore.py` / `derived.py`（看板三层） | polars | 读 CSV、分组、分页、JSON |
| `cli.py` / `pipeline.py` | 无 | 参数解析与用例编排，不碰 DataFrame |
| **`get_history` / `get_price` / `get_fundamentals` / `get_market_*`**（`runtime.py` 适配 + `history.py` 组装） | **pandas** | **官方规定返回 pandas**，策略按 pandas 用法编写 |

**为什么边界不能改 polars**：PTrade 策略常见写法是

```python
df = get_price(...).reset_index()
df["index"] = df["index"].dt.strftime("%Y-%m-%d")
prices = get_price_df(...).set_index("code")
chhl_df[(chhl_df["index"] == date) & (chhl_df["close"] == chhl_df["high_limit"])]["code"].tolist()
```

`.reset_index()` / `.set_index()` / `.dt.strftime()` / 布尔掩码索引都是 pandas 特有，
换 polars 会让**已有策略全部报错**。所以改为在 `DataFeed` 内部用 polars 计算，
到 API 出口再 `.to_pandas()`；组装逻辑在 `history.py`，`runtime.py` 只做参数归一。

写新代码时若不确定该用哪个：**除了上面表格最后一行，都用 polars**。

新增 PTrade API 的流程

1. **先在 `C:\Users\ceke233\.claude\skills\ptrade-api` 查官方签名**，
   不要凭印象写参数与返回类型（`check_limit` 就是反例）。
2. 在 `api.py` 里对应分组（环境/设置/行情/信息/交易/持仓）的工厂内实现，
   并在该工厂的返回字典里注册 —— `build_api()` 会自动合并。
3. 在 `tests/test_api_surface.py` 的探针策略里调用一次并断言语义。
4. 覆盖状态以代码为准，无需另维护文档：注册清单在 `_build_api()`，
   语义断言在 `tests/test_api_surface.py`，两者不符即 CI 失败。

## 验证要求：改完要跑长区间

**只跑短区间是不够的。** 项目已经两次被同一类缺陷咬到 ——
代码路径只在「区间够长 / 数据量够大」时才执行：

- `compute_metrics` 的月度统计有 `len(mr) > 3` 守卫（>3 个月才执行）；
- `history._price_cache` 有 `codes > 500` 守卫（全市场取数才启用）。

8 天区间的回归**会把这两条路径整个绕过**，而且绕过时不报任何错。

所以改动以下任一位置后，除了 `pytest`，还要跑一次长区间：

- `compute_metrics` / 指标计算
- 任何带 `len(...) > N` / `count > N` 守卫的分支
- 缓存（`cache.py`、各 `Cache` 的 put/evict）

```powershell
# 快速（5 秒，CI 里也跑）：跨 14 个月的合成库
& $v -m pytest tests/test_long_horizon_smoke.py -q

# 完整（数分钟，改指标/缓存时跑）：真实库 + 真实策略
cd $env:TEMP\pt_verify\ws7
python -m ptrade_sim.cli backtest --strategy <策略> --config <长区间配置> --no-dashboard
```

## 错误与退出码

**可预期的错误必须用 `exceptions.py` 里的类型**，不要裸 `raise ValueError`：

| 场景 | 类型 | CLI 退出码 |
|---|---|---|
| 配置缺失/非法、策略路径问题 | `ConfigError` / `ConfigFileNotFoundError` / `StrategyPathError` / `StrategyConfigError` | 2 |
| 库缺失、缺表、区间无交易日 | `DataError` / `DatabaseNotFoundError` | 3 |
| 策略代码不合约定 | `StrategyError` / `StrategyImportError` | 4 |
| 队列锁超时、资源准入被拒 | `QueueTimeoutError` | 5 |
| 缺可选依赖 | `DependencyError` | 6 |
| 其它一切 | — | 1 |

几个约定：

- **新异常必须多重继承旧类型**（如 `class XError(ConfigError, FileNotFoundError)`），
  并同步更新 `tests/test_exceptions.py` 的 `BACKWARD_COMPAT` —— 那是向调用方
  承诺的兼容性，删掉父类就会破坏用户代码。
- **`EXIT_CODES` 的顺序有意义**：具体类必须排在父类之前，否则父类抢先命中。
- **编程错误不进体系**。`TypeError` / `AttributeError` 是缺陷，不该被业务逻辑
  捕获；它们落到退出码 1。
- **覆盖率下限是 ratchet**：加了新代码若让某模块掉到 `scripts/check_coverage.py`
  的下限之下，CI 会失败。正确做法是**补测试**，不是调低下限
  （`tests/test_coverage_floors.py::test_ratchet_not_lowered` 会拦住后者）。

## 提 Issue

- **缺陷**：附最小复现（策略片段 + 配置 + 实际输出 vs 期望输出）。
- **官方差异**：注明 PTrade 文档出处，便于核对。
- **新功能**：说明使用场景；若是回测可用的 API，请给出官方签名。

## 目录速览

```
src/ptrade_sim/
├── cli.py           命令行入口（backtest/dashboard/queue/db/env）
├── runtime.py       引擎核心：调度 + 撮合 + 指标
├── api.py           PTrade API 适配层（55 个 API，官方分类 6 组）
├── pipeline.py      回测流水线编排（用例层）
├── server.py        看板 HTTP 层
├── runstore.py      看板数据层（读 run 产物）
├── derived.py       看板派生指标
├── history.py       历史数据取数与组装（pandas 边界之一）
├── conventions.py   市场约定与口径归一（纯函数，无状态）
├── data_source.py   数据源层（DuckDBSource；ParquetSource 仅供 build/verify）
├── data_contract.py 表契约（唯一权威）
├── dbtools.py       DuckDB 构建与校验
├── cache.py         统一缓存（容量 + 内存预算 + LRU）
├── resources.py     资源探查与准入决策
├── queue.py         资源感知回测队列
├── config.py        配置三级分层
└── server.py        看板后端（FastAPI）
tests/               pytest 套件（合成 DuckDB 夹具）
docs/                API 覆盖评估、设计文档
```
