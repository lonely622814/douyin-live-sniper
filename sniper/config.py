"""配置读写。控制面板改的就是这个文件，程序每次启动读它。"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass, field

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class Config:
    # —— 目标 ——
    room_url: str = ""  # 直播间地址，如 https://live.douyin.com/123456789

    # —— 两条触发线 ——
    trigger_midnight: bool = True  # 每天 00:00:00.000 的第一个为你闪耀
    trigger_live_start: bool = False  # 主播一开播就秒抢
    send_times_raw: str = ""  # 自定义发送时间，逗号分隔，如 "14:00:00,20:30"
    live_start_hint: str = ""  # 主播大概几点开播（仅用于提前待命，不依赖它触发）

    # —— 时间参数 ——
    prewarm_lead_s: int = 90  # 提前多少秒进房 + 打开灯牌面板
    final_calibrate_s: int = 30  # 提前多少秒做最后一次对时
    reopen_lead_s: float = 1.5  # 保留字段（旧版用它做刷新）
    exit_lead_s: float = 2.0  # 开火前多少秒退出陪伴之旅（等整点再重进才算刷新）
    prewarm_refresh_s: float = 15.0  # 开火前多少秒先刷新一次陪伴之旅
    # 未开播时：刷新页面 → 等加载完 → 隔几秒再刷。设 1 秒就是"加载完立刻再刷"
    watch_refresh_s: float = 1.0
    # 刷新方式：soft=普通刷新（等同按 F5）；hard=强制刷新（等同 Ctrl+F5，绕过缓存）
    refresh_mode: str = "hard"
    # 两种刷新方式各自的间隔（秒）：刷新完等这么久再刷下一轮
    refresh_interval_hard: float = 2.0   # 强制刷新（Ctrl+F5）
    refresh_interval_soft: float = 1.0   # 普通刷新（F5）
    refresh_interval: float = 5.0        # 统一刷新间隔（秒）。别设太小：整页刷新会被风控盯上
    refresh_wait_load: bool = False      # 是否等页面加载完再判断（默认不等）
    # ── 点赞 ──
    like_enabled: bool = False
    like_mode: str = "js"      # js = 调用页面的点赞接口；mouse = 模拟手工点击
    like_rate: float = 5.0     # 每秒点赞次数
    like_max: int = 0          # 最多点多少次，0 = 不限
    # 用过的直播间地址历史：[{"url": "...", "note": "备注"}]
    rooms: list = field(default_factory=list)
    dedupe_window_s: int = 60  # 两条触发线挨得太近时只发一次

    # —— 行为 ——
    dry_run: bool = True  # True = 只瞄准不开火（第一次务必保持 True）
    keep_foreground: bool = True  # 开火前把浏览器窗口置顶

    # —— 端口 ——
    cdp_port: int = 9333
    panel_port: int = 8777

    def save(self, path: pathlib.Path | None = None) -> pathlib.Path:
        path = path or DEFAULT_PATH
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")
        return path

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> "Config":
        path = path or DEFAULT_PATH
        if not path.exists():
            config = cls()
            config.save(path)
            return config
        raw = json.loads(path.read_text("utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def update(self, patch: dict, path: pathlib.Path | None = None) -> "Config":
        for key, value in patch.items():
            if key in self.__dataclass_fields__:
                setattr(self, key, value)
        self.save(path)
        return self
