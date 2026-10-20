"""会话状态机：给群聊加一层"节奏"，让 bot 有进退场。

问题
----
现有主动回复是「每条消息 1% 抽签 + 群级冷却 + 被点名后 120 秒内 100% 必回」。
它没有"会话"概念：bot 一旦接上话就黏着不走（延续窗口内逢消息必回），说完了也不会
主动退场，下一条无关消息仍可能被抽中。表现出来就是"要么不理人，要么突然话痨"。

模型
----
把每个会话看成有生命周期的状态机，只有两个状态：

    OBSERVING（观望）──进入条件满足──▶ ACTIVE（活跃）
         ▲                                │
         └──────退场条件满足──────────────┘

- OBSERVING：平时只对 @ / 引用 / 关键词响应；按活跃度调制的概率抽签，偶尔主动接话
- ACTIVE：接上话之后进入"在场"状态，本轮对话参与者的后续消息可以稳定跟随

四道刹车（这是与旧方案的本质区别）
1. 活跃时长上限 active_max_duration   —— ACTIVE 最多持续 N 秒，到点强制退场
2. 冷场退场 cold_exit_seconds         —— 群内连续 N 秒没人说话 → 退场
3. 发言轮数上限 max_bot_turns         —— ACTIVE 内连续说满 N 轮 → 主动收尾退场
4. 进入限流 enter_cooldown / max_enters_per_hour —— 两次进入的最小间隔 + 每小时次数上限

额外吸收的两个现成设计
- 活跃度调制（参考市场「心流沉浸版」yikexing/astrbot_plugin_xinliuchenjinban）：
  用「消息频率 + 平均间隔 + 发言人数分散度」三维加权算出 0~1 的群活跃度，
  群越冷清越容易主动开口，群越热闹越克制。alpha=0.5 时活跃度 0 概率 ×1.5、活跃度 1 概率 ×0.5。
- 复读过滤（参考市场「轻量唤醒」wolfycz/wakelite）：用户消息与 bot 最近发言去标点后
  完全相同，视为复读，不响应。

与 active_reply_ctl 的关系
--------------------------
**二选一**。本插件自己完成「判定 → 请求回复 → SKIP 兜底」全链路，不依赖也不修改
active_reply_ctl。两者同时启用会抢「要不要说话」的决策权（wakepro 的教训）。
因此提供 coexist_mode：检测到 active_reply_ctl 也在运行时，自动降级为纯观察器
（只记录状态、打标记，绝不唤醒、绝不发消息），配置里能看到降级原因。
"""

from __future__ import annotations

import math
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Reply
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_session_state"
OBSERVING = "observing"
ACTIVE = "active"

# 优先级固定 500：低于 msg_debounce(9999)，保证看到的是合并后的那一条；
# 高于 active_reply_ctl(0)，以便在它判定前把状态推进到正确阶段。
HANDLER_PRIORITY = 500
ACTIVE_REPLY_STAR = "astrbot_plugin_active_reply_ctl"
SKIP_PATTERN = re.compile(r"^[\[【(（]?\s*skip\s*[\]】)）]?[。.!！~～]*$", re.IGNORECASE)

DEFAULT_CONFIG: dict = {
    "enable": True,
    "coexist_mode": "auto",
    "dry_run": False,
    "quiet_hours": [],
    "active_max_duration": 300,
    "cold_exit_seconds": 90,
    "max_bot_turns": 6,
    "enter_cooldown": 600,
    "max_enters_per_hour": 3,
    "enter_probability": 0.02,
    "activity_modulation": True,
    "activity_gain": 0.5,
    "activity_window": 300,
    "follow_participants": True,
    "follow_probability": 0.85,
    "follow_window": 180,
    "enter_on_keyword": True,
    "keyword_list": ["肥鱼", "大肥鲸", "🐳", "哦鲸鲸"],
    "skip_when_talking_to_others": True,
    "reread_filter": True,
    "min_message_len": 2,
    "skip_marker": "[SKIP]",
    "prompt_template": (
        "群里【{sender}】刚说：\n{message}\n\n"
        "当前群里正在聊天，你已经在场。结合上面的群聊记录判断："
        "如果这句话是在跟别人说、与你无关，或群里在刷屏玩梗，就只回复 {marker} 一个词；"
        "否则以当前人设自然接一句，简短（一两句就够），不要客服腔、不要总结陈词。"
    ),
    "follow_prompt_template": (
        "【{sender}】接着刚才的对话又说：\n{message}\n\n"
        "如果这是在对你说，就以当前人设自然接住，简短；"
        "如果其实是在跟群里其他人说话，只回复 {marker} 一个词。"
    ),
    "debug": False,
}


