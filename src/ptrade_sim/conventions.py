"""A 股市场约定与口径归一。

这个模块只放**纯函数**，不依赖任何内部状态，因此可以被 runtime / history /
dbtools 等各层自由引用而不产生循环导入。

内容分三类，都是**无状态的约定**，故可被 runtime / history / dbtools 各层
自由引用而不产生循环导入：

1. **交易时段与槽位**：241 根/日（含 09:30 集合竞价成交时点）
2. **板块判定与涨跌停规则**：科创板/创业板/北交所/主板的限幅与 ST 处理
3. **表示形式转换**：

- **证券代码**：数据源代码（``.SH``）-> PTrade 代码（``.SS``）；指数代码 -> 6 位数字。
  历史上两套尾缀混用过，导致 6 处 join 静默失效，故统一收口在这里。
- **日期**：8 位紧凑串 ``YYYYMMDD`` 是**数据表与引擎内部日键**的口径；
  ISO ``YYYY-MM-DD`` 只用于 ``daily_stats.date`` 与 API 返回值。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal


def to_ptrade_code(code: str) -> str:
    """数据源代码 -> PTrade 规范代码。

    官方同时接受两种尾缀：上海 ``XSHG``/``SS``、深圳 ``XSHE``/``SZ``
    （见 ptrade-api skill「代码尾缀」）。本项目内部一律统一为**两位尾缀**
    ``.SS``/``.SZ``/``.BJ``，所以这里把四位尾缀与历史混用的 ``.SH``
    一并归一 —— 不做这步会让按代码 join 的地方静默取不到数据。
    """
    if isinstance(code, str):
        return code.replace(".XSHG", ".SS").replace(".XSHE", ".SZ").replace(".SH", ".SS")
    return code


def as_codes(stocks, types: tuple = (list, tuple)) -> list[str]:
    """API 入参「单只 str 或一组代码」-> ``list[PTrade 代码]``。

    官方多数行情/信息 API 的 ``stocks``/``security`` 都允许 str 或序列，
    取数前一律先归一成列表；``set_universe`` 的 ``types`` 额外含 ``set``
    （官方允许集合语义）。
    """
    seq = stocks if isinstance(stocks, types) else [stocks]
    return [to_ptrade_code(s) for s in seq]


def norm_index_code(index_code) -> str:
    """指数代码 -> 6 位数字，兼容官方各种写法。

    官方示例中同一指数可写作 ``000300.SS`` 或 ``000300.XBHS``（中证指数尾缀），
    数据表内统一以 6 位数字为键，故此处削去尾缀并左补零。
    """
    s = str(index_code).strip().upper()
    for suf in (".XBHS", ".XSHG", ".XSHE", ".SS", ".SH", ".SZ", ".BJ"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s.zfill(6) if s.isdigit() else s


# ============================================================
# 日期口径：8 位紧凑串 <-> ISO <-> date
# ============================================================


def norm_day(d) -> str:
    """``2025-01-02`` / ``20250102`` / ``date`` / ``datetime`` -> ``YYYYMMDD``。

    日期入参在官方各 API 里写法不一（策略常直接传 ``datetime.date``），
    取数前一律归一到数据表口径的 8 位紧凑串。
    """
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "").replace(" ", "")[:8]


def day_iso(d8) -> str:
    """8 位日期 ``YYYYMMDD`` -> ``YYYY-MM-DD``（内部比较与展示统一用这个格式）。"""
    s = str(d8)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def day_dt_date(ds: str) -> date:
    return date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))


# ============================================================
# 涨跌停规则（交易所口径）
# ============================================================


def limit_pct(is_st: int, code: str = "", ds: str = "") -> float:
    """涨跌停比例（按板块与日期，交易所规则）：
    - 科创板（688/689）：±20%（无 ST 限制）
    - 创业板（300/301）：2020-08-24 注册制改革后 ±20%，此前 ±10%
    - 北交所（8/4 开头）：±30%
    - 主板（其余）：ST ±5%，非 ST ±10%

    ``is_st`` 通常取自 polars 行，而 polars 的 NULL 会变成 Python ``None``；
    契约未把该列标为 NOT NULL，故 **NULL 一律按非 ST 处理**（否则 ``int(None)``
    会抛 ``TypeError``，让缺 ST 标记的数据整段回测失败）。
    """
    if code.startswith(("688", "689")):
        return 0.20
    if code.startswith(("300", "301")):
        return 0.20 if (not ds or ds >= "20200824") else 0.10
    if code.startswith(("8", "4")):
        return 0.30
    try:
        st = int(is_st)
    except (TypeError, ValueError):  # NULL / 非数值标记 -> 非 ST
        st = 0
    return 0.05 if st else 0.10


def limit_price(pre_close: float, pct: float) -> float:
    """交易所涨/跌停价：Decimal 精确四舍五入（ROUND_HALF_UP），先归一化到分再乘比例。
    Python round() 因浮点表示会把 9.185 舍成 9.18，交易所应为 9.19（601022 案例根因）。"""
    pc = Decimal(str(round(float(pre_close), 2)))
    return float((pc * Decimal(str(1 + pct))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ============================================================
# 分钟时间轴：241 槽/天
# ============================================================


def build_day_slots() -> tuple[str, ...]:
    """构建分钟槽位：241 个。

    ``09:30`` 为**集合竞价成交时点**（实盘集合竞价挂单在 9:30 撮合成交，
    开盘价即该次竞价价），必须单独保留并在该槽触发 handle_data，
    否则策略会丢掉开盘这一最重要的决策点。
    """
    slots = ["09:30"]  # 集合竞价成交时点 + 首分钟 bar
    t = 9 * 60 + 31
    while t <= 11 * 60 + 30:
        slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 1
    t = 13 * 60 + 1
    while t <= 15 * 60:
        slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 1
    return tuple(slots)


#: 分钟时间轴：241 槽/天，全部按**结束时间**标注。
#: ``09:30`` 为集合竞价成交时点（实盘集合竞价挂单在 9:30 撮合成交，开盘价即该次竞价价），
#: 必须单独保留并在该槽触发 ``handle_data``，否则策略会丢掉开盘这一最重要的决策点。
DAY_SLOTS: tuple[str, ...] = build_day_slots()
assert len(DAY_SLOTS) == 241, f"分钟槽位数应为 241，实际 {len(DAY_SLOTS)}"
