"""PTrade API 适配层：把 55 个官方 API 装成策略可见的命名空间。

**为什么单独成模块**：此前这 55 个 API 全部挤在 ``runtime.BacktestEngine._build_api``
一个 **501 行的函数**里（含 52 个嵌套闭包），占 ``runtime.py`` 约五分之一。
它本身没有复杂逻辑，但把引擎的骨架淹没了 —— 找 `_order` 或 `run()` 得先翻过 500 行接线代码。

**分组依据是官方分类**，不是按规模硬切：环境与调度 / 设置类 / 行情 / 证券信息 /
交易 / 持仓查询。实测闭包之间几乎没有互相调用（只有 4 处用共享的 ``_stub``、
1 处 ``_code_aliases``、1 处 ``filter_stock_by_status`` 调 ``get_stock_status``
—— 后两者本就同属「证券状态」，故分组没有割裂依赖）。

**依赖方向**：``runtime → api``，单向。本模块**不 import runtime**。
唯一需要引擎对象的地方是「无持仓时返回空 ``Position``」，
改由引擎提供的 ``e._empty_position(code)`` 承担 —— 对象在哪定义就在哪构造。
"""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from loguru import logger

from ptrade_sim.conventions import (
    as_codes,
    day_dt_date,
    day_iso,
    norm_day,
    norm_index_code,
    to_ptrade_code,
)


def build_api(e) -> dict:
    """把全部 PTrade API 装成字典（``e`` 是 :class:`~ptrade_sim.runtime.BacktestEngine`）。

    按组构建再合并 —— 分组只为可读性，最终仍是一个扁平命名空间
    （策略里直接写 ``get_history(...)``，不带任何前缀）。
    """
    api: dict = {}
    for part in (
        _api_env(e),
        _api_settings(e),
        _api_market(e),
        _api_info(e),
        _api_trading(e),
        _api_position(e),
    ):
        api.update(part)
    return api


# ============================================================
# 共享辅助（各组都用得到）
# ============================================================


def _noop(*args, **kwargs):
    return None


def _stub(e, name: str, ret=None):
    """未实现 API 的占位：首次调用告警（不静默），返回官方「无数据」值。

    需要 ``e`` 只为一件事：``_stub_warned`` 去重集合记在引擎上（每个 run 独立），
    否则同一占位接口会在长回测里刷屏。
    """
    if name not in e._stub_warned:
        logger.warning(f"{name}：本地回测暂未实现，返回空值（占位接口）")
        e._stub_warned.add(name)
    return ret


def _code_aliases(code: str) -> list[str]:
    """同一标的的多种尾缀写法（官方：两位/四位尾缀皆可作字典键）。"""
    out = [code]
    if code.endswith(".SS"):
        out += [code[:-3] + ".SH", code[:-3] + ".XSHG"]
    elif code.endswith(".SZ"):
        out += [code[:-3] + ".XSHE"]
    elif code.endswith(".SH"):
        out += [code[:-3] + ".SS", code[:-3] + ".XSHG"]
    return out