# ======================================================================
# 纯逻辑层（不依赖 AstrBot，可离线单测）
# ======================================================================


@dataclass
class SessionState:
    """单个会话（umo）的状态。"""

    umo: str
    phase: str = OBSERVING
    phase_since: float = 0.0
    last_human_at: float = 0.0
    last_bot_at: float = 0.0
    bot_turns: int = 0
    enters: deque = field(default_factory=deque)
    last_enter_at: float = 0.0
    participants: dict = field(default_factory=dict)  # sender_id -> 最近发言时间
    msg_times: deque = field(default_factory=deque)  # 人类消息时间戳
    sender_times: deque = field(default_factory=deque)  # (ts, sender_id)
    cached_activity: float = 0.0
    activity_at: float = 0.0
    enter_count_hour: int = 0

    def is_active(self) -> bool:
        return self.phase == ACTIVE


class Cfg:
    """配置包装：带类型兜底，避免配置写错把插件搞崩。"""

    def __init__(self, raw: dict | None) -> None:
        data = dict(DEFAULT_CONFIG)
        if raw:
            for k, v in dict(raw).items():
                if k in DEFAULT_CONFIG:
                    data[k] = v
        self._d = data

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def num(self, k: str, default: float) -> float:
        try:
            v = float(self._d.get(k, default))
            return v if math.isfinite(v) else float(default)
        except (TypeError, ValueError):
            return float(default)

    def flag(self, k: str, default: bool = False) -> bool:
        v = self._d.get(k, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "y", "on", "是")
        return bool(v)

    def str_list(self, k: str) -> list[str]:
        v = self._d.get(k) or []
        if isinstance(v, str):
            v = [x for x in re.split(r"[,，\s]+", v) if x]
        return [str(x).strip() for x in v if str(x).strip()]


