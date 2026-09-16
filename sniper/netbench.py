"""网络路径实测：到底哪条路到抖音最快、抖动最小。

本机时钟修准之后，剩下的时间几乎全是"请求在路上"。同样的代码，走 Wi-Fi、走有线、
走手机热点、走 IPv4 还是 IPv6，到达时间能差出几十毫秒——这几十毫秒就是第一名
和"陪跑"的距离。

测两个指标：
  * TCP 连接 RTT —— 纯路径往返，看网络本身的质量
  * HTTP TTFB    —— TCP+TLS+请求+首字节，最接近"点下去到有反应"的真实体感
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import ssl
import statistics
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from . import clock

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"

DEFAULT_TARGETS = [
    ("live.douyin.com", "抖音直播网页 / 进房入口"),
    ("webcast.amemv.com", "抖音直播接口（长连接 / 心跳）"),
    ("aweme.snssdk.com", "抖音主接口"),
    ("223.5.5.5", "阿里公共 DNS（网络基准，不计 HTTP）"),
]


def dwidth(text: str) -> int:
    """中文按 2 列宽计算，保证控制台表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - dwidth(text))


def resolve(host: str, port: int = 443):
    """返回 {地址族: sockaddr}，每个族取第一个可用地址。"""
    out = {}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return {}, str(exc)
    for family, _type, _proto, _canon, sockaddr in infos:
        out.setdefault(family, sockaddr)
    return out, None


def tcp_rtt(sockaddr, family: int, rounds: int, timeout: float = 2.0) -> list[float]:
    """反复建立 TCP 连接，测握手往返时间（毫秒）。"""
    rtts: list[float] = []
    for _ in range(rounds):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        t0 = clock.mono_ns()
        try:
            sock.connect(sockaddr)
            rtts.append((clock.mono_ns() - t0) / 1e6)
        except OSError:
            pass
        finally:
            sock.close()
        time.sleep(0.03)
    return rtts


def http_ttfb(host: str, sockaddr, family: int, rounds: int, timeout: float = 3.0) -> list[float]:
    """TCP + TLS + HTTP GET，测到首字节的时间（毫秒）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 只测时延，不校验证书
    request = (
        f"GET / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0\r\n"
        "Accept: */*\r\nConnection: close\r\n\r\n"
    ).encode()

    results: list[float] = []
    for _ in range(rounds):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            t0 = clock.mono_ns()
            sock.connect(sockaddr)
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                tls.sendall(request)
                first = tls.recv(1)
                if not first:
                    continue
                results.append((clock.mono_ns() - t0) / 1e6)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
        time.sleep(0.05)
    return results


def stats(xs: list[float]) -> dict | None:
    if not xs:
        return None
    xs = sorted(xs)
    p95 = xs[max(0, int(len(xs) * 0.95) - 1)]
    return {
        "n": len(xs),
        "min": xs[0],
        "p50": statistics.median(xs),
        "p95": p95,
        "max": xs[-1],
        "jitter": p95 - xs[0],
    }


def family_name(family: int) -> str:
    return {socket.AF_INET: "IPv4", socket.AF_INET6: "IPv6"}.get(family, str(family))


def run(targets, rounds: int, http_rounds: int) -> dict:
    report = {
        "started_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "targets": [],
    }
    header = (
        pad("目标", 20) + pad("协议族", 8) + pad("地址", 26)
        + pad("连接RTT p50", 13) + pad("p95", 9) + pad("抖动", 9) + "HTTP TTFB p50"
    )
    print(header)
    print("-" * 100)

    for host, note in targets:
        addrs, err = resolve(host)
        if err:
            print(pad(host, 20) + f"解析失败：{err}")
            continue
        for family, sockaddr in sorted(addrs.items()):
            addr_text = sockaddr[0]
            rtt = stats(tcp_rtt(sockaddr, family, rounds))
            is_ip_literal = host.replace(".", "").isdigit()
            ttfb = None if is_ip_literal else stats(http_ttfb(host, sockaddr, family, http_rounds))

            print(
                pad(host, 20) + pad(family_name(family), 8) + pad(addr_text, 26)
                + pad(f"{rtt['p50']:.1f} ms" if rtt else "N/A", 13)
                + pad(f"{rtt['p95']:.1f} ms" if rtt else "-", 9)
                + pad(f"{rtt['jitter']:.1f} ms" if rtt else "-", 9)
                + (f"{ttfb['p50']:.1f} ms" if ttfb else "-")
            )
            report["targets"].append(
                {
                    "host": host,
                    "note": note,
                    "family": family_name(family),
                    "addr": addr_text,
                    "tcp_rtt": rtt,
                    "http_ttfb": ttfb,
                }
            )
    return report


def advise(report: dict) -> None:
    """给出选路建议。"""
    by_host: dict[str, dict[str, dict]] = {}
    for item in report["targets"]:
        by_host.setdefault(item["host"], {})[item["family"]] = item

    print()
    print("=" * 62)
    print("选路建议")
    print("=" * 62)
    for host, families in by_host.items():
        if len(families) < 2:
            continue
        v4, v6 = families.get("IPv4"), families.get("IPv6")
        if not (v4 and v6 and v4["tcp_rtt"] and v6["tcp_rtt"]):
            continue
        better = "IPv4" if v4["tcp_rtt"]["p50"] <= v6["tcp_rtt"]["p50"] else "IPv6"
        gap = abs(v4["tcp_rtt"]["p50"] - v6["tcp_rtt"]["p50"])
        print(f"  {host}: {better} 更快，领先 {gap:.1f} ms")

    samples = [
        t["tcp_rtt"] for t in report["targets"] if t["tcp_rtt"] and t["host"] != "223.5.5.5"
    ]
    if samples:
        worst_jitter = max(s["jitter"] for s in samples)
        print()
        print(f"  到抖音的最大抖动：{worst_jitter:.1f} ms")
        if worst_jitter > 20:
            print("  ⚠ 抖动偏大。抖动比平均延迟更致命，它会让你无法把出手时刻卡准。")
            print("    优先排查：改用有线网卡 > 5GHz Wi-Fi > 2.4GHz；关掉后台下载 / 在线视频；")
            print("    关掉虚拟网卡（本机检测到 VMware 虚拟适配器，会干扰路由选择）。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音抢礼物工具 - 网络路径实测")
    parser.add_argument("--rounds", type=int, default=15, help="每目标 TCP 连接测试次数")
    parser.add_argument("--http-rounds", type=int, default=5, help="每目标 HTTP 往返测试次数")
    parser.add_argument("--label", default="", help="给这次测试起个名字（如 wifi / 有线 / 热点）")
    args = parser.parse_args(argv)

    clock.high_res_begin()
    clock.boost_current_thread("above_normal")

    print(f"网络路径实测（{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}）")
    if args.label:
        print(f"标记：{args.label}")
    print()
    report = run(DEFAULT_TARGETS, rounds=args.rounds, http_rounds=args.http_rounds)
    report["label"] = args.label
    advise(report)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"netbench_{args.label or 'default'}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print()
    print(f"结果已保存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
