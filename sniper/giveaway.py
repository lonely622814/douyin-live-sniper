"""挂机抢福袋 / 红包（网页版，纯 DOM + JS，不用 OCR、不用坐标、不用安卓设备）。

实测拿到的结构（2026-09-18，抖音网页版直播间）：

    左上角是一个"活动槽位"，谁有活动就挂谁：
      <div class="Tn9oybzT ... __biz">
        ├─ 福袋：<img src=".../media/lottery_new.xxxx.png">   （父容器文字 = 倒计时 09:15）
        └─ 红包：<div class="redpacket">…</div>

    点开后的面板：
      #short_touch_land_lottery_land_userMain  （外层遮罩）
        #lottery_close_cotainer                （面板本体，官方 id 拼错了 cotainer）
          .vUHz9XGY  7075人已参加
          .zpfDzjWY  09:15   倒计时
          .CDXK22C2  总10钻 / .efZu_YOi 1个福袋
          .bEsKy6LB  参与条件
            .NXpPiZXN 发送评论：xxx     .KCMgJYJ8 未达成/已达成
          [role=button] 一键发评论参与福袋

设计原则（和"送礼"模块一致）：
* 一切用页面 JS（element.click()），不点坐标、不截图、不用 OCR。
* 花钱类条件（灯牌/粉丝团/送礼物）默认拒绝，开关放开才做。
* 评论类条件按开关执行；关注类条件按开关自动点关注并记录名单。
* 每一步失败只记录、不抛异常，挂机线程永远不因为一次失败而死。
"""

from __future__ import annotations

import json
import random
import re
import time

PANEL_SEL = ("#short_touch_land_lottery_land_userMain, #lottery_close_cotainer, "
             "[id*='lottery_close'], [id*='lottery'][class]")

# ── 找入口：左上角活动槽位里现在挂的是福袋还是红包 ──
JS_FIND = r"""
(function () {
  function box(el) { if (!el) return null; var r = el.getBoundingClientRect();
    return {w: Math.round(r.width), h: Math.round(r.height),
            x: Math.round(r.left), y: Math.round(r.top)}; }
  var bag = document.querySelector('img[src*="lottery"]');
  var rp = document.querySelector('div.redpacket');
  var cd = '';
  if (bag && bag.parentElement) cd = (bag.parentElement.innerText || '').trim();
  var rpText = rp ? (rp.innerText || '').trim() : '';
  return {
    bag: !!bag, bagBox: box(bag),
    redpacket: !!rp, rpBox: box(rp), rpText: rpText.slice(0, 60),
    countdown: /^\d{1,2}:\d{2}$/.test(cd) ? cd : '',
    title: (document.title || '').slice(0, 40),
    url: location.href.slice(0, 90)
  };
})()
"""

# ── 点入口：逐级往上点，哪一级能打开面板就用哪一级 ──
#    注意：__LEVEL__ 是占位符，替换后必须是"调用"，不能只替换参数名（踩过：变成 function(1) 直接语法错误）
JS_CLICK_LEVEL = r"""
(function (level) {
  var bag = document.querySelector('img[src*="lottery"]');
  if (!bag) return {ok: false, why: 'no-icon'};
  var el = bag;
  for (var i = 0; i < level && el.parentElement; i++) el = el.parentElement;
  try { el.click(); } catch (e) { return {ok: false, why: String(e).slice(0, 60)}; }
  return {ok: true, cls: String(el.className || '').slice(0, 50),
          w: Math.round(el.getBoundingClientRect().width)};
})(__LEVEL__)
"""

# ── 入口图标的屏幕坐标（JS 点击无效时，改用真实鼠标事件） ──
JS_BAG_CENTER = r"""
(function () {
  var bag = document.querySelector('img[src*="lottery"]');
  if (!bag) return {ok: false, why: 'no-icon'};
  var r = bag.getBoundingClientRect();
  if (r.width < 4 || r.height < 4) return {ok: false, why: 'icon-invisible'};
  return {ok: true, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
})()
"""

