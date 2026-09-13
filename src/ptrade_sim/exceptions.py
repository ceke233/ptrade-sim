"""本平台的可预期错误体系。

**为什么需要**：此前全仓 **0 个自定义异常**，错误类型是 `ValueError`×8 /
`FileNotFoundError`×4 / `OSError`×3 / `ImportError`×2 / `TimeoutError`×1 混用。
后果是调用方**无法区分失败类别**（"配置写错"与"库不完整"都是 `ValueError`），
CLI 也只能一律 `return 1`，脚本无法按类别处理。

**兼容策略（重要）**：新异常用**多重继承**同时挂到旧类型上，例如
``StrategyPathError(ConfigError, FileNotFoundError)`` —— 于是

- ``except PtradeSimError`` 能统一捕获本平台的所有可预期错误
- 而既有的 ``except FileNotFoundError`` / ``pytest.raises(ValueError)`` **继续有效**

这不是取巧：``json.JSONDecodeError(ValueError)``、``ssl.SSLError(OSError)``
等标准库类型都用了同一手法做迁移。``tests/test_exceptions.py`` 把这份兼容性
**写成了断言**，避免日后有人"清理"掉父类而悄悄破坏调用方。

**边界**：本模块只描述**可预期**的错误（配置/数据/策略/队列/依赖）。
**编程错误**（`TypeError`、`AttributeError`、断言失败）不进这个体系 ——
那些是缺陷，不该被业务逻辑捕获。
"""

from __future__ import annotations

# ============================================================
# 基类
# ============================================================


class PtradeSimError(Exception):
    """本平台所有**可预期**错误的基类。

    CLI 捕获它并按 :func:`exit_code_for` 返回分类退出码；
    库使用者可用它一次捕获全部业务错误，同时仍能按具体类型细分处理。
    """


# ============================================================
# 配置与策略定位
# ============================================================


class ConfigError(PtradeSimError, ValueError):
    """配置缺失或取值非法（如缺 ``db_path``、日期区间为空）。"""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """配置文件不存在。"""


class StrategyPathError(ConfigError, FileNotFoundError):
    """策略路径或目录不存在，或目录内无法确定策略文件。"""


class StrategyConfigError(ConfigError):
    """策略形态非法：扩展名不是 ``.py``，或目录内候选过多而无法抉择。"""


# ============================================================
# 数据
# ============================================================


class DataError(PtradeSimError, ValueError):
    """数据源缺失、不完整或取数失败（缺表、区间无交易日、库内无行情）。"""


class DatabaseNotFoundError(DataError, FileNotFoundError):
    """DuckDB 库文件不存在（还没 ``db build``，或路径写错）。"""


# ============================================================
# 策略代码
# ============================================================


class StrategyError(PtradeSimError, ValueError):
    """策略代码不符合约定（如缺 ``initialize``）。"""


class StrategyImportError(StrategyError, ImportError):
    """策略文件无法作为 Python 模块加载（语法错误、编码问题等）。"""


# ============================================================
# 资源、队列与依赖
# ============================================================


class QueueTimeoutError(PtradeSimError, TimeoutError):
    """等待回测队列锁超时。"""


class DependencyError(PtradeSimError, ImportError):
    """缺少已声明的可选依赖（如未装 ``duckdb``）。"""


# ============================================================
# CLI 退出码映射
# ============================================================

#: 未分类失败（保留 1，与历史行为一致）
EXIT_FAILURE = 1

#: 具体错误类 -> 退出码。**顺序有意义**：先匹配子类，故具体的放前面。
#: 这些码让调用脚本能区分"我配置写错了"与"数据没准备好"，
#: 而不必去解析错误文本。
EXIT_CODES: tuple[tuple[type[BaseException], int], ...] = (
    (ConfigFileNotFoundError, 2),
    (StrategyPathError, 2),
    (StrategyConfigError, 2),
    (ConfigError, 2),
    (StrategyImportError, 4),
    (StrategyError, 4),
    (DatabaseNotFoundError, 3),
    (DataError, 3),
    (QueueTimeoutError, 5),
    (DependencyError, 6),
)


def exit_code_for(exc: BaseException) -> int:
    """把异常映射为 CLI 退出码。

    ==========  ================================
    退出码       含义
    ==========  ================================
    0           成功
    1           未分类失败（含编程错误）
    2           配置 / 策略定位错误
    3           数据错误（库缺失或不完整）
    4           策略代码错误
    5           队列 / 资源等待超时
    6           缺少可选依赖
    ==========  ================================
    """
    for cls, code in EXIT_CODES:
        if isinstance(exc, cls):
            return code
    return EXIT_FAILURE
