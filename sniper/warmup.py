"""直播间预热与定位演练。

这个工具不送礼物，只做"把枪架好、瞄好"的动作，并把瞄准点打出来给你看：

    python -m sniper.warmup --room https://live.douyin.com/123456789

它会：进直播间 → 点开左上角灯牌 → 在 Lynx Shadow DOM 里找到
"送1个为你闪耀"那一行 → 算出「一键赠送」按钮的屏幕绝对坐标。

加 --fire 才会真的点下去（会花 9 钻），默认永远不点。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

from . import browser, clock, fansclub, timebase


def attach(port: int, room: str | None, launch: bool = True):
    """拿到直播间页面的会话；必要时启动浏览器。"""
    if launch and not browser.is_running(port):
        profile = browser.profile_dir(browser.resolve_browser("auto")[0])
        url = room or "https://live.douyin.com/"
        browser.launch(url, profile, port=port)
        time.sleep(1.0)

    target = browser.wait_for_page(port, "", timeout=25)
    return browser.attach(target)


def warm(port: int, room: str | None, verbose: bool = True) -> dict:
    """完整预热流程，返回定位结果。"""
    session = attach(port, room)
    try:
        if room and room not in (session.evaluate("location.href") or ""):
            session.navigate(room)
            time.sleep(4.0)

        url = session.evaluate("location.href")
        title = session.evaluate("document.title")
        if verbose:
            print(f"  当前页面: {title}")
            print(f"  地址    : {url}")

        opened = fansclub.open_panel(session)
        if verbose:
            print(f"  粉丝团面板: {'已打开' if opened else '打开失败'}")

        if not opened:
            return {"ok": False, "reason": "面板没打开", "url": url}

        info = fansclub.locate(session)
        if verbose:
            if info.get("found"):
                print(f"  目标任务: {info['itemText']}")
                print(f"  按钮    : {info['buttonLabel']}  "
                      f"({'可点' if info.get('buttonEnabled') else '不可点'})")
                print(f"  任务进度: {info.get('progress') or '(未识别)'}")
                print(f"  面板    : {info['frame']}")
                print(f"  瞄准点  : ({info['x']:.0f}, {info['y']:.0f})  "
                      f"按钮 {info['w']}x{info['h']}")
            else:
                print("  目标任务: 没找到（可能今天已经送过，或面板结构变了）")
        return {"ok": bool(info.get("found")), "url": url, "locate": info, "session": session}
    except Exception:
        session.close()
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音抢送 - 直播间预热与定位演练")
    parser.add_argument("--room", help="直播间地址，如 https://live.douyin.com/123456789")
    parser.add_argument("--port", type=int, default=browser.DEFAULT_PORT)
    parser.add_argument("--fire", action="store_true", help="真的点击赠送（花 9 钻，慎用）")
    parser.add_argument("--at", metavar="HH:MM:SS", help="等到这个北京时间再点（配合 --fire）")
    args = parser.parse_args(argv)

    print("=" * 62)
    print("直播间预热与定位")
    print("=" * 62)
    result = warm(args.port, args.room)
    session = result.get("session")

    if not result.get("ok"):
        print(f"\n结果：失败 —— {result.get('reason', '未知')}")
        if session:
            session.close()
        return 1

    if args.fire:
        tb = timebase.TimeBase.calibrate(per_server=6, rounds=2)
        print()
        print(f"  本机时钟偏差 {tb.cal.offset_ms:+.1f} ms，时钟精度 ±{tb.cal.sigma_ns / 1e6:.2f} ms")
        if args.at:
            hh, mm, ss = (float(x) for x in args.at.split(":"))
            now = tb.now_ns()
            target = timebase.next_midnight_ns(now)  # 先取明天 0 点
            # 改成本地当天的 HH:MM:SS
            from datetime import datetime, timedelta, timezone

            dt = datetime.fromtimestamp(now / 1e9, timezone(timedelta(hours=8)))
            at = dt.replace(hour=int(hh), minute=int(mm), second=int(ss), microsecond=0)
            target = int(at.timestamp() * 1e9)
            if target < now:
                target += 24 * 3600 * 10**9
            left = (target - tb.now_ns()) / 1e9
            print(f"  等待到 {args.at}（还有 {left:.1f} 秒）…")
            clock.sleep_until_mono(tb.mono_at(target), spin_ns=3_000_000)
        print(f"  开火时刻: {timebase.fmt(tb.now_ns())}")
        outcome = fansclub.fire(session)
        print(f"  结果    : {outcome}")
        time.sleep(1.5)
        print(f"  点击后任务状态: {fansclub.task_status(session)}")

    if session:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
