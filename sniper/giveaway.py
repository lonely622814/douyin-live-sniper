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
  // ★ 福袋入口的两种形态（实测都遇到过）：
  //   ① 图标是 CDN 图片：img[src*="lottery"]
  //   ② 图标是内嵌 base64 图片（换了一版渲染）：槽位 div[class*="__biz"] 有尺寸 + 里面是倒计时文字
  //   所以优先看图，找不到就看"有尺寸且带倒计时的活动槽位"。
  function bagEntry() {
    var byImg = document.querySelector('img[src*="lottery"]');
    if (byImg) return byImg;
    var slots = document.querySelectorAll('div[class*="__biz"]');
    for (var i = 0; i < slots.length; i++) {
      var e = slots[i], r = e.getBoundingClientRect();
      var t = (e.innerText || '').trim();
      if (r.width > 10 && r.height > 10 && /^\d{1,2}:\d{2}$/.test(t)) return e;
    }
    return null;
  }
  function redpacketEntry() {
    var list = document.querySelectorAll('div[class*="redpacket"]');
    for (var i = 0; i < list.length; i++) {
      var r = list[i].getBoundingClientRect();
      if (r.width > 10 && r.height > 10) return list[i];
    }
    return null;
  }
  var bag = bagEntry();
  var rp = redpacketEntry();
  var cd = '';
  if (bag) cd = (bag.innerText || (bag.parentElement || {}).innerText || '').trim();
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
  function bagEntry() {
    var byImg = document.querySelector('img[src*="lottery"]');
    if (byImg) return byImg;
    var slots = document.querySelectorAll('div[class*="__biz"]');
    for (var i = 0; i < slots.length; i++) {
      var e = slots[i], r = e.getBoundingClientRect();
      var t = (e.innerText || '').trim();
      if (r.width > 10 && r.height > 10 && /^\d{1,2}:\d{2}$/.test(t)) return e;
    }
    return null;
  }
  var bag = bagEntry();
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
  function bagEntry() {
    var byImg = document.querySelector('img[src*="lottery"]');
    if (byImg) return byImg;
    var slots = document.querySelectorAll('div[class*="__biz"]');
    for (var i = 0; i < slots.length; i++) {
      var e = slots[i], r = e.getBoundingClientRect();
      var t = (e.innerText || '').trim();
      if (r.width > 10 && r.height > 10 && /^\d{1,2}:\d{2}$/.test(t)) return e;
    }
    return null;
  }
  var bag = bagEntry();
  if (!bag) return {ok: false, why: 'no-icon'};
  var r = bag.getBoundingClientRect();
  if (r.width < 4 || r.height < 4) return {ok: false, why: 'icon-invisible'};
  return {ok: true, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
})()
"""

# ── 读面板：**按文字读**（类名是哈希的，每次渲染都可能变，不能依赖） ──
JS_READ_PANEL = r"""
(function () {
  var root = document.querySelector('#lottery_close_cotainer')
          || document.querySelector('[id*="lottery_close"]')
          || document.querySelector('#short_touch_land_lottery_land_userMain');
  if (!root) return {open: false};
  var txt = (root.innerText || '').replace(/\s+/g, ' ').trim();
  function leaf(re) {
    var hits = [].slice.call(root.querySelectorAll('*')).filter(function (e) {
      var t = (e.innerText || '').trim();
      return t && t.length < 60 && e.children.length === 0 && re.test(t);
    });
    return hits.length ? (hits[0].innerText || '').trim().slice(0, 50) : '';
  }
  // 参与条件：优先取"发送评论：xxx / 关注主播 / 灯牌"这种具体条件，
  // 只有"参与条件"这四个字的标题不算条件（踩过：只读到标题就没法判断要不要花钱）
  function condText() {
    var cands = [].slice.call(root.querySelectorAll('*')).filter(function (e) {
      var t = (e.innerText || '').trim();
      return t && t.length < 60 && e.children.length === 0 &&
             /发送评论|评论：|关注主播|灯牌|粉丝团|分享/.test(t);
    });
    if (cands.length) return (cands[0].innerText || '').trim().slice(0, 50);
    return '';
  }
  var mPeople = txt.match(/(\d[\d,]*)\s*人(?:已)?参与/);
  var mPrize = txt.match(/总\s*([\d,]+)\s*钻/);
  var mBags = txt.match(/(\d+)\s*个福袋/);
  var mCd = txt.match(/(\d{1,2}:\d{2})/);
  // 参与按钮：文字短、块头大（>=120x30）的那个
  var btn = null;
  [].slice.call(root.querySelectorAll('div,button,[role="button"]')).forEach(function (e) {
    if (btn) return;
    var t = (e.innerText || '').trim(), r = e.getBoundingClientRect();
    if (t && t.length <= 14 && r.width >= 120 && r.height >= 28 &&
        /参与|领取|立即|一键|关注|已参与|等待|开奖|任务/.test(t)) btn = e;
  });
  return {
    open: true,
    people: mPeople ? (mPeople[1] + '人已参与') : '',
    countdown: mCd ? mCd[1] : '',
    prize: mPrize ? ('总' + mPrize[1] + '钻') : leaf(/钻|个福袋|奖品/),
    bags: mBags ? (mBags[1] + '个福袋') : '',
    conditions: [condText()].filter(Boolean),
    condState: /已参与|已达成|已领取/.test(txt) ? ['已达成'] :
               (/未达成|未完成/.test(txt) ? ['未达成'] : []),
    button: btn ? (btn.innerText || '').trim().slice(0, 40) : '',
    buttonCls: btn ? String(btn.className || '').slice(0, 60) : '',
    joined: /已参与|等待开奖|已领取|已达成|已提交/.test(txt),
    allText: txt.slice(0, 400)
  };
})()
"""

# ── 找"参与"按钮的屏幕坐标（真实鼠标点击用） ──
JS_BUTTON_CENTER = r"""
(function () {
  var root = document.querySelector('#lottery_close_cotainer')
          || document.querySelector('[id*="lottery_close"]');
  if (!root) return {ok: false, why: 'no-panel'};
  var txt = (root.innerText || '').replace(/\s+/g, ' ');
  if (/已参与|等待开奖|已领取|已达成/.test(txt)) return {ok: false, why: 'already-joined'};
  var btn = null;
  [].slice.call(root.querySelectorAll('div,button,[role="button"]')).forEach(function (e) {
    if (btn) return;
    var t = (e.innerText || '').trim(), r = e.getBoundingClientRect();
    if (t && t.length <= 14 && r.width >= 120 && r.height >= 28 &&
        /参与|领取|立即|一键|关注|任务/.test(t)) btn = e;
  });
  if (!btn) return {ok: false, why: 'no-button'};
  var r = btn.getBoundingClientRect();
  return {ok: true, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
          text: (btn.innerText || '').trim().slice(0, 40)};
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

# ── 关面板：先找真正的"×"；找不到就返回屏幕坐标，由 Python 用真实鼠标点 ──
JS_CLOSE_TARGET = r"""
(function () {
  var root = document.querySelector('#lottery_close_cotainer')
          || document.querySelector('[id*="lottery_close"]')
          || document.querySelector('#short_touch_land_lottery_land_userMain');
  if (!root) return {open: false, ok: false, why: 'already-closed'};
  function R(e) { var r = e.getBoundingClientRect();
    return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
            w: Math.round(r.width), h: Math.round(r.height)}; }
  // ① 类名里带 close 的
  var cand = root.querySelector('[class*="close"],[class*="Close"],[aria-label*="关闭"]');
  if (cand) { var b = R(cand); if (b.w > 0) return {open: true, ok: true, how: 'close类名', x: b.x, y: b.y}; }
  // ② 面板里第一个 svg（老版本是 svg 的 × ）
  var svg = root.querySelector('svg');
  if (svg) { var b2 = R(svg); if (b2.w > 4) return {open: true, ok: true, how: 'svg', x: b2.x, y: b2.y}; }
  // ③ 面板右上角那块小方块的坐标（新版没有 svg，× 就在右上角）
  var r = root.getBoundingClientRect();
  return {open: true, ok: true, how: '右上角推算', x: Math.round(r.left + r.width - 26),
          y: Math.round(r.top + 26)};
})()
"""

# ── 老实现（点 svg）保留兜底 ──
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


# 开奖结果面板的文案（新版：没抽中福袋 送你一个好运气~ 查看幸运观众 知道了）
RE_LOSE = r"没抽中|未抽中|没有抽中|未中奖|没有中奖|很遗憾|下次再来"
RE_WIN = r"恭喜|中奖|获得|抽中"
RE_RESULT_PANEL = r"没抽中|未抽中|没有抽中|未中奖|很遗憾|恭喜|中奖|幸运观众|知道了|查看幸运"


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
        # 当前这个福袋的状态（换新福袋时重置）。
        # 注意：**不能拿倒计时当身份**——倒计时每秒都在变，那样每次都会以为是新福袋，
        # 就会出现"反复开面板又关掉"的鬼畜行为（用户反馈过）。
        self._bag: dict = {}
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

        实测结论（2026-09-19）：福袋入口**只认真人鼠标**，element.click() 不管用。
        所以先派发真实鼠标事件；万一失败，再退回 JS 逐级点。
        """
        # ① 真实鼠标事件（可信事件）
        center = self._evaluate(JS_BAG_CENTER)
        if isinstance(center, dict) and center.get("ok"):
            for attempt in range(2):
                self._real_click(center["x"], center["y"])
                for _ in range(8):                  # 最多等 2 秒
                    time.sleep(0.25)
                    panel = self.read_panel()
                    if panel.get("open"):
                        return {"ok": True, "level": -1, "panel": panel, "how": "真实鼠标"}
                time.sleep(0.3)
        elif isinstance(center, dict) and center.get("why") == "no-icon":
            return {"ok": False, "why": "no-icon（入口图标不见了）"}

        # ② JS 逐级点兜底
        for level in (0, 1, 2, 3):
            clicked = self._evaluate(JS_CLICK_LEVEL.replace("__LEVEL__", str(level)))
            if not isinstance(clicked, dict) or not clicked.get("ok"):
                continue
            for _ in range(5):
                time.sleep(0.25)
                panel = self.read_panel()
                if panel.get("open"):
                    return {"ok": True, "level": level, "panel": panel, "how": "JS点击"}
        return {"ok": False, "why": "panel-not-opened"}

    def _real_click(self, x: int, y: int) -> None:
        """派发一次真实鼠标点击（可信事件）——抖音很多组件只认这个。"""
        if self.session is None:
            return
        try:
            self.session.call("Input.dispatchMouseEvent",
                              {"type": "mousePressed", "x": x, "y": y,
                               "button": "left", "clickCount": 1})
            time.sleep(0.08)
            self.session.call("Input.dispatchMouseEvent",
                              {"type": "mouseReleased", "x": x, "y": y,
                               "button": "left", "clickCount": 1})
        except Exception as exc:
            self.diag(f"真实鼠标事件失败：{exc}")

    def join(self) -> dict:
        """点"参与"按钮——同样用真实鼠标事件（可信事件）。"""
        target = self._evaluate(JS_BUTTON_CENTER)
        if not isinstance(target, dict) or not target.get("ok"):
            why = (target or {}).get("why", "no-button")
            return {"ok": False, "why": why}
        self._real_click(target["x"], target["y"])
        return {"ok": True, "text": target.get("text", ""), "how": "真实鼠标"}

    def _join_by_js(self) -> dict:
        """万一真实鼠标不可用（没有会话）时的兜底。"""
        res = self._evaluate(JS_JOIN)
        return res if isinstance(res, dict) else {"ok": False}

    def _open_panel_legacy(self) -> dict:
        """（保留：仅 JS 点击的老实现，排查用）"""
        center = self._evaluate(JS_BAG_CENTER)
        if isinstance(center, dict) and center.get("ok") and self.session is not None:
            x, y = center["x"], center["y"]
            self._real_click(x, y)
            for _ in range(8):
                time.sleep(0.25)
                panel = self.read_panel()
                if panel.get("open"):
                    return {"ok": True, "level": -1, "panel": panel, "how": "真实鼠标"}
        return {"ok": False, "why": "panel-not-opened"}

    def read_panel(self) -> dict:
        res = self._evaluate(JS_READ_PANEL)
        return res if isinstance(res, dict) else {"open": False}

    def close_panel(self) -> bool:
        """把福袋/红包面板关掉（新版面板里没有 svg，必须用真实鼠标点 × ，再不行按 ESC）。

        参考开源项目的做法：**任何弹窗都要能自己关掉**，否则会一直挂在屏幕上。
        """
        if not self.read_panel().get("open"):
            return True
        target = self._evaluate(JS_CLOSE_TARGET)
        if isinstance(target, dict) and target.get("open") and target.get("ok"):
            self._real_click(target["x"], target["y"])
            time.sleep(0.5)
            if not self.read_panel().get("open"):
                return True
        # 兜底 ①：老的 JS 点 svg
        self._evaluate(JS_CLOSE)
        time.sleep(0.3)
        if not self.read_panel().get("open"):
            return True
        # 兜底 ②：按 ESC（真实按键）
        if self.session is not None:
            try:
                for kind in ("keyDown", "keyUp"):
                    self.session.call("Input.dispatchKeyEvent",
                                      {"type": kind, "key": "Escape", "code": "Escape",
                                       "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
                time.sleep(0.4)
            except Exception:
                pass
        return not self.read_panel().get("open")

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

    def _refresh_bag_state(self, info: dict) -> None:
        """维护"当前这个福袋"的状态：识别到什么算一个新福袋。

        判定规则：**同一个直播间** + 倒计时**没有突然变大**（变大说明换新福袋了）
          → 同一个福袋；否则算新福袋，重置 handled。
        容差给 5 秒：正常情况倒计时是往下走的，只有"新一轮福袋开始"才会突然跳大。
        """
        now = time.time()
        left = parse_countdown(info.get("countdown") or "")
        st = self._bag
        same = (
            st.get("url") == info.get("url")
            and st.get("left") is not None
            and left is not None
            and left <= st["left"] + 5
        )
        if same:
            st["left"] = left
            st["seen"] = now
        else:
            self._bag = {"url": info.get("url"), "left": left, "handled": False,
                         "seen": now, "next_try": 0.0, "logged_joined": False}

    def _handle_result_panel(self) -> bool:
        """处理"开奖结果面板"：读出结果 → 记进日志 → 关掉。

        参考开源项目：任何弹窗都要能自己收掉，否则会一直挂在屏幕上挡着。
        返回 True 表示"刚处理掉一个结果面板"。
        """
        panel = self.read_panel()
        if not panel.get("open"):
            return False
        text = panel.get("allText") or ""
        if not re.search(RE_RESULT_PANEL, text):
            return False
        if re.search(RE_LOSE, text):
            self.log(f"😐 没抽中（{self.room_name}）· {text[:40]}")
            self._record(f"未中奖|{self.room_name}|{text[:60]}")
        elif re.search(RE_WIN, text):
            self.log(f"🎁 中奖了！（{self.room_name}）· {text[:60]}")
            self._record(f"中奖|{self.room_name}|{text[:60]}")
        else:
            self.log(f"ℹ 福袋结果面板，收起（{self.room_name}）")
        closed = self.close_panel()
        self.diag(f"结果面板已关闭={'是' if closed else '否'} 内容={text[:50]}")
        self._bag = {}
        return True

    def tick(self) -> dict:
        """跑一轮。返回这一轮的结果，给上层记日志/状态用。"""
        self._roll_day()
        info = self.find()
        if not info:
            return {"state": "no-page"}
        self.room_name = info.get("title") or self.room_name
        self.countdown = info.get("countdown") or "-"

        # ★ 先看有没有"开奖结果面板"挂着（不管现在有没有新福袋，都要先收掉）
        if self._handle_result_panel():
            return {"state": "result-closed"}

        if not info.get("bag"):
            # 没有福袋：隔一会儿就把旧状态清掉，等下个福袋从"未处理"开始
            if self._bag and time.time() - self._bag.get("seen", 0) > 20:
                self._bag = {}
            # ★ 福袋没了（比如开奖了）但面板还挂在那儿 -> 读出结果并关掉
            panel = self.read_panel()
            if panel.get("open"):
                text = panel.get("allText") or ""
                done_bag = self._bag
                if done_bag and not done_bag.get("result_logged"):
                    done_bag["result_logged"] = True
                    if re.search(r"恭喜|中奖|获得", text):
                        self.log(f"🎁 中奖了！{self.room_name} · {text[:80]}")
                        self._record(f"中奖|{self.room_name}|{text[:80]}")
                    elif re.search(r"未中奖|没有中奖|很遗憾", text):
                        self.log(f"😐 未中奖：{self.room_name} · {text[:60]}")
                        self._record(f"未中奖|{self.room_name}|{text[:60]}")
                    else:
                        self.log(f"ℹ 福袋已结束，收起面板（{self.room_name}）")
                closed = self.close_panel()
                self.diag(f"福袋结束后关面板：{'成功' if closed else '失败'}")
                self._bag = {}
            # 红包留给阶段 3，这里只报告状态
            return {"state": "no-bag", "redpacket": bool(info.get("redpacket")),
                    "countdown": info.get("countdown", "")}

        self._refresh_bag_state(info)
        if self._bag.get("handled"):
            return {"state": "already-handled"}       # 这个福袋处理过了，不再开面板
        if time.time() < self._bag.get("next_try", 0):
            return {"state": "cooldown"}

        left = parse_countdown(info.get("countdown") or "")
        if left is not None and left < int(self.cfg.giveaway_min_left_s or 20):
            return {"state": "too-late", "left": left}
        if left is not None and left > int(self.cfg.giveaway_max_left_s or 600):
            return {"state": "too-long", "left": left}

        # 看到福袋立刻出声，别让人以为程序没反应
        if self.last_result != f"发现福袋 {info.get('countdown','')}":
            self.log(f"👀 发现福袋：{self.room_name} · 倒计时 {info.get('countdown') or '?'}"
                     f"（{left if left is not None else '?'} 秒后开奖），正在点开面板…")
            self.last_result = f"发现福袋 {info.get('countdown','')}"

        # 开面板
        opened = self.open_panel()
        if not opened.get("ok"):
            self.failed_total += 1
            self._bag["next_try"] = time.time() + 20      # 20 秒后再试这个福袋
            why = opened.get("why")
            self.log(f"⚠ 福袋面板没打开（{why}）：{self.room_name}"
                     + ("  ← 通常是福袋刚好结束了" if "no-icon" in str(why) else ""))
            self.diag(f"福袋面板没打开：{why} 房间={self.room_name}")
            return {"state": "panel-failed", "why": why}

        panel = opened.get("panel") or {}
        self.people = panel.get("people") or "-"
        self.countdown = panel.get("countdown") or self.countdown
        prize = f"{panel.get('prize','')} {panel.get('bags','')}".strip()

        # ★ 已经参与过了（我们自己参与过，或者你手动点过）：
        #   标记为已处理 + 关面板 + 只记一次日志，绝不再反复开合
        if panel.get("joined") or any("已" in str(s) for s in (panel.get("condState") or [])):
            self._bag["handled"] = True
            self.close_panel()
            if not self._bag.get("logged_joined"):
                self._bag["logged_joined"] = True
                self.last_result = "这个福袋已经参与过"
                self.log(f"ℹ 这个福袋已经参与过了，跳过（{self.room_name} · "
                         f"{prize or '未知奖品'} · 倒计时 {panel.get('countdown') or '-'}）")
                self.diag(f"福袋已参与过，跳过 房间={self.room_name}")
            return {"state": "already-joined"}

        # 花钱类条件：默认拒绝
        cost = self.cost_condition(panel)
        if cost and not self.cfg.giveaway_allow_lamp:
            self._bag["handled"] = True
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
            self._bag["next_try"] = time.time() + 20
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
            self._bag["next_try"] = time.time() + 20
            self.close_panel()
            self.last_result = "点了但没确认到参与"
            self.log(f"⚠ 福袋点了但没确认到参与（可能没生效）：{self.room_name} · "
                     f"点击的按钮「{joined.get('text')}」· 面板仍显示「{after.get('button') or ''}」"
                     f"{after.get('condState') or ''}")
            self.diag(f"福袋参与未确认 房间={self.room_name} 点后按钮={after.get('button')} "
                      f"条件={after.get('condState')}")
            return {"state": "join-unverified"}

        self._bag["handled"] = True
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
