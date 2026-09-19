"""直播间列表抓取（阶段 2）。

做法：**开一个后台标签页**去读"关注页 / 直播首页"，读完立刻关掉——
这样不会把当前正在挂机的直播间页面顶掉（原项目是"上划切房间"，网页版直接导航更简单）。

实测（2026-09-19）：
    https://www.douyin.com/follow   → 20 个直播间（包含自己关注的主播，优先用这个）
    https://live.douyin.com/        → 20 个直播间（首页推荐，作为补充）
"""

from __future__ import annotations

import json
import time

from . import browser, cdp

# 抓页面里所有"直播间链接"
JS_ROOM_LINKS = r"""
(function () {
  var ids = [];
  document.querySelectorAll('a[href]').forEach(function (a) {
    var m = (a.href || '').match(/live\.douyin\.com\/(\d{6,})/);
    if (m && ids.indexOf(m[1]) < 0) ids.push(m[1]);
  });
  return JSON.stringify(ids);
})()
"""

DEFAULT_SOURCES = ("https://www.douyin.com/follow", "https://live.douyin.com/")


def scrape_rooms(port: int, sources=DEFAULT_SOURCES, max_rooms: int = 40,
                 per_page_wait: float = 6.0) -> list[str]:
    """返回直播间地址列表（https://live.douyin.com/<id>），按来源顺序去重。"""
    result: list[str] = []
    for url in sources:
        tab = None
        session = None
        try:
            tab = browser.open_tab(url, port=port)
            ws = (tab or {}).get("webSocketDebuggerUrl")
            if not ws:
                continue
            session = cdp.CDP(ws, timeout=15)
            session.call("Runtime.enable")
            time.sleep(per_page_wait)          # 等页面把直播间列表渲染出来
            raw = session.call("Runtime.evaluate",
                               {"expression": JS_ROOM_LINKS, "returnByValue": True}).get("result", {}).get("value")
            ids = json.loads(raw) if isinstance(raw, str) else []
            for rid in ids:
                full = f"https://live.douyin.com/{rid}"
                if full not in result:
                    result.append(full)
                if len(result) >= max_rooms:
                    break
        except Exception:
            continue
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            tab_id = (tab or {}).get("id")
            if tab_id:
                browser.close_tab(tab_id, port=port)
    return result[:max_rooms]


def filter_rooms(rooms: list[str], blacklist: list[str], extra_skip: list[str] | None = None) -> list[str]:
    """按黑名单过滤（黑名单里写房间号或主播名都行）。"""
    skip = [str(x).strip() for x in (blacklist or []) if str(x).strip()]
    skip += [str(x).strip() for x in (extra_skip or []) if str(x).strip()]
    if not skip:
        return list(rooms)
    out = []
    for room in rooms:
        if any(kw and kw in room for kw in skip):
            continue
        out.append(room)
    return out
