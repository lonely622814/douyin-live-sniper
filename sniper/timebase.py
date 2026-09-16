"""时间基准：把本机时钟校准到毫秒以内，并用单调秒表推算任意未来时刻。

这是整个程序的地基。抢"0 点第一个"本质上是比谁的请求先到抖音服务器，
本机时钟如果偏了 300ms，鼠标点得再快也没用。

命令行：
    python -m sniper.timebase                    # 标定一次并打印报告
    python -m sniper.timebase --selftest 200     # 附带触发精度自检
    python -m sniper.timebase --drift 60         # 测本机晶振漂移率
    python -m sniper.timebase --watch 60         # 常驻，每 60 秒重新标定
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from . import clock, ntp

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"


@dataclass
class Calibration:
    """一次标定的结果快照。"""

    wall_ns: int  # 标定时刻的本机系统时钟
    mono_ns: int  # 同一时刻的单调秒表读数
    offset_ns: int  # 服务器时间 - 本机时间
    sigma_ns: int  # 偏差估计的离散度（1σ）
    bound_ns: int  # 单程不对称带来的最坏误差上界
    min_delay_ms: float
    median_delay_ms: float
    samples_total: int
    samples_kept: int
    servers_used: int
    servers_seen: int
    sampling_s: float  # 采样耗时
    taken_at: str  # 人类可读的北京时间
    cluster_servers: list[str]  # 参与最终估计的服务器
    per_server: list[dict]  # 每台授时服务器的明细

    @property
    def offset_ms(self) -> float:
        return self.offset_ns / 1e6


class TimeBase:
    """补偿时钟：对外给出"真实时间"，精度取决于最近一次标定。"""

    def __init__(self, cal: Calibration):
        self.cal = cal

    @classmethod
    def calibrate(
        cls,
        servers=ntp.DEFAULT_SERVERS,
        per_server: int = 6,
        timeout: float = 0.8,
        min_valid: int = 5,
        rounds: int = 2,
        round_gap_s: float = 0.25,
    ) -> "TimeBase":
        """采样并标定。

        默认跑两轮采样再合并，目的是摊平"某一瞬间网络正好拥塞"带来的偏差。
        """
        started = clock.mono_ns()
        samples: list[ntp.Sample] = []
        failures: list[str] = []
        for index in range(max(1, rounds)):
            got, bad = ntp.sample_many(
                servers=servers, per_server=per_server, timeout=timeout
            )
            samples.extend(got)
            failures.extend(bad)
            if index + 1 < rounds:
                time.sleep(round_gap_s)

        stats, rows = ntp.consensus(samples, min_valid=min_valid)
        sampling_s = (clock.mono_ns() - started) / 1e9
        if not stats:
            raise RuntimeError(
                "NTP 全部失败：检查 UDP 123 端口是否被网络或防火墙封禁。"
                f" 失败样本 {len(failures)} 条。"
            )

        # 采样结束后立刻取一次本机时刻，作为秒表与绝对时间的锚点
        wall_ns = clock.now_unix_ns()
        mono_ns = clock.mono_ns()

        cal = Calibration(
            wall_ns=wall_ns,
            mono_ns=mono_ns,
            offset_ns=stats["offset_ns"],
            sigma_ns=int(stats["sigma_ns"]),
            bound_ns=int(stats["bound_ns"]),
            min_delay_ms=stats["min_delay_ms"],
            median_delay_ms=stats["median_delay_ms"],
            samples_total=stats["total"],
            samples_kept=stats["kept"],
            servers_used=stats["servers_used"],
            servers_seen=stats["servers_seen"],
            sampling_s=sampling_s,
            taken_at=fmt(wall_ns + stats["offset_ns"]),
            cluster_servers=[row["server"] for row in stats["cluster"]],
            per_server=rows,
        )
        return cls(cal)

    def now_ns(self) -> int:
        """补偿后的真实时间（Unix 纳秒）。"""
        elapsed = clock.mono_ns() - self.cal.mono_ns
        return self.cal.wall_ns + elapsed + self.cal.offset_ns

    def now(self) -> float:
        return self.now_ns() / 1e9

    def age_s(self) -> float:
        """距上次标定过了多久（秒）。"""
        return (clock.mono_ns() - self.cal.mono_ns) / 1e9

    def uncertainty_ns(self) -> int:
        """当前总不确定度：采样离散度 + 本机晶振漂移（按 50ppm 保守估计）。"""
        drift = int(self.age_s() * 50e-6 * 1e9)
        return self.cal.sigma_ns + drift

    def mono_at(self, target_unix_ns: int) -> int:
        """把"真实世界的某个时刻"换算成本机单调秒表读数，用于精确定时。"""
        delta = target_unix_ns - self.cal.wall_ns - self.cal.offset_ns
        return self.cal.mono_ns + delta

    def to_dict(self) -> dict:
        return asdict(self.cal)

    def save(self, path: pathlib.Path | None = None) -> pathlib.Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = path or (DATA_DIR / "timebase.json")
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), "utf-8")
        return path


def fmt(unix_ns: int, with_ms: bool = True) -> str:
    dt = datetime.fromtimestamp(unix_ns / 1e9, BEIJING)
    if with_ms:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def next_midnight_ns(unix_ns: int) -> int:
    """下一个北京时间 00:00:00.000 对应的 Unix 纳秒。"""
    dt = datetime.fromtimestamp(unix_ns / 1e9, BEIJING)
    tomorrow = dt.date() + timedelta(days=1)
    target = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0, tzinfo=BEIJING)
    return int(target.timestamp() * 1e9)


def selftest(tb: TimeBase, trials: int = 200, spin_ns: int = 2_000_000) -> dict:
    """触发精度自检：反复"到点唤醒"，统计实际偏差（微秒）。"""
    clock.high_res_begin()
    clock.boost_process()
    clock.boost_current_thread("time_critical")

    short_deltas = []
    for _ in range(trials):
        target = clock.mono_ns() + random.randint(40_000_000, 90_000_000)
        actual = clock.sleep_until_mono(target, spin_ns=spin_ns)
        short_deltas.append((actual - target) / 1000.0)

    second_deltas = []
    for _ in range(20):
        target_unix = ((tb.now_ns() // 1_000_000_000) + 1) * 1_000_000_000
        target_mono = clock.mono_ns() + (tb.mono_at(target_unix) - clock.mono_ns())
        actual = clock.sleep_until_mono(target_mono, spin_ns=spin_ns)
        second_deltas.append((actual - target_mono) / 1000.0)

    def summarise(xs):
        xs = sorted(xs)
        return {
            "median_us": statistics.median(xs),
            "p95_us": xs[int(len(xs) * 0.95) - 1],
            "max_us": xs[-1],
            "mean_us": statistics.fmean(xs),
        }

    return {"short": summarise(short_deltas), "second_aligned": summarise(second_deltas)}


def measure_drift(seconds: float = 60.0) -> dict:
    """连续标定两次，测本机晶振相对标准时间的漂移率（ppm）。"""
    print("第一次标定…")
    a = TimeBase.calibrate()
    print(f"等待 {seconds:.0f} 秒后第二次标定（保持程序运行，别改系统时间）…")
    time.sleep(seconds)
    b = TimeBase.calibrate()

    delta_ns = b.cal.offset_ns - a.cal.offset_ns
    ppm = delta_ns / (seconds * 1e9) * 1e6
    # 要让 0 点误差控制在 5ms 内，最迟多久必须重新标定一次
    if abs(ppm) > 1e-9:
        interval_s = 5e-3 / (abs(ppm) * 1e-6)
    else:
        interval_s = float("inf")
    return {
        "seconds": seconds,
        "delta_ms": delta_ns / 1e6,
        "ppm": ppm,
        "recommended_recal_interval_s": interval_s,
    }


def repeat_calibration(runs: int = 5, per_server: int = 8) -> dict:
    """连续独立标定多次，看结果自己重不重复得出来。

    这才是"我们到底能把 0 点卡多准"的真实数字：单次标定内部的离散度
    只反映采样噪声，而独立多次标定之间的波动，才反映路径选择带来的系统性误差。
    """
    offsets = []
    for i in range(runs):
        tb = TimeBase.calibrate(per_server=per_server, rounds=2)
        offsets.append(tb.cal.offset_ms)
        print(
            f"  第 {i + 1}/{runs} 次: {tb.cal.offset_ms:+.2f} ms "
            f"(最小延迟 {tb.cal.min_delay_ms:.1f}ms, "
            f"来自 {len(tb.cal.cluster_servers)} 台)"
        )
    return {
        "runs": runs,
        "median_ms": statistics.median(offsets),
        "min_ms": min(offsets),
        "max_ms": max(offsets),
        "spread_ms": max(offsets) - min(offsets),
        "offsets_ms": offsets,
    }


def print_report(tb: TimeBase, detail: bool = True) -> None:
    off_ms = tb.cal.offset_ms
    direction = "慢" if off_ms > 0 else "快"

    print("=" * 62)
    print("时间基准标定（北京时间）")
    print("=" * 62)
    print(f"  授时服务器     : {tb.cal.servers_seen} 台应答，{tb.cal.servers_used} 台进入估计")
    print(f"  有效样本       : {tb.cal.samples_kept} / {tb.cal.samples_total}")
    print(f"  最小往返延迟   : {tb.cal.min_delay_ms:.1f} ms")
    print(f"  中位往返延迟   : {tb.cal.median_delay_ms:.1f} ms")
    print(f"  簇内一致性     : ±{tb.cal.sigma_ns / 1e6:.3f} ms (1σ)")
    print()
    print(f"  本机时钟偏差   : {off_ms:+.1f} ms  →  本机比标准时间{direction} {abs(off_ms):.1f} ms")
    if abs(off_ms) > 100:
        print(
            f"  ⚠ 偏差超过 100ms：你手动抢 0 点，等于每次都差着 {abs(off_ms):.0f}ms 才出手。"
        )
    print()
    print(f"  当前真实时间   : {fmt(tb.now_ns())}")
    nxt = next_midnight_ns(tb.now_ns())
    left = (nxt - tb.now_ns()) / 1e9
    print(
        f"  距下个 0 点    : {int(left // 3600):02d}:{int(left % 3600 // 60):02d}:{left % 60:06.3f}"
    )
    print(f"  0 点目标(纳秒) : {nxt}")
    print(f"  对应秒表读数   : {tb.mono_at(nxt)}")
    if detail:
        print(f"  采样耗时       : {tb.cal.sampling_s:.1f} s")
        print(f"  当前不确定度   : ±{tb.uncertainty_ns() / 1e6:.3f} ms")
        print(f"  估计来自       : {', '.join(tb.cal.cluster_servers)}")
        print(f"  不对称上界     : ±{tb.cal.bound_ns / 1e6:.1f} ms（最坏情况，实际远小于此）")
    print("=" * 62)


def print_per_server(tb: TimeBase) -> None:
    """每台授时服务器的明细，用来判断哪些源值得信任。"""
    print()
    print("各授时服务器明细（按路径质量排序）")
    print("-" * 62)
    print(f"  {'服务器':<28}{'最小延迟':>9}{'样本':>8}{'偏差(ms)':>12}")
    for row in tb.cal.per_server:
        print(
            f"  {row['server']:<28}"
            f"{row['min_delay_ms']:>7.1f}ms"
            f"{row['n_kept']:>4}/{row['n_total']:<4}"
            f"{row['offset_ns'] / 1e6:>+12.2f}"
        )
    print("-" * 62)
    print("  同一台服务器的偏差应该稳定；偏差彼此差得越多，说明路径不对称或源质量差。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音抢礼物工具 - 时间基准标定")
    parser.add_argument("--samples", type=int, default=6, help="每个授时服务器的采样次数")
    parser.add_argument("--rounds", type=int, default=2, help="采样轮数（多轮可摊平瞬时拥塞）")
    parser.add_argument("--timeout", type=float, default=0.8, help="单次 NTP 超时（秒）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每台授时服务器明细")
    parser.add_argument("--selftest", type=int, metavar="N", help="触发精度自检次数")
    parser.add_argument("--drift", type=float, metavar="SEC", help="测晶振漂移率，需等待 SEC 秒")
    parser.add_argument("--repeat", type=int, metavar="N", help="独立标定 N 次，看结果重不重复")
    parser.add_argument("--watch", type=float, metavar="SEC", help="常驻模式，每 SEC 秒重新标定")
    args = parser.parse_args(argv)

    clock.high_res_begin()
    try:
        tb = TimeBase.calibrate(
            per_server=args.samples, timeout=args.timeout, rounds=args.rounds
        )
    except RuntimeError as exc:
        print(f"标定失败：{exc}", file=sys.stderr)
        return 2

    print_report(tb)
    if args.verbose:
        print_per_server(tb)
    saved = tb.save()
    print(f"标定结果已保存：{saved}")

    if args.selftest:
        print()
        print(f"触发精度自检中（{args.selftest} 次短等待 + 20 次整秒对齐）…")
        res = selftest(tb, trials=args.selftest)
        for key, label in (("short", "短等待 40~90ms"), ("second_aligned", "整秒对齐")):
            s = res[key]
            print(
                f"  {label:<14} 中位 {s['median_us']:+.0f}µs | "
                f"P95 {s['p95_us']:+.0f}µs | 最大 {s['max_us']:+.0f}µs"
            )
        print("  （正值=比目标晚醒，负值=早醒。这段抖动就是本机触发精度的地板）")

    if args.drift:
        print()
        d = measure_drift(seconds=args.drift)
        print(
            f"  漂移           : {d['delta_ms']:+.3f} ms / {d['seconds']:.0f}s "
            f"= {d['ppm']:+.2f} ppm"
        )
        rec = d["recommended_recal_interval_s"]
        if rec == float("inf"):
            print("  重新标定建议   : 漂移可忽略")
        else:
            print(f"  重新标定建议   : 每 {rec:.0f} 秒一次（可保证 0 点误差 <5ms）")

    if args.repeat:
        print()
        print(f"独立标定 {args.repeat} 次，检验可重复性：")
        rep = repeat_calibration(runs=args.repeat, per_server=max(4, args.samples))
        print(
            f"  结果中位 {rep['median_ms']:+.2f} ms，"
            f"范围 {rep['min_ms']:+.2f} ~ {rep['max_ms']:+.2f} ms，"
            f"极差 {rep['spread_ms']:.2f} ms"
        )
        print("  这个极差就是当前网络条件下时钟的可信精度；越小越好。")

    if args.watch:
        print()
        print(f"常驻标定模式：每 {args.watch:.0f} 秒重新标定一次，Ctrl+C 退出。")
        try:
            while True:
                time.sleep(args.watch)
                tb = TimeBase.calibrate(
                    per_server=max(3, args.samples // 2), timeout=args.timeout
                )
                left = (next_midnight_ns(tb.now_ns()) - tb.now_ns()) / 1e9
                print(
                    f"  [{fmt(tb.now_ns())}] 偏差 {tb.cal.offset_ms:+.1f}ms "
                    f"离散 ±{tb.cal.sigma_ns / 1e6:.2f}ms "
                    f"不确定度 ±{tb.uncertainty_ns() / 1e6:.2f}ms "
                    f"距0点 {int(left // 3600):02d}:{int(left % 3600 // 60):02d}:{left % 60:06.3f}"
                )
        except KeyboardInterrupt:
            print("\n已退出常驻标定。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
