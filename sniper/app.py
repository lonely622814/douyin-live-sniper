"""主程序：双击 `启动控制台.cmd` 启动，开一个横版小窗口，后台按你设定的时间出手。

全程只用 JS（通过浏览器调试接口在页面里执行），不碰鼠标、不抢前台：

    进直播间        Page.navigate
    点灯牌          element.click()
    点「去参与」    element.click()      Lynx 面板也认 JS
    点「点亮粉丝星」element.click()      实测 1 毫秒，真能送出去
    点二次确认      element.click()
    刷新陪伴之旅    location.reload()    完整重载，300~600 毫秒

监听也是页面 JS 常驻：注入到直播间页面里，每 80 毫秒查一次"是否开播 / 谁送出为你闪耀"，
一有变化就通过调试协议的 binding 通道主动推给控制台（不走网络，不受 CSP 限制）。
"""

from __future__ import annotations

import argparse
import collections
import os
import pathlib
import re
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone

from . import config, panel, timebase, webflow

ROOT = pathlib.Path(__file__).resolve().parent.parent
BEIJING = timezone(timedelta(hours=8))


class Controller:
    # 整页刷新后抖音要 1~2 秒才渲染出状态，刷得比这更快页面会一直处于加载中，
    # 反而什么都判不出来。所以刷新周期有个下限（秒）。
    MIN_REFRESH_CYCLE = 2.0
    # 内存整理：页面 JS 堆超过这个值、或者刷了这么多轮，就换一个新标签重建渲染进程
    RECYCLE_HEAP_MB = 280.0
    RECYCLE_EVERY_CYCLES = 240

    GIFT_PATTERNS = (
        re.compile(r"([^\s：:×x]{1,18})[：:]?\s*送出了?\s*为你闪耀"),
        re.compile(r"([^\s：:×x]{1,18})\s*送\s*为你闪耀"),
        re.compile(r"为你闪耀\s*[×x]\s*\d+"),
    )

    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.tb: timebase.TimeBase | None = None
        self.flow = webflow.WebFlow(port=cfg.cdp_port, room_url=cfg.room_url)
        self.logs: collections.deque = collections.deque(maxlen=500)
        self.light_state = "-"
        self.first_gift = "-"
        self.lit_value = "-"
        self.room_name = "-"
        self.token = secrets.token_urlsafe(18)
        self.room_live_state: bool | None = None
        self.monitor_installed = False
        self.armed = False
        self.already_done_today = False
        self.like_count = 0
        self.last_heartbeat: float | None = None
        self.monitor_ticks = 0
        self.timings: dict[str, float] = {}
        self._action_lock = threading.Lock()
        self._stop = threading.Event()
        # ── 诊断日志（事后对账用）──
        self._diag_lock = threading.Lock()
        self._diag_cleaned = 0.0
        self.watch_cycles = 0                 # 未开播时刷新的轮次
        self.last_reload_ts = "-"             # 最近一次刷新的时刻（时分秒.毫秒）
        self.last_reload_at: float | None = None
        self.page_challenge = False           # 抖音是否把页面换成了验证码中间页
        self.page_title = "-"
        self.page_heap_mb: float | None = None
        self.watch_on = bool(cfg.watch_enabled)
        self.diag(
            f"===== 启动 ===== 房间={cfg.room_url or '(未设置)'} "
            f"刷新间隔={cfg.refresh_interval:g}s 刷新方式={'强制' if cfg.refresh_mode == 'hard' else '普通'} "
            f"等加载={cfg.refresh_wait_load} 秒抢={cfg.trigger_live_start} 0点={cfg.trigger_midnight} "
            f"演练={cfg.dry_run} 端口={cfg.cdp_port}"
        )
        self.log("控制台已启动（纯 JS 版）")

    # ---------- 日志 / 状态 ----------

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append({"ts": stamp, "msg": message})
        print(f"[{stamp}] {message}")
        # 重要事件同时落盘，重启后也查得到
        if message[:1] in ("★", "✅", "⚠", "⛔"):
            self.diag(message)

    # ---------- 诊断日志 ----------

    @staticmethod
    def diag_dir() -> pathlib.Path:
        path = ROOT / "logs"
        path.mkdir(exist_ok=True)
        return path

    def diag(self, message: str) -> None:
        """写诊断日志：按天一个文件、保留 7 天。

        只记"能事后对账"的东西：每一轮刷新、判定到开播、送出、异常。
        不记页面每一个动作，一天几 MB。
        """
        try:
            now = time.time()
            stamp = time.strftime("%H:%M:%S", time.localtime(now))
            millis = int(now * 1000) % 1000
            path = self.diag_dir() / time.strftime("watch-%Y%m%d.log", time.localtime(now))
            with self._diag_lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{stamp}.{millis:03d} {message}\n")
                if now - self._diag_cleaned > 3600:
                    self._diag_cleaned = now
                    cutoff = now - 7 * 86400
                    for old in self.diag_dir().glob("watch-*.log"):
                        try:
                            if old.stat().st_mtime < cutoff:
                                old.unlink()
                        except Exception:
                            pass
        except Exception:
            pass

    def timing(self, name: str, milliseconds: float) -> None:
        self.timings[name] = round(milliseconds, 1)

    def status(self) -> dict:
        """给界面用的状态。**绝不碰浏览器**，否则浏览器一卡整个界面就死。"""
        now_ns = self.tb.now_ns() if self.tb else None
        countdown, next_trigger = None, "-"
        if now_ns:
            targets = self._targets(now_ns)
            if targets:
                target, reason = targets[0]
                countdown = (target - now_ns) / 1e6
                left = countdown / 1000
                next_trigger = (
                    f"{reason} 还有 {int(left // 3600):02d}:"
                    f"{int(left % 3600 // 60):02d}:{left % 60:06.3f}"
                )
        return {
            "now": timebase.fmt(now_ns) if now_ns else "",
            "countdown_ms": countdown,
            "offset_ms": self.tb.cal.offset_ms if self.tb else None,
            "sigma_ms": self.tb.cal.sigma_ns / 1e6 if self.tb else None,
            "browser": "已连接" if self.flow.session else "-",
            "room": self.room_name,
            "panel": self.light_state,
            "lit_value": self.lit_value,
            "first_gift": self.first_gift,
            "timings": dict(self.timings),
            "monitor": self.monitor_installed,
            "heartbeat_age": (
                round(time.monotonic() - self.last_heartbeat, 1)
                if self.last_heartbeat
                else None
            ),
            "monitor_ticks": self.monitor_ticks,
            "live": self.room_live_state,
            "watching": True,
            "watch_enabled": bool(self.cfg.watch_enabled),
            "refresh_enabled": bool(self.cfg.refresh_enabled),
            "auto_recycle": bool(self.cfg.auto_recycle),
            "live_grab": bool(self.cfg.trigger_live_start),
            "midnight": bool(self.cfg.trigger_midnight),
            "dry_run": bool(self.cfg.dry_run),
            "armed": self.armed,
            "already_done": self.already_done_today,
            "like_enabled": bool(self.cfg.like_enabled),
            "like_mode": self.cfg.like_mode,
            "like_rate": self.cfg.like_rate,
            "like_count": self.like_count,
            "next_trigger": next_trigger,
            "watch_cycles": self.watch_cycles,
            "last_reload_time": self.last_reload_ts,
            "last_reload_age": (
                round(time.monotonic() - self.last_reload_at, 1)
                if self.last_reload_at
                else None
            ),
            "challenge": self.page_challenge,
            "page_title": self.page_title,
            "page_heap_mb": self.page_heap_mb,
            "logs": list(self.logs)[-60:],
        }

    # ---------- 页面 JS 推上来的事件 ----------

    def handle_event(self, payload: dict) -> None:
        kind = (payload or {}).get("kind")
        if kind == "hello":
            self.monitor_installed = True
            if payload.get("live") is not None:
                self.room_live_state = bool(payload.get("live"))
            self.log(
                f"监听已就位（当前{'正在开播' if self.room_live_state else '未开播'}）："
                f"{(payload.get('url') or '')[:60]}"
            )
            return
        if kind == "heartbeat":
            self.last_heartbeat = time.monotonic()
            self.monitor_ticks = int(payload.get("ticks") or 0)
            if payload.get("live") is not None:
                self.room_live_state = bool(payload.get("live"))
            if not self.monitor_installed:
                self.monitor_installed = True
                self.log("监听已就位（收到心跳）")
            return
        if kind == "live":
            live = bool(payload.get("live"))
            self.room_live_state = live
            if live:
                self.log("★ 页面 JS 检测到开播！")
                self.fire_live_grab()
            else:
                self.log("页面 JS 检测到主播下播")
            return
        if kind == "gift":
            text = (payload.get("text") or "").strip()
            if text:
                if self.first_gift == "-":
                    self.first_gift = text
                stamp = timebase.fmt(self.tb.now_ns()) if self.tb else ""
                self.log(f"★ 检测到送礼：{text}   时间 {stamp}")

    # ---------- 时间基准 ----------

    def calibrate(self) -> None:
        self.log("NTP 对时…")
        self.tb = timebase.TimeBase.calibrate(per_server=6, rounds=2)
        self.log(
            f"对时完成：本机时钟偏差 {self.tb.cal.offset_ms:+.1f} ms，"
            f"精度 ±{self.tb.cal.sigma_ns / 1e6:.2f} ms"
        )

    # ---------- 准备 ----------

    def update_room_name(self) -> None:
        try:
            title = self.flow.attach().evaluate("document.title") or ""
            if title:
                self.room_name = title.split("的抖音直播间")[0][:24]
        except Exception:
            pass

    def install_monitor(self) -> bool:
        """确保页面里跑着监听脚本，并且本连接注册好接收回调。

        每次都要走一遍（页面脚本是幂等的），因为"注册回调"是每个连接各自的事。
        """
        try:
            ok = self.flow.install_monitor(self.handle_event)
            self.monitor_installed = bool(ok)
            return bool(ok)
        except Exception:
            return False

    def refresh_state(self) -> str:
        try:
            if not self.flow.accompany_target():
                self.light_state = "陪伴之旅未打开"
                return self.light_state
            state = self.flow.button_state()
            text = state.get("text") or "?"
            self.light_state = f"{'可点' if not state.get('disabled') else '不可点'}（{text}）"
            value = self.flow.lit_value()
            if value is not None:
                self.lit_value = value
        except Exception as exc:
            self.light_state = f"读取失败({exc})"
        return self.light_state

    def prepare(self) -> bool:
        """进直播间 → 开灯牌 → 进陪伴之旅。全程 JS。"""
        if not self.cfg.room_url:
            self.log("还没填直播间地址")
            return False
        started = time.monotonic()
        if not self.flow.goto_room():
            return False
        self.install_monitor()
        self.update_room_name()
        if self.flow.accompany_target():
            cost = (time.monotonic() - started) * 1000
            self.timing("打开并准备", cost)
            self.armed = True
            self.log(f"陪伴之旅已经在待命（{cost:.0f} ms）：{self.refresh_state()}")
            return True
        t_panel = time.monotonic()
        if not self.flow.open_panel():
            self.log("粉丝团面板没打开（确认已登录、且是同一个直播间）")
            return False
        self.timing("开面板", (time.monotonic() - t_panel) * 1000)
        t_journey = time.monotonic()
        if not self.flow.open_accompany():
            self.log("陪伴之旅没打开")
            return False
        self.timing("进陪伴之旅", (time.monotonic() - t_journey) * 1000)
        cost = (time.monotonic() - started) * 1000
        self.timing("打开并准备", cost)
        self.armed = True
        state = self.refresh_state()
        self.log(f"准备完成（{cost:.0f} ms）：{state}")
        # 关键：架好枪的同时立刻判断"今天是不是已经送过了"。
        # 是的话就记住，之后不再反复开陪伴之旅（用户反馈过：送完了还疯狂打开）。
        label = self.flow.button_state().get("text") or ""
        try:
            st = self.flow.button_state()
            if st.get("disabled") and ("已点亮" in label or "已送" in label):
                if not self.already_done_today:
                    self.log(f"发现今天已经送过了（{label}）→ 不再反复架枪")
                self.already_done_today = True
        except Exception:
            pass
        return True

    # ---------- 送出 ----------

    def send_now(self, label: str = "立即送出") -> webflow.SendResult:
        # 先明确识别"今天已经送过了"——这不是失败，是今天额度用完。
        # 按钮会变成禁用的「今日已点亮 / 今日已送出」，文字里带"已"字。
        try:
            state = self.flow.button_state()
            text = (state.get("text") or "")
            if state.get("found") and state.get("disabled") and (
                "已点亮" in text or "已送" in text
            ):
                result = webflow.SendResult(
                    ok=False,
                    kind="already_done",
                    lit_before=self.flow.lit_value(),
                    reason=(
                        f"今天已经送过了（按钮显示「{text}」）——"
                        f"这不是失败，是今天的额度已经用完，要等跨天重置。"
                    ),
                )
                self.already_done_today = True
                self.log(f"ℹ 判定为「今天已送过」：{result.reason}")
                return result
        except Exception:
            pass

        result = self.flow.send_once(label)
        self.already_done_today = result.kind == "already_done"
        if result.lit_after is not None:
            self.lit_value = result.lit_after
        self.refresh_state()
        self.timing("点击送出", result.detail.get("click_ms", 0.0))
        self.timing("确认弹窗", result.detail.get("confirm_ms", 0.0))
        self.timing("校验结果", result.detail.get("verify_ms", 0.0))
        self.timing("送出（含确认+校验）", result.elapsed_ms)
        if result.kind == "already_done":
            self.log(f"ℹ {result.reason}")
        else:
            self.log(("✅ " if result.ok else "⚠ ") + result.reason)
        return result

    # ---------- 时间表 ----------

    def _parse_send_times(self) -> list[tuple[int, int, int]]:
        raw_text = self.cfg.send_times_raw or ""
        for full, half in (
            ("：", ":"), ("，", ","), ("、", ","), ("；", ","), ("．", "."),
            ("０", "0"), ("１", "1"), ("２", "2"), ("３", "3"), ("４", "4"),
            ("５", "5"), ("６", "6"), ("７", "7"), ("８", "8"), ("９", "9"),
        ):
            raw_text = raw_text.replace(full, half)
        out: list[tuple[int, int, int]] = []
        for raw in raw_text.split(","):
            raw = raw.strip().replace(" ", "")
            if not raw:
                continue
            parts = raw.split(":")
            try:
                hour = int(parts[0])
                minute = int(parts[1]) if len(parts) > 1 else 0
                second = int(parts[2]) if len(parts) > 2 else 0
            except ValueError:
                continue
            if 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
                out.append((hour, minute, second))
        return out

    @staticmethod
    def _next_time_ns(now_ns: int, hms: tuple[int, int, int]) -> int:
        stamp = datetime.fromtimestamp(now_ns / 1e9, BEIJING)
        target = stamp.replace(hour=hms[0], minute=hms[1], second=hms[2], microsecond=0)
        value = int(target.timestamp() * 1e9)
        return value if value > now_ns else value + 86_400_000_000_000

    def _targets(self, now_ns: int) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        if self.cfg.trigger_midnight:
            out.append((timebase.next_midnight_ns(now_ns), "0 点首发"))
        for hms in self._parse_send_times():
            out.append(
                (self._next_time_ns(now_ns, hms),
                 f"{hms[0]:02d}:{hms[1]:02d}:{hms[2]:02d} 定时")
            )
        out.sort()
        return out

    # ---------- 首发监听 ----------

    def watch_gifts(self, seconds: float) -> None:
        self.log(f"开始监听首发动向（{seconds:.0f} 秒）…")
        seen: set[str] = set()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                text = self.flow.attach().evaluate(
                    "document.body.innerText.replace(/\\s+/g,' ')"
                ) or ""
                for pattern in self.GIFT_PATTERNS:
                    for match in pattern.finditer(text):
                        item = match.group(0).strip()[:40]
                        if not item or item in seen:
                            continue
                        seen.add(item)
                        if self.first_gift == "-":
                            self.first_gift = item
                        stamp = timebase.fmt(self.tb.now_ns()) if self.tb else ""
                        self.log(f"★ 检测到送礼：{item}   时间 {stamp}")
            except Exception:
                pass
            time.sleep(0.15)
        self.log("首发监听结束")

    # ---------- 开播秒抢 ----------

    def like_worker(self) -> None:
        """点赞线程：按你设置的频率点，可随时开关。"""
        self.log("点赞线程已启动（默认关闭，可在控制台打开）")
        done = 0
        while not self._stop.is_set():
            try:
                if not self.cfg.like_enabled:
                    done = 0
                    time.sleep(0.3)
                    continue
                # 只在主播开播时点赞（没开播点赞没意义）
                if self.room_live_state is False:
                    time.sleep(0.5)
                    continue
                limit = int(self.cfg.like_max or 0)
                if limit and done >= limit:
                    self.cfg.update({"like_enabled": False})
                    self.log(f"点赞完成：已点 {done} 次（达到上限，自动关闭）")
                    continue
                rate = max(0.2, float(self.cfg.like_rate or 1))
                ok = self.flow.like_once(self.cfg.like_mode)
                if ok:
                    done += 1
                    self.like_count = done
                    if done % 10 == 1:
                        self.log(
                            f"点赞中：已点 {done} 次（"
                            f"{'调用JS接口' if self.cfg.like_mode=='js' else '模拟手工点击'}，"
                            f"{rate:.1f} 次/秒）"
                        )
                else:
                    if done == 0:
                        self.log("点赞：找不到点赞按钮，下面把页面里可能的候选列出来：")
                        for c in self.flow.like_diagnose():
                            self.log(
                                f"    候选 {c['tag']} cls={c['cls']!r} txt={c['txt']!r} "
                                f"alt={c['alt']!r} img={c['img']!r} "
                                f"位置({c['x']},{c['y']}) {c['w']}x{c['h']}"
                            )
                        self.log("把这几行发我，我照着改选择器。")
                    time.sleep(1.0)
                time.sleep(1.0 / rate)
            except Exception as exc:
                self.log(f"点赞异常：{exc}")
                time.sleep(1.0)

    def live_watcher(self) -> None:
        """开播守候：**定时强制刷新直播间页面**来确认开播。

        为什么必须刷新：实测抖音的直播间页面停留久了之后不会自己更新
        （主播开播了页面还停在"未开播"的样子），只靠页面 JS 监听会漏掉，
        用户就遇到过"人家开播两分钟了还没检测到"。

        所以这里改成双保险：
          1. 页面 JS 常驻监听（80ms）—— 页面自己更新时能秒抓到
          2. 每 N 秒强制刷新一次页面 —— 页面不更新时靠它兜底
        """
        self.log(
            f"开播守候已启动：页面 JS 每 80ms 自查 + 每 "
            f"{max(0.3, float(self.cfg.refresh_interval or 1.0)):g} 秒刷新页面兜底，"
            f"刷新后立刻高频判定"
        )
        last_house = 0.0
        offline_since = time.monotonic()
        last_report = time.monotonic()
        last_cycle_log = 0.0
        last_reload = time.monotonic()
        empty_streak = 0                 # 页面连续几次没回应
        while not self._stop.is_set():
            try:
                # 整页刷新后，抖音要 1~2 秒才渲染出"直播中"。刷得比这更快，
                # 页面会一直停在加载中、永远判不出来（实测过：间隔 1 秒时会这样）。
                # 所以真正的周期有个下限。
                interval = max(self.MIN_REFRESH_CYCLE,
                               float(self.cfg.refresh_interval or 1.0))
                # 监听总开关关掉：不判定、不刷新，也不去动浏览器
                if not self.cfg.watch_enabled:
                    if self.watch_on:
                        self.watch_on = False
                        self.log("开播监听：已关闭（不守候、不刷新）")
                        self.diag("监听关闭")
                    time.sleep(0.5)
                    continue
                if not self.watch_on:
                    self.watch_on = True
                    self.log("开播监听：已开启，继续守候")
                    self.diag("监听开启")
                # 已经开播：停止刷新，只留低频的收拾工作（装监听、保持架枪）
                if self.room_live_state is True:
                    if time.monotonic() - last_house > 1.0:
                        last_house = time.monotonic()
                        self.housekeeping()
                        self.arm_if_needed()
                    time.sleep(0.4)
                    continue
                if time.monotonic() - last_report > 60:
                    last_report = time.monotonic()
                    waited = (time.monotonic() - offline_since) / 60
                    self.log(
                        f"守候中：已等 {waited:.0f} 分钟（每 {interval:g} 秒刷新一次页面兜底，"
                        f"刷新后立刻高频判定）"
                    )
                    self.diag(
                        f"守候中 已等{waited:.0f}分钟 刷新={'开' if self.cfg.refresh_enabled else '关'} "
                        f"页面{self._sig(self.flow.room_live())} 心跳={self._heartbeat_text()} "
                        f"页面标题={self.page_title}"
                    )

                # ① 页面自己更新时，这里就能抓到（另一条路是页面里那个 80ms 的 JS 监听）
                quick = self.flow.room_live()
                if not quick:
                    # 页面连着几次不给回应：多半是标签被换掉/页面卡死，主动重连一次
                    empty_streak += 1
                    if empty_streak >= 10:
                        empty_streak = 0
                        self.log("页面连着几次没回应，重连调试通道")
                        self.diag("页面无响应，重连 CDP 会话")
                        self.flow.reset_session()
                else:
                    empty_streak = 0
                self._notice_page_state(quick)
                if quick.get("live"):
                    self.diag(f"★ 开播判定（页面自查）页面{self._sig(quick)}")
                    self.on_live_detected("页面自查")
                    continue

                # ② 没到刷新点：短睡继续查。这一小圈不做任何别的 CDP 动作，别浪费时间
                #    页面刷新兜底关掉时，只做页面自查（便宜、不触发风控），永不刷新
                if not self.cfg.refresh_enabled:
                    time.sleep(0.1)
                    continue
                #    注意：一旦被风控换成验证码页，就把节奏放慢，别继续猛刷加重风控
                gap = interval * (4.0 if self.page_challenge else 1.0)
                if time.monotonic() - last_reload < gap:
                    time.sleep(0.07)
                    continue

                # ③ 到点了：刷新。刷新之后的整个间隔都用来判定（这就是本轮的"等待期"），
                #    页面一旦渲染出"直播中"，最多 70ms 后就被抓住。
                t_reload = time.monotonic()
                last_reload = t_reload          # 间隔从"刷新这一刻"开始算，周期=间隔
                self.watch_cycles += 1
                cycle_no = self.watch_cycles
                self.last_reload_at = t_reload
                self.last_reload_ts = (
                    time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
                )
                try:
                    self.flow.set_reload_mode(self.cfg.refresh_mode == "hard")
                    self.flow.reload_page()
                except Exception:
                    self.flow.attach()
                self.monitor_installed = False      # 页面重载了，注入的脚本没了
                found = None
                info_last: dict = {}
                if self.cfg.refresh_wait_load:
                    # 勾了"等页面加载完再计时"：先等到页面能回应，间隔从那一刻开始算
                    while (time.monotonic() < t_reload + 12.0
                           and not self._stop.is_set()):
                        info = self.flow.room_live()
                        info_last = info
                        if info.get("live"):
                            found = (time.monotonic() - t_reload) * 1000
                            break
                        if info:                      # 页面活了
                            last_reload = time.monotonic()
                            break
                        time.sleep(0.07)
                else:
                    # 不等待：刷新之后的整个间隔都用来判定，页面一渲染出直播中就抓住
                    deadline = t_reload + interval
                    while time.monotonic() < deadline and not self._stop.is_set():
                        info_last = self.flow.room_live()
                        self._notice_page_state(info_last)
                        if info_last.get("live"):
                            found = (time.monotonic() - t_reload) * 1000
                            break
                        time.sleep(0.07)
                if found is not None:
                    self.diag(
                        f"★ 开播判定 #{cycle_no} 刷新{self.last_reload_ts} "
                        f"耗时{found:.0f}ms 页面{self._sig(info_last)}"
                    )
                    self.on_live_detected(f"刷新后 {found:.0f} ms 判定到")
                    continue

                # 这轮没开播：收拾浏览器（重装监听、确认还在目标直播间）
                cost = (time.monotonic() - t_reload) * 1000
                self.housekeeping()
                # 每一轮都落一行：这就是事后对账的"心跳"，能看出程序当时在不在干活
                self.diag(
                    f"#{cycle_no} 刷新{self.last_reload_ts} 耗时{cost:.0f}ms "
                    f"页面{self._sig(info_last)} 心跳={self._heartbeat_text()} "
                    f"查询{self.monitor_ticks} → 未开播"
                )
                # 每 5 轮记一次页面内存，方便事后看"刷新久了是不是越来越占内存"
                if cycle_no % 5 == 0:
                    self._log_page_metrics(cycle_no)
                    self.maybe_recycle_tab()
                # 每 2 秒就刷一轮，日志不能每轮都写，不然会淹没运行日志
                if time.monotonic() - last_cycle_log > 60:
                    last_cycle_log = time.monotonic()
                    self.log(
                        f"刷新一轮未见开播：刷新→判定 {cost:.0f} ms（{interval:g} 秒后再刷）"
                    )

            except Exception as exc:
                self.log(f"监听守卫异常：{exc}")
            # 这里的等待决定了"最坏情况多久才能发现开播"：原来是无条件 2 秒，
            # 加上刷新本身和后面的收拾动作，一轮能拖到 5~10 秒。改成几乎不睡。
            time.sleep(0.05)
        # 线程退出也要留痕：日志突然断在这里，就说明是程序停了而不是页面没更新
        self.diag(f"守候线程退出（共刷新 {self.watch_cycles} 轮）")

    @staticmethod
    def _sig(info: dict) -> str:
        """把页面读到的原始信号压成一行——事后能区分"抖音页面没更新"还是"选择器没抓到"。"""
        if not info:
            return "[页面还没响应]"
        return (
            f"[未开播={int(bool(info.get('offline')))} "
            f"礼物栏={int(bool(info.get('hasGift')))} "
            f"视频={int(bool(info.get('playing')))}]"
        )

    def _heartbeat_text(self) -> str:
        """页面 JS 监听的心跳：它是"程序到底在不在监听"的直接证据。"""
        if not self.last_heartbeat:
            return "无(页面JS未上报)"
        return f"{time.monotonic() - self.last_heartbeat:.1f}s前"

    def _notice_page_state(self, info: dict) -> None:
        """识别抖音的风控页面（验证码/中间页）。

        被换成验证码页之后，页面里根本没有直播间，程序会一直判成"未开播"，
        用户却以为程序在正常守候。所以必须单独认出来 + 明显提示 + 自动降频。
        """
        if not info:
            return
        title = str(info.get("title") or "").strip()
        if title and title != self.page_title:
            self.page_title = title
        challenge = bool(info.get("challenge"))
        if challenge == self.page_challenge:
            return
        self.page_challenge = challenge
        if challenge:
            self.log(
                f"⚠ 抖音把页面换成了「{title or '验证码页'}」：现在判不出开播，"
                f"请到直播间那个 Chrome 窗口手动过一次验证（刷新节奏已自动放慢）"
            )
            self.diag(f"⚠ 风控页面：title={title or '空'} 已自动降频，等人工过验证")
        else:
            self.log("页面恢复正常，继续守候")
            self.diag(f"页面恢复正常：title={title or '空'}")

    def _log_page_metrics(self, cycle_no: int) -> None:
        """把页面自身的内存写进诊断日志——用来看"刷久了是不是越来越占内存"。"""
        m = self.flow.page_metrics()
        if not m:
            return
        self.page_heap_mb = m.get("heap_mb")
        self.diag(
            f"内存 第{cycle_no}轮 页面JS堆={m.get('heap_mb')}MB "
            f"DOM节点={m.get('nodes')} 文档={m.get('docs')}"
        )

    def _seconds_to_next_trigger(self) -> float | None:
        """离下一个定时出手还有多少秒（没有定时、或时钟没标定时返回 None）。"""
        if not self.tb:
            return None
        targets = self._targets(self.tb.now_ns())
        if not targets:
            return None
        return (targets[0][0] - self.tb.now_ns()) / 1e9

    def maybe_recycle_tab(self) -> None:
        """内存整理：条件合适时换掉整个标签页，把渲染进程连同内存一起重建。

        换标签有代价（新标签要加载几秒，而且会丢掉"已架好"状态），所以只在
        ①开了刷新兜底（否则本来就不会刷、也不会涨）
        ②没在播、没架枪（此刻没有需要保住的状态）
        ③离下一个定时出手还有 5 分钟以上（别在要紧关头动手）
        ④页面内存超过阈值，或者已经刷了 240 轮（约 10~20 分钟）时 才换。
        """
        if (not self.cfg.refresh_enabled or not self.cfg.auto_recycle
                or self.room_live_state or self.armed):
            return
        left = self._seconds_to_next_trigger()
        if left is not None and left < 300:
            return
        heap = self.page_heap_mb or 0.0
        cycles_since = self.watch_cycles - getattr(self, "_last_recycle_cycle", 0)
        if heap < self.RECYCLE_HEAP_MB and cycles_since < self.RECYCLE_EVERY_CYCLES:
            return
        self._last_recycle_cycle = self.watch_cycles
        self.log(f"内存整理：换一个新标签接管直播间（当前页面堆 {heap:.0f} MB，"
                 f"已刷 {cycles_since} 轮）")
        result = self.flow.recycle_room_tab()
        self.monitor_installed = False          # 新页面里还没有监听脚本
        if result.get("ok"):
            self.log(f"内存整理完成，新标签已就绪（{result.get('waited', 0)*1000:.0f} ms）")
            self.diag(f"换标签成功 换前堆={heap:.0f}MB 已刷{cycles_since}轮 "
                      f"新标签就绪{result.get('waited', 0)*1000:.0f}ms")
        else:
            self.log(f"内存整理失败：{result.get('note') or '未知原因'}")
            self.diag(f"换标签失败：{result.get('note') or '未知原因'}")

    def on_live_detected(self, how: str) -> None:
        """判定到开播：停止刷新（状态一变 True，主循环就不再刷），并按需触发秒抢。

        同一次开播只触发一次（20 秒内不重复），避免页面状态抖动时反复出手。
        """
        if self.room_live_state is True:
            return
        self.room_live_state = True
        self.log(f"★ 检测到主播已开播（{how}）！已停止刷新")
        self.fire_live_grab()

    def fire_live_grab(self) -> None:
        """开播秒抢的统一入口。

        20 秒内只触发一次（页面状态抖动时会反复报 live，不能每次都起线程）；
        同时清掉已经结束的线程引用，别让线程对象一直堆着。
        """
        if self.already_done_today:
            self.log("（今天已经送过了，不再送出）")
            return
        if not self.cfg.trigger_live_start:
            self.log("（开播秒抢未开启，只记录不动作）")
            return
        if time.monotonic() - getattr(self, "_last_grab_at", 0.0) < 20:
            return
        self._last_grab_at = time.monotonic()
        alive = [t for t in getattr(self, "_grab_threads", []) if t.is_alive()]
        thread = threading.Thread(target=self.live_grab, daemon=True, name="live-grab")
        thread.start()
        alive.append(thread)
        self._grab_threads = alive

    def housekeeping(self) -> None:
        """让浏览器停在目标直播间、页面里装着监听脚本。低频调用。"""
        try:
            if not self.flow.ensure_browser():
                return
            if self.cfg.room_url:
                self.flow.goto_room()
                if not self.room_name:
                    self.update_room_name()
            # 心跳断了说明注入的脚本随页面一起丢了，重新装一次
            if self.last_heartbeat and time.monotonic() - self.last_heartbeat > 5:
                self.monitor_installed = False
            if not self.monitor_installed:
                self.install_monitor()
            self.armed = bool(self.flow.accompany_target())
        except Exception:
            pass

    def arm_if_needed(self) -> None:
        """主播在播时提前把陪伴之旅架好，省掉开播瞬间进房的那一两秒。"""
        if not self.cfg.trigger_live_start or self.armed:
            return
        if self.already_done_today:
            if not getattr(self, "_arm_skip_logged", False):
                self._arm_skip_logged = True
                self.log(
                    "今天已经送过了，不架枪也不送。"
                    "（开了定时、或 0 点前 90 秒时会自动开始架枪）"
                )
            return
        if time.monotonic() - getattr(self, "_last_prearm", 0.0) <= 60:
            return
        self._last_prearm = time.monotonic()
        self.log("提前架枪：打开陪伴之旅待命")
        if self._action_lock.acquire(timeout=5):
            try:
                self.prepare()
            finally:
                self._action_lock.release()

    def live_grab(self) -> None:
        """开播秒抢：检测到开播后尽可能快地把礼物送出去。"""
        if not self._action_lock.acquire(timeout=60):
            self.log("开播秒抢：有别的操作在占用，跳过这一次")
            return
        try:
            for attempt in range(3):
                started = time.monotonic()
                if self.flow.accompany_target():
                    # 提速关键：开播不是跨天，页面状态通常还有效。
                    # 先看按钮能不能点，能点就**跳过刷新**直接出手（省 300~600 毫秒）。
                    state = self.flow.button_state()
                    if state.get("found") and not state.get("disabled"):
                        self.log("开播秒抢：按钮已就绪，跳过刷新，直接出手")
                        self.timing("刷新今日状态", 0.0)
                    else:
                        self.log("开播秒抢：按钮不可用，刷新后出手")
                        cost = self.flow.refresh()
                        self.timing("刷新今日状态", max(cost, 0))
                else:
                    self.log("开播秒抢：还没架枪，现在进房 + 进陪伴之旅")
                    if not self.prepare():
                        self.log(f"开播秒抢：第 {attempt+1} 轮准备失败，重试")
                        time.sleep(0.4)
                        continue
                ready = time.monotonic()
                self.log(f"开播秒抢：准备就绪用了 {(ready-started)*1000:.0f} ms，开始出手")
                result = self.send_now("开播首发")
                self.log(
                    f"开播秒抢：从检测到开播到送出共 {(time.monotonic()-started)*1000:.0f} ms"
                )
                if result.ok or result.lit_before is not None:
                    return
                time.sleep(0.3)
            self.log("开播秒抢：连续 3 轮都没成功")
        finally:
            self._action_lock.release()

    # ---------- 定时调度 ----------

    def scheduler(self) -> None:
        done: dict[str, set[str]] = {}
        last_calibrate = 0.0
        last_state_read = 0.0
        last_monitor_check = 0.0

        while not self._stop.is_set():
            try:
                if self.tb is None:
                    self.calibrate()
                    last_calibrate = time.monotonic()
                    continue
                if time.monotonic() - last_calibrate > 900:
                    self.calibrate()
                    last_calibrate = time.monotonic()
                if time.monotonic() - last_state_read > 20:
                    last_state_read = time.monotonic()
                    if self.flow.accompany_target():
                        self.refresh_state()
                if time.monotonic() - last_monitor_check > 5:
                    last_monitor_check = time.monotonic()
                    if self.flow.session is not None:
                        self.install_monitor()

                targets = self._targets(self.tb.now_ns())
                if not targets:
                    time.sleep(0.3)
                    continue

                now_ns = self.tb.now_ns()
                target, reason = targets[0]
                left = (target - now_ns) / 1e9
                key = f"t-{target}"
                phase = done.setdefault(key, set())

                if left <= self.cfg.prewarm_lead_s and "warm" not in phase:
                    phase.add("warm")
                    self.log(f"距【{reason}】{left:.0f} 秒，开始准备")
                    self.prepare()

                if left <= 15 and "prefresh" not in phase:
                    phase.add("prefresh")
                    if self.flow.accompany_target():
                        self.flow.refresh()
                    threading.Thread(
                        target=self.watch_gifts, args=(150.0,), daemon=True,
                        name="gift-watch",
                    ).start()

                if left <= self.cfg.final_calibrate_s and "cal" not in phase:
                    phase.add("cal")
                    self.log(f"距【{reason}】{left:.1f} 秒，最后一次对时")
                    self.calibrate()
                    last_calibrate = time.monotonic()

                if 0 < left <= 0.6 and "fire" not in phase:
                    phase.add("fire")
                    from . import clock

                    clock.sleep_until_mono(self.tb.mono_at(target), spin_ns=1_200_000)
                    fired = time.monotonic()
                    if not self.flow.accompany_target():
                        self.prepare()
                    # 只有跨天那次需要刷新（状态按天变）；白天定时不用刷新，省 300~600ms
                    if "0 点" in reason:
                        self.log("到点了，先刷新陪伴之旅（跨天要重新拉当天状态）")
                        cost = self.flow.refresh()
                        self.timing("刷新今日状态", max(cost, 0))
                    self.log(f"距目标时刻 {(time.monotonic()-fired)*1000:.0f} ms，开始送出")
                    self.send_now(reason)
                    self.log(f"本次流程总耗时 {(time.monotonic()-fired)*1000:.0f} ms")
                    time.sleep(1.5)

                time.sleep(0.1)
            except Exception as exc:
                self.log(f"调度异常：{exc}")
                time.sleep(2.0)

    # ---------- 控制台动作 ----------

    def run_action(self, name: str, payload: dict | None = None) -> dict:
        data = payload or {}
        if not self._action_lock.acquire(timeout=90):
            return {"ok": False, "note": "上一个操作还没结束，稍等一下"}
        try:
            if name == "detect_room":
                room = self.detect_room()
                if room:
                    self.cfg.update({"room_url": room})
                    self.flow.room_url = room
                    self.log(f"已识别当前直播间：{room}")
                    return {"ok": True, "note": room}
                return {"ok": False, "note": "没找到已打开的抖音直播间"}
            if name == "calibrate":
                self.calibrate()
                return {"ok": True}
            if name in ("prepare", "arm"):
                ok = self.prepare()
                return {"ok": ok, "note": "已准备好" if ok else "准备失败，看日志"}
            if name == "state":
                if not self.flow.ensure_browser():
                    return {"ok": False, "note": "浏览器没启动"}
                return {"ok": True, "note": self.refresh_state()}
            if name == "refresh_accompany":
                if not self.flow.ensure_browser():
                    return {"ok": False, "note": "浏览器没启动"}
                if not self.flow.accompany_target() and not self.prepare():
                    return {"ok": False, "note": "陪伴之旅没打开"}
                cost = self.flow.refresh()
                self.refresh_state()
                if cost >= 0:
                    self.timing("刷新今日状态", cost)
                return {"ok": cost >= 0,
                        "note": f"已刷新（{cost:.0f} ms）" if cost >= 0 else "刷新失败"}
            if name == "send_now":
                if not self.flow.ensure_browser():
                    return {"ok": False, "note": "浏览器没启动"}
                if not self.flow.accompany_target() and not self.prepare():
                    return {"ok": False, "note": "准备失败"}
                result = self.send_now("立即送出")
                return {"ok": result.ok, "note": result.reason}
            if name == "dry_run_test":
                if not self.flow.ensure_browser():
                    return {"ok": False, "note": "浏览器没启动"}
                t0 = time.monotonic()
                if not self.prepare():
                    return {"ok": False, "note": "准备失败"}
                ready = time.monotonic()
                cost = self.flow.refresh()
                self.timing("刷新今日状态", max(cost, 0))
                state = self.flow.button_state()
                self.log(
                    f"演练完成：准备 {(ready-t0)*1000:.0f} ms，刷新 {cost:.0f} ms，"
                    f"按钮「{state.get('text')}」，注入值 {self.flow.lit_value()}（没有真的点）"
                )
                return {"ok": bool(state.get("found")),
                        "note": "全程走通，演练模式没有送出"}
            if name == "toggle_live_grab":
                new_value = not self.cfg.trigger_live_start
                self.cfg.update({"trigger_live_start": new_value})
                self.log(
                    "开播秒抢：✅ 已开启，主播一开播就立刻出手" if new_value
                    else "开播秒抢：已关闭"
                )
                return {"ok": True, "note": "已开启" if new_value else "已关闭"}
            if name == "set_refresh_mode":
                mode = str(data.get("mode") or self.cfg.refresh_mode)
                self.cfg.update({"refresh_mode": mode})
                self.log(f"刷新方式：{'强制刷新（等同 Ctrl+F5）' if mode=='hard' else '普通刷新（等同 F5）'}")
                return {"ok": True, "note": mode}
            if name == "save_room":
                url = (self.cfg.room_url or "").strip()
                if not url:
                    return {"ok": False, "note": "还没填直播间地址"}
                note = str(data.get("note") or "").strip()
                rooms = [r for r in (self.cfg.rooms or []) if r.get("url") != url]
                rooms.insert(0, {"url": url, "note": note})
                self.cfg.update({"rooms": rooms[:30]})
                self.log(f"已保存直播间到历史：{note or url}")
                return {"ok": True, "note": f"已保存（共 {len(rooms[:30])} 个）"}
            if name == "open_logs":
                # 打开诊断日志文件夹，出问题时直接把里面的文件发出来
                path = self.diag_dir()
                try:
                    os.startfile(str(path))      # noqa: S606 - Windows 桌面程序
                    return {"ok": True, "note": f"已打开 {path.name}"}
                except Exception as exc:
                    return {"ok": False, "note": f"打不开：{exc}"}
            if name == "probe_like":
                self.log("开始探测点赞接口：点一下屏幕，抓期间的网络请求…")
                r = self.flow.probe_like()
                if not r.get("ok"):
                    self.log("探测：点击没成功（找不到视频区域？）")
                    return {"ok": False, "note": "点击没成功"}
                reqs = r.get("requests") or []
                if not reqs:
                    self.log("探测：没抓到相关请求（可能点赞是走长连接 WebSocket 的）")
                    return {"ok": True, "note": "没抓到 HTTP 请求，可能走长连接"}
                self.log(f"探测到 {len(reqs)} 条相关请求，最有可能是点赞接口的：")
                for u in reqs:
                    self.log(f"    {u[:150]}")
                self.log("把这几行发我，我照着做精准的接口点赞。")
                return {"ok": True, "note": f"抓到 {len(reqs)} 条，看日志"}
            return {"ok": False, "note": f"未知动作 {name}"}
        except Exception as exc:
            self.log(f"动作 {name} 出错：{exc}")
            return {"ok": False, "note": str(exc)}
        finally:
            self._action_lock.release()

    def detect_room(self) -> str:
        try:
            if not self.flow.ensure_browser():
                return ""
            url = self.flow.attach().evaluate("location.href") or ""
            if "live.douyin.com" in url or "/follow/live/" in url:
                return url.split("?")[0]
        except Exception:
            pass
        return ""


