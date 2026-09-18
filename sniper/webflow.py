"""纯 JS 版动作链 —— 全程不碰鼠标，也不用抢前台。

实测结论（2026-09-14 在真实直播间逐个验证）：

    进直播间        Page.navigate            ✓
    点灯牌开面板    element.click()          ✓
    点「去参与」    element.click()          ✓（Lynx 面板也认 JS，之前判断错了）
    点「点亮粉丝星」element.click()          ✓ 真的送出去了，耗时 1 毫秒
    点确认弹窗      element.click()          ✓
    刷新陪伴之旅    location.reload()        ✓ 完整重载、320~570 毫秒

早期以为"Lynx 不认 JS 注入"是因为点错了元素（点到了里面的文字节点，
而不是那一层真正的按钮）。正确做法：先按类名找按钮，找不到再按文字找并向上
取"按钮级"的祖先。

全 JS 的好处：不需要移动鼠标、不需要抢前台、不需要校准坐标、
不会因为窗口移动而点偏、单步耗时 1 毫秒级。
"""

from __future__ import annotations

import json
import collections
import re
import time
from dataclasses import dataclass, field

from . import browser, cdp

ACCOMPANY_URL_KEY = "star_accompany"

# 常驻监听脚本：注入直播间页面之后，由**页面自己的 JS** 时时刻刻盯着
# "是否开播"和"谁送出了为你闪耀"，一有变化就主动推给本地控制台。
#
# 上报通道用 CDP 的 binding（页面直接调用一个函数），不走网络 ——
# 抖音页面的 CSP 会拦掉往 127.0.0.1 发的 fetch（"Failed to fetch"），
# 而 binding 是调试协议自己的通道，不受任何页面安全策略限制。
MONITOR_JS = r"""
(function(send, version) {
  if (window.__sniperMon && window.__sniperMon.version === version) {
    return {already: true, live: window.__sniperMon.live};
  }
  // 旧版本（比如用 fetch 上报的）要换掉，否则它会一直跑还一直失败
  if (window.__sniperMon && window.__sniperMon.observer) {
    try { window.__sniperMon.observer.disconnect(); } catch (e) {}
  }
  if (window.__sniperMon && window.__sniperMon.timer) {
    clearInterval(window.__sniperMon.timer);
  }

  function report(kind, data) {
    try {
      send(JSON.stringify(Object.assign({kind: kind, at: Date.now()}, data || {})));
    } catch (e) {}
  }

  function isLive() {
    const text = document.body ? document.body.innerText : '';
    const offline = /暂未开播|未开播|已下播|直播已结束/.test(text);
    const hasGift = !!document.querySelector('[data-e2e="gifts-container"]');
    return hasGift && !offline;
  }

  const seenGifts = {};
  let seenCount = 0;
  let lastScan = 0;
  function scanGifts() {
    // 送礼播报不必每 80ms 扫全页，400ms 一次足够，也省 CPU
    const nowMs = Date.now();
    if (nowMs - lastScan < 400) return;
    lastScan = nowMs;
    for (const el of document.querySelectorAll('span,div')) {
      let own = '';
      for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
      own = own.trim().replace(/\s+/g, ' ');
      if (!own || own.length > 40) continue;
      if (own.indexOf('为你闪耀') === -1) continue;
      if (seenGifts[own]) continue;
      seenGifts[own] = 1;
      // 别让去重表无限长大（页面开很久时）
      if (++seenCount > 500) {
        for (const key in seenGifts) delete seenGifts[key];
        seenCount = 0;
      }
      report('gift', {text: own});
    }
  }

  window.__sniperMon = {installed: true, version: version, live: null, ticks: 0,
                        startedAt: Date.now()};
  // 注意：基线要取"安装那一刻的状态"。
  // 如果设成 null，第一次 tick 就会把"当前正在开播"当成"刚刚开播"推上去，
  // 程序一启动就会误触发一次秒抢（实测踩过这个坑）。
  let last = isLive();
  window.__sniperMon.live = last;
  let lastBeat = Date.now();
  function tick() {
    const live = isLive();
    window.__sniperMon.live = live;
    window.__sniperMon.ticks++;
    if (live !== last) {
      last = live;
      report('live', {live: live});
    }
    // 每 5 秒报一次心跳，好让控制台能确认监听器还活着
    if (Date.now() - lastBeat > 5000) {
      lastBeat = Date.now();
      report('heartbeat', {ticks: window.__sniperMon.ticks, live: live});
    }
    scanGifts();
  }
  window.__sniperMon.timer = setInterval(tick, 80);
  // 提速：页面 DOM 一变就立刻检查，不再死等 80 毫秒的轮询。
  // 用 40 毫秒节流，避免直播弹幕刷屏把 CPU 打满。
  let lastQuick = 0;
  function quickTick() {
    const now = Date.now();
    if (now - lastQuick < 40) return;
    lastQuick = now;
    try { tick(); } catch (e) {}
  }
  try {
    const observer = new MutationObserver(quickTick);
    observer.observe(document.documentElement, {childList: true, subtree: true});
    window.__sniperMon.observer = observer;
  } catch (e) {}
  tick();
  report('hello', {url: location.href.slice(0, 90), live: last});
  return {installed: true, live: window.__sniperMon.live};
})(window.__sniperSend, 7)
"""