class SessionMachine:
    """会话状态机的纯逻辑部分。所有时间都用 monotonic 秒。"""

    def __init__(self, cfg: Cfg) -> None:
        self.cfg = cfg
        self.states: dict[str, SessionState] = {}

    # ---------- 基础 ----------

    def state(self, umo: str) -> SessionState:
        st = self.states.get(umo)
        if st is None:
            st = SessionState(umo=umo)
            self.states[umo] = st
        return st

    def _window(self) -> float:
        return max(30.0, self.cfg.num("activity_window", 300))

    def record_human(self, umo: str, now: float, sender: str) -> None:
        st = self.state(umo)
        st.last_human_at = now
        st.msg_times.append(now)
        st.sender_times.append((now, sender))
        st.participants[sender] = now
        self._trim(st, now)

    def record_bot(self, umo: str, now: float) -> None:
        st = self.state(umo)
        st.last_bot_at = now
        if st.phase == ACTIVE:
            st.bot_turns += 1

    def _trim(self, st: SessionState, now: float) -> None:
        cutoff = now - max(self._window(), 120.0)
        while st.msg_times and st.msg_times[0] < cutoff:
            st.msg_times.popleft()
        while st.sender_times and st.sender_times[0][0] < cutoff:
            st.sender_times.popleft()
        follow_w = max(30.0, self.cfg.num("follow_window", 180))
        for sid in [k for k, ts in st.participants.items() if now - ts > follow_w * 2]:
            st.participants.pop(sid, None)

    # ---------- 活跃度 ----------

    def activity(self, umo: str, now: float) -> float:
        """群活跃度 0~1。采纳「心流沉浸版」三维加权：频率 / 平均间隔 / 人数分散度。"""
        st = self.state(umo)
        if st.activity_at > 0 and now - st.activity_at < 5.0:
            return st.cached_activity

        window = self._window()
        recent = [t for t in st.msg_times if now - t <= window]
        if not recent:
            st.cached_activity = 0.0
            st.activity_at = now
            return 0.0

        # 频率：每分钟消息数 / 10 封顶
        freq = min(1.0, (len(recent) / (window / 60.0)) / 10.0)

        # 平均间隔：<=5s 满分，>=120s 零分，中间线性
        interval = 0.0
        if len(recent) >= 2:
            gaps = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
            avg = sum(gaps) / len(gaps)
            if avg <= 5:
                interval = 1.0
            elif avg < 120:
                interval = 1.0 - (avg - 5) / 115.0

        # 人数分散度：5 个不同的人封顶
        senders = {s for t, s in st.sender_times if now - t <= window}
        diversity = min(1.0, len(senders) / 5.0)

        act = round(max(0.0, min(1.0, freq * 0.4 + interval * 0.3 + diversity * 0.3)), 3)
        st.cached_activity = act
        st.activity_at = now
        return act

    def effective_probability(self, umo: str, now: float) -> float:
        """活跃度调制的进入概率：群冷清时更容易开口，热闹时更克制。"""
        base = max(0.0, min(1.0, self.cfg.num("enter_probability", 0.02)))
        if not self.cfg.flag("activity_modulation", True):
            return base
        gain = max(0.0, min(1.0, self.cfg.num("activity_gain", 0.5)))
        act = self.activity(umo, now)
        p = base * (1 + gain * (1 - 2 * act))
        return max(0.0, min(1.0, p))

    # ---------- 免打扰 ----------

    def in_quiet_hours(self, now_ts: float | None = None) -> bool:
        rules = self.cfg.str_list("quiet_hours")
        if not rules:
            return False
        cur = datetime.fromtimestamp(now_ts if now_ts is not None else time.time())
        minutes = cur.hour * 60 + cur.minute
        for rule in rules:
            m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", rule.strip())
            if not m:
                continue
            sh, sm, eh, em = (int(x) for x in m.groups())
            start, end = sh * 60 + sm, eh * 60 + em
            if start <= end:
                if start <= minutes < end:
                    return True
            else:  # 跨零点
                if minutes >= start or minutes < end:
                    return True
        return False

    # ---------- 退场 ----------

    def check_exit(self, umo: str, now: float) -> str | None:
        """检查是否需要退场，返回退场原因；None 表示继续活跃。"""
        st = self.state(umo)
        if st.phase != ACTIVE:
            return None

        max_dur = self.cfg.num("active_max_duration", 300)
        if max_dur > 0 and now - st.phase_since >= max_dur:
            return "活跃超时"

        cold = self.cfg.num("cold_exit_seconds", 90)
        if cold > 0 and st.last_human_at and now - st.last_human_at >= cold:
            return "冷场退场"

        max_turns = int(self.cfg.num("max_bot_turns", 6))
        if max_turns > 0 and st.bot_turns >= max_turns:
            return "轮数上限"

        return None

    def exit_phase(self, umo: str, now: float, reason: str) -> None:
        st = self.state(umo)
        st.phase = OBSERVING
        st.phase_since = now
        st.bot_turns = 0
        if self.cfg.flag("debug", False):
            logger.info(f"[{PLUGIN_NAME}] 退场｜umo={umo}｜原因={reason}")

    # ---------- 进入 ----------

    def _rate_limited(self, umo: str, now: float) -> str | None:
        st = self.state(umo)
        cd = self.cfg.num("enter_cooldown", 600)
        if cd > 0 and st.last_enter_at and now - st.last_enter_at < cd:
            return f"进入冷却（剩余 {cd - (now - st.last_enter_at):.0f}s）"
        cap = int(self.cfg.num("max_enters_per_hour", 3))
        if cap > 0:
            while st.enters and now - st.enters[0] > 3600:
                st.enters.popleft()
            if len(st.enters) >= cap:
                return f"本小时进入次数已达上限 {cap}"
        return None

    def enter_phase(self, umo: str, now: float, reason: str) -> None:
        st = self.state(umo)
        st.phase = ACTIVE
        st.phase_since = now
        st.bot_turns = 0
        st.last_enter_at = now
        st.enters.append(now)
        st.enter_count_hour = len(st.enters)
        if self.cfg.flag("debug", False):
            logger.info(f"[{PLUGIN_NAME}] 进入活跃｜umo={umo}｜原因={reason}")

    def can_enter(self, umo: str, now: float) -> str | None:
        """能否进入活跃态。返回 None 表示可以，否则返回拒绝原因。"""
        if self.in_quiet_hours():
            return "免打扰时段"
        return self._rate_limited(umo, now)

    # ---------- 复读 ----------

    @staticmethod
    def normalize(text: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", text or "").lower()

    def is_reread(self, text: str, bot_msgs: list[str]) -> bool:
        if not self.cfg.flag("reread_filter", True):
            return False
        cleaned = self.normalize(text)
        if not cleaned:
            return False
        return any(m and self.normalize(m) == cleaned for m in bot_msgs)


@dataclass
class Decision:
    """一次消息的决策结果。"""

    reply: bool = False  # 是否触发回复
    entered: bool = False  # 本次是否进入活跃态
    reason: str = ""
    follow: bool = False  # 是否为"跟随对话"路径


# ======================================================================
# 插件层
# ======================================================================


class SessionStatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg = Cfg(dict(config) if config else None)
        self.machine = SessionMachine(self.cfg)
        self._bot_msgs: dict[str, deque] = {}
        self._degraded = False
        self._degrade_reason = ""

    async def initialize(self) -> None:
        self._degraded = self._should_degrade()
        if self._degraded:
            logger.warning(
                f"[{PLUGIN_NAME}] 降级为纯观察模式：{self._degrade_reason}"
                "（此时不会唤醒、不会发言）"
            )

    # ---------- 共存检测 ----------

    def _loaded_star_names(self) -> set[str]:
        names: set[str] = set()
        try:
            reg = getattr(self.context, "_star_manager", None)
            stars = getattr(reg, "star_map", None) or {}
            for meta in stars.values():
                nm = getattr(meta, "name", "") or ""
                root = getattr(meta, "root_dir_name", "") or ""
                if nm:
                    names.add(nm)
                if root:
                    names.add(root)
        except Exception:
            pass
        return names

    def _should_degrade(self) -> bool:
        mode = str(self.cfg.get("coexist_mode", "auto")).strip().lower()
        if mode == "off":
            return True
        if mode == "on":
            return False
        names = self._loaded_star_names()
        if ACTIVE_REPLY_STAR in names or "active_reply_ctl" in names:
            self._degrade_reason = "检测到 active_reply_ctl 同时启用，避免抢占决策权"
            return True
        return False

    @property
    def degraded(self) -> bool:
        return self._degraded

    # ---------- 工具 ----------

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        return str(event.get_self_id() or "").strip()

    def _addressed(self, event: AstrMessageEvent) -> bool:
        if event.is_at_or_wake_command:
            return True
        me = self._self_id(event)
        if not me:
            return False
        for seg in event.get_messages():
            if isinstance(seg, At) and str(getattr(seg, "qq", "") or "").strip() == me:
                return True
            if isinstance(seg, Reply) and str(getattr(seg, "sender_id", "") or "") == me:
                return True
        return False

    def _talking_to_others(self, event: AstrMessageEvent) -> bool:
        me = self._self_id(event)
        for seg in event.get_messages():
            if isinstance(seg, At):
                qq = str(getattr(seg, "qq", "") or "").strip()
                if qq and qq != me:
                    return True
            elif isinstance(seg, Reply):
                sender = str(getattr(seg, "sender_id", "") or "")
                if sender and sender != me:
                    return True
        return False

    def _remember_bot_msg(self, umo: str, text: str) -> None:
        dq = self._bot_msgs.get(umo)
        if dq is None:
            dq = deque(maxlen=8)
            self._bot_msgs[umo] = dq
        if text:
            dq.append(text)

    # ---------- 主判定 ----------

    def _decide(
        self,
        umo: str,
        now: float,
        sender: str,
        text: str,
    ) -> Decision:
        cfg = self.cfg
        m = self.machine
        st = m.state(umo)

        # 冷场 / 超时 / 轮数 → 先退场
        why = m.check_exit(umo, now)
        if why:
            m.exit_phase(umo, now, why)

        # 复读过滤
        if m.is_reread(text, list(self._bot_msgs.get(umo, []))):
            return Decision(reason="复读过滤")

        # 关键词
        kw_hit = cfg.flag("enter_on_keyword", True) and any(
            k in text for k in cfg.str_list("keyword_list")
        )

        # 已在活跃态：跟随对话参与者
        if st.phase == ACTIVE:
            follow_w = cfg.num("follow_window", 180)
            is_participant = (
                cfg.flag("follow_participants", True)
                and sender in st.participants
                and now - st.participants.get(sender, 0) <= follow_w
            )
            if kw_hit:
                return Decision(reply=True, reason="活跃态·关键词", follow=True)
            if not is_participant:
                return Decision(reason="活跃态·非参与者")
            if random.random() <= cfg.num("follow_probability", 0.85):
                return Decision(reply=True, reason="活跃态·跟随", follow=True)
            return Decision(reason="活跃态·跟随未命中")

        # 观望态
        if kw_hit:
            blocked = m.can_enter(umo, now)
            if blocked:
                return Decision(reason=f"关键词进入被拦：{blocked}")
            m.enter_phase(umo, now, "关键词")
            return Decision(reply=True, entered=True, reason="关键词进入")

        blocked = m.can_enter(umo, now)
        if blocked:
            return Decision(reason=f"观望：{blocked}")

        p = m.effective_probability(umo, now)
        if random.random() < p:
            m.enter_phase(umo, now, f"抽签 {p:.4f}")
            return Decision(reply=True, entered=True, reason=f"抽签进入 {p:.4f}")
        return Decision(reason=f"观望：未命中 {p:.4f}")

    # ---------- 消息钩子 ----------

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=HANDLER_PRIORITY
    )
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群消息并决定是否参与。"""
        if not self.cfg.flag("enable", True):
            return

        umo = event.unified_msg_origin
        now = time.monotonic()
        sender = str(event.get_sender_id() or "").strip()
        me = self._self_id(event)

        # 忽略 bot 自己（含其他 bot 的可能性交给上层）
        if me and sender == me:
            self.machine.record_bot(umo, now)
            return

        # ① 先做退场检查——必须在本条消息入库之前，否则 last_human_at 被刷新，
        #    冷场退场永远不会触发。
        why = self.machine.check_exit(umo, now)
        if why:
            self.machine.exit_phase(umo, now, why)

        # ② 消息入库（同时更新活跃度统计与参与者列表）
        self.machine.record_human(umo, now, sender)

        text = (event.message_str or "").strip()
        has_image = any(isinstance(c, Image) for c in event.get_messages())

        # 被点名：主链路本来就会回复，这里只负责把状态推进到活跃
        if self._addressed(event):
            st = self.machine.state(umo)
            if st.phase != ACTIVE:
                if not self.machine.in_quiet_hours():
                    self.machine.enter_phase(umo, now, "被点名")
            else:
                st.bot_turns = 0  # 被点名视为重新开始计数
            return

        # 明确指向他人 → 只贡献活跃度，不参与
        if self.cfg.flag(
            "skip_when_talking_to_others", True
        ) and self._talking_to_others(event):
            return

        # 内容过短且无图 → 不值得接
        if len(text) < int(self.cfg.num("min_message_len", 2)) and not has_image:
            return

        decision = self._decide(umo, now, sender, text)
        if self.cfg.flag("debug", False):
            logger.info(
                f"[{PLUGIN_NAME}] 判定｜umo={umo}｜sender={sender}｜"
                f"reply={decision.reply}｜{decision.reason}"
            )

        if not decision.reply:
            return

        if self.degraded or self.cfg.flag("dry_run", False):
            if self.cfg.flag("dry_run", False):
                logger.info(
                    f"[{PLUGIN_NAME}] [dry_run] 本应回复但未发送｜umo={umo}｜{decision.reason}"
                )
            return

        prompt = self._build_prompt(event, sender, text, decision.follow)
        event.set_extra("session_state_skip_enabled", True)
        conversation = await self._get_conversation(event)
        yield event.request_llm(prompt=prompt, conversation=conversation)

    def _build_prompt(self, event: AstrMessageEvent, sender: str, text: str, follow: bool) -> str:
        tpl = (
            self.cfg.get("follow_prompt_template")
            if follow
            else self.cfg.get("prompt_template")
        ) or "{message}"
        name = str(event.get_sender_name() or sender or "群成员")
        out = str(tpl).replace("{message}", text).replace("{sender}", name)
        out = out.replace("{member}", name)
        out = out.replace("{marker}", str(self.cfg.get("skip_marker", "[SKIP]")))
        return out

    async def _get_conversation(self, event: AstrMessageEvent):
        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(event.unified_msg_origin)
            if not cid:
                cid = await mgr.new_conversation(
                    event.unified_msg_origin, event.get_platform_id()
                )
            return await mgr.get_conversation(event.unified_msg_origin, cid)
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 获取会话失败")
            return None

    # ---------- SKIP 兜底 ----------

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.cfg.flag("enable", True) or not resp:
            return
        if not event.get_extra("session_state_skip_enabled"):
            return
        try:
            text = (resp.completion_text or "").strip()
            if not text:
                return
            if not SKIP_PATTERN.match(text):
                # 记录 bot 真实发言，供复读过滤
                self._remember_bot_msg(event.unified_msg_origin, text)
                self.machine.record_bot(event.unified_msg_origin, time.monotonic())
                return
            resp.completion_text = ""
            event.stop_event()
            # 说了 SKIP 等于自己退场，静默收尾
            umo = event.unified_msg_origin
            self.machine.exit_phase(umo, time.monotonic(), "LLM 主动 SKIP")
            logger.info(f"[{PLUGIN_NAME}] LLM 判定不适配，静默跳过｜umo={umo}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] SKIP 检测失败: {e}", exc_info=True)

    # ---------- 指令 ----------

    @filter.command("节奏")
    async def cmd_state(self, event: AstrMessageEvent, arg: str = ""):
        """查看/重置当前会话的节奏状态：/节奏 或 /节奏 重置"""
        umo = event.unified_msg_origin
        arg = (arg or "").strip()

        if arg in ("重置", "reset"):
            if self.machine.in_quiet_hours():
                yield event.plain_result("免打扰时段内不做变更。")
                return
            self.machine.states.pop(umo, None)
            yield event.plain_result("已重置本会话的节奏状态。")
            return

        now = time.monotonic()
        st = self.machine.state(umo)
        why = self.machine.check_exit(umo, now)
        if why:
            self.machine.exit_phase(umo, now, why)
            st = self.machine.state(umo)

        act = self.machine.activity(umo, now)
        p = self.machine.effective_probability(umo, now)
        if st.phase == ACTIVE:
            left = max(0.0, self.cfg.num("active_max_duration", 300) - (now - st.phase_since))
            cold_left = max(
                0.0, self.cfg.num("cold_exit_seconds", 90) - (now - st.last_human_at)
            )
            phase_line = f"活跃中（剩余 {left:.0f}s，冷场退场倒计时 {cold_left:.0f}s）"
        else:
            phase_line = "观望中"
        lines = [
            f"状态：{phase_line}",
            f"群活跃度：{act:.2f}（进入概率 {p:.3%}）",
            f"本轮已发言：{st.bot_turns} 轮 / 上限 {int(self.cfg.num('max_bot_turns', 6))}",
            f"本小时进入次数：{len(st.enters)} / 上限 {int(self.cfg.num('max_enters_per_hour', 3))}",
        ]
        if st.last_enter_at:
            lines.append(f"上次进入：{(now - st.last_enter_at):.0f}s 前")
        if self.machine.in_quiet_hours():
            lines.append("⚠️ 当前处于免打扰时段")
        if self.degraded:
            lines.append(f"⚠️ 已降级为纯观察：{self._degrade_reason}")
        if self.cfg.flag("dry_run", False):
            lines.append("⚠️ dry_run 模式：只判定不发送")
        yield event.plain_result("\n".join(lines))
