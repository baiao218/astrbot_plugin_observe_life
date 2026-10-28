"""观测生活 —— 让她知道你在电脑上做什么。

三个出口：
  1. 每轮对话悄悄附带「她此刻看到的」（注入用户消息的附加块，不动 system_prompt，
     这样 DeepSeek 的前缀缓存不会被破坏）
  2. 长时间专注 / 深夜还在用电脑时，她可能主动说一句
  3. 把今天的时间痕迹写成 today_brief.txt，日记插件会读它

采集只走本机 Win32 API，不发往任何地方，也不截图。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.collector import IS_WINDOWS, sample
from .core.store import (
    CAT_LABEL,
    FOCUS_CATS,
    ActivityStore,
    _fmt_clock,
    _fmt_duration,
    friendly_app,
)
from .core.summarize import (
    _label_of,
    build_diary_material,
    build_injection,
    care_signal,
)

PLUGIN_NAME = "astrbot_plugin_observe_life"

CARE_PROMPT = """{scene}

你想主动说一句话。要求：
1. 只说一句，不超过 30 个字，口语，像随手发的一句，不像通知。
2. 绝对不要报数字、时长、「我注意到」「连续」「已经」这类词。
3. 不要问「你在干嘛」「还在忙吗」——你本来就知道。
4. 不要说教，不要「记得休息哦」这种客服语气。
5. 可以带一点点你自己的情绪（心疼、不满、理直气壮），但不要煽情。
6. 只输出那句话本身，不要加引号、不要解释、不要写动作描写。

