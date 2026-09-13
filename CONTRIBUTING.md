# 贡献指南

感谢愿意改进 ptrade-sim。这份文档写的是**这个项目特有的约定**，
通用 Python 规范不重复。读完能避开几个已经踩过的坑。

---

## 环境准备

```bash
git clone https://github.com/ceke233/ptrade-sim.git && cd ptrade-sim
uv venv && uv pip install -e ".[dev,duckdb,dashboard]"
pre-commit install          # 安装提交前钩子（ruff + 格式 + 行尾）
```

要求 Python ≥ 3.10。CI 跑 3.10 / 3.11 / 3.12 / 3.13 四个版本。

---

## 测试

```bash
pytest                    # 全部（约 35s，489 项）
pytest -m unit            # 纯逻辑单测（不连库、不起服务）
pytest -m integration     # 需要合成 DuckDB 夹具或引擎的用例
pytest tests/test_engine.py -q              # 单文件
pytest --cov --cov-report=json:coverage.json
python scripts/check_coverage.py            # 每模块覆盖率下限
```

### 覆盖率是 ratchet，只能升不能降

`pyproject.toml` 的 `fail_under = 70` 只是**全局**门槛，它会掩盖局部裸奔 ——
cache 92%、config 98% 的余量足以盖住 dbtools 58%。所以另有
`scripts/check_coverage.py` 按模块设下限，CI 在 pytest 之后跑它。

**某模块覆盖率涨上去后，应把 `FLOORS` 里的数字跟着调高**（取新实测值 −1）。
覆盖率掉了就是 CI 失败，正确做法是**补测试**，不是调低下限 ——
`tests/test_coverage_floors.py::test_ratchet_not_lowered` 会拦住后者。

该脚本对「登记了下限但未出现在 `coverage.json`」的模块**判失败**：
否则往 `[tool.coverage.run] omit` 里加一行就能让任意模块从统计中消失。

### 测试不依赖真实行情库

`tests/conftest.py` 提供合成微型 DuckDB（3 只股票 × 5 个交易日，毫秒级建好），
表结构直接取自 `data_contract` —— **契约被改坏时夹具会立刻失败**。
`engine_factory` 等夹具建在其上。

`tests/test_long_horizon_smoke.py` 另建一个跨 14 个月的合成库（约 5 秒），
专门覆盖「只有区间够长才走到」的代码路径。

### 写测试的约定

- **说清防的是什么问题**，不是「测了什么方法」。注释与断言消息里写清
  失败意味着什么后果。
- **变异检验**：写完关键测试后，把被测行为故意改坏，确认测试真的会失败。
  一个删掉被测代码也照样通过的测试是**负资产** —— 它给人虚假的安全感。
  本仓库多处测试在注释里记录了所通过的变异检验。
- **不要写恒真断言**。`assert x >= 1`（x 由 `or 1` 保证）、
  `assert a in b or isinstance(b, dict)`（后半恒真）这类等于没断言。
  本仓库曾用「把降级分支改成 `max_parallel=-999`」的方式抓出过一批。
- **不要只比较结果相等就宣称缓存命中** —— 缓存全失效时结果同样相等。
  用 monkeypatch 给被调函数装计数器来验证机制。

---

## 静态检查

```bash
ruff check src tests scripts     # lint（E/W/F/I/UP/B/C4/SIM/RET/PTH/RUF）
ruff format src tests scripts    # 格式化
mypy                             # 类型检查
```

`scripts/` 也要一起检查。pre-commit 与 CI 都会跑。

---

## 验证要求：改完要跑长区间

**只跑短区间是不够的。** 项目已经**两次**被同一类缺陷咬到 ——
代码路径只在「区间够长 / 数据量够大」时才执行：

- `compute_metrics` 的月度统计有 `len(mr) > 3` 守卫（>3 个月才执行）；
- `history._price_cache` 有 `codes > 500` 守卫（全市场取数才启用）。

8 天区间的回归**会把这两条路径整个绕过，而且绕过时不报任何错**：

