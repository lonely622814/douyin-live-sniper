"""极简 SNTP 客户端：只用标准库，从多个授时服务器并行采集时间样本。

采用标准 NTP 四点法算偏差，并用往返延迟做质量过滤：延迟越小的样本，
偏差估计越可信；被网络排队拖慢的样本一律丢掉。
"""

from __future__ import annotations

import socket
import statistics
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import clock

NTP_PORT = 123
NTP_EPOCH_OFFSET = 2_208_988_800  # 1900-01-01 到 1970-01-01 的秒数

# 国内可达性较好、自身同步质量高的授时源，覆盖多家 anycast 与专线授时服务。
# anycast 源（阿里云 / 腾讯云）通常离用户最近，是低延迟样本的主力。
DEFAULT_SERVERS = (
    "ntp.aliyun.com",
    "ntp1.aliyun.com",
    "ntp2.aliyun.com",
    "ntp3.aliyun.com",
    "ntp4.aliyun.com",
    "ntp5.aliyun.com",
    "time1.cloud.tencent.com",
    "time2.cloud.tencent.com",
    "time3.cloud.tencent.com",
    "ntp.ntsc.ac.cn",  # 中科院国家授时中心
    "ntp.tuna.tsinghua.edu.cn",
    "ntp.sjtu.edu.cn",
    "cn.pool.ntp.org",
)

_REQUEST = b"\x1b" + 47 * b"\x00"  # LI=0 VN=3 Mode=3（客户端）


@dataclass(slots=True)
class Sample:
    server: str
    offset_ns: int  # 服务器时间 - 本机时间
    delay_ns: int  # 往返延迟
    stratum: int

    @property
    def offset_ms(self) -> float:
        return self.offset_ns / 1e6

    @property
    def delay_ms(self) -> float:
        return self.delay_ns / 1e6


def _parse_timestamp(raw: bytes) -> int:
    """NTP 时间戳（64 位定点）转 Unix 纳秒。"""
    secs, frac = struct.unpack("!II", raw)
    return (secs - NTP_EPOCH_OFFSET) * 1_000_000_000 + ((frac * 1_000_000_000) >> 32)