def _api_env(e) -> dict:
    """环境、日志与调度（``log`` / ``g`` / ``context`` / ``run_daily`` / 周期与杂项）"""
    # 日志对象：把 loguru 适配成官方的 log.info/warn/error/debug
    log = SimpleNamespace(
        info=lambda msg, *a: logger.info(e._fmt_log(msg, a)),
        warn=lambda msg, *a: logger.warning(e._fmt_log(msg, a)),
        warning=lambda msg, *a: logger.warning(e._fmt_log(msg, a)),
        error=lambda msg, *a: logger.error(e._fmt_log(msg, a)),
        debug=lambda msg, *a: logger.debug(e._fmt_log(msg, a)),
    )

    def run_daily(context, func, time="9:31"):
        t = str(time).strip()
        hh, mm = t.split(":")[:2]
        key = f"{int(hh):02d}:{int(mm):02d}"
        if key == "13:00":  # 官方：13:00 触发对应下午开盘
            key = "13:01"
        e.schedule.setdefault(key, []).append(func)

    def get_frequency():
        """当前业务代码的周期（官方：分钟返回 minute，每日返回 daily）。"""
        return e.frequency

    def get_business_type():
        """当前策略的业务类型（官方：股票返回 'stock'）。"""
        return "stock"

    def is_trade():
        """是否交易（实盘）场景；回测固定返回 False。"""
        return False

    def create_dir(user_path):
        """创建文件路径（官方支持研究/回测/交易）。"""
        try:
            Path(str(user_path)).mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            logger.warning(f"create_dir({user_path}) 失败：{exc}")
            return False

    def get_research_path():
        return str(e.output_dir) + "/"

    def get_user_name(login_account=True):
        """登录终端的资金账号；回测场景返回固定占位值。"""
        return "ptrade-sim-backtest"

    def get_strategy_params(key=None, default=None):
        """读取策略入参（``strategy_config.json`` 的 ``params`` 段）。

        **本平台扩展**，非 PTrade 官方 API —— 官方的 ``set_parameters``
        仅交易模块可用，回测里没有等价的入参机制。

        用法::

            # strategy_config.json
            { "params": { "max_hold": 3, "threshold": 0.05 } }

            # strategy.py
            def initialize(context):
                p = get_strategy_params()          # 全部 -> dict（只读副本）
                n = get_strategy_params("max_hold", 5)   # 单个 -> 值 / 默认值
        """
        params = e.config.get("params") or {}
        if not isinstance(params, dict):
            return {} if key is None else default
        if key is None:
            return dict(params)  # 返回副本，避免策略改到配置
        return params.get(key, default)

    return {
        "log": log,
        "g": e.g,
        "context": e.context,
        "run_daily": run_daily,
        "get_frequency": get_frequency,
        "get_business_type": get_business_type,
        "is_trade": is_trade,
        "create_dir": create_dir,
        "get_research_path": get_research_path,
        "get_user_name": get_user_name,
        "get_strategy_params": get_strategy_params,
    }


def _api_settings(e) -> dict:
    """设置类 API（官方「设置函数」分类，11 个里的 9 个；另 2 个是 no-op）。"""

    def set_universe(universe):
        e.universe = as_codes(universe, (list, tuple, set))

    def set_benchmark(security):
        e.benchmark = to_ptrade_code(security)

    def set_commission(commission_ratio=0.0003, min_commission=5.0, type="STOCK"):
        e.commission_ratio = float(commission_ratio)
        e.min_commission = float(min_commission)

    def set_slippage(slippage=0.001):
        e.slippage_ratio = float(slippage)
        e.fixed_slippage = None

    def set_fixed_slippage(fixed_slippage=0.0):
        e.fixed_slippage = float(fixed_slippage)

    def set_limit_mode(mode="LIMITED"):
        # LIMITED：限制涨跌停成交（拒一字板）；UNLIMITED：不限制
        e._limit_mode = str(mode).upper()

    return {
        "set_universe": set_universe,
        "set_benchmark": set_benchmark,
        "set_commission": set_commission,
        "set_slippage": set_slippage,
        "set_fixed_slippage": set_fixed_slippage,
        "set_volume_ratio": _noop,
        "set_limit_mode": set_limit_mode,
        "set_yesterday_position": _noop,
        "set_parameters": _noop,
    }