| 缺陷 | 触发条件 | 后果 |
|---|---|---|
| `mr.kurt()` | 区间 > 3 个月 | polars 无此方法 → run 死掉、**不产出 `summary.json`** |
| `_price_cache` 只写不清 | codes > 500 | 每天一条、永不释放 → 内存无界增长 |

所以改动以下任一位置后，除 `pytest` 外还要跑一次长区间：

- 指标计算（`compute_metrics`）
- 任何带 `len(...) > N` / `count > N` 守卫的分支
- 缓存（`cache.py` 与各 `Cache` 的 put/evict）
- 内存预算 / 资源准入（`resources.py`、`cache.duckdb_memory_limit`）

```bash
# 快速（约 5 秒，CI 里也跑）：跨 14 个月的合成库
pytest tests/test_long_horizon_smoke.py -q

# 完整（数分钟，改指标/缓存时跑）：真实库 + 真实策略
ptrade-sim backtest --strategy <策略> --start 2020-01-01 --end 2025-12-31
```

跑长区间时留意 `summary.json` 里有没有 `data_errors` —— 见下节。

---

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

几条约定：

- **新异常必须多重继承旧类型**，如
  `class StrategyPathError(ConfigError, FileNotFoundError)`，
  并同步更新 `tests/test_exceptions.py` 的 `BACKWARD_COMPAT`。
  那是对调用方的兼容承诺（既有的 `except FileNotFoundError` 不能失效），
  删掉父类就会破坏用户代码。这不是取巧 —— `json.JSONDecodeError(ValueError)`、
  `ssl.SSLError(OSError)` 都是标准库的同类做法。
- **`EXIT_CODES` 的顺序有意义**：具体类必须排在父类之前，否则父类抢先命中。
- **编程错误不进体系**。`TypeError` / `AttributeError` 是缺陷，不该被业务逻辑
  捕获；它们落到退出码 1。

### 取数失败必须响亮失败，不能静默降级

`DuckDBSource._q` 为了支持存在性探测，会把查询异常吞成「无数据」（返回 `None`）。
这是**必要的容错**，但必须配合两点：

1. 失败计入 `query_error_count` / `query_errors`（`data_errors()` 给出摘要）；
2. 收尾时由 `pipeline` 写入 `summary.json` 的 `data_errors` 字段，
   **并以退出码 3 结束**。

只留一行 `WARNING` 是不够的。实测一次 6 年分钟回测里 7 次
`Out of Memory Error` 被完全淹没在 28000 行日志中，而它们让同一策略、
同一区间的结果从 12620.69% 变成 5706.29% —— 用户拿到的是一份
**看起来很正常的错误结果**。

---

## 数据与契约

`data_contract.py` 是**表与列口径的唯一权威**。PTrade 的字段命名与常见 A 股
数据源不同，集中定义一处才能避免各取数路径各自翻译（那正是「同一字段两处
口径不一致」的温床）。

- 新增字段：先改契约，再改 `dbtools` 的构建与 `data_source` 的取数；
  夹具会因契约变化立刻失败，这能确保没有遗漏的取数路径。
- 改列名/类型属于**破坏性变更**：既有库需要重建或迁移，请在 PR 里说明。
- 库内表共 10 张，其中 5 张必需（见 README）。
- **不要**在取数侧写 `SELECT a AS b` 之类的别名翻译 —— 映射放契约里。

---

## polars 与 pandas 的边界（重要）

**内部一律用 polars；pandas 只允许出现在 PTrade API 边界。**

原因：官方 API 返回 pandas（`get_history` / `get_price` / `get_fundamentals` /
`get_market_*`），策略代码直接依赖它；而引擎内部用 pandas 会拖慢并在长区间
放大内存。边界之外出现 pandas 属于架构违规，`tests/test_architecture.py`
会失败。

**迁移到 polars 时最容易犯的两个错**（本项目都犯过，且都是静默的）：

1. **方法名不存在**。`pl.Series` 有 `skew()` 但**没有** `kurt()`（pandas 有）。
   漏改会在长区间抛 `AttributeError`，短区间因守卫短路而看不出来。
2. **默认参数不同**。polars 的 `skew()` 默认 `bias=True`（有偏），
   pandas 是无偏 → 数值不一致但**不报错**。