def query(server: str, timeout: float = 0.8) -> Sample:
    """向单个服务器发一次 SNTP 请求并计算偏差。失败抛 OSError。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = clock.now_unix_ns()
        sock.sendto(_REQUEST, (server, NTP_PORT))
        data, _ = sock.recvfrom(1024)
        t4 = clock.now_unix_ns()

    if len(data) < 48:
        raise OSError(f"{server}: 响应过短 ({len(data)} 字节)")

    mode = data[0] & 0x07
    stratum = data[1]
    if mode not in (4, 5):
        raise OSError(f"{server}: 非服务器模式响应 (mode={mode})")
    if stratum == 0:
        raise OSError(f"{server}: 收到 Kiss-o'-Death (stratum=0)")

    t2 = _parse_timestamp(data[32:40])  # 服务器接收时刻
    t3 = _parse_timestamp(data[40:48])  # 服务器发送时刻

    offset_ns = ((t2 - t1) + (t3 - t4)) // 2
    delay_ns = (t4 - t1) - (t3 - t2)
    if delay_ns < 0:
        raise OSError(f"{server}: 时间戳异常（负延迟）")

    return Sample(server=server, offset_ns=offset_ns, delay_ns=delay_ns, stratum=stratum)


def sample_many(
    servers=DEFAULT_SERVERS,
    per_server: int = 6,
    timeout: float = 0.8,
    workers: int = 24,
    retries: int = 1,
):
    """并行采集，返回 (成功样本列表, 失败原因列表)。"""
    jobs = [(s, n) for s in servers for n in range(per_server)]
    samples: list[Sample] = []
    failures: list[str] = []

    def run(server: str):
        last_err = None
        for _ in range(retries + 1):
            try:
                return query(server, timeout=timeout)
            except OSError as exc:
                last_err = exc
        return last_err

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(run, [job[0] for job in jobs]):
            if isinstance(result, Sample):
                samples.append(result)
            elif result is not None:
                failures.append(str(result))

    return samples, failures


def _weighted_median(pairs):
    """按权重取中位数，pairs = [(值, 权重), ...]。"""
    pairs = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= total / 2:
            return value
    return pairs[-1][0]


def consensus(samples, slack_ms: float = 2.0, min_valid: int = 3):
    """多服务器共识估计时钟偏差。

    单看所有样本的中位数并不稳——一条慢路径上的一堆样本会把结果拽偏。
    这里改成"每台服务器先自己收敛出一个结论，再整体投票"：

    1. 每台服务器只保留自己延迟最低的那一簇样本（延迟差 < slack），取中位数；
    2. 各服务器按 ``1/(最小延迟+2ms)²`` 加权，取加权中位数；
    3. 用各服务器结论之间的 MAD 估计不确定度——这台机器
       "和世界时间到底差多少"的真实把握程度，就体现在这个数上。

    返回 (统计字典, 每台服务器的明细列表)。
    """
    if not samples:
        return {}, []

    by_server: dict[str, list[Sample]] = {}
    for s in samples:
        by_server.setdefault(s.server, []).append(s)

    rows = []
    for server, group in by_server.items():
        min_delay = min(s.delay_ns for s in group)
        cutoff = max(min_delay * 1.5, min_delay + slack_ms * 1e6)
        kept = [s for s in group if s.delay_ns <= cutoff]
        offsets = sorted(s.offset_ns for s in kept)
        offset = statistics.median(offsets)
        mad = statistics.median(abs(o - offset) for o in offsets) if len(offsets) > 1 else 0.0
        # 路径越远越"薄"，权重越低
        weight = 1.0 / ((min_delay / 1e6 + 2.0) ** 2)
        rows.append(
            {
                "server": server,
                "n_total": len(group),
                "n_kept": len(kept),
                "min_delay_ms": min_delay / 1e6,
                "offset_ns": int(offset),
                "mad_ns": mad,
                "weight": weight,
            }
        )

    rows.sort(key=lambda r: r["min_delay_ms"])
    all_delays = [s.delay_ns for s in samples]
    global_min_ms = min(all_delays) / 1e6

    # 只信最快的那一簇路径。
    #
    # NTP 四点法默认去程回程耗时相同，实际链路并不对称，估计偏差里就混进了
    # (去程 - 回程)/2 这个系统性误差。它的大小跟路径长度成正比，所以越慢的
    # 服务器越不可信（实测这台机器上，38ms 的源和 222ms 的源能差出 60ms）。
    # 做法：以全局最小延迟为基准，只保留 1.8 倍以内的服务器。
    cluster = [r for r in rows if r["min_delay_ms"] <= max(global_min_ms * 1.5, global_min_ms + 5.0)]
    if len(cluster) < min_valid:
        cluster = rows[: max(1, min(min_valid, len(rows)))]

    offset = int(_weighted_median([(r["offset_ns"], r["weight"]) for r in cluster]))
    residuals = [abs(r["offset_ns"] - offset) for r in cluster]
    mad = statistics.median(residuals) if len(residuals) > 1 else 0.0
    # 下限 0.15ms，避免样本太少时给出"过度自信"的小数
    sigma = max(mad * 1.4826, 150_000.0)

    stats = {
        "total": len(samples),
        "kept": sum(r["n_kept"] for r in cluster),
        "min_delay_ms": global_min_ms,
        "median_delay_ms": statistics.median(all_delays) / 1e6,
        "offset_ns": offset,
        "mad_ns": mad,
        "sigma_ns": sigma,
        "max_residual_ns": max(residuals) if residuals else 0,
        "servers_used": len(cluster),
        "servers_seen": len(rows),
        "cluster": cluster,
        "per_server": rows,
        # 单程不对称在最坏情况下的影响上界：RTT/2
        "bound_ns": int(global_min_ms / 2 * 1e6),
    }
    return stats, rows
