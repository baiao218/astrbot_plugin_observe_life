"""活动记录的落盘、分类与聚合。

存储格式刻意选成「每次采样一行原始数据」，不做实时合并：
  - 写入是纯 append，不担心断电或被强杀时丢历史
  - 合并放到读取时做，逻辑可以随时改进而不影响旧数据
  - 一天约 600~900 行、100KB 左右，保留 14 天也就 1.4MB

一行长这样：
  {"t": 1793182121, "app": "code.exe", "title": "main.py - Visual Studio Code", "idle": 0.2}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api import logger

from .collector import Snapshot

# ---------- 分类 ----------

CATEGORY_MAP: dict[str, str] = {
    # 写代码
    "code.exe": "code",
    "code - insiders.exe": "code",
    "cursor.exe": "code",
    "idea64.exe": "code",
    "pycharm64.exe": "code",
    "webstorm64.exe": "code",
    "goland64.exe": "code",
    "clion64.exe": "code",
    "devenv.exe": "code",
    "sublime_text.exe": "code",
    "notepad++.exe": "code",
    "androidstudio64.exe": "code",
    # 终端归到「干活」，不算纯写代码
    "windowsterminal.exe": "work",
    "wt.exe": "work",
    "powershell.exe": "work",
    "pwsh.exe": "work",
    "cmd.exe": "work",
    "conhost.exe": "work",
    # 浏览器
    "chrome.exe": "browse",
    "msedge.exe": "browse",
    "firefox.exe": "browse",
    "brave.exe": "browse",
    "opera.exe": "browse",
    "360se.exe": "browse",
    "360chrome.exe": "browse",
    "qqbrowser.exe": "browse",
    # 聊天
    "qq.exe": "chat",
    "wechat.exe": "chat",
    "weixin.exe": "chat",
    "telegram.exe": "chat",
    "discord.exe": "chat",
    "wxwork.exe": "chat",
    "dingtalk.exe": "chat",
    "feishu.exe": "chat",
    # 影音
    "potplayer.exe": "media",
    "potplayermini64.exe": "media",
    "potplayermini.exe": "media",
    "vlc.exe": "media",
    "mpc-hc64.exe": "media",
    "cloudmusic.exe": "media",
    "qqmusic.exe": "media",
    "spotify.exe": "media",
    "bilibili.exe": "media",
    "哔哩哔哩.exe": "media",
    # 游戏
    "steam.exe": "game",
    "steamwebhelper.exe": "game",
    "epicgameslauncher.exe": "game",
    "yuanshen.exe": "game",
    "genshinimpact.exe": "game",
    "league of legends.exe": "game",
    "valorant.exe": "game",
    "gameloop.exe": "game",
    # 文档
    "winword.exe": "office",
    "excel.exe": "office",
    "powerpnt.exe": "office",
    "wps.exe": "office",
    "et.exe": "office",
    "wpp.exe": "office",
    "acrobat.exe": "office",
    "acrord32.exe": "office",
    "sumatrapdf.exe": "office",
    # 设计
    "photoshop.exe": "design",
    "illustrator.exe": "design",
    "figma.exe": "design",
    "blender.exe": "design",
    # 通用工具
    "deepseek harness.exe": "work",
    "workbuddy.exe": "work",
}

CAT_LABEL: dict[str, str] = {
    "code": "写代码",
    "work": "在终端和工具里干活",
    "browse": "浏览网页",
    "chat": "聊天",
    "media": "看视频或听歌",
    "game": "玩游戏",
    "office": "写文档",
    "design": "做设计",
    "other": "用别的程序",
}

# 这些活动长时间连续做，才算「专注」，值得提醒休息
FOCUS_CATS = {"code", "work", "office", "design"}

# 看视频时人就是不动键鼠的。若拿 idle 硬卡，一集 40 分钟的片子会被整段判成「不在电脑前」，
# 所以这几类豁免空闲判定，只靠「窗口还在前台」说明人还在。
IDLE_EXEMPT_CATS = {"media", "game"}

# 资源管理器和桌面不算有效活动，会把全天空闲时间算成「在用电脑」
IGNORED_APPS = {"explorer.exe", "searchhost.exe", "shellexperiencehost.exe",
                "startmenuexperiencehost.exe", "lockapp.exe", "textinputhost.exe"}


def classify(app: str) -> str:
    return CATEGORY_MAP.get(app.lower(), "other")


# 直接按首字母大写会得到 Qq / Msedge / Potplayermini64 这种，所以常用程序手动起名
APP_DISPLAY: dict[str, str] = {
    "code.exe": "VS Code",
    "code - insiders.exe": "VS Code",
    "cursor.exe": "Cursor",
    "devenv.exe": "Visual Studio",
    "idea64.exe": "IDEA",
    "pycharm64.exe": "PyCharm",
    "webstorm64.exe": "WebStorm",
    "goland64.exe": "GoLand",
    "clion64.exe": "CLion",
    "androidstudio64.exe": "Android Studio",
    "sublime_text.exe": "Sublime Text",
    "notepad++.exe": "Notepad++",
    "qq.exe": "QQ",
    "wechat.exe": "微信",
    "weixin.exe": "微信",
    "wxwork.exe": "企业微信",
    "telegram.exe": "Telegram",
    "discord.exe": "Discord",
    "msedge.exe": "Edge",
    "chrome.exe": "Chrome",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "windowsterminal.exe": "终端",
    "wt.exe": "终端",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "cmd.exe": "cmd",
    "potplayer.exe": "PotPlayer",
    "potplayermini64.exe": "PotPlayer",
    "potplayermini.exe": "PotPlayer",
    "vlc.exe": "VLC",
    "cloudmusic.exe": "网易云音乐",
    "qqmusic.exe": "QQ音乐",
    "spotify.exe": "Spotify",
    "bilibili.exe": "哔哩哔哩",
    "steam.exe": "Steam",
    "steamwebhelper.exe": "Steam",
    "winword.exe": "Word",
    "excel.exe": "Excel",
    "powerpnt.exe": "PowerPoint",
    "wps.exe": "WPS",
    "photoshop.exe": "Photoshop",
    "illustrator.exe": "Illustrator",
    "blender.exe": "Blender",
    "explorer.exe": "资源管理器",
    "deepseek harness.exe": "DeepSeek Harness",
}


def friendly_app(app: str) -> str:
    """把 code.exe 变成 VS Code、qq.exe 变成 QQ。"""
    key = app.lower()
    if key in APP_DISPLAY:
        return APP_DISPLAY[key]
    stem = key.rsplit(".exe", 1)[0]
    if not stem:
        return app
    return " ".join(word.capitalize() for word in stem.replace("_", " ").split())


# ---------- 数据结构 ----------


@dataclass(slots=True)
class Segment:
    start: int
    end: int
    app: str
    title: str
    cat: str

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)


@dataclass
class DaySummary:
    day: str
    active_seconds: int = 0
    by_cat: dict[str, int] = field(default_factory=dict)
    top_apps: list[tuple[str, int]] = field(default_factory=list)
    longest_focus: tuple[str, int] = ("", 0)
    first_at: str = ""
    last_at: str = ""
    segment_count: int = 0

    @property
    def active_hours(self) -> float:
        return round(self.active_seconds / 3600, 1)

    def cat_line(self, limit: int = 4) -> str:
        """「写代码 3 小时 20 分、浏览网页 1 小时」这样的句子。"""
        items = sorted(self.by_cat.items(), key=lambda kv: kv[1], reverse=True)
        parts = [
            f"{CAT_LABEL.get(cat, cat)} {_fmt_duration(sec)}"
            for cat, sec in items[:limit]
            if sec >= 60
        ]
        return "、".join(parts)


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours} 小时"
    return f"{hours} 小时 {rest} 分"


def _fmt_clock(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# ---------- 存储 ----------


class ActivityStore:
    def __init__(self, data_dir: Path, retention_days: int = 14) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(1, retention_days)
        self._current: Segment | None = None
        self._last_cleanup = 0.0
        # 一次回合里会连着问「当前/近30分/今天」，共用同一份解析结果
        self._seg_cache_key: tuple | None = None
        self._seg_cache_at: float = 0.0
        self._seg_cache_val: list[Segment] = []

    # ----- 写入 -----

    def _path_for(self, day: date) -> Path:
        return self.data_dir / f"{day.isoformat()}.jsonl"

    def record(self, snap: Snapshot, interval: int) -> None:
        """写一行原始采样。若和前一条同窗口，直接跳过以减少体积。"""
        row = {
            "t": snap.ts,
            "app": snap.app,
            "title": snap.title[:300],
            "idle": round(snap.idle, 1),
        }
        try:
            with self._path_for(date.today()).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[观测] 写入活动记录失败：{e}")

    # ----- 读取 -----

    def load_raw(self, day: date) -> list[dict]:
        path = self._path_for(day)
        if not path.exists():
            return []
        rows: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # 写入被打断时可能有半行，跳过
        except Exception as e:
            logger.warning(f"[观测] 读取活动记录失败：{e}")
        return rows

    def segments(self, day: date, interval: int, idle_timeout: int) -> list[Segment]:
        """把原始采样合并成连续片段。

        合并规则：同一个 app+title，且与上一行间隔不超过 interval*3，视为同一段。
        空闲超过 idle_timeout 的行不计入（人不在电脑前）。
        """
        cache_key = (day, interval, idle_timeout)
        now_ts = time.time()
        if (
            self._seg_cache_key == cache_key
            and now_ts - self._seg_cache_at < 20
        ):
            return self._seg_cache_val

        rows = self.load_raw(day)
        segs: list[Segment] = []

        for row in rows:
            app = str(row.get("app") or "").lower()
            if not app or app in IGNORED_APPS:
                continue

            cat = classify(app)
            if (
                float(row.get("idle") or 0) >= idle_timeout
                and cat not in IDLE_EXEMPT_CATS
            ):
                continue

            ts = int(row.get("t") or 0)
            if ts <= 0:
                continue
            title = str(row.get("title") or "")

            if segs and segs[-1].app == app and segs[-1].title == title:
                if ts - segs[-1].end <= interval * 3:
                    segs[-1].end = ts
                    continue

            segs.append(
                Segment(start=ts, end=ts, app=app, title=title, cat=cat)
            )

        self._seg_cache_key = cache_key
        self._seg_cache_at = now_ts
        self._seg_cache_val = segs
        return segs

    def invalidate_cache(self) -> None:
        """清空片段缓存。删除当天记录后必须调用，否则还会读到旧结果。"""
        self._seg_cache_key = None
        self._seg_cache_at = 0.0
        self._seg_cache_val = []

    def day_summary(
        self, day: date, interval: int, idle_timeout: int
    ) -> DaySummary:
        segs = self.segments(day, interval, idle_timeout)
        summary = DaySummary(day=day.isoformat())
        if not segs:
            return summary

        summary.segment_count = len(segs)
        summary.first_at = _fmt_clock(segs[0].start)
        summary.last_at = _fmt_clock(segs[-1].end)

        by_app: dict[str, int] = {}
        for seg in segs:
            # 每段至少算一个采样间隔，避免高频切换时全部归零
            dur = max(seg.duration, interval)
            summary.by_cat[seg.cat] = summary.by_cat.get(seg.cat, 0) + dur
            by_app[seg.app] = by_app.get(seg.app, 0) + dur

        summary.active_seconds = sum(summary.by_cat.values())
        summary.top_apps = sorted(by_app.items(), key=lambda kv: kv[1], reverse=True)[:5]

        # 最长连续专注：把相邻同类且间隔不大的片段串起来
        best_cat, best_sec = "", 0
        run_cat, run_start, run_end = "", 0, 0
        for seg in segs:
            if seg.cat not in FOCUS_CATS:
                if run_cat and (run_end - run_start) > best_sec:
                    best_cat, best_sec = run_cat, run_end - run_start
                run_cat = ""
                continue
            if run_cat == seg.cat and seg.start - run_end <= 180:
                run_end = seg.end
            else:
                if run_cat and (run_end - run_start) > best_sec:
                    best_cat, best_sec = run_cat, run_end - run_start
                run_cat, run_start, run_end = seg.cat, seg.start, seg.end
        if run_cat and (run_end - run_start) > best_sec:
            best_cat, best_sec = run_cat, run_end - run_start
        summary.longest_focus = (best_cat, best_sec)

        return summary

    def current_segment(
        self, interval: int, idle_timeout: int
    ) -> Segment | None:
        """当前正在用的窗口。

        最后一条记录如果已经是几分钟之前的事了，说明人已经走开（或者刚关机），
        这时必须返回 None —— 否则她会把「三小时前在写代码」当成「现在在写代码」。
        """
        segs = self.segments(date.today(), interval, idle_timeout)
        if not segs:
            return None
        last = segs[-1]
        max_gap = max(interval * 3, 180)
        if time.time() - last.end > max_gap:
            return None
        return last

    def recent_summary(
        self, minutes: int, interval: int, idle_timeout: int
    ) -> DaySummary:
        """只统计最近 N 分钟。"""
        cutoff = int(datetime.now().timestamp()) - minutes * 60
        segs = [
            s for s in self.segments(date.today(), interval, idle_timeout)
            if s.end >= cutoff
        ]
        summary = DaySummary(day=f"最近 {minutes} 分钟")
        if not segs:
            return summary

        for seg in segs:
            dur = max(min(seg.duration, seg.end - cutoff), interval)
            summary.by_cat[seg.cat] = summary.by_cat.get(seg.cat, 0) + dur
        summary.active_seconds = sum(summary.by_cat.values())
        summary.segment_count = len(segs)
        return summary

    def focus_streak_minutes(self, interval: int, idle_timeout: int) -> tuple[str, int]:
        """当前已经连续专注了多久（同一类专注活动，中间不超 3 分钟空档）。"""
        segs = self.segments(date.today(), interval, idle_timeout)
        if not segs:
            return "", 0

        last = segs[-1]
        if last.cat not in FOCUS_CATS:
            return "", 0

        end = last.end
        start = last.start
        for seg in reversed(segs[:-1]):
            if seg.cat != last.cat:
                break
            if start - seg.end > 180:
                break
            start = seg.start
        return last.cat, max(0, (end - start) // 60)

    def cleanup(self) -> None:
        """删掉超过保留期的日文件。"""
        cutoff = date.today() - timedelta(days=self.retention_days)
        for path in self.data_dir.glob("*.jsonl"):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                    logger.info(f"[观测] 清理旧记录 {path.name}")
                except OSError:
                    pass
