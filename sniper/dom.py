"""页面元素定位与真实点击。

抖音网页版是 React 单页应用，CSS 类名是编译后的乱码（``JLYNHKZg`` 这种），
写死选择器一升级就废。所以定位策略按优先级分三层：

1. **按文字找**：找到 innerText 匹配目标文字的元素，取面积最小的那个
   （面积最小 = 最贴近实际按钮，而不是包着它的整块容器）。
2. **按学习结果找**：用户在"学习模式"里用鼠标指着目标元素停一下，
   我们记下它的标签、类名、文字、属性，之后按这批特征重新匹配。
3. **按坐标兜底**：实在找不到就点上次记录的坐标。

定位到之后一律用 CDP 派发**真实鼠标事件**（mousePressed/mouseReleased），
而不是 ``element.click()``——前者走浏览器的完整输入链路，和真人点击同一条路径。
"""

from __future__ import annotations

import json
import time

# 在页面里按文字找元素。返回候选列表（按"面积从小到大"排序）。
FIND_JS = r"""
(function(text, exactOnly, limit) {
  const want = String(text);
  const out = [];
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    let t;
    try { t = (el.innerText || '').trim().replace(/\s+/g, ' '); } catch (e) { continue; }
    if (!t) continue;
    if (exactOnly ? (t !== want) : (t.indexOf(want) === -1)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    if (parseFloat(s.opacity || '1') < 0.1) continue;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
    out.push({
      text: t.slice(0, 60),
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 120),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height),
      area: Math.round(r.width * r.height),
      exact: t === want,
      visible: r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth,
    });
  }
  out.sort((a, b) => (a.area - b.area));
  return out.slice(0, limit || 12);
})
"""

# 鼠标悬停学习：记录鼠标停过的元素。用户只需要把鼠标移到目标上停一下。
HOVER_TRACKER_JS = r"""
(function() {
  function info(el) {
    if (!el || el.nodeType !== 1) return null;
    const r = el.getBoundingClientRect();
    const path = [];
    let n = el;
    for (let i = 0; i < 5 && n && n.nodeType === 1; i++) {
      let part = n.tagName.toLowerCase();
      const cls = (n.className || '').toString().trim().split(/\s+/)[0];
      if (cls) part += '.' + cls;
      path.unshift(part);
      n = n.parentElement;
    }
    return {
      tag: el.tagName,
      cls: (el.className || '').toString(),
      text: (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height),
      path: path.join(' > '),
      attrs: Array.prototype.slice.call(el.attributes, 0, 12).map(function(a) {
        return a.name + '=' + a.value;
      }).join(' ').slice(0, 300),
      ts: Date.now()
    };
  }
  if (!window.__sniperHoverInstalled) {
    window.__sniperHoverInstalled = true;
    window.__sniperHover = null;
    window.__sniperHoverCount = 0;
    document.addEventListener('mousemove', function(e) {
      const cur = window.__sniperHover;
      const cls = (e.target.className || '').toString();
      if (cur && cur.tag === e.target.tagName && cur.cls === cls) return;
      const data = info(e.target);
      if (data) { window.__sniperHover = data; window.__sniperHoverCount++; }
    }, true);
  }
  return {installed: true, count: window.__sniperHoverCount, last: window.__sniperHover};
})()
"""


def candidates(session, text: str, exact: bool = False, limit: int = 12) -> list[dict]:
    """按文字列出候选元素，按面积升序（越小越像真正的按钮）。"""
    expression = f"({FIND_JS})({json.dumps(text)}, {str(bool(exact)).lower()}, {int(limit)})"
    return session.evaluate(expression) or []


def find(session, text: str, exact: bool = False) -> dict | None:
    """找到最合适的那个元素。"""
    found = candidates(session, text, exact=exact, limit=12)
    if not found:
        return None
    # 优先「文字完全相等」，其次面积最小
    exact_hits = [c for c in found if c["exact"]]
    return (exact_hits or found)[0]


def click_text(session, text: str, exact: bool = False, hold_ms: float = 0.0) -> dict | None:
    """按文字找到元素，并用真实鼠标事件点它。返回点中的元素信息。"""
    target = find(session, text, exact=exact)
    if target is None:
        return None
    session.click_at(target["x"], target["y"], settle_ms=hold_ms)
    return target


