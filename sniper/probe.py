"""调试探针：直接对直播间页面做侦察（开发用，交付时也会留着你排查问题用）。

例子：
    python -m sniper.probe --launch https://live.douyin.com/
    python -m sniper.probe --list
    python -m sniper.probe --eval "document.title"
    python -m sniper.probe --scan            # 扫描页面上可点的文字节点
    python -m sniper.probe --shot shot.png
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from . import browser, dom

SCAN_JS = r"""
(() => {
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll('div,span,button,a,img,svg,i,p');
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) continue;
    if (rect.bottom < 0 || rect.top > innerHeight) continue;
    if (rect.right < 0 || rect.left > innerWidth) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    if (parseFloat(style.opacity || '1') < 0.1) continue;
    // 只保留"自己直接含文字"的节点，避免整棵树的容器全被列出来
    let ownText = '';
    for (const child of el.childNodes) {
      if (child.nodeType === 3) ownText += child.textContent;
    }
    ownText = ownText.trim().replace(/\s+/g, ' ');
    if (!ownText) continue;
    if (ownText.length > 24) continue;
    const key = ownText + '|' + Math.round(rect.left) + ',' + Math.round(rect.top);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      text: ownText,
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 90),
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
    });
  }
  return {url: location.href, title: document.title, count: out.length, items: out};
})()
"""

# 在页面上画一个环形标记，用来直观确认"程序瞄的是哪一点"
MARK_JS = r"""
(function(x, y, label, color) {
  const ring = document.createElement('div');
  ring.className = '__sniper_mark';
  ring.style.cssText = 'position:fixed;left:' + (x - 24) + 'px;top:' + (y - 24) +
    'px;width:48px;height:48px;border:3px solid ' + color +
    ';border-radius:50%;z-index:2147483647;pointer-events:none;' +
    'box-shadow:0 0 0 2px rgba(0,0,0,.5), inset 0 0 8px rgba(255,255,255,.35)';
  const tag = document.createElement('div');
  tag.className = '__sniper_mark';
  tag.style.cssText = 'position:fixed;left:' + (x + 26) + 'px;top:' + (y - 26) +
    'px;background:' + color + ';color:#fff;font:600 12px/1.5 sans-serif;' +
    'padding:2px 7px;border-radius:4px;z-index:2147483647;pointer-events:none;white-space:nowrap';
  tag.textContent = label;
  document.body.appendChild(ring);
  document.body.appendChild(tag);
  return {ok: true, marks: document.querySelectorAll('.__sniper_mark').length};
})
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音抢送 - 页面调试探针")
    parser.add_argument("--port", type=int, default=browser.DEFAULT_PORT)
    parser.add_argument("--profile", default=None, help="浏览器配置目录（默认 chrome-profile/）")
    parser.add_argument("--launch", metavar="URL", help="启动浏览器并打开 URL")
    parser.add_argument("--list", action="store_true", help="列出所有页面目标")
    parser.add_argument("--open", metavar="URL", help="新开标签页")
    parser.add_argument("--eval", metavar="JS", help="在页面里执行 JS 并打印结果")
    parser.add_argument("--eval-file", metavar="PATH", help="执行文件里的 JS（避免命令行转义地狱）")
    parser.add_argument("--scan", action="store_true", help="扫描页面上带文字的可见节点")
    parser.add_argument("--click", metavar="X,Y", help="在视口坐标派发一次真实鼠标点击")
    parser.add_argument("--click-hold", type=float, default=0.0, metavar="MS", help="按下与抬起之间的间隔（毫秒）")
    parser.add_argument("--move", metavar="X,Y", help="只移动鼠标，不点击")
    parser.add_argument("--wheel", metavar="X,Y,DY", help="在坐标处派发滚轮")
    parser.add_argument("--find", metavar="TEXT", help="按文字找元素，列出候选")
    parser.add_argument("--click-text", metavar="TEXT", help="按文字找元素并用真实鼠标点击")
    parser.add_argument("--hover-install", action="store_true", help="安装鼠标悬停学习器")
    parser.add_argument("--hover-read", action="store_true", help="读取最近悬停过的元素")
    parser.add_argument("--rec-install", action="store_true", help="安装点击录制器")
    parser.add_argument("--rec-read", action="store_true", help="读取已录制的点击")
    parser.add_argument("--rec-clear", action="store_true", help="清空录制记录")
    parser.add_argument("--flow-install", action="store_true", help="安装完整流程录制器")
    parser.add_argument("--flow-read", action="store_true", help="读取完整流程录制")
    parser.add_argument("--flow-clear", action="store_true", help="清空流程录制")
    parser.add_argument("--flow2-install", action="store_true", help="安装深度流程录制器（含 iframe 内部）")
    parser.add_argument("--flow2-read", action="store_true", help="读取深度流程录制")
    parser.add_argument("--text", action="store_true", help="打印整页可见文字")
    parser.add_argument("--navigate", metavar="URL", help="让当前标签页跳到指定 URL")
    parser.add_argument("--shot", metavar="PATH", help="截图保存到文件")
    parser.add_argument("--mark", metavar="X,Y,标签,颜色", help="在页面上画环形标记")
    parser.add_argument("--unmark", action="store_true", help="清除所有标记")
    parser.add_argument("--clip", metavar="X,Y,W,H", help="配合 --shot：只截指定区域")
    parser.add_argument("--url-contains", default="", help="指定操作哪个标签页")
    args = parser.parse_args(argv)

    if args.launch:
        profile = pathlib.Path(args.profile or (pathlib.Path(__file__).parent.parent / "chrome-profile"))
        browser.launch(args.launch, profile, port=args.port)
        target = browser.wait_for_page(args.port, "douyin", timeout=30)
        print(f"已启动，页面：{target['url']}")
        print(f"调试端口：{args.port}")
        print("提示：第一次需要在这个窗口里登录抖音。")
        return 0

    if args.list:
        for target in browser.list_targets(args.port):
            print(f"[{target.get('type'):9}] {target.get('title','')[:40]:<42} {target.get('url','')[:90]}")
        return 0

    if args.open:
        target = browser.open_tab(args.open, port=args.port)
        print(json.dumps(target, ensure_ascii=False, indent=2))
        return 0

    target = browser.wait_for_page(args.port, args.url_contains, timeout=15)
    session = browser.attach(target)
    try:
        # 先做动作（跳转 / 点击），再看结果，顺序反过来会看到上一页的内容
        if args.navigate:
            session.navigate(args.navigate)
            time.sleep(4.0)
            print(f"已跳转：{session.evaluate('location.href')}")
        if args.click:
            x_text, y_text = args.click.split(",")
            x, y = float(x_text), float(y_text)
            session.click_at(x, y, settle_ms=args.click_hold)
            print(f"已在 ({x:.0f},{y:.0f}) 派发点击（按住 {args.click_hold:.0f}ms）")
            time.sleep(3.0)
            print(f"当前页面：{session.evaluate('location.href')}")
        if args.click_text:
            target = dom.click_text(session, args.click_text, hold_ms=args.click_hold)
            print(f"点击目标：{target}" if target else f"没找到文字 {args.click_text!r}")
            time.sleep(2.5)
        if args.move:
            x_text, y_text = args.move.split(",")
            x, y = float(x_text), float(y_text)
            session.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": x, "y": y,
                "button": "none", "buttons": 0, "pointerType": "mouse",
            })
            print(f"鼠标已移动到 ({x:.0f},{y:.0f})")
        if args.wheel:
            x_text, y_text, dy_text = args.wheel.split(",")
            x, y, dy = float(x_text), float(y_text), float(dy_text)
            session.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy,
                "pointerType": "mouse",
            })
            print(f"已派发滚轮 ({x:.0f},{y:.0f}) deltaY={dy:.0f}")
            time.sleep(1.0)
        script = args.eval
        if args.eval_file:
            script = pathlib.Path(args.eval_file).read_text("utf-8")
        if script:
            value = session.evaluate(script)
            print(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)
        if args.hover_install:
            print(dom.install_hover_tracker(session))
        if args.hover_read:
            print(json.dumps(dom.read_hover(session), ensure_ascii=False, indent=2))
        if args.rec_install:
            print(dom.install_click_recorder(session))
        if args.rec_clear:
            dom.clear_clicks(session)
            print("录制记录已清空")
        if args.rec_read:
            clicks = dom.read_clicks(session)
            print(f"共 {len(clicks)} 次点击")
            for index, click in enumerate(clicks, 1):
                print(f"  --- 第 {index} 次  屏幕({click['x']},{click['y']}) ---")
                for item in click["chain"]:
                    print(
                        f"      {item['tag']:<9} ({item['x']:>5},{item['y']:>5}) "
                        f"{item['w']:>4}x{item['h']:<4} {item['cls'][:40]:<42} {item['txt']!r}"
                    )
        if args.flow_install:
            print(dom.install_flow_recorder(session))
        if args.flow_clear:
            dom.clear_flow(session)
            print("流程录制已清空")
        if args.flow_read:
            steps = dom.read_flow(session)
            print(f"共录制到 {len(steps)} 步")
            for step in steps:
                print(f"\n===== 第 {step['step']} 步  点击位置({step['at'][0]},{step['at'][1]}) =====")
                for item in step["chain"]:
                    print(
                        f"   {item['tag']:<9} ({item['x']:>5},{item['y']:>5}) "
                        f"{item['w']:>4}x{item['h']:<4} {item['cls'][:38]:<40} {item['txt']!r}"
                    )
                for panel in step.get("after", []) or []:
                    print(f"   -- 面板{panel['idx']} {panel['box']} --")
                    for text in panel["text"]:
                        print(f"      {text!r}")
        if args.flow2_install:
            print(dom.install_flow_deep(session))
        if args.flow2_read:
            steps = dom.read_flow_deep(session)
            print(f"共录制到 {len(steps)} 步")
            for step in steps:
                print(f"\n===== 第 {step['step']} 步  来自[{step['from']}] 位置({step['at'][0]},{step['at'][1]}) =====")
                for item in step["chain"]:
                    print(
                        f"   {item['tag']:<9} ({item['x']:>5},{item['y']:>5}) "
                        f"{item['w']:>4}x{item['h']:<4} {item['cls'][:38]:<40} {item['txt']!r}"
                    )
                for panel in step.get("after", []) or []:
                    print(f"   -- 之后的面板{panel['idx']} {panel['box']} --")
                    print(f"      按钮: {panel.get('buttons')}")
                    for text in panel.get("texts", []):
                        print(f"      {text!r}")
        if args.find:
            for item in dom.candidates(session, args.find):
                print(
                    f"  ({item['x']:>5},{item['y']:>5}) {item['w']:>4}x{item['h']:<4} "
                    f"area={item['area']:<8} exact={str(item['exact']):<5} {item['tag']:<6} "
                    f"{item['text']!r:<30} {item['cls'][:44]}"
                )
        if args.text:
            print(dom.visible_text(session))
        if args.scan:
            result = session.evaluate(SCAN_JS)
            print(f"页面：{result['title']}  ({result['url']})")
            print(f"可见文字节点：{result['count']} 个（按 y 坐标排序）")
            for item in sorted(result["items"], key=lambda i: (i["y"], i["x"])):
                print(
                    f"  ({item['x']:>5},{item['y']:>5}) {item['w']:>4}x{item['h']:<4} "
                    f"{item['tag']:<6} {item['text']!r:<28} {item['cls']}"
                )
        if args.unmark:
            count = session.evaluate(
                "(() => { const m = document.querySelectorAll('.__sniper_mark');"
                " m.forEach(e => e.remove()); return m.length; })()"
            )
            print(f"已清除 {count} 个标记")
        if args.mark:
            parts = args.mark.split(",")
            x, y = float(parts[0]), float(parts[1])
            label = parts[2] if len(parts) > 2 else "aim"
            color = parts[3] if len(parts) > 3 else "#ff2d55"
            info = session.evaluate(f"({MARK_JS})({x}, {y}, {label!r}, {color!r})")
            print(f"已标记 ({x:.0f},{y:.0f}) {label}  当前标记数={info.get('marks')}")
        if args.shot:
            clip = None
            if args.clip:
                clip = tuple(float(v) for v in args.clip.split(","))
            data = session.screenshot(clip=clip)
            pathlib.Path(args.shot).write_bytes(data)
            print(f"截图已保存：{args.shot} ({len(data)} 字节)")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