现在是 {clock}。"""


class ObserveLifePlugin(Star):
    """观测日常：应用与窗口、专注时长、作息。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.settings = config

        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.interval = max(15, int(config.get("sample_interval") or 45))
        self.idle_timeout = max(60, int(config.get("idle_timeout") or 600))

        self.store = ActivityStore(
            self.data_dir, int(config.get("retention_days") or 14)
        )

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._paused = False
        self._care_state_path = self.data_dir / "care_state.json"
        self._brief_path = self.data_dir / "today_brief.txt"
        self._sample_count = 0
        self._last_sample_at: float = 0.0

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        if not IS_WINDOWS:
            logger.warning("[观测] 当前不是 Windows，采集功能不可用")
            return
        if not bool(self.settings.get("enable", True)):
            logger.info("[观测] 已在配置中关闭，不启动")
            return

        self._running = True
        self._tasks.append(asyncio.create_task(self._sample_loop()))
        if bool(self.settings.get("care_enable", True)):
            self._tasks.append(asyncio.create_task(self._care_loop()))

        logger.info(
            f"[观测] 已启动：每 {self.interval} 秒采样一次，"
            f"空闲 {self.idle_timeout} 秒判定为不在电脑前，"
            f"数据目录 {self.data_dir}"
        )

    async def terminate(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.info("[观测] 已停止")

    # ---------- 采集循环 ----------

    async def _sample_loop(self) -> None:
        while self._running:
            try:
                if not self._paused:
                    snap = sample()
                    if snap is not None:
                        self.store.record(snap, self.interval)
                        self._sample_count += 1
                        self._last_sample_at = datetime.now().timestamp()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[观测] 采样出错：{e}")

            # 每 10 次采样刷新一次给日记用的摘要，顺便清理过期文件
            if self._sample_count and self._sample_count % 10 == 0:
                try:
                    self._write_brief()
                    self.store.cleanup()
                except Exception as e:
                    logger.warning(f"[观测] 刷新摘要失败：{e}")

            await asyncio.sleep(self.interval)

    def _write_brief(self) -> None:
        """把今天的活动摘要写到 today_brief.txt，供日记插件读取。"""
        material = build_diary_material(self.store, self.interval, self.idle_timeout)
        self._brief_path.write_text(material or "", encoding="utf-8")

    # ---------- 对话注入 ----------

    @filter.on_llm_request()
    async def inject_observation(self, event: AstrMessageEvent, req: Any) -> None:
        if self._paused or not bool(self.settings.get("inject_enable", True)):
            return
        try:
            block = build_injection(self.store, self.interval, self.idle_timeout)
        except Exception as e:
            logger.warning(f"[观测] 生成注入块失败：{e}")
            return
        if not block:
            return

        max_chars = int(self.settings.get("inject_max_chars") or 400)
        if len(block) > max_chars:
            block = block[:max_chars].rstrip()

        # 挂在用户消息的附加块上，而不是 system_prompt：
        # system_prompt 一变，DeepSeek 的前缀缓存整段失效；附加块在末尾，前面仍能命中。
        req.extra_user_content_parts.append(TextPart(text=block))

    # ---------- 主动关心 ----------

    async def _care_loop(self) -> None:
        await asyncio.sleep(120)  # 启动后先等两分钟，别一开机就说话
        check_every = max(60, int(self.settings.get("care_check_interval") or 300))

        while self._running:
            try:
                if not self._paused:
                    await self._maybe_care()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[观测] 主动关心出错：{e}")
            await asyncio.sleep(check_every)

    def _load_care_state(self) -> dict:
        try:
            return json.loads(self._care_state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_care_state(self, state: dict) -> None:
        try:
            self._care_state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[观测] 保存关心状态失败：{e}")

    def _care_allowed(self) -> bool:
        """每天有次数上限，两次之间要隔够时间。"""
        state = self._load_care_state()
        today = date.today().isoformat()
        if state.get("date") != today:
            state = {"date": today, "count": 0, "last_ts": 0, "late_done": False}
            self._save_care_state(state)

        limit = max(1, int(self.settings.get("care_daily_limit") or 2))
        if int(state.get("count") or 0) >= limit:
            return False

        gap = max(1, int(self.settings.get("care_min_gap_hours") or 4)) * 3600
        last = float(state.get("last_ts") or 0)
        if last and datetime.now().timestamp() - last < gap:
            return False
        return True

    async def _maybe_care(self) -> None:
        if not bool(self.settings.get("care_enable", True)):
            return

        signal = care_signal(
            self.store,
            self.interval,
            self.idle_timeout,
            int(self.settings.get("care_focus_minutes") or 90),
            int(self.settings.get("care_late_hour") or 1),
        )
        if signal is None:
            return

        kind, scene = signal

        state = self._load_care_state()
        # 深夜那句一晚只要一次
        if kind == "late" and state.get("late_done"):
            return
        if not self._care_allowed():
            return

        umo = str(self.settings.get("care_umo") or "").strip()
        if not umo:
            logger.info("[观测] 未配置 care_umo，跳过主动关心")
            return

        text = await self._compose_care(scene)
        if not text:
            return

        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            logger.info(f"[观测] 主动关心（{kind}）：{text}")
        except Exception as e:
            logger.warning(f"[观测] 发送失败：{e}")
            return

        state = self._load_care_state()
        state["date"] = date.today().isoformat()
        state["count"] = int(state.get("count") or 0) + 1
        state["last_ts"] = int(datetime.now().timestamp())
        if kind == "late":
            state["late_done"] = True
        self._save_care_state(state)

    async def _compose_care(self, scene: str) -> str:
        """让她用自己的口吻生成这一句。失败时返回空串（宁可不发）。"""
        persona_prompt = await self._resolve_persona()

        provider = None
        try:
            provider = self.context.get_using_provider()
        except Exception:
            pass
        if provider is None:
            return ""

        task = CARE_PROMPT.format(
            scene=scene, clock=datetime.now().strftime("%H:%M")
        )
        system_prompt = f"{persona_prompt}\n\n{task}" if persona_prompt else task

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    system_prompt=system_prompt, prompt="说那句话。"
                ),
                timeout=45,
            )
        except Exception as e:
            logger.warning(f"[观测] 生成关心语失败：{e}")
            return ""

        text = str(getattr(resp, "completion_text", "") or "").strip()
        text = text.strip("\"'“”「」\n ")
        # 太长说明模型没听指令，宁可丢掉也不要发一段说教
        if not text or len(text) > 80:
            return ""
        return text

    async def _resolve_persona(self) -> str:
        mgr = getattr(self.context, "persona_manager", None)
        if mgr is None:
            return ""

        wanted = str(self.settings.get("persona_id") or "").strip()
        if not wanted:
            # 没配就跟随「她现在是谁」
            try:
                cfg = self.context.get_config()
                wanted = str(
                    cfg.get("agent_runner", {})
                    .get("config", {})
                    .get("persona", {})
                    .get("persona_id")
                    or ""
                )
            except Exception:
                wanted = ""

        persona = None
        if wanted:
            try:
                persona = mgr.get_persona_v3_by_id(wanted)
            except Exception:
                persona = None
        if not persona:
            try:
                persona = await mgr.get_default_persona_v3()
            except Exception:
                return ""
        if not persona:
            return ""
        return str(persona.get("prompt") or "").strip()

    # ---------- 命令 ----------

    @filter.command("观测状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看观测插件状态与今天的情况。"""
        today = self.store.day_summary(
            date.today(), self.interval, self.idle_timeout
        )
        current = self.store.current_segment(self.interval, self.idle_timeout)

        lines = ["【观测状态】"]
        lines.append("采集：" + ("已暂停" if self._paused else "进行中"))
        lines.append(f"采样间隔：{self.interval} 秒，本次已采 {self._sample_count} 次")
        lines.append(f"空闲判定：{self.idle_timeout} 秒")

        if current is None:
            lines.append("当前：他不在电脑前，或看不到窗口")
        else:
            lines.append(f"当前：{_label_of(current)}")

        if today.active_seconds >= 60:
            lines.append(
                f"今天：在电脑前 {today.active_hours} 小时"
                f"（{today.first_at}~{today.last_at}，{today.segment_count} 段）"
            )
            cat_line = today.cat_line(limit=4)
            if cat_line:
                lines.append(f"分布：{cat_line}")
            focus_cat, focus_sec = today.longest_focus
            if focus_sec >= 600:
                lines.append(
                    f"最长连续：{CAT_LABEL.get(focus_cat, focus_cat)} "
                    f"{_fmt_duration(focus_sec)}"
                )
        else:
            lines.append("今天：还没有足够数据")

        if today.top_apps:
            apps = "、".join(
                f"{friendly_app(a)}({_fmt_duration(s)})"
                for a, s in today.top_apps[:3]
                if s >= 60
            )
            if apps:
                lines.append(f"常用：{apps}")

        yield event.plain_result("\n".join(lines))

    @filter.command("观测今天")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_today(self, event: AstrMessageEvent):
        """按时间顺序列出今天切换过的窗口。"""
        segs = self.store.segments(date.today(), self.interval, self.idle_timeout)
        if not segs:
            yield event.plain_result("今天还没有采集到记录。")
            return

        # 只留 2 分钟以上的片段，否则噪音太多
        keep = [s for s in segs if s.duration >= 120][-25:]
        if not keep:
            yield event.plain_result("今天的片段都很短，凑不出有意义的时间线。")
            return

        lines = ["【今天的时间线】"]
        for seg in keep:
            lines.append(
                f"{_fmt_clock(seg.start)} {_fmt_duration(seg.duration)}  "
                f"{_label_of(seg)}"
            )
        total = sum(max(s.duration, self.interval) for s in segs)
        lines.append(f"—— 合计 {_fmt_duration(total)}，{len(segs)} 段")
        yield event.plain_result("\n".join(lines))

    @filter.command("观测暂停")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_pause(self, event: AstrMessageEvent):
        """暂停采集（不再记录任何窗口）。"""
        self._paused = True
        logger.info("[观测] 已暂停采集")
        yield event.plain_result("观测已暂停，现在不记录任何窗口。发「观测继续」恢复。")

    @filter.command("观测继续")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_resume(self, event: AstrMessageEvent):
        """恢复采集。"""
        self._paused = False
        logger.info("[观测] 已恢复采集")
        yield event.plain_result("观测已恢复。")

    @filter.command("观测清除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_clear(self, event: AstrMessageEvent):
        """删除今天的记录。"""
        path = self.data_dir / f"{date.today().isoformat()}.jsonl"
        removed = 0
        if path.exists():
            try:
                path.unlink()
                removed = 1
            except OSError as e:
                yield event.plain_result(f"删除失败：{e}")
                return
        self._seg_cache_reset()
        yield event.plain_result(
            "今天的记录已删除。" if removed else "今天本来就没有记录。"
        )

    def _seg_cache_reset(self) -> None:
        """删掉记录后让片段缓存失效，免得状态里还显示已删的数据。"""
        self.store.invalidate_cache()