# ── 读面板：人数 / 倒计时 / 奖品 / 参与条件 / 按钮文案 ──
JS_READ_PANEL = r"""
(function () {
  var root = document.querySelector('#lottery_close_cotainer')
          || document.querySelector('#short_touch_land_lottery_land_userMain');
  if (!root) return {open: false};
  var q = function (sel) { return root.querySelector(sel); };
  var t = function (sel) { var e = q(sel); return e ? (e.innerText || '').trim() : ''; };
  var btn = root.querySelector('[role="button"]');
  if (!btn) {
    var cands = [].slice.call(root.querySelectorAll('div,button,span'))
      .filter(function (d) { return d.children.length === 0 &&
        /参与|领取|立即|一键/.test(d.innerText || ''); });
    btn = cands.length ? cands[cands.length - 1] : null;
  }
  var conds = [];
  [].slice.call(root.querySelectorAll('.NXpPiZXN, .mvB8rsOL > div')).forEach(function (e) {
    var s = (e.innerText || '').trim();
    if (s) conds.push(s.slice(0, 60));
  });
  var done = [];
  [].slice.call(root.querySelectorAll('.KCMgJYJ8')).forEach(function (e) {
    var s = (e.innerText || '').trim();
    if (s) done.push(s.slice(0, 20));
  });
  // 按钮所在容器的所有文字（按钮文案变化时也能读到）
  var allText = (root.innerText || '').replace(/\s+/g, ' ').slice(0, 400);
  return {
    open: true,
    people: t('.vUHz9XGY'),
    countdown: t('.zpfDzjWY'),
    prize: t('.CDXK22C2'),
    bags: t('.efZu_YOi'),
    conditions: conds,
    condState: done,
    button: btn ? (btn.innerText || '').trim().slice(0, 40) : '',
    buttonCls: btn ? String(btn.className || '').slice(0, 60) : '',
    joined: /已参与|等待开奖|已领取|已提交/.test(allText),
    allText: allText
  };
})()
"""

# ── 点"参与"按钮 ──
JS_JOIN = r"""
(function () {
  var root = document.querySelector('#lottery_close_cotainer')
          || document.querySelector('#short_touch_land_lottery_land_userMain');
  if (!root) return {ok: false, why: 'no-panel'};
  var btn = root.querySelector('[role="button"]');
  if (!btn) return {ok: false, why: 'no-button'};
  var text = (btn.innerText || '').trim();
  try { btn.click(); } catch (e) { return {ok: false, why: String(e).slice(0, 60)}; }
  return {ok: true, text: text.slice(0, 40), cls: String(btn.className || '').slice(0, 60)};
})()
"""

# ── 关面板（点右上角那个 × ，失败就按 ESC） ──
JS_CLOSE = r"""
(function () {
  var root = document.querySelector('#lottery_close_cotainer')
          || document.querySelector('#short_touch_land_lottery_land_userMain');
  if (!root) return {ok: true, why: 'already-closed'};
  var svg = root.querySelector('svg');
  if (svg) { try { (svg.parentElement || svg).click(); } catch (e) {} }
  return {ok: true};
})()
"""

# ── 发一条评论（养号用，也是评论类条件的等价动作） ──
JS_SEND_COMMENT = r"""
(function (text) {
  var box = document.querySelector('[data-e2e="danmaku-input"] textarea, textarea[placeholder*="说点什么"],'
        + ' input[placeholder*="说点什么"], .webcast-chatroom___input textarea');
  if (!box) {
    var cands = [].slice.call(document.querySelectorAll('textarea,input'))
      .filter(function (e) { var r = e.getBoundingClientRect(); return r.width > 80 && r.height > 12; });
    box = cands.length ? cands[0] : null;
  }
  if (!box) return {ok: false, why: 'no-input'};
  box.focus();
  var proto = box.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(box, text);
  box.dispatchEvent(new Event('input', {bubbles: true}));
  box.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  box.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return {ok: true};
})()
"""

# ── 关注主播 ──
JS_FOLLOW = r"""
(function () {
  var btn = document.querySelector('[data-e2e="live-followbutton"]');
  if (!btn) return {ok: false, why: 'no-follow-button'};
  var text = (btn.innerText || '').trim();
  if (!/关注/.test(text) || /已关注/.test(text)) return {ok: true, skipped: true, text: text};
  try { btn.click(); } catch (e) { return {ok: false, why: String(e).slice(0, 60)}; }
  return {ok: true, text: text};
})()
"""


def parse_countdown(text: str) -> int | None:
    """把 09:15 这样的倒计时转成秒；读不到就返回 None。"""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


