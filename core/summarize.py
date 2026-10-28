"""把活动数据变成三样东西：

1. 每轮对话附带的观测块（尽量短，且不破坏前缀缓存）
2. 给日记插件用的今日素材（人读的中文段落）
3. 主动关心的契机信号（给模型看的场景描述）
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .store import (
    CAT_LABEL,
    FOCUS_CATS,
    ActivityStore,
    DaySummary,
    Segment,
    _fmt_clock,
    _fmt_duration,
    friendly_app,
)

# 浏览器标题里那些固定后缀，留着对判断没帮助还占字数
_TITLE_SUFFIXES = re.compile(
    r"\s*[-—–]\s*(Google Chrome|Microsoft[\s]?Edge|Mozilla Firefox|Brave|"
    r"Microsoft​ Edge|个人\s*[-—–]\s*Microsoft[\s]?Edge|工作\s*[-—–]\s*Microsoft[\s]?Edge)\s*$",
    re.IGNORECASE,
)

# 这些标题没有信息量，等于「没在做什么具体的事」
_EMPTY_TITLES = {"", "新标签页", "新建标签页", "new tab", "about:blank", "qq", "微信"}


def _clean_title(title: str, limit: int = 46) -> str:
    text = _TITLE_SUFFIXES.sub("", (title or "").strip())
    text = text.replace("&&", "&").replace("\n", " ").strip()
    if text.lower() in _EMPTY_TITLES:
        return ""
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _label_of(seg: Segment) -> str:
    """「DeepSeek Harness · 如何为 AI 添加 skill」这样的短标签。"""
    app = friendly_app(seg.app)
    title = _clean_title(seg.title)
    if not title:
        return app
    # 标题里已经带着程序名了（「main.py - Visual Studio Code」），就不用再加一遍
    if app.lower() in title.lower():
        return title
    return f"{app} · {title}"


def _cat_line(summary: DaySummary, limit: int = 3) -> str:
    items = sorted(summary.by_cat.items(), key=lambda kv: kv[1], reverse=True)
    parts = [
        f"{CAT_LABEL.get(cat, cat)} {_fmt_duration(sec)}"
        for cat, sec in items[:limit]
        if sec >= 60
    ]
    return "、".join(parts)


# ---------- 1. 对话注入块 ----------


def build_injection(
    store: ActivityStore, interval: int, idle_timeout: int
) -> str:
    now = datetime.now()
    clock = now.strftime("%H:%M")
    current = store.current_segment(interval, idle_timeout)

    if current is None:
        return (
            f"[你此刻的观测｜{clock}] 他不在电脑前，或者你暂时看不到窗口。"
            "这只是状态，不用特意提。"
        )

    recent = store.recent_summary(30, interval, idle_timeout)
    today = store.day_summary(date.today(), interval, idle_timeout)

    lines = [
        f"[你此刻的观测｜{clock}｜不是他说的，是你看到的。"
        "别复述这些数字，别说「我注意到」，自然点用]",
        f"正在：{_label_of(current)}",
    ]

    recent_line = _cat_line(recent, limit=3)
    if recent_line:
        lines.append(f"近 30 分钟：{recent_line}")

    if today.active_seconds >= 300:
        parts = [f"在电脑前 {today.active_hours} 小时"]
        today_line = _cat_line(today, limit=3)
        if today_line:
            parts.append(today_line)
        lines.append("今天：" + "；".join(parts))

    focus_cat, focus_minutes = store.focus_streak_minutes(interval, idle_timeout)
    if focus_cat in FOCUS_CATS and focus_minutes >= 60:
        lines.append(
            f"注意：他已经连续{CAT_LABEL.get(focus_cat, focus_cat)} "
            f"{focus_minutes} 分钟没换过事了。"
        )

    return "\n".join(lines)


# ---------- 2. 日记素材 ----------


def build_diary_material(
    store: ActivityStore, interval: int, idle_timeout: int
) -> str:
    """给日记用的第三人称素材。故意写得像「旁白」，让她改写而不是照抄。"""
    today = date.today()
    summary = store.day_summary(today, interval, idle_timeout)

    if summary.active_seconds < 600:
        return ""

    bits: list[str] = []
    bits.append(
        f"他今天在电脑前待了大约 {summary.active_hours} 小时，"
        f"从 {summary.first_at} 到 {summary.last_at}。"
    )

    cat_line = _cat_line(summary, limit=4)
    if cat_line:
        bits.append(f"花时间最多的事：{cat_line}。")

    focus_cat, focus_sec = summary.longest_focus
    if focus_sec >= 3600:
        bits.append(
            f"最长的一次没有停下来，是在{CAT_LABEL.get(focus_cat, focus_cat)}，"
            f"差不多 {_fmt_duration(focus_sec)}。"
        )

    if summary.top_apps:
        apps = "、".join(
            f"{friendly_app(app)}（{_fmt_duration(sec)}）"
            for app, sec in summary.top_apps[:3]
            if sec >= 120
        )
        if apps:
            bits.append(f"开得最久的是：{apps}。")

    # 细分片段，给模型一点具体的画面感（取几段较长的）
    segs = [
        s
        for s in store.segments(today, interval, idle_timeout)
        if s.duration >= 600 and s.cat not in {"chat", "other"}
    ]
    segs.sort(key=lambda s: s.duration, reverse=True)
    samples = []
    for seg in segs[:3]:
        label = _label_of(seg)
        samples.append(f"{_fmt_clock(seg.start)} 左右在{label}")
    if samples:
        bits.append("零散的片段：" + "；".join(samples) + "。")

    bits.append(
        "（这些只是你看到的时间痕迹，不是你和他之间发生过的事。"
        "不要罗列它们，也不要写「我看着他」这种旁观者口吻。）"
    )
    return "\n".join(bits)


# ---------- 3. 主动关心信号 ----------


LATE_END_HOUR = 6


def is_late_hour(hour: int, late_hour: int) -> bool:
    """hour 这个钟点算不算「深夜」。

    late_hour < 6：表示 [late_hour, 6) 这段，比如 1 点填进来就是 1:00~5:59。
    late_hour >= 6：表示跨午夜，[late_hour, 24) ∪ [0, 6)。
    """
    if late_hour < LATE_END_HOUR:
        return late_hour <= hour < LATE_END_HOUR
    return hour >= late_hour or hour < LATE_END_HOUR


def care_signal(
    store: ActivityStore,
    interval: int,
    idle_timeout: int,
    focus_minutes: int,
    late_hour: int,
) -> tuple[str, str] | None:
    """返回 (类型, 场景描述)。没有该关心的事时返回 None。"""
    now = datetime.now()
    current = store.current_segment(interval, idle_timeout)
    if current is None:
        return None

    # 深夜还在用
    if is_late_hour(now.hour, late_hour):
        clock = now.strftime("%H:%M")
        return (
            "late",
            f"现在是 {clock}，他还在电脑上，正在{_label_of(current)}。",
        )

    # 长时间专注没休息
    cat, minutes = store.focus_streak_minutes(interval, idle_timeout)
    if cat in FOCUS_CATS and minutes >= focus_minutes:
        return (
            "focus",
            f"他已经在{CAT_LABEL.get(cat, cat)}上连续坐了 {minutes} 分钟，"
            f"中间没换过别的事。",
        )

    return None