def install_hover_tracker(session) -> dict:
    return session.evaluate(HOVER_TRACKER_JS)


def read_hover(session) -> dict | None:
    return session.evaluate(
        "({count: window.__sniperHoverCount, last: window.__sniperHover})"
    )


def wait_for_text(session, text: str, timeout: float = 5.0, interval: float = 0.1) -> dict | None:
    """等某段文字出现在页面上（用于判断弹窗/面板是否打开）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = find(session, text)
        if found:
            return found
        time.sleep(interval)
    return None


def is_text_present(session, text: str) -> bool:
    return session.evaluate(
        f"document.body.innerText.indexOf({json.dumps(text)}) !== -1"
    ) is True


def visible_text(session, limit: int = 2000) -> str:
    """整页可见文字，排查问题时很有用。"""
    text = session.evaluate("document.body.innerText.replace(/\\s+/g, ' ').trim()")
    return (text or "")[:limit]


# 点击录制器：用 composedPath() 穿透 Shadow DOM，精确记录"用户到底点了哪个元素"。
# 这是给真人演示一遍流程、让程序学习点击目标用的。
CLICK_RECORDER_JS = r"""
(function() {
  if (window.__sniperClicks) return {installed: true, already: true, count: window.__sniperClicks.length};
  window.__sniperClicks = [];
  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
    return {
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 60),
      txt: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height)
    };
  }
  document.addEventListener('click', function(e) {
    const path = e.composedPath ? e.composedPath() : [e.target];
    const chain = [];
    for (const node of path) {
      if (chain.length >= 6) break;
      if (!node || node.nodeType !== 1) continue;
      const d = describe(node);
      if (d && (d.txt || d.cls)) chain.push(d);
    }
    window.__sniperClicks.push({t: Date.now(), x: e.clientX, y: e.clientY, chain: chain});
  }, true);
  return {installed: true, count: 0};
})()
"""


def install_click_recorder(session) -> dict:
    return session.evaluate(CLICK_RECORDER_JS)


def read_clicks(session) -> list[dict]:
    return session.evaluate("(window.__sniperClicks || [])") or []


def clear_clicks(session) -> int:
    return session.evaluate("(window.__sniperClicks = []) && 0") or 0


# 进阶录制器：除了记录点击路径，还在每次点击后给所有 Lynx 面板拍一张"文字快照"。
# 这样用户完整走一遍流程之后，我们能看到每一步界面变成了什么样。
FLOW_RECORDER_JS = r"""
(function() {
  if (window.__flow) return {already: true, steps: window.__flow.length};
  window.__flow = [];

  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {left: 0, top: 0, width: 0, height: 0};
    return {
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 50),
      txt: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 46),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height)
    };
  }

  function snapshot() {
    const panels = [];
    let i = 0;
    for (const f of document.querySelectorAll('iframe')) {
      let doc = null;
      try { doc = f.contentDocument; } catch (e) { continue; }
      if (!doc) continue;
      const v = doc.querySelector('lynx-view');
      if (!v || !v.shadowRoot) { i++; continue; }
      const r = f.getBoundingClientRect();
      const texts = [];
      for (const t of v.shadowRoot.querySelectorAll('x-text')) {
        const s = (t.textContent || '').trim().replace(/\s+/g, ' ');
        if (s && texts.indexOf(s) === -1) texts.push(s);
      }
      panels.push({
        idx: i,
        box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
        text: texts.slice(0, 40)
      });
      i++;
    }
    return panels;
  }

  document.addEventListener('click', function(e) {
    const path = e.composedPath ? e.composedPath() : [e.target];
    const chain = [];
    for (const node of path) {
      if (chain.length >= 5) break;
      if (!node || node.nodeType !== 1) continue;
      const d = describe(node);
      if (d && (d.txt || d.cls)) chain.push(d);
    }
    window.__flow.push({step: window.__flow.length + 1, at: [e.clientX, e.clientY], chain: chain});
    // 界面变化有动画，稍等再拍快照
    setTimeout(function() {
      if (window.__flow.length) {
        window.__flow[window.__flow.length - 1].after = snapshot();
      }
    }, 900);
  }, true);
  return {installed: true, steps: 0};
})()
"""


def install_flow_recorder(session) -> dict:
    return session.evaluate(FLOW_RECORDER_JS)


def read_flow(session) -> list[dict]:
    return session.evaluate("(window.__flow || [])") or []


def clear_flow(session) -> None:
    session.evaluate("(window.__flow = []) && 0")


# 深度录制器：装到"所有能访问到的文档"上，包括面板所在的 iframe。
# 之前只有顶层文档装了监听，所以面板内部的点击全都漏掉了——
# 关键点：iframe 里的事件不会冒泡到父文档，必须逐个文档装。
FLOW_DEEP_JS = r"""
(function() {
  const root = window.top || window;
  if (!root.__flow) root.__flow = [];

  root.__panels = function() {
    const panels = [];
    let i = 0;
    for (const f of document.querySelectorAll('iframe')) {
      let doc = null;
      try { doc = f.contentDocument; } catch (e) { i++; continue; }
      if (!doc) { i++; continue; }
      const v = doc.querySelector('lynx-view');
      if (!v || !v.shadowRoot) { i++; continue; }
      const r = f.getBoundingClientRect();
      if (r.width < 60) { i++; continue; }
      const texts = [];
      const buttons = [];
      for (const t of v.shadowRoot.querySelectorAll('x-text')) {
        const s = (t.textContent || '').trim().replace(/\s+/g, ' ');
        if (s && texts.indexOf(s) === -1) texts.push(s);
      }
      for (const b of v.shadowRoot.querySelectorAll('[class*=btn]')) {
        const s = (b.textContent || '').trim();
        if (!s) continue;
        const br = b.getBoundingClientRect();
        buttons.push(s + '@' + Math.round(r.left + br.left + br.width / 2) + ',' +
                     Math.round(r.top + br.top + br.height / 2));
      }
      panels.push({idx: i, box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                   texts: texts.slice(0, 45), buttons: buttons});
      i++;
    }
    return panels;
  };

  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {left: 0, top: 0, width: 0, height: 0};
    return {
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 50),
      txt: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 44),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height)
    };
  }

  function install(doc, label) {
    if (!doc || doc.__flowInstalled) return;
    doc.__flowInstalled = true;
    root.__flowInstalled = root.__flowInstalled || [];
    root.__flowInstalled.push(label);
    doc.addEventListener('click', function(e) {
      const path = e.composedPath ? e.composedPath() : [e.target];
      const chain = [];
      for (const node of path) {
        if (chain.length >= 5) break;
        if (!node || node.nodeType !== 1) continue;
        const d = describe(node);
        if (d && (d.txt || d.cls)) chain.push(d);
      }
      const step = {step: root.__flow.length + 1, from: label, at: [e.clientX, e.clientY], chain: chain};
      root.__flow.push(step);
      setTimeout(function() { step.after = root.__panels(); }, 900);
    }, true);
    let subs = [];
    try { subs = doc.querySelectorAll('iframe'); } catch (e) { subs = []; }
    for (let i = 0; i < subs.length; i++) {
      let sub = null;
      try { sub = subs[i].contentDocument; } catch (e) { sub = null; }
      if (sub) install(sub, label + '>if' + i);
    }
    let hosts = [];
    try { hosts = doc.querySelectorAll('*'); } catch (e) { hosts = []; }
    for (const el of hosts) {
      if (!el.shadowRoot) continue;
      for (const f of el.shadowRoot.querySelectorAll('iframe')) {
        let sub = null;
        try { sub = f.contentDocument; } catch (e) { sub = null; }
        if (sub) install(sub, label + '>shadowIf');
      }
    }
  }
  root.__flowInstallAll = function() { install(document, 'top'); };
  root.__flowInstallAll();
  // 看门狗：面板一关一开就是新文档，必须不停地补装监听
  if (!root.__flowWatchdog) {
    root.__flowWatchdog = setInterval(function() {
      try { root.__flowInstallAll(); } catch (e) {}
    }, 400);
  }
  return {installed: root.__flowInstalled, steps: root.__flow.length};
})()
"""


def install_flow_deep(session) -> dict:
    return session.evaluate(FLOW_DEEP_JS)


def read_flow_deep(session) -> list[dict]:
    return session.evaluate("((window.top || window).__flow || [])") or []
