# ptrade-sim

PTrade 量化策略的本地模拟回测平台（分钟级撮合），支持 `uv tool install` 一键安装。

在本地复刻 [恒生 PTrade](https://www.ptrade.com) 的核心策略 API 与撮合行为，让 PTrade 策略无需修改即可在本地全市场数据上回测，用于策略开发、调参与验证。

> ⚠️ **免责声明**：本项目仅供学习与研究，不构成任何投资建议。回测结果不代表未来收益。

## 安装

```bash
uv tool install git+https://github.com/ceke233/ptrade-sim.git
# 或本地开发
uv tool install --editable /path/to/ptrade-sim
```

安装后获得全局命令 `ptrade-sim`。

## 快速开始

### 1. 准备行情数据

引擎需要按 **hive 分区** 组织的 parquet 行情目录，结构如下：

```
data_dir/
├── ashare_calendar/data.parquet            # 交易日历: date(YYYYMMDD)
├── ashare_stock_basic/data.parquet         # 股票基础: code,name,market,list_status,list_date,delist_date
├── ashare_1d_stock/year=YYYY/month=MM/day=DD/data.parquet   # 日线（每天一个全市场文件）
├── ashare_1d_index/…                       # 指数日线（基准）
├── ashare_1d_feature/year=…                # 估值/股本: total_mv,circ_mv,float_share,total_share
└── ashare_1m_stock/year=…                  # 分钟线（每天一个全市场文件）
```

各表列格式见 [sample_data/](sample_data/)：

| 文件 | 说明 |
|---|---|
| `daily_stock_sample.csv` | 日线（code,date,open,high,low,close,pre_close,vol,amount,adj_factor,name,is_st,…）|
| `daily_feature_sample.csv` | 估值/股本（code,date,total_mv,circ_mv,total_share,float_share,…）|
| `minute_stock_sample.csv` | 分钟线（code,trade_time,open,high,low,close,vol,amount,…）|
| `calendar_sample.csv` | 交易日历 |
| `stock_basic_sample.csv` | 股票基础信息 |
| `auction_sample.csv` | （可选）L2 集合竞价增强：date,code,hq_px,business_amount |
| `trades_output_sample.csv` | 回测输出：成交明细 |
| `daily_stats_output_sample.csv` | 回测输出：每日净值/回撤 |

### 2. 编写策略

PTrade 风格策略，保存为任意 `.py`（如 `my_strategy.py`）：

```python
def initialize(context):
    set_benchmark("000300.XSHG")
    set_commission(commission_ratio=0.0002, min_commission=5.0, type="STOCK")
    run_daily(context, my_buy, time="09:30")

def my_buy(context):
    code = get_Ashares()[0]
    order_value(code, 10000)   # 引擎注入的 API 可直接调用
```

### 3. 配置

复制 `config.example.json` 为 `ptrade_config.json`（放当前工作目录），修改 `data_dir`：

```json
{
  "data_dir": "G:/data",
  "start_date": "2021-01-01",
  "end_date": "2025-12-31",
  "capital_base": 100000,
  "benchmark": "000300.SS",
  "strategy": "my_strategy.py",
  "preload": { "mode": "rolling", "rolling_window_days": 2, "threads": 16 },
  "cost": {
    "commission_ratio": 0.0003, "min_commission": 5.0,
    "handling_fee_ratio": 4.87e-05, "stamp_tax": 0.001, "slippage_ratio": 0.0
  }
}
```

### 4. 运行回测

```bash
cd /path/to/workspace    # 配置、策略相对当前目录
ptrade-sim backtest
ptrade-sim backtest --strategy my_strategy.py --start 2024-01-01 --end 2024-12-31 --data-dir /mnt/data
```

参数：`--config`（默认 `./ptrade_config.json`）、`--strategy`、`--start`、`--end`、`--capital`、`--data-dir`、`--output-dir`（默认 `./backtest_results`）。

### 5. 结果

输出到 `backtest_results/{策略名}-{时间戳}/`：

- `summary.json` — 总收益/年化/夏普/最大回撤/胜率/盈亏比/月度收益等
- `daily_stats.csv` — 每日净值、回撤、基准
- `trades.csv` — 每笔成交（时间/代码/方向/数量/价格/佣金/盈亏）
- `report.html` — 可视化报告（净值曲线、回撤、月度热力图）

## 特性

- **PTrade API 兼容层**：35 个常用 API（见下）
- **分钟级撮合**：241 根/交易日，含 09:30 集合竞价 bar；T+1、除权除息、佣金/印花税/过户费、涨跌停限制
- **涨跌停板块规则**：主板 ±10%（ST ±5%）、创业板 2020-08-24 起 ±20%、科创板 ±20%、北交所 ±30%
- **L2 集合竞价增强（可选）**：工作目录 `data/l2_auction.parquet`（9:25 竞价量价）存在时自动优先使用
- **可选数据**：工作目录 `data/name_change_df.csv`（更名历史）自动加载

## API 支持清单

### 已实现（35 个）

| 类别 | API |
|---|---|
| 初始化 | `initialize(context)`、`run_daily`、`set_benchmark`、`set_commission`、`set_slippage`、`set_fixed_slippage`、`set_limit_mode`、`set_universe` |
| 行情 | `get_price`、`get_history`、`get_trend_data`、`get_snapshot`、`get_trade_days`、`get_trading_day`、`get_trading_day_by_date`、`get_all_trades_days` |
| 交易 | `order`、`order_value`、`order_target`、`order_target_value`、`cancel_order`、`get_orders`、`get_open_orders`、`get_order`、`get_trades`、`check_limit` |
| 证券 | `get_Ashares`、`get_stock_name`、`get_stock_info`、`get_stock_status`、`get_index_stocks`、`get_fundamentals` |
| 其他 | `get_research_path`、`get_market_list`、`get_market_detail`、`filter_stock_by_status` |

### 未实现 / 受限

- `get_fundamentals`：仅支持 `valuation`（市值），其余报表返回空 DataFrame
- `get_index_stocks`：无指数成分表时返回空列表
- `get_market_detail`：返回空 DataFrame（无盘口明细）
- 实盘相关（`subscribe` / `set_universe` 实盘模式 / 推送类 API）不支持

> 如果你需要某个未实现的 API，欢迎提 [Issue](https://github.com/ceke233/ptrade-sim/issues) 说明用途与调用方式。

## 与真实 PTrade 的差异

本地回测与真实 PTrade 的差异主要来自**输入数据口径**，而非引擎逻辑：

1. **集合竞价**：本地优先用 `l2_auction` 精确 9:25 竞价量价；缺失时用 09:30 分钟 bar 近似（Tushare 将竞价与首分钟合成一根，边缘股票判定可能不同）
2. **指数成分**：`get_index_stocks` 无成分表时返回空
3. **财务数据**：仅 `valuation` 可用

## 开发

```bash
git clone https://github.com/ceke233/ptrade-sim.git
cd ptrade-sim
uv sync          # 或 pip install -e .
ptrade-sim backtest --help
```

## License

MIT