class GiveawayBot:
    """一次 tick 就是一轮：看入口 → 判定 → 开面板 → 参与 → 记录。"""

    def __init__(self, flow, cfg, log, diag=None, timing=None, record_path=None):
        self.flow = flow
        self.cfg = cfg
        self.log = log
        self.diag = diag or (lambda _m: None)
        self.timing = timing or (lambda _n, _ms: None)
        self.record_path = record_path        # None = 写默认的 logs/福袋红包记录.log
        self.session = None

        # ── 统计 ──
        self.joined_total = 0
        self.failed_total = 0
        self.today = time.strftime("%Y-%m-%d")
        self.joined_today = 0
        self.room_name = "-"
        self.countdown = "-"
        self.people = "-"
        self.last_result = "-"
        self.last_action_at = 0.0
        self._comment_times: list[float] = []      # 评论时间戳（全局限流用）
        self._room_comment: dict[str, list[float]] = {}   # 每房间评论时间戳
        self._seen_bag: set[str] = set()           # 已处理过的福袋（房间+倒计时）避免重复参与
        self._recent_attempt: dict[str, float] = {}  # 最近尝试过的时间（失败后冷却，别猛点）
        self.follow_log = []                       # 自动关注记录（阶段 4 会落盘）

    # ---------- 基础 ----------

    def _evaluate(self, expr: str):
        try:
            if self.session is None:
                self.session = self.flow.attach()
            return self.session.evaluate(expr)
        except Exception:
            self.session = None            # 会话坏了，下次重连
            return None

    def _eval_with_arg(self, expr: str, arg: str):
        """带字符串参数的表达式（避免拼接转义问题）。"""
        return self._evaluate(f"(function(){{ return {expr}({json.dumps(arg, ensure_ascii=False)}); }})()")

    def find(self) -> dict:
        """看左上角槽位现在挂的是什么。"""
        res = self._evaluate(JS_FIND)
        return res if isinstance(res, dict) else {}

    # ---------- 面板 ----------

    def open_panel(self) -> dict:
        """点开福袋面板。

        两轮尝试：
          ① JS 逐级点（图标本身 → 父1 → 父2 → 父3），每次最多等 1.2 秒；
          ② 还打不开就用**真实鼠标事件**（CDP 派发，属于"可信事件"，
             有些组件只认真人手势）。
        """
        for level in (0, 1, 2, 3):
            clicked = self._evaluate(JS_CLICK_LEVEL.replace("__LEVEL__", str(level)))
            if not isinstance(clicked, dict) or not clicked.get("ok"):
                why = (clicked or {}).get("why", "click-failed")
                if why == "no-icon":
                    return {"ok": False, "why": "no-icon（福袋刚好结束了）"}
                continue
            for _ in range(5):                      # 最多等 1.2 秒
                time.sleep(0.25)
                panel = self.read_panel()
                if panel.get("open"):
                    return {"ok": True, "level": level, "panel": panel, "how": "JS点击"}

        # ② 真实鼠标事件兜底
        center = self._evaluate(JS_BAG_CENTER)
        if isinstance(center, dict) and center.get("ok") and self.session is not None:
            x, y = center["x"], center["y"]
            self.diag(f"JS 点击没打开面板，改用真实鼠标事件 ({x},{y})")
            try:
                self.session.call("Input.dispatchMouseEvent",
                                  {"type": "mousePressed", "x": x, "y": y,
                                   "button": "left", "clickCount": 1})
                time.sleep(0.06)
                self.session.call("Input.dispatchMouseEvent",
                                  {"type": "mouseReleased", "x": x, "y": y,
                                   "button": "left", "clickCount": 1})
            except Exception as exc:
                self.diag(f"真实鼠标事件失败：{exc}")
            for _ in range(8):
                time.sleep(0.25)
                panel = self.read_panel()
                if panel.get("open"):
                    return {"ok": True, "level": -1, "panel": panel, "how": "真实鼠标"}
        return {"ok": False, "why": "panel-not-opened"}

    def read_panel(self) -> dict:
        res = self._evaluate(JS_READ_PANEL)
        return res if isinstance(res, dict) else {"open": False}

    def close_panel(self) -> None:
        self._evaluate(JS_CLOSE)
        time.sleep(0.3)

    def join(self) -> dict:
        res = self._evaluate(JS_JOIN)
        return res if isinstance(res, dict) else {"ok": False}

    # ---------- 规则判定 ----------

    def cost_condition(self, panel: dict) -> str:
        """面板里有没有"花钱"类条件（灯牌/粉丝团/送礼物/钻石）。返回命中的那句。"""
        text = " ".join(panel.get("conditions") or []) + " " + (panel.get("allText") or "")
        for kw in ("灯牌", "粉丝团", "送礼物", "送个", "钻石礼物", "付费", "开通"):
            if kw in text:
                return kw
        return ""

    def need_follow(self, panel: dict) -> bool:
        text = " ".join(panel.get("conditions") or []) + " " + (panel.get("allText") or "")
        return "关注" in text and "已关注" not in text

    def need_comment(self, panel: dict) -> bool:
        text = " ".join(panel.get("conditions") or []) + " " + (panel.get("button") or "")
        return "评论" in text

    # ---------- 频控 ----------

    def _allow_comment(self, room: str) -> bool:
        now = time.time()
        hour = 3600
        self._comment_times = [t for t in self._comment_times if now - t < hour]
        if len(self._comment_times) >= int(self.cfg.giveaway_comment_per_hour or 20):
            return False
        per_room = [t for t in self._room_comment.get(room, []) if now - t < hour]
        self._room_comment[room] = per_room
        if len(per_room) >= int(self.cfg.giveaway_comment_per_room_hour or 3):
            return False
        return True

    def _mark_comment(self, room: str) -> None:
        now = time.time()
        self._comment_times.append(now)
        self._room_comment.setdefault(room, []).append(now)

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.today:
            self.today = today
            self.joined_today = 0
            self.log(f"进入新的一天，今日参与计数已清零（昨天共参与 {self.joined_today} 个）")

    # ---------- 主循环 ----------

    def tick(self) -> dict:
        """跑一轮。返回这一轮的结果，给上层记日志/状态用。"""
        self._roll_day()
        info = self.find()
        if not info:
            return {"state": "no-page"}
        self.room_name = info.get("title") or self.room_name
        self.countdown = info.get("countdown") or "-"

        if not info.get("bag"):
            # 没有福袋：红包留给阶段 3，这里只报告状态
            return {"state": "no-bag", "redpacket": bool(info.get("redpacket")),
                    "countdown": info.get("countdown", "")}

        left = parse_countdown(info.get("countdown") or "")
        if left is not None and left < int(self.cfg.giveaway_min_left_s or 20):
            return {"state": "too-late", "left": left}
        if left is not None and left > int(self.cfg.giveaway_max_left_s or 600):
            return {"state": "too-long", "left": left}

        key = f"{info.get('url','')}|{info.get('countdown','')}"
        if key in self._seen_bag:
            return {"state": "already-handled"}
        if time.time() - self._recent_attempt.get(key, 0.0) < 30:
            return {"state": "cooldown"}

        # 看到福袋立刻出声，别让人以为程序没反应
        if self.last_result != f"发现福袋 {info.get('countdown','')}":
            self.log(f"👀 发现福袋：{self.room_name} · 倒计时 {info.get('countdown') or '?'}"
                     f"（{left if left is not None else '?'} 秒后开奖），正在点开面板…")
            self.last_result = f"发现福袋 {info.get('countdown','')}"

        # 开面板
        opened = self.open_panel()
        if not opened.get("ok"):
            self.failed_total += 1
            self._recent_attempt[key] = time.time()
            why = opened.get("why")
            self.log(f"⚠ 福袋面板没打开（{why}）：{self.room_name}"
                     + ("  ← 通常是福袋刚好结束了" if "no-icon" in str(why) else ""))
            self.diag(f"福袋面板没打开：{why} 房间={self.room_name}")
            return {"state": "panel-failed", "why": why}

        panel = opened.get("panel") or {}
        self.people = panel.get("people") or "-"
        self.countdown = panel.get("countdown") or self.countdown
        prize = f"{panel.get('prize','')} {panel.get('bags','')}".strip()

        # 花钱类条件：默认拒绝
        cost = self.cost_condition(panel)
        if cost and not self.cfg.giveaway_allow_lamp:
            self._seen_bag.add(key)
            self.close_panel()
            self.last_result = f"跳过（条件要花钱：{cost}）"
            self.log(f"福袋跳过：条件涉及「{cost}」，按设置不参与（{self.room_name}）")
            self.diag(f"福袋跳过 花钱条件={cost} 房间={self.room_name} 奖品={prize}")
            return {"state": "skip-cost", "cost": cost}

        # 关注类条件：按开关自动关注
        if self.need_follow(panel) and self.cfg.follow_auto:
            res = self._evaluate(JS_FOLLOW)
            if isinstance(res, dict) and res.get("ok") and not res.get("skipped"):
                self.log(f"福袋条件要关注，已自动关注主播（{self.room_name}）")
                self.follow_log.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "room": self.room_name, "url": info.get("url", ""),
                                        "why": "福袋参与条件"})
                self.diag(f"自动关注 {self.room_name}")
                time.sleep(1.2)

        # 评论类条件：频控 + 点"一键发评论参与福袋"
        if self.need_comment(panel):
            room_key = info.get("url", "")
            if not self.cfg.giveaway_auto_comment:
                self.close_panel()
                self.last_result = "跳过（需要发评论，但开关关着）"
                return {"state": "skip-comment-disabled"}
            if not self._allow_comment(room_key):
                self.close_panel()
                self.last_result = "跳过（评论太频繁，限流中）"
                self.diag("福袋跳过：评论频控命中")
                return {"state": "skip-comment-rate"}

        # 参与
        t0 = time.monotonic()
        joined = self.join()
        cost_ms = (time.monotonic() - t0) * 1000
        self.timing("福袋参与", cost_ms)
        if not joined.get("ok"):
            self.failed_total += 1
            self._recent_attempt[key] = time.time()
            self.close_panel()
            self.diag(f"点参与失败：{joined.get('why')}")
            return {"state": "join-failed", "why": joined.get("why")}

        # ★ 关键：点完必须回头确认，不能"点了就算成功"（否则会骗人）
        time.sleep(1.5)
        after = self.read_panel()
        cond_done = any("已" in str(s) for s in (after.get("condState") or []))
        btn_gone = "参与" not in (after.get("button") or "参与")
        verified = bool(after.get("joined") or cond_done or btn_gone or not after.get("open"))
        if not verified:
            self.failed_total += 1
            self._recent_attempt[key] = time.time()
            self.close_panel()
            self.last_result = "点了但没确认到参与"
            self.log(f"⚠ 福袋点了但没确认到参与（可能没生效）：{self.room_name} · "
                     f"点击的按钮「{joined.get('text')}」· 面板仍显示「{after.get('button') or ''}」"
                     f"{after.get('condState') or ''}")
            self.diag(f"福袋参与未确认 房间={self.room_name} 点后按钮={after.get('button')} "
                      f"条件={after.get('condState')}")
            return {"state": "join-unverified"}

        self._seen_bag.add(key)
        self._recent_attempt[key] = time.time()
        self.joined_total += 1
        self.joined_today += 1
        if self.need_comment(panel):
            self._mark_comment(info.get("url", ""))
        self.last_action_at = time.time()
        self.last_result = f"已参与（{joined.get('text') or '参与'}）"
        self.log(f"✅ 福袋已参与：{self.room_name} · {prize or '未知奖品'} · "
                 f"{self.people} · 倒计时 {panel.get('countdown') or '-'} · "
                 f"动作「{joined.get('text') or ''}」({cost_ms:.0f} ms)")
        self.diag(f"福袋参与 房间={self.room_name} 奖品={prize} 人数={self.people} "
                  f"按钮={joined.get('text')} 今日第 {self.joined_today} 个")
        self._record(f"参与成功|{self.room_name}|{prize}|{panel.get('countdown','')}|"
                     f"{joined.get('text','')}")

        time.sleep(1.5)
        self.close_panel()
        return {"state": "joined", "prize": prize}

    # ---------- 记录 ----------

    def _record(self, line: str) -> None:
        """把结果写进 logs/福袋红包记录.log（阶段 4 会加中奖统计）。"""
        try:
            import pathlib

            if self.record_path:
                path = pathlib.Path(self.record_path)
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                root = pathlib.Path(__file__).resolve().parent.parent
                path = root / "logs" / "福袋红包记录.log"
                path.parent.mkdir(exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}|{line}\n")
        except Exception:
            pass

    def summary(self) -> dict:
        return {
            "room": self.room_name,
            "countdown": self.countdown,
            "people": self.people,
            "joined_total": self.joined_total,
            "joined_today": self.joined_today,
            "failed_total": self.failed_total,
            "last_result": self.last_result,
            "follow_pending": len(self.follow_log),
        }