def _api_market(e) -> dict:
    """行情取数（``get_history`` / ``get_price`` / 竞价 / 快照 / 当前 K 线计数）。"""

    def get_history(
        count,
        frequency="1d",
        field="close",
        security_list=None,
        fq=None,
        include=False,
        fill="nan",
        is_dict=False,
    ):
        return e._get_history(int(count), frequency, field, security_list, fq, include, is_dict)

    def get_price(
        security,
        start_date=None,
        end_date=None,
        frequency="1d",
        fields=None,
        fq=None,
        count=None,
        is_dict=False,
    ):
        return e.history.price(
            security, start_date, end_date, frequency, fields, fq, count, is_dict
        )

    def get_trend_data(date=None, stocks=None):
        return e.history.trend_data(stocks)

    def get_snapshot(security=None):
        return e._get_snapshot(to_ptrade_code(security) if security else None)

    def get_current_kline_count():
        """当前时间的分钟 bar 数量（官方：回测中返回回测日当前时间的分钟 bar 数）。

        日线模式无盘中分钟 bar → 0；分钟模式返回已过的槽位数。
        """
        if e._daily_mode:
            return 0
        return max(0, e._slot_pos + 1)

    return {
        "get_history": get_history,
        "get_price": get_price,
        "get_trend_data": get_trend_data,
        "get_snapshot": get_snapshot,
        "get_current_kline_count": get_current_kline_count,
    }


