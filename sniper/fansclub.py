"""粉丝团面板（灯牌）的开启、定位与状态判断。

实测出来的页面结构（2026-09-14，抖音直播网页版）：

    直播间左上角「灯牌」（按钮文字含"粉丝团"，旁边是关注按钮）
        ↓ 点击
    弹出 375x836 的面板 —— 它是一个 Lynx 动态化容器
        iframe(blob:) > <lynx-view> > #shadow-root
            .task-item                      任务条目
                .item-right-btn             "一键赠送" 按钮 ← 我们要点的
            "今日任务0/3"
                ├ 参与"陪伴之旅"玩法        [去参与]
                ├ 送1个"为你闪耀" 9钻，200亲密度  [一键赠送]   ← 目标
                ├ 送700钻礼物              [去送礼]
                └ 观看20分钟直播

关键点：内容在 **跨层 Shadow DOM** 里，普通 DOM 查询看不见，
而且坐标必须"iframe 相对坐标 + iframe 在页面里的偏移"换算成屏幕绝对坐标。
窗口大小一变坐标就变，所以每次开抢前都要重新算一遍。
"""

from __future__ import annotations

import json
import time

from . import dom

# 灯牌按钮上可能出现的文字（开面板前是"粉丝团"，开着时可能变成"今日任务"）
ENTRY_TEXTS = ("粉丝团", "今日任务")

# 目标任务的标识文字与要点的按钮
TASK_KEYWORD = "为你闪耀"
ACTION_BUTTON = "一键赠送"


# 一次 JS 调用里把整条链路走完：找面板 → 找目标任务 → 找按钮 → 换算绝对坐标
LOCATE_JS = r"""
(function(taskKeyword, buttonText) {
  const frames = Array.prototype.slice.call(document.querySelectorAll('iframe'));
  for (const f of frames) {
    let fr;
    try { fr = f.getBoundingClientRect(); } catch (e) { continue; }
    if (fr.width < 120 || fr.height < 120) continue;
    let doc = null;
    try { doc = f.contentDocument; } catch (e) { continue; }
    if (!doc) continue;
    const view = doc.querySelector('lynx-view');
    if (!view || !view.shadowRoot) continue;
    const root = view.shadowRoot;

    let item = null;
    for (const el of root.querySelectorAll('.task-item')) {
      if ((el.textContent || '').indexOf(taskKeyword) !== -1) { item = el; break; }
    }
    if (!item) continue;

    let btn = item.querySelector('.item-right-btn');
    if (!btn) continue;
    const btnLabel = (btn.textContent || '').trim();
    const r = btn.getBoundingClientRect();

    // 面板里的任务总数/完成数，用来判断今天是不是已经送过了
    let progress = '';
    for (const el of root.querySelectorAll('*')) {
      const t = (el.textContent || '').trim();
      if (/今日任务\s*\d\s*\/\s*\d/.test(t) && t.length < 20) { progress = t; break; }
    }

    return {
      found: true,
      x: fr.left + r.left + r.width / 2,
      y: fr.top + r.top + r.height / 2,
      w: Math.round(r.width),
      h: Math.round(r.height),
      buttonLabel: btnLabel,
      buttonEnabled: !btn.className.toString().includes('disabled'),
      itemText: (item.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90),
      progress: progress,
      frame: {x: Math.round(fr.left), y: Math.round(fr.top), w: Math.round(fr.width), h: Math.round(fr.height)},
      viewport: {w: innerWidth, h: innerHeight}
    };
  }
  return {found: false};
})
"""


def locate(session) -> dict:
    """定位「为你闪耀」任务的赠送按钮，返回屏幕绝对坐标。"""
    expression = f"({LOCATE_JS})({json.dumps(TASK_KEYWORD)}, {json.dumps(ACTION_BUTTON)})"
    return session.evaluate(expression) or {"found": False}


def panel_open(session) -> bool:
    """面板是否已经打开（有 Lynx 面板且能看到目标任务）。"""
    return bool(locate(session).get("found"))


def open_panel(session, timeout: float = 6.0) -> bool:
    """确保粉丝团面板打开。已经开着就直接返回 True。"""
    if panel_open(session):
        return True

    for text in ENTRY_TEXTS:
        target = dom.find(session, text)
        if not target:
            continue
        session.click_at(target["x"], target["y"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if panel_open(session):
                return True
            time.sleep(0.15)
    return False


def task_status(session) -> dict:
    """任务面板的当前状态，用于日志与赛后复盘。"""
    info = locate(session)
    if not info.get("found"):
        return {"open": False}
    return {
        "open": True,
        "progress": info.get("progress", ""),
        "item": info.get("itemText", ""),
        "button": info.get("buttonLabel", ""),
        "enabled": info.get("buttonEnabled", False),
        "point": [round(info["x"], 1), round(info["y"], 1)],
    }


def fire(session, point: tuple[float, float] | None = None) -> dict:
    """在目标按钮上派发一次真实鼠标点击。这是 0 点那一击。"""
    if point is None:
        info = locate(session)
        if not info.get("found"):
            return {"ok": False, "reason": "找不到为你闪耀的赠送按钮"}
        point = (info["x"], info["y"])
    session.click_at(point[0], point[1])
    return {"ok": True, "point": [round(point[0], 1), round(point[1], 1)]}
