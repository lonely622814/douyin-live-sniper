"""PC 端完整动作链：灯牌 → 陪伴之旅 → 点亮粉丝星 →（可选）确认。

为什么分成两套机制：

* **读**：用 CDP。抖音的粉丝团面板和陪伴之旅页面都是跨域 Lynx 容器，
  页面内容在 Shadow DOM 或者独立 iframe 里，但它会作为独立调试目标暴露出来，
  所以我们可以直接挂进去读 DOM、拿精确坐标。
* **点**：用 Win32 SendInput（操作系统级真实鼠标）。实测 Lynx 完全不认 CDP
  注入的鼠标事件（事件到了宿主元素上但内部不响应），只有真实鼠标才有效。

两者之间靠"只移动鼠标不点击"的零副作用校准对齐坐标系。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import browser, cdp, wininput

# 陪伴之旅页面的调试目标特征
ACCOMPANY_URL_KEY = "star_accompany"

# 各步骤要点的东西
PANEL_ENTRY_TEXTS = ("粉丝团", "今日任务")  # 左上角灯牌
JOURNEY_BUTTON_TEXTS = ("去参与", "陪伴之旅")  # 面板里进陪伴之旅
LIGHT_BUTTON_TEXTS = ("点亮粉丝星", "点亮")  # 陪伴之旅里的最终按钮
CONFIRM_TEXTS = ("确认", "确定", "立即", "赠送", "继续")

# 判断直播间是不是正在开播
LIVE_JS = r"""(() => {
  const text = document.body ? document.body.innerText : '';
  const offline = /暂未开播|未开播|已下播|直播已结束/.test(text);
  const hasGift = !!document.querySelector('[data-e2e="gifts-container"]');
  const video = document.querySelector('video');
  return {
    offline: offline,
    hasGift: hasGift,
    playing: video ? !video.paused : false,
    live: hasGift && !offline
  };
})()"""


@dataclass
class Hit:
    """一个可以点的目标。"""

    text: str = ""
    selector: str = ""
    view_x: float = 0.0
    view_y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    where: str = ""  # top / panel / accompany
    extra: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.width > 0 and self.height > 0


class PCFlow:
    """PC 端动作链。"""

    def __init__(self, port: int = browser.DEFAULT_PORT, room_url: str = ""):
        self.port = port
        self.room_url = room_url
        self.session: cdp.CDP | None = None
        self.hwnd = 0
        self.origin: tuple[float, float] | None = None
        self.dpr = 1.0
        self.log: list[str] = []

    @staticmethod
    def room_id(url: str) -> str:
        """从直播间地址里抠出房间号。分享链接后面会带一堆参数，只比房间号最可靠。"""
        import re

        match = re.search(r"(\d{6,})", url or "")
        return match.group(1) if match else (url or "")

    # ---------- 基础设施 ----------

    def note(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        self.log.append(line)
        print(line)

    def attach(self) -> cdp.CDP:
        if self.session is None:
            target = browser.wait_for_page(self.port, "", timeout=20)
            self.session = browser.attach(target)
        return self.session

    def ensure_window(self) -> bool:
        self.hwnd = wininput.find_window(title_contains="抖音")
        if not self.hwnd:
            self.note("没找到抖音浏览器窗口")
            return False
        wininput.bring_to_front(self.hwnd)
        return wininput.is_foreground(self.hwnd)

    def calibrate(self) -> tuple[float, float]:
        """只移动鼠标、不点击，测出网页视口左上角在屏幕上的物理坐标。"""
        session = self.attach()
        self.dpr = session.evaluate("devicePixelRatio") or 1.0
        session.evaluate(
            "window.__cal=null;"
            "document.addEventListener('mousemove',e=>{window.__cal=[e.clientX,e.clientY];},true);'ok'"
        )
        samples: list[tuple[float, float]] = []
        for attempt in range(3):
            # 每轮都重新抢一次前台：别的窗口随时可能把焦点抢走
            if not wininput.is_foreground(self.hwnd):
                wininput.bring_to_front(self.hwnd)
                time.sleep(0.2)
            samples = []
            for sx, sy in ((400, 300), (900, 700), (1500, 500)):
                session.evaluate("window.__cal=null;'ok'")
                wininput.move_to(sx, sy)
                # 轮询间隔压到 15ms、最多 12 次：快一秒就多一分赢面
                for _ in range(12):
                    raw = session.evaluate("JSON.stringify(window.__cal)")
                    if raw and raw != "null":
                        vx, vy = json.loads(raw)
                        samples.append((sx - vx * self.dpr, sy - vy * self.dpr))
                        break
                    time.sleep(0.015)
            if len(samples) >= 3:
                break
            time.sleep(0.2)
        if not samples:
            raise RuntimeError("校准失败：移动鼠标后页面没有收到 mousemove")
        ox = sum(a for a, _ in samples) / len(samples)
        oy = sum(b for _, b in samples) / len(samples)

        # 样本太少就别信——三四个点里只要有一个被别的窗口收走，
        # 算出来的原点就是错的，照着点会点到无关的地方。
        if len(samples) < 3 and self.origin is not None:
            self.note(
                f"校准样本不足（{len(samples)}/3），保留上一次的原点 "
                f"({self.origin[0]:.1f},{self.origin[1]:.1f})"
            )
            return self.origin

        # 合理性校验：视口左上角必须落在屏幕内，样本之间也不能太散。
        # 不满足几乎一定意味着"鼠标事件打到别的窗口去了"。
        screen_w = wininput.user32.GetSystemMetrics(0)
        screen_h = wininput.user32.GetSystemMetrics(1)
        spread = max(abs(a - ox) for a, _ in samples) + max(abs(b - oy) for _, b in samples)
        if not (-5 <= ox <= screen_w * 0.8) or not (-5 <= oy <= screen_h * 0.8) or spread > 12:
            raise RuntimeError(
                f"校准结果不合理：原点({ox:.1f},{oy:.1f}) 样本离散 {spread:.1f}px "
                f"屏幕 {screen_w}x{screen_h}，多半是目标窗口没拿到前台"
            )
        self.origin = (ox, oy)
        self.note(f"坐标校准完成：原点物理({ox:.1f},{oy:.1f}) 缩放{self.dpr} 样本{len(samples)}")
        return self.origin

    def click_view(self, x: float, y: float, hold_ms: float = 45) -> bool:
        """在网页视口坐标 (x, y) 处用真实鼠标点一下。"""
        if not self.hwnd:
            self.ensure_window()  # 兜底：没找过窗口就先找一次
        if self.origin is None:
            self.calibrate()
        ox, oy = self.origin  # type: ignore[misc]
        sx = int(round(ox + x * self.dpr))
        sy = int(round(oy + y * self.dpr))
        # 抢前台：真实鼠标点击只会打到前台窗口上，所以这一步必须先成功。
        # Windows 对后台进程抢焦点有限制，失败是常态，所以多试几轮。
        for _ in range(6):
            if wininput.is_foreground(self.hwnd):
                break
            wininput.bring_to_front(self.hwnd)
            time.sleep(0.12)
        if not wininput.is_foreground(self.hwnd):
            self.note("窗口不在前台，放弃点击（避免点到别的地方）")
            return False
        ok = wininput.click_screen_safe(sx, sy, self.hwnd, hold_ms=hold_ms)
        if not ok:
            self.note("点击被拦下：目标窗口没有拿到前台")
        return ok

    # ---------- 读页面 ----------

    def panel_hits(self, texts: tuple[str, ...]) -> list[Hit]:
        """在粉丝团面板（Lynx Shadow DOM）里找按钮。"""
        session = self.attach()
        js = """((texts) => {
          const out = [];
          for (const f of document.querySelectorAll('iframe')) {
            let doc = null; try { doc = f.contentDocument; } catch (e) { continue; }
            if (!doc) continue;
            const v = doc.querySelector('lynx-view');
            if (!v || !v.shadowRoot) continue;
            const fr = f.getBoundingClientRect();
            if (fr.width < 200) continue;
            for (const el of v.shadowRoot.querySelectorAll('[class*=btn]')) {
              const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
              if (!t) continue;
              if (!texts.some(x => t.indexOf(x) !== -1)) continue;
              const r = el.getBoundingClientRect();
              if (r.width < 8 || r.height < 8) continue;
              out.push({text: t, cls: (el.className||'').toString(),
                        view_x: fr.left + r.left + r.width/2,
                        view_y: fr.top + r.top + r.height/2,
                        width: r.width, height: r.height});
            }
          }
          return out;
        })""" 
        raw = session.evaluate(f"({js})({json.dumps(list(texts))})") or []
        return [
            Hit(
                text=item["text"],
                selector=item.get("cls", ""),
                view_x=item["view_x"],
                view_y=item["view_y"],
                width=item["width"],
                height=item["height"],
                where="panel",
            )
            for item in raw
        ]

    def top_hits(self, texts: tuple[str, ...]) -> list[Hit]:
        """在顶层文档里按文字找可点元素（灯牌按钮等）。"""
        session = self.attach()
        js = """((texts) => {
          const out = [];
          for (const el of document.querySelectorAll('button,div,span,a')) {
            const t = (el.innerText || '').trim().replace(/\\s+/g, '');
            if (!t || t.length > 10) continue;
            if (!texts.some(x => t.indexOf(x) !== -1)) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 16 || r.width > 260 || r.height < 12 || r.height > 44) continue;
            if (r.top > 140 || r.left > 700) continue;
            out.push({text: t, cls: (el.className||'').toString(),
                      view_x: r.left + r.width/2, view_y: r.top + r.height/2,
                      width: r.width, height: r.height});
          }
          out.sort((a, b) => a.width - b.width);
          return out;
        })"""
        raw = session.evaluate(f"({js})({json.dumps(list(texts))})") or []
        return [
            Hit(
                text=item["text"],
                selector=item.get("cls", ""),
                view_x=item["view_x"],
                view_y=item["view_y"],
                width=item["width"],
                height=item["height"],
                where="top",
            )
            for item in raw
        ]

    def accompany_target(self) -> dict | None:
        for target in browser.list_targets(self.port):
            if target.get("type") == "iframe" and ACCOMPANY_URL_KEY in (target.get("url") or ""):
                return target
        return None

    def accompany_frame_box(self) -> tuple[float, float, float, float] | None:
        """陪伴之旅 iframe 在顶层视口里的位置。

        不能靠 iframe 的 src 找：有的直播间它是个 blob 地址，src 里看不到关键字。
        可靠做法是查框架树，按 frameId 找到承载它的那个元素，再取盒子模型。
        """
        session = self.attach()
        try:
            session.call("DOM.enable")
            tree = session.call("Page.getFrameTree")["frameTree"]

            def find(node) -> str | None:
                frame = node["frame"]
                if ACCOMPANY_URL_KEY in (frame.get("url") or ""):
                    return frame["id"]
                for child in node.get("childFrames", []):
                    got = find(child)
                    if got:
                        return got
                return None

            frame_id = find(tree)
            if frame_id:
                owner = session.call("DOM.getFrameOwner", {"frameId": frame_id})
                box = session.call("DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]})
                quad = box["model"]["content"]
                x1, y1, x2, _y2 = quad[0], quad[1], quad[2], quad[3]
                return (x1, y1, x2 - x1, quad[5] - y1)
        except Exception:
            pass

        # 退路：按 src 关键词找
        raw = session.evaluate(f"""(() => {{
          for (const f of document.querySelectorAll('iframe')) {{
            if ((f.src || '').indexOf({json.dumps(ACCOMPANY_URL_KEY)}) === -1) continue;
            const r = f.getBoundingClientRect();
            if (r.width < 50) continue;
            return [r.left, r.top, r.width, r.height];
          }}
          return null;
        }})()""")
        return tuple(raw) if raw else None

    def accompany_hits(self, texts: tuple[str, ...], class_key: str = "") -> list[Hit]:
        """在陪伴之旅页面里找按钮。"""
        target = self.accompany_target()
        if not target:
            return []
        box = self.accompany_frame_box()
        if not box:
            return []
        session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=8)
        try:
            js = """((texts, classKey) => {
              const out = [];
              for (const el of document.querySelectorAll('*')) {
                let own = '';
                for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
                own = own.trim().replace(/\\s+/g, ' ');
                const full = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                const cls = (el.className || '').toString();
                const byClass = classKey && cls.indexOf(classKey) !== -1;
                // 只用"元素自身的直接文字"匹配，避免命中包着一大片内容的容器
                const byText = own && own.length <= 30 && texts.some(x => own.indexOf(x) !== -1);
                if (!byClass && !byText) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 10 || r.height < 10) continue;
                out.push({text: own || full.slice(0, 30), cls: cls,
                          x: r.left + r.width/2, y: r.top + r.height/2,
                          width: r.width, height: r.height});
              }
              return out;
            })"""
            raw = (
                session.evaluate(
                    f"({js})({json.dumps(list(texts))}, {json.dumps(class_key)})"
                )
                or []
            )
        finally:
            session.close()
        hits = []
        for item in raw:
            hits.append(
                Hit(
                    text=item["text"],
                    selector=item["cls"],
                    view_x=box[0] + item["x"],
                    view_y=box[1] + item["y"],
                    width=item["width"],
                    height=item["height"],
                    where="accompany",
                )
            )
        return hits

    # ---------- 动作 ----------

    def open_panel(self, timeout: float = 6.0) -> bool:
        """点左上角灯牌，打开粉丝团面板。"""
        if self.panel_open():
            self.note("粉丝团面板已经开着")
            return True
        hits = self.top_hits(PANEL_ENTRY_TEXTS)
        if not hits:
            self.note("找不到左上角灯牌按钮")
            return False
        hit = hits[0]
        self.note(f"点灯牌「{hit.text}」({hit.view_x:.0f},{hit.view_y:.0f})")
        self.click_view(hit.view_x, hit.view_y)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.panel_hits(("去参与", "陪伴之旅", "点亮")):
                self.note("粉丝团面板已打开")
                return True
            time.sleep(0.2)
        self.note("粉丝团面板没打开")
        return False

    def panel_open(self) -> bool:
        """粉丝团面板是不是开着。

        注意别用按钮文字判断：面板里进陪伴之旅那个按钮写的是「去参与」，
        文字里并没有"陪伴之旅"三个字。判据是"有没有一块可见的 Lynx 面板"。

        还必须验证它**真的在最上面**：退出陪伴之旅之后，旧的面板 iframe
        可能还留在 DOM 里但已经不可见，只看"存在"会误判成"开着"，
        然后点上去就是一片空气（这个坑踩过）。
        """
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
                        const cx = r.left + r.width / 2;
                        const cy = r.top + r.height / 2;
                        if (document.elementFromPoint(cx, cy) === f) return true;
                      }
                      return false;
                    })()"""
                )
            )
        except Exception:
            return False

    def open_accompany(self, timeout: float = 8.0) -> bool:
        """在面板里点「去参与」，打开陪伴之旅页面。"""
        if self.accompany_target():
            self.note("陪伴之旅页面已经开着")
            return True
        hits = self.panel_hits(JOURNEY_BUTTON_TEXTS)
        if not hits:
            self.note("面板里找不到陪伴之旅入口")
            return False
        hit = hits[0]
        self.note(f"点「{hit.text}」({hit.view_x:.0f},{hit.view_y:.0f})")
        self.click_view(hit.view_x, hit.view_y)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.accompany_target():
                time.sleep(0.8)  # 等页面渲染完
                self.note("陪伴之旅页面已打开")
                return True
            time.sleep(0.2)
        self.note("陪伴之旅页面没打开")
        return False

    def light_button(self) -> Hit | None:
        """陪伴之旅里那个「点亮粉丝星（9钻）」按钮。

        各直播间的实现不一样，实测两种：
            DIV.accompany-button[.accompany-button-disabled]   335x44
              └─ DIV.accompany-button-text                      75x21
            DIV.btn_pking                                      335x44
              └─ DIV.combo-btn-text                           118x21

        所以不能写死类名，统一按**文字**认：先找到自身文字含「点亮粉丝星」的元素，
        再往上找一层"按钮级"的祖先作为点击目标。
        今天已经点亮过时文字会变成「今日已点亮」，那时就找不到目标。
        """
        target = self.accompany_target()
        box = self.accompany_frame_box()
        if not target or not box:
            return None
        session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=8)
        try:
            js = r"""((keyword) => {
              for (const el of document.querySelectorAll('*')) {
                let own = '';
                for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
                own = own.trim().replace(/\s+/g, ' ');
                if (!own || own.length > 24) continue;
                if (own.indexOf(keyword) === -1) continue;
                if (own.indexOf('已点亮') !== -1 || own.indexOf('已送') !== -1) continue;
                let node = el, best = el;
                for (let i = 0; i < 4 && node; i++) {
                  const r = node.getBoundingClientRect();
                  if (r.width >= 60 && r.width <= 420 && r.height >= 24 && r.height <= 90) best = node;
                  node = node.parentElement;
                }
                const r = best.getBoundingClientRect();
                return {text: own, cls: (best.className || '').toString(),
                        x: r.left + r.width / 2, y: r.top + r.height / 2,
                        width: r.width, height: r.height};
              }
              return null;
            })"""
            item = session.evaluate(f"({js})({json.dumps(LIGHT_BUTTON_TEXTS[0])})")
        finally:
            session.close()
        if not item:
            return None
        return Hit(
            text=item["text"],
            selector=(item.get("cls") or "").strip(),
            view_x=box[0] + item["x"],
            view_y=box[1] + item["y"],
            width=item["width"],
            height=item["height"],
            where="accompany",
        )

    def exit_accompany(self) -> bool:
        """点陪伴之旅左上角的返回箭头，退回粉丝团面板。

        为什么要退出再进来：实测 ``location.reload()`` 是从缓存恢复页面，
        根本不会重新拉取当天状态（所以刷新了等于没刷）。
        只有退出再重新进来，页面才是全新创建的、会重新拉状态。
        """
        target = self.accompany_target()
        box = self.accompany_frame_box()
        if not target or not box:
            return False
        session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=8)
        try:
            item = session.evaluate(
                """(() => {
                  for (const el of document.querySelectorAll('*')) {
                    const cls = (el.className || '').toString();
                    if (cls.indexOf('arrow-back') === -1) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 6) continue;
                    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                  }
                  return null;
                })()"""
            )
        finally:
            session.close()
        if not item:
            self.note("陪伴之旅里没找到返回按钮")
            return False
        vx, vy = box[0] + item["x"], box[1] + item["y"]
        self.note(f"退出陪伴之旅：点返回键 ({vx:.0f},{vy:.0f})")
        ok = self.click_view(vx, vy, hold_ms=40)
        # 退出之后粉丝团面板会重新布局，「去参与」的位置会变（实测会跳 70 像素），
        # 所以要等它稳定下来再走下一步，否则会点在错误的行上。
        time.sleep(0.9)
        return ok

    def settled_journey_hit(self, tries: int = 8):
        """等面板布局稳定后再给出「去参与」的位置（连续两次读到一样才算稳）。"""
        last = None
        hit = None
        for _ in range(tries):
            hits = self.panel_hits(JOURNEY_BUTTON_TEXTS)
            if hits:
                hit = hits[0]
                current = (round(hit.view_x), round(hit.view_y))
                if current == last:
                    return hit
                last = current
            time.sleep(0.15)
        return hit

    def reload_accompany_js(self, timeout: float = 6.0) -> bool:
        """用陪伴之旅页面自己的 JS 重新加载它（实测 320~570ms），返回按钮是否就绪。

        这是最快的刷新方式：不需要鼠标、不需要抢前台，
        而且是完整重载（会重新拉取当天状态），不是缓存恢复。
        """
        target = self.accompany_target()
        if not target:
            return False
        try:
            session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=6)
            session.evaluate("setTimeout(function(){location.reload();},0); 'ok'")
            session.close()
        except Exception as exc:
            self.note(f"JS 刷新触发失败：{exc}")
            return False
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if self.light_button():
                self.note(f"JS 刷新完成，按钮就绪用了 {(time.monotonic() - started) * 1000:.0f} ms")
                return True
            time.sleep(0.015)
        return False

    def click_light_by_js(self) -> bool:
        """用页面自己的 JS 点「点亮粉丝星」。实测有效，耗时约 1 毫秒。

        比真实鼠标快三个数量级，而且不需要抢前台、不需要校准坐标、
        不会点偏。多个选择器依次兜底，覆盖不同直播间的实现差异。
        """
        target = self.accompany_target()
        if not target:
            return False
        session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=6)
        try:
            used = session.evaluate(
                """(() => {
                  // 1) 按类名（实测过的两种）
                  for (const sel of ['.btn_pking', '.accompany-button']) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    if ((el.className || '').toString().indexOf('disabled') !== -1) continue;
                    el.click();
                    return sel;
                  }
                  // 2) 按文字：找到含「点亮粉丝星」的元素，再点它那层按钮祖先
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
                    const target = best || el;
                    target.click();
                    return 'text:' + (target.className || '').toString().trim();
                  }
                  return '';
                })()"""
            )
        finally:
            session.close()
        if used:
            self.note(f"JS 点击成功（{used}）")
        return bool(used)

    def confirm_by_js(self) -> bool:
        """用 JS 点二次确认弹窗里的「确认」。"""
        target = self.accompany_target()
        if not target:
            return False
        session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=6)
        try:
            used = session.evaluate(
                """(() => {
                  for (const sel of ['.webcast-base-confirm-ok', '[class*=confirm-ok]']) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8) continue;
                    el.click();
                    return sel + '|' + (el.textContent || '').trim();
                  }
                  for (const el of document.querySelectorAll('*')) {
                    let own = '';
                    for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
                    own = own.trim();
                    if (own !== '确认' && own !== '确定') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 12) continue;
                    el.click();
                    return 'text:' + own;
                  }
                  return '';
                })()"""
            )
        finally:
            session.close()
        if used:
            self.note(f"JS 点了确认（{used}）")
        return bool(used)

    def confirm_dialog_open(self) -> bool:
        """二次确认弹窗是否开着。"""
        target = self.accompany_target()
        if not target:
            return False
        session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=6)
        try:
            return bool(
                session.evaluate(
                    "!!document.querySelector('.webcast-base-confirm-ok')"
                )
            )
        except Exception:
            return False
        finally:
            session.close()

    def reenter_accompany(self, timeout: float = 5.0) -> float:
        """重新进陪伴之旅，返回"从点击到按钮出现"的毫秒数。"""
        started = time.monotonic()
        # 点一次不成功就再来一次：退出和重进之间页面有动画，
        # 偶尔第一次点击会被吃掉。
        for attempt in range(2):
            if not self.panel_open():
                self.open_panel()
            hit = self.settled_journey_hit()
            if hit is None:
                self.note("面板里找不到「去参与」")
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                return -1.0
            self.note(f"重新进陪伴之旅：点「{hit.text}」({hit.view_x:.0f},{hit.view_y:.0f})")
            self.click_view(hit.view_x, hit.view_y, hold_ms=40)

            deadline = time.monotonic() + (timeout if attempt == 0 else 3.0)
            while time.monotonic() < deadline:
                if self.light_button():
                    return (time.monotonic() - started) * 1000
                time.sleep(0.012)
            self.note("第一次点击没反应，重试一次")
        return -1.0

    def light_state(self) -> str:
        """今天点亮过没有：done / ready / unknown。"""
        if self.light_button():
            return "ready（按钮可点）"
        done = [
            h
            for h in self.accompany_hits(("今日已点亮",))
            if "已点亮" in h.text and h.height <= 90
        ]

    def room_live(self) -> dict:
        """直播间是否在播。"""
        try:
            return self.attach().evaluate(LIVE_JS) or {}
        except Exception:
            return {}
        if done:
            return f"done（{done[0].text}）"
        return "unknown"

    def lit_value(self) -> int | None:
        """陪伴之旅里「你已注入 N 点亮值」的那个 N。

        这是判断"今天到底送出没有"最可靠的数字——大于 0 就是送出过。
        （按钮文字不可靠：送出之后不重新打开页面，它还是显示「点亮粉丝星」。）
        """
        target = self.accompany_target()
        if not target:
            return None
        session = cdp.CDP(target["webSocketDebuggerUrl"], timeout=8)
        try:
            text = session.evaluate("(document.body.innerText||'').replace(/\\s+/g,' ')") or ""
        finally:
            session.close()
        import re

        match = re.search(r"你已注入\s*(\d+)\s*点亮值", text)
        return int(match.group(1)) if match else None

    def arm(self) -> Hit | None:
        """把枪架好：进面板、进陪伴之旅、定位最终按钮。返回目标。"""
        if not self.ensure_window():
            return None
        self.calibrate()
        # 陪伴之旅页面是"弹窗"性质，它开着的时候再点灯牌是无效的。
        # 所以先看它是否已经开着，开着就直接用，不重复走一遍。
        if not self.accompany_target():
            if not self.open_panel():
                return None
            if not self.open_accompany():
                return None
        else:
            self.note("陪伴之旅已经开着，直接瞄准")
        hit = self.light_button()
        if hit:
            self.note(f"瞄准完成：「{hit.text}」({hit.view_x:.0f},{hit.view_y:.0f}) {hit.width:.0f}x{hit.height:.0f}")
        else:
            self.note("陪伴之旅里没找到「点亮粉丝星」按钮（可能今天已经点亮过了）")
        return hit

    def fire(self, hit: Hit | None = None) -> bool:
        """开火：点「点亮粉丝星」。"""
        hit = hit or self.light_button()
        if not hit:
            self.note("没有可点的目标")
            return False
        self.note(f"开火：点「{hit.text}」({hit.view_x:.0f},{hit.view_y:.0f})")
        return self.click_view(hit.view_x, hit.view_y, hold_ms=35)

    def verify_aim(self, hit: Hit, tolerance: float = 8.0) -> bool:
        """开火前校验：把鼠标移到目标位置，让页面回报它实际收到的坐标。

        这是防止"坐标算歪了、点空"的最后一道保险——不点，只移动。
        """
        session = self.attach()
        session.evaluate(
            "window.__aim=null;"
            "if(!window.__aimInstalled){"
            "  document.addEventListener('mousemove',function(e){window.__aim=[e.clientX,e.clientY];},true);"
            "  window.__aimInstalled=true;}"
            "'ok'"
        )
        if self.origin is None:
            self.calibrate()
        ox, oy = self.origin  # type: ignore[misc]
        sx = int(round(ox + hit.view_x * self.dpr))
        sy = int(round(oy + hit.view_y * self.dpr))
        if not wininput.is_foreground(self.hwnd):
            wininput.bring_to_front(self.hwnd)
            time.sleep(0.15)
        session.evaluate("window.__aim=null;'ok'")
        wininput.move_to(sx, sy)
        time.sleep(0.12)
        raw = session.evaluate("JSON.stringify(window.__aim)")
        if not raw or raw == "null":
            self.note("瞄准校验失败：页面没收到鼠标移动（窗口可能不在前台）")
            return False
        vx, vy = json.loads(raw)
        dx, dy = abs(vx - hit.view_x), abs(vy - hit.view_y)
        if dx > tolerance or dy > tolerance:
            self.note(
                f"瞄准校验偏差过大：期望({hit.view_x:.0f},{hit.view_y:.0f}) "
                f"实际收到({vx},{vy})"
            )
            return False
        self.note(f"瞄准校验通过：鼠标实际落在 ({vx},{vy})，偏差 ({dx:.0f},{dy:.0f}) 像素")
        return True

    def confirm_hits(self) -> list[Hit]:
        """找二次确认弹窗里的确认按钮。"""
        found = self.accompany_hits(CONFIRM_TEXTS) + self.panel_hits(CONFIRM_TEXTS)
        return [h for h in found if h.text not in ("点亮粉丝星", "点亮")]


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="PC 端动作链演练（默认不开火）")
    parser.add_argument("--room", default="", help="直播间地址（需要时切过去）")
    parser.add_argument("--port", type=int, default=browser.DEFAULT_PORT)
    parser.add_argument("--arm", action="store_true", help="执行到瞄准为止：进面板、进陪伴之旅、定位按钮")
    parser.add_argument("--fire", action="store_true", help="真的点下去（花 9 钻）")
    parser.add_argument("--state", action="store_true", help="只读一遍当前状态")
    args = parser.parse_args(argv)

    flow = PCFlow(port=args.port, room_url=args.room)
    session = flow.attach()
    if args.room:
        current = session.evaluate("location.href") or ""
        if args.room not in current:
            flow.note(f"切换到直播间 {args.room}")
            session.navigate(args.room)
            time.sleep(4.0)

    if args.state or not (args.arm or args.fire):
        flow.note("当前状态：")
        flow.note(f"  灯牌按钮: {[h.text for h in flow.top_hits(PANEL_ENTRY_TEXTS)]}")
        flow.note(f"  面板按钮: {[h.text for h in flow.panel_hits(('去参与', '一键赠送', '点亮'))]}")
        target = flow.accompany_target()
        flow.note(f"  陪伴之旅: {'已打开' if target else '未打开'}")
        if target:
            hits = flow.accompany_hits(("点亮", "亲密度", "星"))
            for h in hits[:8]:
                flow.note(f"    「{h.text}」({h.view_x:.0f},{h.view_y:.0f}) {h.width:.0f}x{h.height:.0f}")
        return 0

    if args.arm or args.fire:
        hit = flow.arm()
        if hit is None:
            return 1
        if args.fire:
            flow.fire(hit)
            time.sleep(0.6)
            confirms = flow.confirm_hits()
            if confirms:
                flow.note(f"出现二次确认：{[h.text for h in confirms]}，自动点确认")
                flow.click_view(confirms[0].view_x, confirms[0].view_y)
            time.sleep(1.5)
            flow.note("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