def _api_info(e) -> dict:
    """证券信息与状态（名称/信息/涨跌停/交易日/估值/指数成分，含占位接口）。"""

    def get_stock_name(stocks):
        codes = as_codes(stocks)
        # 官方：始终返回 dict（str 入参也返回 {code: name}）
        out = {}
        for c in codes:
            key = (e._day_str, c)
            nm = e._name_cache.get(key)
            if nm is None and key not in e._name_cache:
                nm = e.feed.stock_name(c, e._day_str)
                e._name_cache[key] = nm
            out[c] = nm
        return out

    def get_stock_info(stocks, field=None):
        """证券基础信息（**按回测日**取时点值）。

        官方签名没有日期参数，但该接口在**回测模块也可用** ——
        所以三个字段都必须反映「回测当日」而非「今天」，否则等于把未来
        信息泄漏给策略：

        - ``stock_name``：走 ``feed.stock_name(code, 回测日)``（当日日线
          ``name`` 优先，与 ``get_stock_name`` 完全一致）。**曾误用
          ``stock_basic.name``** —— 那是「末尾快照」，例如某只在 2021 年
          叫「鸿达兴业」的股票，2024 年退市后基础表里是「ST鸿达(退)」，
          回测到 2021 年就会看到这个名字，同时泄漏「后来戴帽」与「后来退市」。
        - ``de_listed_date``：回测日**尚未退市**时返回官方的「未退市」约定值
          ``2900-01-01``，只有已退市才给真实日期。**曾直接回表里的退市日** ——
          策略在 2021 年就能读到「2024-03-18 退市」，等于知道结局。
        - ``listed_date``：上市日是静态事实，不变（回测日必然已在市，
          否则它不会出现在股票池里）。
        """
        codes = as_codes(stocks)
        fields = field if (field is None or isinstance(field, list)) else [field]
        out = {}
        for c in codes:
            ck = (e._day_str, c, tuple(fields) if fields is not None else None)
            if ck in e._info_cache:
                out[c] = e._info_cache[ck]
                continue
            item = {}
            b = e.feed.basic_dict()
            row = b.get(c)
            if row:
                ld = row.get("list_date")
                dd = row.get("delist_date")
                # ① 简称取**当日**生效值（与 get_stock_name 同源，避免两者不一致）
                item["stock_name"] = e.feed.stock_name(c, e._day_str)
                # ② 上市日：静态事实
                item["listed_date"] = None if ld is None else day_iso(ld)
                # ③ 退市日：只在**回测日已退市**时才给真实日期，否则按官方约定
                #    返回「未退市」哨兵值 —— 否则就是前视泄漏
                if dd is None or str(dd) == "" or norm_day(dd) > norm_day(e._day_str):
                    item["de_listed_date"] = "2900-01-01"
                else:
                    item["de_listed_date"] = day_iso(dd)
            else:
                item = {
                    "stock_name": None,
                    "listed_date": None,
                    "de_listed_date": None,
                }
            if fields is None:
                # 官方：field 不入参时默认只返回 stock_name
                res = {"stock_name": item["stock_name"]}
            else:
                res = {k: item.get(k) for k in fields}
            e._info_cache[ck] = res
            out[c] = res
        # 官方：始终返回嵌套 dict（str 入参也返回 {code: {...}}）
        return out

    def get_stock_status(stocks, query_type="ST", query_date=None):
        codes = as_codes(stocks)
        if query_type == "DELISTING_SORTING":
            return {}  # 官方：仅交易场景支持当日查询
        ds = norm_day(query_date) if query_date else e._day_str
        out = {}
        for c in codes:
            out[c] = e._stock_status_one(c, query_type, ds)
        return out

    def filter_stock_by_status(stock_list, filter_types=("ST", "HALT", "DELISTING")):
        if isinstance(filter_types, str):
            filter_types = [filter_types]
        st = get_stock_status(stock_list, "ST") if "ST" in filter_types else {}
        halt = get_stock_status(stock_list, "HALT") if "HALT" in filter_types else {}
        de = get_stock_status(stock_list, "DELISTING") if "DELISTING" in filter_types else {}
        return [c for c in stock_list if not (st.get(c) or halt.get(c) or de.get(c))]

    def get_Ashares(date=None):
        ds = norm_day(date) if date else e._day_str
        return list(e.feed.get_Ashares(ds))

    def get_trade_days(start_date=None, end_date=None, count=None):
        return e.history.trade_days(start_date, end_date, count)

    def get_all_trades_days(date=None):
        ds = norm_day(date) if date else e._day_str
        days = e.feed.trade_days[: e.feed.day_index(ds) + 1]
        return np.array([day_iso(d) for d in days])

    def get_trading_day(day=0):
        days = e.feed.trade_days
        i = min(max(e.feed.day_index(e._day_str) + int(day), 0), len(days) - 1)
        return day_dt_date(days[i])

    def get_trading_day_by_date(query_date, day=0):
        q = norm_day(query_date)
        days = e.feed.trade_days
        i = bisect_left(days, q)  # 非交易日 -> 下一交易日
        i = min(max(i + int(day), 0), len(days) - 1)
        return day_iso(days[i])

    def check_limit(security, query_date=None):
        """涨跌停状态（官方）。

        ``security`` 为 str 或 list[str]；返回 ``dict[str:int]``：
        1 涨停 / 0 既不涨停也不跌停 / -1 跌停。
        个别代码查询异常时该代码返回 0（官方规定），不影响其余代码。
        """
        codes = [security] if isinstance(security, str) else list(security)
        out: dict[str, int] = {}
        for c in codes:
            try:
                code = to_ptrade_code(c)
                out[code] = e._check_limit(code, query_date)
            except Exception:
                out[str(c)] = 0
        return out

    def get_fundamentals(stocks, statement="valuation", date=None, **kwargs):
        """财务/估值数据。'valuation' 支持市值（ashare_1d_feature），其余报表本地无数据返回空。"""
        ds = norm_day(date) if date else (e.feed.prev_day(e._day_str) or e._day_str)
        codes = as_codes(stocks)
        if statement == "valuation":
            # 内部用 polars，**在 API 边界转 pandas** —— 官方返回 DataFrame，
            # 且以证券代码为索引（index=secu_code），策略普遍按 pandas 用法编写。
            df = e.feed.valuation_frame(codes, ds)
            pdf = df.drop("code").to_pandas() if df.height else df.to_pandas()
            pdf.index = pd.Index(df["code"].to_list(), name="secu_code")
            return pdf
        logger.warning(f"get_fundamentals：本地无财务表（{statement}），返回空 DataFrame")
        return pd.DataFrame()

    def get_index_stocks(index_code, date=None):
        """获取指数成分股（官方签名：index_code, date）。

        date 缺省取当前回测日（官方：回测中默认取当前回测周期所属历史日期）。
        数据来自库内表 ``ashare_index_weight``（成分+权重拉链表，区间左闭右开）。

        **能力差异**（已按源区分，不会静默给错）：
          - baostock 源（沪深300/上证50/中证500）含调出日期 → 时点精确，无未来函数；
          - akshare 源（创业板指/科创50/上证指数/深证成指）仅当前快照 →
            **不做区间过滤**，返回完整当前成分；查询历史日期时按 ``snapshot_bias`` 告警。
        """
        ic = norm_index_code(index_code)
        ds = norm_day(date) if date else e._day_str
        q = e.feed.index_query(ic, ds)
        codes, reason = q.codes, q.reason
        # 每 (指数, 原因) 只告警一次，避免逐 bar 刷屏
        if reason != "ok" and (ic, reason) not in e._warned_index_stocks:
            e._warned_index_stocks.add((ic, reason))
            if reason == "no_table":
                logger.warning(
                    "get_index_stocks：缺少指数成分权重表 ashare_index_weight，"
                    "返回空列表；请向 DuckDB 写入该表（结构见 data_contract.INDEX_WEIGHT）"
                )
            elif reason == "unknown_index":
                have = sorted(e.feed.index_members())
                logger.warning(
                    f"get_index_stocks({index_code})：成分表中无此指数，返回空列表。已收录：{have}"
                )
            elif reason == "before_coverage":
                info = e.feed.index_member_info(ic) or {}
                logger.warning(
                    f"get_index_stocks({index_code}, {ds})：该指数成分数据自 "
                    f"{info.get('min_in', '?')} 起，查询日早于覆盖起点，返回空列表"
                    f"（源无更早数据，非「当日无成分」）"
                )
            elif reason == "snapshot_bias":
                info = e.feed.index_member_info(ic) or {}
                logger.warning(
                    f"get_index_stocks({index_code}, {ds})：该指数为**快照源**"
                    f"（{info.get('source')}，抓取日 {info.get('snapshot_date')}），"
                    f"不具备历史时点能力 → 已返回**当前** {len(codes)} 只成分，"
                    f"含幸存者偏差（历史调出的股票缺失、当时未纳入的股票混入）；"
                    f"精确回测请改用 baostock 覆盖的沪深300/上证50/中证500"
                )
        return codes

    def get_stock_exrights(stock_code, date=None):
        """[占位] 证券除权除息明细（官方返回 DataFrame / None）。需补分红送配数据表。"""
        return _stub(e, "get_stock_exrights")

    def get_stock_blocks(stock_code):
        """[占位] 证券所属板块（官方返回 dict[str: list[list[str,str]]] / None）。需补板块码表。"""
        return _stub(e, "get_stock_blocks")

    def get_industry_stocks(industry_code):
        """[占位] 行业成分股（官方返回 list[str]）。需补行业码表。"""
        return _stub(e, "get_industry_stocks", [])

    def get_reits_list(date=None):
        """[占位] 基础设施公募 REITs 代码列表（官方返回 list[str]）。"""
        return _stub(e, "get_reits_list", [])

    def get_market_list():
        return pd.DataFrame(
            {
                "finance_mic": ["SS", "SZ"],
                "finance_name": ["上海证券交易所", "深圳证券交易所"],
            }
        )

    def get_market_detail(finance_mic):
        return pd.DataFrame(columns=["hq_type_code", "prod_code", "prod_name", "trade_time_rule"])

    return {
        "get_stock_name": get_stock_name,
        "get_stock_info": get_stock_info,
        "get_stock_status": get_stock_status,
        "filter_stock_by_status": filter_stock_by_status,
        "get_Ashares": get_Ashares,
        "get_trade_days": get_trade_days,
        "get_all_trades_days": get_all_trades_days,
        "get_trading_day": get_trading_day,
        "get_trading_day_by_date": get_trading_day_by_date,
        "check_limit": check_limit,
        "get_fundamentals": get_fundamentals,
        "get_index_stocks": get_index_stocks,
        "get_stock_exrights": get_stock_exrights,
        "get_stock_blocks": get_stock_blocks,
        "get_industry_stocks": get_industry_stocks,
        "get_reits_list": get_reits_list,
        "get_market_list": get_market_list,
        "get_market_detail": get_market_detail,
    }