@dataclass
class SendResult:
    ok: bool = False
    reason: str = ""
    # 结果分类，用来区分"失败"和"今天已送过（不是失败）"：
    #   ok / already_done / no_button / failed
    kind: str = ""
    lit_before: int | None = None
    lit_after: int | None = None
    confirmed: bool = False
    elapsed_ms: float = 0.0
    detail: dict = field(default_factory=dict)


class WebFlow:
    """全部通过页面 JS 完成，不使用任何操作系统级输入。"""

    def __init__(self, port: int = browser.DEFAULT_PORT, room_url: str = "",
                 browser_pref: str = "auto"):
        self.port = port
        self.room_url = room_url
        self.browser_pref = browser_pref
        # 认一次浏览器（Chrome / Edge / 自定义路径），后面配置目录都按它来
        self.browser_kind, self.browser_path = browser.resolve_browser(browser_pref)
        self.session: cdp.CDP | None = None
        self._acc_session: cdp.CDP | None = None   # 陪伴之旅 iframe 的会话（复用）
        self._acc_ws: str = ""
        self.log: collections.deque = collections.deque(maxlen=300)  # 别无限长

    # ---------- 基础设施 ----------

    def note(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.log.append(line)
        print(line)

    def attach(self) -> cdp.CDP:
        if self.session is None:
            target = self._pick_target()
            if target is None:
                raise TimeoutError("等不到浏览器页面")
            self.session = browser.attach(target)
        return self.session

    def _pick_target(self):
        """挑出"抖音直播间"那个页面。

        不能直接用第一个页面目标：控制台自己也是个页面，
        打开控制台窗口之后它可能排在最前面，结果监听脚本被装到了控制台自己身上
        （实测踩过这个坑，日志里会看到监听页面是 127.0.0.1:8777）。
        """
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            pages = [
                t for t in browser.list_targets(self.port)
                if t.get("type") == "page" and "127.0.0.1" not in (t.get("url") or "")
            ]
            for target in pages:
                url = target.get("url") or ""
                if "live.douyin.com" in url or "/follow/live/" in url:
                    return target
            for target in pages:
                if "douyin.com" in (target.get("url") or ""):
                    return target
            if pages:
                return pages[0]
            time.sleep(0.3)
        return None

    def ensure_browser(self) -> bool:
        if browser.is_running(self.port):
            if self.session is None:
                self.attach()
            return True

        # 端口没起来：先清掉"占了配置目录但没开调试端口"的残留实例，
        # 否则新实例带的调试端口会被忽略，程序永远连不上浏览器。
        killed = browser.kill_browser_profile(self._profile_dir())
        if killed:
            self.note(f"清理了 {killed} 个没有调试端口的残留浏览器进程")
            time.sleep(1.5)

        if not self.room_url:
            self.note("直播间地址是空的")
            return False
        browser.launch(self.room_url, self._profile_dir(), port=self.port,
                       prefer=self.browser_pref)
        if not browser.wait_for_port(self.port, timeout=25):
            self.note("浏览器调试端口没起来")
            return False
        self.session = None
        self.attach()
        return True

    def _profile_dir(self):
        """这台浏览器自己的配置目录（Chrome 和 Edge 分开，互不影响）。"""
        return browser.profile_dir(self.browser_kind)

    @staticmethod
    def room_id(url: str) -> str:
        match = re.search(r"(\d{6,})", url or "")
        return match.group(1) if match else (url or "")

    def goto_room(self, force: bool = False) -> bool:
        """进直播间。房间号一致就不重复导航（导航会重新加载整个页面）。

        force=True 时不管当前在哪都重新导航一次 —— 用户换直播间时要用。
        """
        if not self.ensure_browser():
            return False
        session = self.attach()
        current = session.evaluate("location.href") or ""
        want = self.room_id(self.room_url)
        have = self.room_id(current)
        if force or (want and want != have):
            self.note(f"进入直播间 {self.room_url}")
            session.navigate(self.room_url)
            time.sleep(4.5)
            # 换房间后页面整个重载，旧的注入脚本和 iframe 会话都作废
            self._drop_accompany()
        return True

    def reload_page(self) -> bool:
        """刷新直播间页面：soft=普通刷新(等同 F5)，hard=强制刷新(等同 Ctrl+F5)。"""
        try:
            session = self.attach()
            if self._hard_reload:
                session.call("Page.reload", {"ignoreCache": True})
            else:
                session.evaluate("location.reload()")
            return True
        except Exception:
            return False

    _hard_reload = True

    def set_reload_mode(self, hard: bool) -> None:
        self._hard_reload = bool(hard)

    def room_live(self) -> dict:
        try:
            return (
                self.attach().evaluate(
                    r"""(() => {
                      const text = document.body ? document.body.innerText : '';
                      const offline = /暂未开播|未开播|已下播|直播已结束/.test(text);
                      const hasGift = !!document.querySelector('[data-e2e="gifts-container"]');
                      const video = document.querySelector('video');
                      const title = document.title || '';
                      // 抖音风控会把页面换成"验证码/中间页"，这时候页面里什么都没有，
                      // 程序会一直判成"未开播"——必须单独认出来，否则永远等不到开播。
                      const challenge = /验证码|中间页|安全验证|captcha/i.test(title)
                                        || /验证码|安全验证/.test(text.slice(0, 200));
                      return {offline: offline, hasGift: hasGift,
                              playing: video ? !video.paused : false,
                              title: title.slice(0, 40), challenge: challenge,
                              live: hasGift && !offline};
                    })()"""
                )
                or {}
            )
        except Exception:
            return {}

    def page_metrics(self) -> dict:
        """页面自己的内存占用（给诊断日志用）。CDP 的一次轻量调用，不碰页面 DOM。"""
        try:
            session = self.attach()
            try:
                session.call("Performance.enable")
            except Exception:
                pass
            metrics = {
                m["name"]: m["value"]
                for m in session.call("Performance.getMetrics", timeout=6).get("metrics", [])
            }
            return {
                "heap_mb": round(metrics.get("JSHeapUsedSize", 0) / 1048576, 1),
                "nodes": int(metrics.get("Nodes", 0)),
                "docs": int(metrics.get("Documents", 0)),
            }
        except Exception:
            return {}

    # ---------- 点赞 ----------

    LIKE_FIND_JS = r"""(() => {
      const sels = ['[data-e2e="like-btn"]', '[data-e2e="live-like"]',
                    '[data-e2e="like-button"]', '[data-e2e*="like"]',
                    '.like-btn', '[class*="likeButton"]', '[class*="like-button"]',
                    '[class*="likeBtn"]', '[class*="LikeBtn"]',
                    '[class*="digg"]', '[class*="Digg"]',
                    '[class*="heart"]', '[class*="Heart"]'];
      for (const s of sels) {
        const el = document.querySelector(s);
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 6 || r.height < 6 || r.width > 260 || r.height > 260) continue;
        return {found: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
                cls: (el.className || '').toString().slice(0, 40), by: s};
      }
      return {found: false};
    })()"""

    LIKE_DIAG_JS = r"""(() => {
      const out = [];
      for (const el of document.querySelectorAll('img,svg,i,div,span,button')) {
        const r = el.getBoundingClientRect();
        if (r.width < 14 || r.height < 14 || r.width > 200 || r.height > 200) continue;
        // 只找右下角区域（点赞按钮一般在这儿）
        if (r.top < innerHeight * 0.45) continue;
        const cls = (el.className || '').toString();
        const txt = (el.textContent || '').trim().slice(0, 12);
        const alt = el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('alt') || '');
        const src = el.tagName === 'IMG' ? (el.src || '').slice(-40) : '';
        if (!/like|digg|heart|zan|点赞/i.test(cls + txt + alt + src)) continue;
        out.push({tag: el.tagName, cls: cls.slice(0, 40), txt: txt, alt: alt,
                  img: src, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
                  w: Math.round(r.width), h: Math.round(r.height)});
        if (out.length >= 12) break;
      }
      return out;
    })()"""

    def like_diagnose(self) -> list:
        try:
            return self.attach().evaluate(self.LIKE_DIAG_JS) or []
        except Exception:
            return []

    def probe_like(self) -> dict:
        """探测"点赞"到底调用了哪个接口：点一下屏幕，抓期间发出的网络请求。"""
        session = self.attach()
        hits: list[str] = []
        try:
            session.call("Network.enable")
        except Exception:
            return {"ok": False, "reason": "Network 域不可用"}

        def on_request(params):
            try:
                url = params.get("request", {}).get("url", "")
            except Exception:
                return
            if not url:
                return
            low = url.lower()
            if any(k in low for k in ("like", "digg", "zan", "praise", "room", "webcast")):
                if url not in hits:
                    hits.append(url)

        session.off("Network.requestWillBeSent")
        session.on("Network.requestWillBeSent", on_request)
        ok = self.like_once("screen")
        time.sleep(2.0)
        session.off("Network.requestWillBeSent")
        return {"ok": ok, "requests": hits[:12]}

    def like_button(self) -> dict:
        try:
            return self.attach().evaluate(self.LIKE_FIND_JS) or {"found": False}
        except Exception:
            return {"found": False}

    def like_once(self, mode: str = "js") -> bool:
        """点一次赞。

        mode: screen = 点击屏幕（抖音直播的点赞就是点视频区域）
              js     = 调用页面的点赞入口
              mouse  = 对点赞按钮派发真实鼠标点击
        """
        if mode == "screen":
            try:
                pos = self.attach().evaluate(
                    r"""(() => {
                      const v = document.querySelector('video');
                      if (v) {
                        const r = v.getBoundingClientRect();
                        if (r.width > 50) return [r.left + r.width * 0.5, r.top + r.height * 0.5];
                      }
                      return [innerWidth * 0.5, innerHeight * 0.5];
                    })()"""
                )
                if not pos:
                    return False
                self.attach().click_at(pos[0], pos[1])
                return True
            except Exception:
                return False
        info = self.like_button()
        if not info.get("found"):
            return False
        try:
            session = self.attach()
            if mode == "mouse":
                # 模拟手工：用调试协议派发真实鼠标按下/抬起（和真人点击同一条链路）
                session.click_at(info["x"], info["y"])
            else:
                # 调用页面自己的点赞入口
                session.evaluate(
                    r"""(() => {
                      const sels = ['[data-e2e="like-btn"]', '[data-e2e="live-like"]',
                                    '[data-e2e="like-button"]', '[data-e2e*="like"]',
                                    '.like-btn', '[class*="likeButton"]', '[class*="like-button"]',
                                    '[class*="likeBtn"]', '[class*="LikeBtn"]',
                                    '[class*="digg"]', '[class*="Digg"]',
                                    '[class*="heart"]', '[class*="Heart"]'];
                      for (const s of sels) {
                        const el = document.querySelector(s);
                        if (!el) continue;
                        el.click();
                        return s;
                      }
                      return '';
                    })()"""
                )
            return True
        except Exception:
            return False

    # ---------- 常驻监听（由页面 JS 主动推送） ----------

    def install_monitor(self, on_event) -> bool:
        """把监听脚本注入直播间页面，并接上推送通道。

        事件通过 CDP binding 送回（页面调 window.__sniperSend(...)），
        不走网络，所以不会被页面的 CSP 拦掉。
        """
        try:
            session = self.attach()
            session.call("Runtime.enable")
            session.call("Runtime.addBinding", {"name": "__sniperSend"})

            def handler(params):
                if params.get("name") != "__sniperSend":
                    return
                try:
                    payload = json.loads(params.get("payload") or "{}")
                except Exception:
                    return
                try:
                    on_event(payload)
                except Exception:
                    pass

            session.off("Runtime.bindingCalled")
            session.on("Runtime.bindingCalled", handler)
            return bool(session.evaluate(MONITOR_JS))
        except Exception as exc:
            self.note(f"注入监听脚本失败：{exc}")
            return False

    def monitor_state(self) -> dict:
        """读一下页面里监听器的状态（活着没、当前是否在播、跳了多少次）。"""
        try:
            return (
                self.attach().evaluate(
                    "window.__sniperMon ? {installed: true, live: window.__sniperMon.live,"
                    " ticks: window.__sniperMon.ticks} : {installed: false}"
                )
                or {"installed": False}
            )
        except Exception:
            return {"installed": False}

    # ---------- 面板 ----------

    def panel_open(self) -> bool:
        """粉丝团面板是否真的开着（必须是最上层可见的那块）。"""
        try:
            return bool(
                self.attach().evaluate(
                    """(() => {
                      for (const f of document.querySelectorAll('iframe')) {
                        let d = null;
                        try { d = f.contentDocument; } catch (e) { continue; }
                        if (!d) continue;
                        const v = d.querySelector('lynx-view');
                        if (!v || !v.shadowRoot) continue;
                        const r = f.getBoundingClientRect();
                        if (r.width < 200 || r.height < 300) continue;
                        // 只要有一块够大的 Lynx 面板就算开着。
                        // 不要再用 elementFromPoint 校验"最上层"——陪伴之旅盖在上面时
                        // 会误判成"没开"，然后点一下灯牌反而把面板关掉了。
                        return true;
                      }
                      return false;
                    })()"""
                )
            )
        except Exception:
            return False

    def open_panel(self, timeout: float = 6.0) -> bool:
        """点左上角灯牌打开面板（纯 JS）。已经开着就直接返回，避免又点关。"""
        if self.panel_open():
            return True
        # 页面没加载完的时候点灯牌是没用的（实测：点完什么都不发生，
        # 过几秒页面自己就绪后，之前那些点击才一起生效）。
        # 所以先等页面真正就绪，再点。
        ready_deadline = time.monotonic() + 25.0
        while time.monotonic() < ready_deadline:
            try:
                if self.attach().evaluate(
                    "!!document.querySelector('[data-e2e=\"gifts-container\"]')"
                ):
                    break
            except Exception:
                pass
            time.sleep(0.3)

        ready_deadline = time.monotonic() + 15.0
        while time.monotonic() < ready_deadline:
            hit = self.attach().evaluate(
                r"""(() => {
                  for (const el of document.querySelectorAll('button')) {
                    const t = (el.innerText || '').replace(/\s+/g, '');
                    if (!/粉丝团|今日任务/.test(t)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 30 || r.width > 220 || r.height < 16 || r.top > 120) continue;
                    el.click();
                    return t;
                  }
                  return '';
                })()"""
            )
            if hit:
                self.note(f"JS 点了灯牌「{hit}」")
                break
            if self.panel_open():
                return True
            time.sleep(0.4)
        else:
            self.note("等不到灯牌按钮（页面可能还没加载完）")
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.panel_open():
                return True
            time.sleep(0.08)
        # 没开成 —— 很可能刚才那一下其实把"已经开着的面板"点关了。
        # 再点一次把它开回来。
        self.note("面板没开成，再点一次灯牌")
        self.attach().evaluate(
            r"""(() => {
              for (const el of document.querySelectorAll('button')) {
                const t = (el.innerText || '').replace(/\s+/g, '');
                if (!/粉丝团|今日任务/.test(t)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 30 || r.width > 220 || r.top > 120) continue;
                el.click();
                return true;
              }
              return false;
            })()"""
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.panel_open():
                return True
            time.sleep(0.08)
        return False

    def open_accompany(self, timeout: float = 8.0) -> bool:
        """在面板里点「去参与」，打开陪伴之旅（纯 JS）。"""
        if self.accompany_target():
            return True
        if not self.panel_open():
            self.open_panel()
        # 面板 iframe 出现得很快，但里面的 Lynx 内容要过一会儿才渲染出来，
        # 所以要反复找「去参与」，不能找一次就放弃。
        hit = ""
        find_deadline = time.monotonic() + 10.0
        while time.monotonic() < find_deadline and not hit:
            hit = self.attach().evaluate(
                r"""(() => {
                  for (const fr of document.querySelectorAll('iframe')) {
                    let d = null; try { d = fr.contentDocument; } catch (e) { continue; }
                    if (!d) continue;
                    const v = d.querySelector('lynx-view');
                    if (!v || !v.shadowRoot) continue;
                    const r = fr.getBoundingClientRect();
                    if (r.width < 200) continue;
                    let best = null;
                    for (const el of v.shadowRoot.querySelectorAll('[class*=btn]')) {
                      const t = (el.textContent || '').trim();
                      if (t !== '去参与') continue;
                      const br = el.getBoundingClientRect();
                      if (br.width < 20) continue;
                      if (!best || br.width > best.w) best = {el: el, w: br.width, t: t};
                    }
                    if (best) { best.el.click(); return best.t; }
                  }
                  return '';
                })()"""
            )
            if not hit:
                if not self.panel_open():
                    self.open_panel()
                time.sleep(0.3)
        if not hit:
            self.note("面板里找不到「去参与」（等了 10 秒）")
            return False
        self.note("JS 点了「去参与」")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.accompany_target():
                # 等页面真正渲染出来（按钮出现）才算打开，
                # 只等 0.4 秒经常不够，后面会找不到按钮。
                for _ in range(60):
                    if self.button_state().get("found"):
                        return True
                    time.sleep(0.1)
                return True
            time.sleep(0.08)
        return False

    # ---------- 陪伴之旅 ----------

    def accompany_target(self) -> dict | None:
        for target in browser.list_targets(self.port):
            if target.get("type") == "iframe" and ACCOMPANY_URL_KEY in (target.get("url") or ""):
                return target
        return None

    def _accompany_session(self) -> cdp.CDP | None:
        """陪伴之旅 iframe 的调试会话。

        这里**复用**会话：早期版本每次调用都新建一条 WebSocket 和一个读线程，
        反复刷新状态会一点点泄漏线程和句柄（开一整晚就明显了）。
        只有在 iframe 被重建（调试地址变了）时才换新的。
        """
        target = self.accompany_target()
        if not target:
            self._drop_accompany()
            return None
        ws = target.get("webSocketDebuggerUrl") or ""
        if self._acc_session is not None and self._acc_ws == ws:
            return self._acc_session
        self._drop_accompany()
        try:
            self._acc_session = cdp.CDP(ws, timeout=8)
            self._acc_ws = ws
        except Exception:
            self._acc_session = None
            self._acc_ws = ""
            return None
        return self._acc_session

    def _drop_accompany(self) -> None:
        if self._acc_session is not None:
            try:
                self._acc_session.close()
            except Exception:
                pass
        self._acc_session = None
        self._acc_ws = ""

    def reset_session(self) -> None:
        """丢掉当前页面会话（连陪伴之旅那条一起），下次用的时候重新连。

        长跑时页面可能卡死或标签被换掉，这时候旧会话一直报错、程序却还以为在监听；
        主动断掉重连就能自己恢复。同时也把连接关干净，不留半死的 socket。
        """
        try:
            if self.session is not None:
                self.session.close()
        except Exception:
            pass
        self.session = None
        self._drop_accompany()

    def recycle_room_tab(self) -> dict:
        """换标签：新开一个标签加载直播间，等它能用了再关掉旧标签。

        目的：整页刷新久了页面内存只增不减（V8 的堆不会还给系统），
        只有把标签整个换掉、让 Chrome 重建渲染进程，内存才会真正回收。
        顺序很重要——**先开新的、再关旧的**，否则中间会有一段没有页面的空窗。
        返回 {"ok":…, "waited":…}
        """
        url = self.room_url
        if not url:
            return {"ok": False, "note": "没有直播间地址"}
        before = {
            t.get("id")
            for t in browser.list_targets(self.port)
            if t.get("type") == "page"
        }
        old = self._pick_target()
        try:
            browser.open_tab(url, port=self.port)
        except Exception as exc:
            return {"ok": False, "note": f"新标签没开起来：{exc}"}
        # 等**新出现**的那个标签真的有内容（最多 12 秒），期间旧标签还在正常干活
        started = time.monotonic()
        ready = browser.wait_for_new_page(port=self.port, exclude_ids=before, timeout=12.0)
        waited = time.monotonic() - started
        if old and old.get("id"):
            browser.close_tab(old["id"], port=self.port)
        # 会话绑在旧标签上，必须丢掉重连
        self.reset_session()
        self.note(f"已换标签（新标签就绪用了 {waited*1000:.0f} ms）")
        return {"ok": ready, "waited": waited}

    def page_text(self) -> str:
        session = self._accompany_session()
        if not session:
            return ""
        try:
            return (
                session.evaluate("(document.body.innerText||'').replace(/\\s+/g,' ')") or ""
            )
        except Exception:
            return ""
        finally:
            session.close()

    def lit_value(self) -> int | None:
        match = re.search(r"你已注入\s*(\d+)\s*点亮值", self.page_text())
        return int(match.group(1)) if match else None

    def button_state(self) -> dict:
        """返回当前点亮按钮的状态（文字 + 是否禁用）。"""
        session = self._accompany_session()
        if not session:
            return {}
        try:
            return (
                session.evaluate(
                    """(() => {
                      const el = document.querySelector('.btn_pking')
                              || document.querySelector('.accompany-button');
                      if (!el) return {found: false};
                      const cls = (el.className || '').toString();
                      return {found: true, text: (el.textContent || '').trim(),
                              disabled: cls.indexOf('disabled') !== -1,
                              cls: cls.trim()};
                    })()"""
                )
                or {}
            )
        except Exception:
            return {}
        finally:
            session.close()

    def refresh(self, timeout: float = 6.0) -> float:
        """用页面自己的 JS 重新加载陪伴之旅，返回"从刷新到按钮就绪"的毫秒数。"""
        session = self._accompany_session()
        if not session:
            return -1.0
        started = time.monotonic()
        try:
            session.evaluate("setTimeout(function(){location.reload();},0); 'ok'")
        except Exception as exc:
            self.note(f"JS 刷新触发失败：{exc}")
            return -1.0
        finally:
            try:
                session.close()
            except Exception:
                pass
        while time.monotonic() - started < timeout:
            if self.button_state().get("found"):
                cost = (time.monotonic() - started) * 1000
                self.note(f"JS 刷新完成，按钮就绪用了 {cost:.0f} ms")
                return cost
            time.sleep(0.015)
        return -1.0

    def fire(self) -> bool:
        """JS 点「点亮粉丝星」。多选择器兜底。"""
        session = self._accompany_session()
        if not session:
            return False
        try:
            used = session.evaluate(
                r"""(() => {
                  for (const sel of ['.btn_pking', '.accompany-button']) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    if ((el.className || '').toString().indexOf('disabled') !== -1) continue;
                    el.click();
                    return sel;
                  }
                  for (const el of document.querySelectorAll('*')) {
                    let own = '';
                    for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
                    own = own.trim();
                    if (!own || own.indexOf('点亮粉丝星') === -1) continue;
                    if (own.indexOf('已点亮') !== -1) continue;
                    let node = el, best = null;
                    for (let i = 0; i < 4 && node; i++) {
                      const r = node.getBoundingClientRect();
                      if (r.width >= 60 && r.width <= 420 && r.height >= 24 && r.height <= 90) best = node;
                      node = node.parentElement;
                    }
                    (best || el).click();
                    return 'text';
                  }
                  return '';
                })()"""
            )
        finally:
            session.close()
        if used:
            self.note(f"JS 点了「点亮粉丝星」（{used}）")
        return bool(used)

    def confirm_if_open(self, timeout: float = 1.5) -> bool:
        """出现二次确认就用 JS 点掉。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            session = self._accompany_session()
            if session:
                try:
                    used = session.evaluate(
                        r"""(() => {
                          const el = document.querySelector('.webcast-base-confirm-ok');
                          if (!el) return '';
                          el.click();
                          return (el.textContent || '').trim() || 'ok';
                        })()"""
                    )
                    if used:
                        self.note(f"JS 点了二次确认「{used}」")
                        return True
                except Exception:
                    pass
                finally:
                    session.close()
            time.sleep(0.012)
        return False

    # ---------- 一个完整的"送出去" ----------

    def send_once(self, label: str = "手动送出") -> SendResult:
        """完整一次：读基准值 → JS 点击 → 处理确认 → 刷新验证。"""
        result = SendResult()
        started = time.monotonic()

        if not self.accompany_target():
            if not self.open_accompany():
                result.reason = "陪伴之旅没打开"
                return result

        result.lit_before = self.lit_value()
        state = self.button_state()
        # 按钮可能是"禁用"状态（今天已点亮）：这不算找不到，
        # 但也不能点。跨天时同样要重试等服务器重置。
        if state.get("found") and state.get("disabled"):
            for _ in range(12):
                time.sleep(0.2)
                self.refresh()
                state = self.button_state()
                if state.get("found") and not state.get("disabled"):
                    self.note("重试后按钮变为可点（服务器刚重置完）")
                    break
        if not state.get("found"):
            # 跨天那一刻：刷新后如果按钮还没出现（服务器可能晚一点点才重置），
            # 就再等一会儿重新拉一次，最多试到约 2.5 秒。
            recovered = False
            for _ in range(12):
                time.sleep(0.2)
                if self.button_state().get("found"):
                    recovered = True
                    break
                self.refresh()
            if not recovered:
                result.reason = "陪伴之旅里找不到点亮按钮"
                return result
            self.note("重试后按钮出现了（服务器刚重置完）")
            result.lit_before = self.lit_value()

        self.note(f"【{label}】点之前：按钮「{state.get('text')}」，注入点亮值 {result.lit_before}")
        t_click = time.monotonic()
        clicked = self.fire()
        result.detail["click_ms"] = (time.monotonic() - t_click) * 1000
        if not clicked:
            result.reason = "JS 点击没找到按钮"
            return result

        t_confirm = time.monotonic()
        result.confirmed = self.confirm_if_open()
        result.detail["confirm_ms"] = (time.monotonic() - t_confirm) * 1000
        time.sleep(0.8)
        t_verify = time.monotonic()
        self.refresh()
        time.sleep(0.8)
        result.lit_after = self.lit_value()
        result.detail["verify_ms"] = (time.monotonic() - t_verify) * 1000
        result.elapsed_ms = (time.monotonic() - started) * 1000

        before, after = result.lit_before, result.lit_after
        if before is not None and after is not None and after > before:
            result.ok = True
            result.reason = f"送出成功：注入点亮值 {before} → {after}"
        else:
            result.reason = (
                f"未确认送出：注入点亮值 {before} → {after}。"
                f"多半是今天这个直播间的点亮次数已经用完"
            )
        return result
