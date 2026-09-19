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
    watch_enabled: bool = True  # 开播监听总开关：关了就不守候、不判定、不刷新
    refresh_enabled: bool = True  # 页面刷新兜底开关：关了只靠页面 JS 自己上报
    auto_recycle: bool = True  # 自动换标签（内存整理）：页面堆太大时换新标签重建渲染进程
    # 用哪个浏览器打开直播间：auto=自动（先 Chrome 后 Edge）/ chrome / edge / exe 完整路径
    browser: str = "auto"

    # ── 挂机抢福袋 / 红包（网页版，阶段 1）──
    giveaway_enabled: bool = False        # 挂机总开关（默认关，你在控制台手动开）
    giveaway_rooms: list = field(default_factory=list)   # 房间列表（阶段 2 会用自动抓取补充）
    rooms_auto: bool = True               # 自动抓房间列表（关注页 + 直播首页）
    giveaway_auto_comment: bool = True    # 允许自动发评论（=点"一键发评论参与福袋"）
    giveaway_allow_lamp: bool = False     # 是否允许"灯牌/粉丝团/送礼物"这类花钱条件的福袋
    follow_auto: bool = True              # 条件要关注主播时自动关注（并记进名单）
    redpacket_auto: bool = True           # 自动领红包（阶段 3）
    engage_enabled: bool = True           # 养号：在直播间定时刷评论（阶段 4）
    giveaway_min_left_s: int = 20         # 倒计时少于这么多秒就不参与（来不及）
    giveaway_max_left_s: int = 600        # 倒计时多于这么多秒就换下一个房间（不干等）
    giveaway_room_hold_s: int = 180       # 一个房间最多待多久没福袋就换
    giveaway_comment_per_room_hour: int = 3    # 每个房间每小时最多发几条评论
    giveaway_comment_per_hour: int = 20        # 全局每小时最多发几条评论
    giveaway_daily_limit: int = 200       # 每天最多参与多少个福袋
    # 奖品筛选（照抄原项目 contains_want / contains_not_want，"不想要"优先）
    giveaway_want_keywords: list = field(default_factory=list)
    giveaway_skip_keywords: list = field(default_factory=list)
    # 参与成功后随机静默多久（拟人化，照抄原项目 random_delay(180,420)）
    giveaway_post_join_wait_min: int = 0
    giveaway_post_join_wait_max: int = 0
    rest_periods: list = field(default_factory=list)   # 不挂机时段，如 ["03:00-07:00"]
    room_blacklist: list = field(default_factory=list)  # 不去的房间/主播
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
