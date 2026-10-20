"""会话状态机离线单测。

只测纯逻辑层（SessionMachine / Cfg / Decision），不依赖 AstrBot 运行时、
不发网络、不用真实时钟——时间一律显式传入，保证可重复。

跑法：
    python tests/test_state_machine.py       # 任意 Python 3.10+
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "main.py"


# ----------------------------------------------------------------------
# astrbot 最小 mock
# ----------------------------------------------------------------------


class _Logger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _install_astrbot_mock() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = _Logger()

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = _Logger()

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object

    class _EventMessageType:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP_MESSAGE"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def event_message_type(*a, **k):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def command(*a, **k):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def on_llm_response(*a, **k):
            def deco(fn):
                return fn

            return deco

    event.filter = _Filter

    comp = types.ModuleType("astrbot.api.message_components")

    class At:
        def __init__(self, qq=None):
            self.qq = qq

    class Reply:
        def __init__(self, sender_id=None):
            self.sender_id = sender_id

    class Image:
        pass

    comp.At = At
    comp.Reply = Reply
    comp.Image = Image

    prov = types.ModuleType("astrbot.api.provider")

    class LLMResponse:
        completion_text = ""

    prov.LLMResponse = LLMResponse

    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    star.Context = Context
    star.Star = Star

    api.event = event
    api.message_components = comp
    api.provider = prov
    api.star = star

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.message_components"] = comp
    sys.modules["astrbot.api.provider"] = prov
    sys.modules["astrbot.api.star"] = star


_install_astrbot_mock()

spec = importlib.util.spec_from_file_location("session_state_main", PLUGIN_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["session_state_main"] = mod
spec.loader.exec_module(mod)

Cfg = mod.Cfg
SessionMachine = mod.SessionMachine
ACTIVE = mod.ACTIVE
OBSERVING = mod.OBSERVING

UMO = "aiocqhttp:GroupMessage:10086"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def mk(**over):
    cfg = dict(mod.DEFAULT_CONFIG)
    cfg.update(over)
    return Cfg(cfg)


# ----------------------------------------------------------------------
# 用例：退场（四道刹车）
# ----------------------------------------------------------------------


def test_cold_exit():
    """冷场退场：群里安静超过阈值就退场。"""
    m = SessionMachine(mk(cold_exit_seconds=90))
    m.enter_phase(UMO, 1000.0, "测试")
    m.record_human(UMO, 1000.0, "u1")
    check("进入后为活跃态", m.state(UMO).phase == ACTIVE)

    check("冷场未到时不退场", m.check_exit(UMO, 1050.0) is None)
    why = m.check_exit(UMO, 1095.0)
    check("冷场到期退场", why == "冷场退场", str(why))

    m.exit_phase(UMO, 1095.0, why)
    st = m.state(UMO)
    check("退场后回到观望", st.phase == OBSERVING)
    check("退场后轮数归零", st.bot_turns == 0)


def test_active_timeout():
    """活跃时长上限：到点强制退场。"""
    m = SessionMachine(mk(active_max_duration=300, cold_exit_seconds=9999))
    m.enter_phase(UMO, 0.0, "测试")
    m.record_human(UMO, 299.0, "u1")
    check("未到上限不退场", m.check_exit(UMO, 299.0) is None)
    why = m.check_exit(UMO, 300.0)
    check("到上限退场", why == "活跃超时", str(why))


def test_max_turns_exit():
    """轮数上限：说满 N 轮主动收尾。"""
    m = SessionMachine(mk(max_bot_turns=3, active_max_duration=9999, cold_exit_seconds=9999))
    m.enter_phase(UMO, 0.0, "测试")
    for i in range(2):
        m.record_bot(UMO, 10.0 + i)
    check("未满轮数不退场", m.check_exit(UMO, 20.0) is None, str(m.state(UMO).bot_turns))
    m.record_bot(UMO, 30.0)
    why = m.check_exit(UMO, 31.0)
    check("满轮数退场", why == "轮数上限", str(why))


def test_record_bot_only_counts_in_active():
    m = SessionMachine(mk())
    m.record_bot(UMO, 10.0)
    check("观望态发言不计轮数", m.state(UMO).bot_turns == 0)


# ----------------------------------------------------------------------
# 用例：进入限流
# ----------------------------------------------------------------------


def test_enter_cooldown():
    m = SessionMachine(mk(enter_cooldown=600))
    m.enter_phase(UMO, 1000.0, "测试")
    m.exit_phase(UMO, 1001.0, "测试")
    check("冷却内拒绝进入", m.can_enter(UMO, 1200.0) is not None, "")
    check("冷却结束可进入", m.can_enter(UMO, 1700.0) is None, str(m.can_enter(UMO, 1700.0)))


def test_hourly_cap():
    m = SessionMachine(mk(enter_cooldown=0, max_enters_per_hour=2))
    m.enter_phase(UMO, 1000.0, "a")
    m.exit_phase(UMO, 1001.0, "a")
    m.enter_phase(UMO, 1100.0, "b")
    m.exit_phase(UMO, 1101.0, "b")
    blocked = m.can_enter(UMO, 1200.0)
    check("小时次数达上限被拦", blocked is not None and "上限" in blocked, str(blocked))
    # 滚动窗口：第一小时之后放行
    check("滚动窗口过期后放行", m.can_enter(UMO, 1000.0 + 3601) is None)


def test_quiet_hours_cross_midnight():
    m = SessionMachine(mk(quiet_hours=["23:00-08:00"]))
    import datetime as _dt

    def ts(h, mi):
        return _dt.datetime(2026, 10, 18, h, mi).timestamp()

    check("23:30 属免打扰", m.in_quiet_hours(ts(23, 30)) is True)
    check("03:00 属免打扰（跨零点）", m.in_quiet_hours(ts(3, 0)) is True)
    check("07:59 属免打扰", m.in_quiet_hours(ts(7, 59)) is True)
    check("08:00 不属免打扰", m.in_quiet_hours(ts(8, 0)) is False)
    check("14:00 不属免打扰", m.in_quiet_hours(ts(14, 0)) is False)


def test_quiet_hours_normal_range():
    m = SessionMachine(mk(quiet_hours=["01:00-06:00"]))
    import datetime as _dt

    def ts(h):
        return _dt.datetime(2026, 10, 18, h, 30).timestamp()

    check("02:30 属免打扰", m.in_quiet_hours(ts(2)) is True)
    check("07:30 不属免打扰", m.in_quiet_hours(ts(7)) is False)


# ----------------------------------------------------------------------
# 用例：活跃度与概率调制
# ----------------------------------------------------------------------


def test_activity_empty_vs_busy():
    m = SessionMachine(mk(activity_window=300))
    check("空群活跃度=0", m.activity(UMO, 1000.0) == 0.0)

    m2 = SessionMachine(mk(activity_window=300))
    # 10 个人在 300 秒内各发 3 条 → 30 条 / 5 分钟 = 6 条/分，间隔很短
    t = 1000.0
    for i in range(30):
        m2.record_human(UMO, t + i * 3, f"u{i % 10}")
    busy = m2.activity(UMO, t + 30 * 3)
    check("热闹群活跃度偏高", busy > 0.6, f"activity={busy}")


def test_activity_modulation_direction():
    """冷清群应更容易开口，热闹群更克制。"""
    base = 0.10
    quiet = SessionMachine(mk(enter_probability=base, activity_gain=0.5))
    p_quiet = quiet.effective_probability(UMO, 1000.0)

    busy = SessionMachine(mk(enter_probability=base, activity_gain=0.5))
    t = 1000.0
    for i in range(40):
        busy.record_human(UMO, t + i * 2, f"u{i % 10}")
    p_busy = busy.effective_probability(UMO, t + 80)

    check("冷清群概率高于基础值", p_quiet > base, f"{p_quiet} vs {base}")
    check("热闹群概率低于冷清群", p_busy < p_quiet, f"{p_busy} vs {p_quiet}")


def test_activity_modulation_off():
    m = SessionMachine(mk(enter_probability=0.1, activity_modulation=False))
    check("关闭调制后等于基础概率", abs(m.effective_probability(UMO, 1000.0) - 0.1) < 1e-9)


# ----------------------------------------------------------------------
# 用例：复读过滤
# ----------------------------------------------------------------------


def test_reread_filter():
    m = SessionMachine(mk())
    bots = ["今天天气真不错啊"]
    check("原文算复读", m.is_reread("今天天气真不错啊", bots) is True)
    check("去标点后算复读", m.is_reread("今天天气真不错啊！！！", bots) is True)
    check("不同内容不算复读", m.is_reread("明天要下雨", bots) is False)
    check("空消息不算复读", m.is_reread("", bots) is False)

    m2 = SessionMachine(mk(reread_filter=False))
    check("关闭后不判定复读", m2.is_reread("今天天气真不错啊", bots) is False)


# ----------------------------------------------------------------------
# 用例：决策路径
# ----------------------------------------------------------------------


def test_decide_observing_no_hit():
    m = SessionMachine(mk(enter_probability=0.0))
    m.record_human(UMO, 1000.0, "u1")
    p = m.effective_probability(UMO, 1000.0)
    check("概率 0 时不开口", p == 0.0, str(p))


def test_enter_then_check_bot_turn_reset():
    m = SessionMachine(mk())
    m.enter_phase(UMO, 0.0, "a")
    m.record_bot(UMO, 1.0)
    m.record_bot(UMO, 2.0)
    check("活跃态累计轮数", m.state(UMO).bot_turns == 2)
    m.exit_phase(UMO, 3.0, "test")
    m.enter_phase(UMO, 4.0, "b")
    check("再次进入轮数归零", m.state(UMO).bot_turns == 0)


def test_participants_tracking():
    m = SessionMachine(mk(follow_window=180))
    m.record_human(UMO, 1000.0, "u1")
    m.record_human(UMO, 1010.0, "u2")
    st = m.state(UMO)
    check("参与者被记录", "u1" in st.participants and "u2" in st.participants)
    # 超过 2*follow_window 后被清理
    m.record_human(UMO, 1500.0, "u3")
    check("过期参与者被清理", "u1" not in m.state(UMO).participants, str(m.state(UMO).participants))


def test_config_fallbacks():
    c = Cfg({"enter_probability": "not-a-number", "max_bot_turns": None})
    check("非法数值回退默认", abs(c.num("enter_probability", 0.02) - 0.02) < 1e-9)
    check("None 回退默认", c.num("max_bot_turns", 6) == 6)
    c2 = Cfg({"enable": "false", "activity_modulation": "true"})
    check("字符串布尔解析", c2.flag("enable", True) is False and c2.flag("activity_modulation") is True)
    c3 = Cfg({"keyword_list": "a, b，c"})
    check("字符串列表拆分", c3.str_list("keyword_list") == ["a", "b", "c"], str(c3.str_list("keyword_list")))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"运行 {len(tests)} 个用例\n")
    for fn in tests:
        print(f"- {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(fn.__name__)
            print(f"  [ERROR] {fn.__name__}: {exc}")
    print(f"\n通过 {len(PASSED)} / 失败 {len(FAILED)}")
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