def _api_trading(e) -> dict:
    """交易与委托查询（下单四件套 + 撤单 + 委托/成交查询）。"""

    def order(security, amount, limit_price=None):
        return e._order(to_ptrade_code(security), int(amount))

    def order_value(security, value):
        return e._order_by_value(to_ptrade_code(security), float(value))

    def order_target(security, amount):
        return e._order_target(to_ptrade_code(security), int(amount))

    def order_target_value(security, value):
        return e._order_by_target_value(to_ptrade_code(security), float(value))

    def cancel_order(order_or_id):
        oid = order_or_id if isinstance(order_or_id, str) else getattr(order_or_id, "id", None)
        od = e.orders.get(oid)
        if od and od.status in ("filled",):
            return False
        if od:
            od.status = "canceled"
        return True

    def get_open_orders():
        return {
            oid: od
            for oid, od in e.orders.items()
            if od.status not in ("filled", "canceled", "rejected")
        }

    def get_order(order_id):
        return e.orders.get(order_id)

    def get_orders():
        return dict(e.orders)

    def get_trades():
        return dict(enumerate(e.trades))

    return {
        "order": order,
        "order_value": order_value,
        "order_target": order_target,
        "order_target_value": order_target_value,
        "cancel_order": cancel_order,
        "get_open_orders": get_open_orders,
        "get_order": get_order,
        "get_orders": get_orders,
        "get_trades": get_trades,
    }