def open_panel_window(
    url: str, width: int = 1040, height: int = 760, room_url: str = ""
) -> None:
    """把控制台开成横版小窗口，并且和直播间**在同一个窗口里**（不同标签）。

    两个关键点：

    1. 必须在我们自己那个带调试端口的 Chrome 实例里开。
       早期用 ``chrome.exe --app=... --user-data-dir=同一个目录`` 单独启动，
       结果这个没带调试端口的进程成了主实例，之后程序再也连不上浏览器（踩过这个大坑）。
    2. ``newWindow: False`` 会把新标签放进当前窗口，这样你只看得到一个窗口，
       不会再有"两个页面分不清哪个能用"的问题。控制台标签放前台、直播间在后台。
    """
    import json
    import urllib.request

    from . import browser as browser_mod
    from . import cdp as cdp_mod

    port = browser_mod.DEFAULT_PORT
    last_error = ""
    for _attempt in range(6):
        session = None
        try:
            if not browser_mod.is_running(port):
                # 关键：用**直播间地址**启动浏览器。
                # 如果拿控制台地址启动，就会先多开一个控制台窗口，
                # 之后直播间又在另一个窗口打开 —— 变成两个窗口（用户反馈过）。
                browser_mod.launch(
                    room_url or url, ROOT / "chrome-profile", port=port
                )
                browser_mod.wait_for_port(port, timeout=25)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=8
            ) as resp:
                ws_url = json.loads(resp.read().decode("utf-8"))["webSocketDebuggerUrl"]

            session = cdp_mod.CDP(ws_url, timeout=12)
            # 等直播间页面出现（最多 30 秒），确认它已经在某个窗口里，
            # 这样控制台才能作为"同一窗口的新标签"加进去。
            room = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                infos = session.call("Target.getTargets").get("targetInfos", [])
                for item in infos:
                    target_url = item.get("url") or ""
                    if item.get("type") == "page" and (
                        "live.douyin.com" in target_url
                        or "/follow/live/" in target_url
                    ):
                        room = item
                        break
                if room:
                    break
                time.sleep(0.5)

            # 注意：尺寸参数只在"新开窗口"时才允许，同窗口开标签时带上会被拒绝
            # （报错 Target position can only be set for new windows）
            params: dict = {"url": url, "newWindow": room is None}
            if room is None:
                params["width"] = width
                params["height"] = height
            created = session.call("Target.createTarget", params)
            target_id = created.get("targetId")
            if target_id:
                try:
                    session.call("Target.activateTarget", {"targetId": target_id})
                except Exception:
                    pass
                try:
                    info = session.call(
                        "Browser.getWindowForTarget", {"targetId": target_id}
                    )
                    window_id = info.get("windowId")
                    if window_id:
                        session.call(
                            "Browser.setWindowBounds",
                            {"windowId": window_id,
                             "bounds": {"left": 80, "top": 60,
                                        "width": width, "height": height,
                                        "windowState": "normal"}},
                        )
                except Exception:
                    pass
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1.5)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
    print(f"[提示] 控制台小窗口没开成（{last_error}），改用系统浏览器打开。")
    webbrowser.open(url)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音为你闪耀抢送工具（纯 JS 版）")
    parser.add_argument("--config", default=str(config.DEFAULT_PATH))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    cfg = config.Config.load(pathlib.Path(args.config))
    controller = Controller(cfg)
    server = panel.make_server(controller, cfg.panel_port)
    url = f"http://127.0.0.1:{cfg.panel_port}/"

    print("=" * 64)
    print("为你闪耀 · 抢送控制台（纯 JS 版）")
    print("=" * 64)
    print(f"  控制台地址：{url}")
    print(f"  演练模式  ：{'开（不会真的发送）' if cfg.dry_run else '关（会真的发送）'}")
    print("  按 Ctrl+C 退出")
    print("=" * 64)

    if not args.no_browser:
        threading.Timer(
            0.7, lambda: open_panel_window(url, room_url=cfg.room_url)
        ).start()
    threading.Thread(target=controller.scheduler, daemon=True, name="scheduler").start()
    # 监听线程**始终**启动：界面上的"正在监听"和主播状态都靠它。
    # 是否真的在开播瞬间抢，由「主播一开播就秒抢」开关决定。
    threading.Thread(target=controller.live_watcher, daemon=True, name="live-watch").start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
        controller._stop.set()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
