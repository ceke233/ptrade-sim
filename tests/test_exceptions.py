"""异常体系测试。

这套体系的**核心价值是「分类」**：调用方能区分"配置写错"与"库不完整"，
CLI 能给出不同退出码。同时它用**多重继承**保住向后兼容 —— 这两个性质都必须被测试锁住，
否则日后一次"清理父类"的改动就会悄悄破坏所有调用方。
"""

from __future__ import annotations

import pytest

from ptrade_sim import exceptions as E

pytestmark = pytest.mark.unit

#: 所有具体异常类（不含基类与常量）
CONCRETE = [
    E.ConfigError,
    E.ConfigFileNotFoundError,
    E.StrategyPathError,
    E.StrategyConfigError,
    E.DataError,
    E.DatabaseNotFoundError,
    E.StrategyError,
    E.StrategyImportError,
    E.QueueTimeoutError,
    E.DependencyError,
]

#: 新异常 -> 必须仍然兼容的旧类型（迁移期保证旧 `except` 不失效）。
#: 这等价于 json.JSONDecodeError(ValueError) 那种标准库做法。
BACKWARD_COMPAT = {
    E.ConfigError: (ValueError,),
    E.ConfigFileNotFoundError: (ValueError, FileNotFoundError),
    E.StrategyPathError: (ValueError, FileNotFoundError),
    E.StrategyConfigError: (ValueError,),
    E.DataError: (ValueError,),
    E.DatabaseNotFoundError: (ValueError, FileNotFoundError),
    E.StrategyError: (ValueError,),
    E.StrategyImportError: (ValueError, ImportError),
    E.QueueTimeoutError: (TimeoutError,),
    E.DependencyError: (ImportError,),
}


# ============================================================
# 层级
# ============================================================


def test_all_concrete_inherit_base():
    """每个具体异常都必须是 PtradeSimError —— 否则 ``except PtradeSimError`` 会漏。"""
    for cls in CONCRETE:
        assert issubclass(cls, E.PtradeSimError), f"{cls.__name__} 未继承基类"


def test_base_is_not_a_legacy_type():
    """基类**不该**同时是 ValueError：那样 ``except ValueError`` 会捕获一切，
    失去分类的意义（子类各自挂旧类型就够了）。"""
    assert not issubclass(E.PtradeSimError, (ValueError, OSError, ImportError))


def test_backward_compat_with_legacy_types():
    """**关键回归防护**：新异常必须仍能被既有的 ``except ValueError`` /
    ``except FileNotFoundError`` 等捕获，否则用户代码会突然漏掉异常。"""
    for cls, legacy in BACKWARD_COMPAT.items():
        for lt in legacy:
            assert issubclass(cls, lt), f"{cls.__name__} 不再是 {lt.__name__}（会破坏现有调用方）"
            assert isinstance(cls("x"), lt)


def test_catch_by_base_works():
    for cls in CONCRETE:
        try:
            raise cls("boom")
        except E.PtradeSimError as exc:
            assert isinstance(exc, cls)
        else:  # pragma: no cover
            pytest.fail(f"{cls.__name__} 未被基类捕获")


def test_catch_by_legacy_still_works():
    """用旧写法捕获新异常 —— 模拟未升级的用户代码。"""
    with pytest.raises(FileNotFoundError):
        raise E.DatabaseNotFoundError("no db")
    with pytest.raises(ValueError):
        raise E.DataError("bad data")
    with pytest.raises(TimeoutError):
        raise E.QueueTimeoutError("slow")
    with pytest.raises(ImportError):
        raise E.DependencyError("no duckdb")


# ============================================================
# 退出码
# ============================================================


def test_exit_code_mapping():
    """退出码是 CLI 的对外契约，逐个锁住。"""
    expected = {
        E.ConfigError: 2,
        E.ConfigFileNotFoundError: 2,
        E.StrategyPathError: 2,
        E.StrategyConfigError: 2,
        E.DataError: 3,
        E.DatabaseNotFoundError: 3,
        E.StrategyError: 4,
        E.StrategyImportError: 4,
        E.QueueTimeoutError: 5,
        E.DependencyError: 6,
    }
    for cls, code in expected.items():
        assert E.exit_code_for(cls("x")) == code, f"{cls.__name__} 退出码应为 {code}"


def test_subclass_wins_over_parent_in_mapping():
    """退出码映射按顺序匹配，**具体的必须排在父类之前**。

    若父类排在前面，它会抢先命中，子类的码就永远用不到
    （例如 ``ConfigError`` 若排在 ``ConfigFileNotFoundError`` 之前，
    后者就形同虚设）。这条测试锁住顺序，避免以后有人"按字母排序"整理这个元组。
    """
    seen: list[type] = []
    for cls, _ in E.EXIT_CODES:
        for earlier in seen:
            # earlier 在前，若 cls 是 earlier 的子类，则 earlier 会抢先命中 cls
            assert not issubclass(cls, earlier), (
                f"{earlier.__name__} 排在 {cls.__name__} 之前，而后者是它的子类 —— "
                f"父类会抢先命中，{cls.__name__} 的退出码永远用不到"
            )
        seen.append(cls)


def test_unknown_error_falls_back_to_1():
    """编程错误不进异常体系 —— 必须落到未分类码 1，而不是被误判成业务错误。"""
    assert E.exit_code_for(RuntimeError("bug")) == 1
    assert E.exit_code_for(KeyError("oops")) == 1
    assert E.exit_code_for(ValueError("plain")) == 1  # 未迁移的裸 ValueError
    assert E.EXIT_FAILURE == 1


def test_programming_errors_are_not_caught_as_business_errors():
    """TypeError / AttributeError 这类缺陷不该被业务捕获逻辑吃掉。"""
    for exc in (TypeError("t"), AttributeError("a")):
        assert not isinstance(exc, E.PtradeSimError)
        assert E.exit_code_for(exc) == 1