def _api_position(e) -> dict:
    """持仓查询三件套（官方字段口径，两种尾缀皆可作键）。"""

    def get_position(security):
        """获取单只标的持仓信息（官方）。

        无持仓时返回**空 Position**（``amount == 0``），而非 None ——
        官方语义如此，策略里可直接 ``get_position(c).amount``。
        """
        code = to_ptrade_code(security)
        # 无持仓时返回**空 Position**（官方语义），由引擎构造 ——
        # 这样 api.py 无需 import runtime，避免循环依赖。
        return e.portfolio.positions.get(code) or e._empty_position(code)

    def get_positions(security=None):
        """获取多只标的持仓信息（官方）。

        ``security`` 可为 str / list[str]，不传则取全部持仓。
        返回 ``{代码: Position}``；官方允许两位与四位尾缀皆可作键，
        故同一持仓会以多种写法同时挂载。
        """
        pf = e.portfolio.positions
        if security is None:
            codes = list(pf)
        elif isinstance(security, str):
            codes = [to_ptrade_code(security)]
        else:
            codes = [to_ptrade_code(c) for c in security]
        out: dict = {}
        for c in codes:
            pos = pf.get(c)
            if pos is None:
                continue
            for alias in _code_aliases(c):
                out[alias] = pos
        return out

    def get_all_positions():
        """获取账户全部持仓（官方为柜台原始字段列表）。

        ⚠️ 回测无柜台，故按其字段语义给出**等价结构**（非柜台原文），
        字段名沿用官方：``stock_code`` / ``current_amount`` / ``enable_amount``
        / ``last_price`` / ``cost_price`` / ``market_value``。
        """
        rows = []
        for code, pos in e.portfolio.positions.items():
            rows.append(
                {
                    "stock_code": code,
                    "stock_name": e.feed.stock_name(code, e._day_str) or "",
                    "current_amount": float(pos.total_amount),
                    "enable_amount": float(pos.closeable_amount),
                    "last_price": float(pos.price),
                    "cost_price": float(pos.avg_cost),
                    "market_value": float(pos.value),
                    "income_balance": float((pos.price - pos.avg_cost) * pos.total_amount),
                }
            )
        return rows

    return {
        "get_position": get_position,
        "get_positions": get_positions,
        "get_all_positions": get_all_positions,
    }