所以迁移任何统计调用时，**必须与 pandas 逐个对照数值**，
而不是「跑通了就算」。`tests/test_multimonth_metrics.py` 是范例。

---

## 策略形态

**推荐一个策略一个目录：**

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

`db_path` / `queue` / `cache` 等**机器级**设置留在 `ptrade_config.json` ——
换台机器不该改策略目录。

结果目录名取**策略目录名**（单文件策略取文件名），**不是**展示名：
展示名可含中文与空格，不能直接做路径。
`StrategyBundle.stem` 是显式字段，不要改回「从 `dir` 推导」——
单文件形态的 `dir` 是父目录，那样会让 `examples/a.py` 的结果目录叫
`examples-<时间戳>`，且同目录多个单文件策略无法区分
（`tests/test_bundle_stem.py` 锁住这一点）。

### 新增 API

1. 官方签名与语义以 PTrade 官方文档为准（本仓库整理版见 `docs/ptrade_api.md`）；
2. 在 `api.py` 里对应分组（环境/设置/行情/信息/交易/持仓）的工厂内实现，
   并在该工厂的返回字典里注册 —— `build_api()` 会自动合并；
3. 补 `tests/test_api_surface.py` 的调用与语义断言；
4. **若只是占位**（暂时无数据表），返回官方「无数据」值并用 `_stub` 告警，
   不要让它 `NameError` —— 策略会因此静默停摆；
5. 更新 README 的 API 清单（该表与 `build_api()` 的返回值必须一致）。

---

## 提交规范

- 提交信息用**祈使句**，说明**为什么**改，而不只是改了什么。
- 破坏性变更在提交体里用 `⚠️ BREAKING CHANGE:` 明确点出
  （例：CLI 退出码从统一 1 变为分类码）。
- 修 bug 时把**触发条件**写进提交体（如「>3 个月才触发」「>500 只才启用」），
  这样以后 `git log` 能查到为什么有这些守卫和测试。
- `CHANGELOG.md` 同步更新。
- 提交前跑一遍：
  `ruff check src tests scripts && ruff format --check src tests scripts && mypy && pytest`。

---

## 提 Issue

- **Bug**：给复现命令、期望与实际、以及 `summary.json` / `output.log` 的相关片段。
  若 `summary.json` 里有 `data_errors`，请一并贴上 —— 那说明结果不可信。
- **新功能**：说明使用场景；若是回测可用的 API，请给出官方签名。
- **性能**：给区间长度、股票池规模、机器内存，以及 `ptrade-sim env` 的资源快照。

---

## 目录速览

```
src/ptrade_sim/
├── cli.py           命令行入口（backtest/dashboard/queue/db/env）
├── pipeline.py      回测流水线编排（用例层）
├── runtime.py       引擎核心：调度 + 撮合 + 指标
├── api.py           PTrade API 适配层（55 个 API，官方分类 6 组）
├── history.py       历史数据取数与组装（pandas 边界之一）
├── conventions.py   市场约定与口径归一（纯函数，无状态）
├── exceptions.py    异常体系与 CLI 退出码映射
├── data_source.py   数据源层（DuckDBSource；ParquetSource 仅供 build/verify）
├── data_contract.py 表契约（唯一权威）
├── dbtools.py       DuckDB 构建与校验
├── cache.py         统一缓存（容量 + 内存预算 + LRU）
├── resources.py     资源探查与准入决策
├── queue.py         资源感知回测队列
├── config.py        配置三级分层
├── server.py        看板后端（FastAPI 路由 + 静态托管）
├── runstore.py      看板数据层（读 run 产物）
└── derived.py       看板派生指标（月度矩阵 / β-α 回归）
tests/               pytest 套件（合成 DuckDB 夹具）
scripts/             dev 工具（每模块覆盖率下限）
docs/                PTrade API 参考
```

依赖方向（`tests/test_architecture.py` 强制）：

```
cli → pipeline → runtime → {api, cache, conventions, data_source, history}
server → {runstore, derived}        derived → runstore
```

底层模块（`api` / `conventions` / `cache` / `data_contract` / `exceptions`）
**不得**反向依赖 `runtime` 或 `cli`。
