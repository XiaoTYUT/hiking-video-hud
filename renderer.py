"""KML 轨迹数据层 + 视频渲染层

纯后端：解析 → 计算 → 渲染帧，GUI 和 CLI 都共用这里。
"""

from __future__ import annotations

import io
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

Point = Tuple[float, float, float, float]  # lon, lat, elev, rel_time


# ─────────────────────── 瓦片地图工具 ───────────────────────

TILE_CACHE_DIR = (Path(os.environ.get("APPDATA", Path.home())) / "KmlPath" / "tiles")

GAODE_TILE_URL = ("https://webrd0{s}.is.autonavi.com/appmaptile"
                  "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}")


def _lonlat_to_webmerc_px(lon: float, lat: float, z: int) -> Tuple[float, float]:
    """经纬度 → Web 墨卡托全局像素坐标（zoom=z）。"""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n * 256.0
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * 256.0
    return x, y


def _webmerc_px_to_lonlat(x: float, y: float, z: int) -> Tuple[float, float]:
    """Web 墨卡托全局像素坐标 → 经纬度（zoom=z）。_lonlat_to_webmerc_px 的反函数。"""
    n = 2 ** z
    lon = x / (n * 256.0) * 360.0 - 180.0
    y_norm = y / (n * 256.0)   # 0..1（北=0，南=1）
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y_norm)))
    return lon, math.degrees(lat_rad)


# ── WGS-84 → GCJ-02（高德瓦片是火星坐标；KML 是 WGS-84，仅地图显示时转换）──

def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi)
            + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi)
            + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi)
            + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi)
            + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi)
            + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi)
            + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _wgs84_to_gcj02(lon: float, lat: float) -> Tuple[float, float]:
    """WGS-84 → GCJ-02。中国大陆以外区域不偏移，直接返回原坐标。"""
    if not (73.66 < lon < 135.05 and 3.86 < lat < 53.55):
        return lon, lat
    a = 6378245.0                    # 克拉索夫斯基椭球长半轴
    ee = 0.00669342162296594323      # 偏心率平方
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = math.radians(lat)
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat


def _fetch_tile(z: int, x: int, y: int):
    """下载一个地图瓦片，带磁盘缓存。返回 PIL Image 或 None。"""
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = TILE_CACHE_DIR / f"{z}_{x}_{y}.png"
    if cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGBA")
        except Exception:
            cache_file.unlink(missing_ok=True)

    s = (x + y) % 4 + 1
    url = GAODE_TILE_URL.format(s=s, x=x, y=y, z=z)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        cache_file.write_bytes(data)
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None

KML_NS = {"kml": "http://www.opengis.net/kml/2.2",
          "gx": "http://www.google.com/kml/ext/2.2"}


def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ─────────────────────── 数据计算 ───────────────────────

def parse_kml(kml_path: Path) -> tuple[List[Point], dict]:
    tree = ET.parse(kml_path)
    root = tree.getroot()
    raw = []
    for elem in root.findall(".//gx:coord", KML_NS):
        parts = elem.text.strip().split()
        if len(parts) >= 3:
            raw.append((float(parts[0]), float(parts[1]), float(parts[2])))
    meta = {}
    for data in root.findall(".//kml:ExtendedData/kml:Data", KML_NS):
        name = data.get("name")
        val_elem = data.find("kml:value", KML_NS)
        if name and val_elem is not None and val_elem.text:
            meta[name] = val_elem.text.strip()

    n = len(raw)
    # ── 逐点瞬时速度：GxTrackExtendedData 第一列（m/s，与 coord 一一对应）──
    # 两步路导出格式 "speed,0.0;speed,0.0;..."；第二列预留（心率/步频位，实测全 0）
    speeds: List[float] = []
    ext_raw = meta.pop("GxTrackExtendedData", "")
    if ext_raw:
        for item in ext_raw.split(";"):
            item = item.strip()
            if not item:
                continue
            try:
                speeds.append(max(0.0, float(item.split(",")[0])))
            except (ValueError, IndexError):
                speeds.append(0.0)
    if len(speeds) < n:
        speeds += [0.0] * (n - len(speeds))
    elif len(speeds) > n:
        speeds = speeds[:n]
    # ── 时间：优先用 <gx:Track> 下的 <when> 时间戳 ──
    # 两步路 KML 中 <when> 与 <gx:coord> 一一对应，
    # 总运动时长 = 最后一个 when - 第一个 when（含暂停，与两步路 UI 一致）
    whens = root.findall(".//gx:Track/kml:when", KML_NS)
    rel_times = None
    if len(whens) >= n:
        try:
            ts = [datetime.strptime(w.text.strip(), "%Y-%m-%dT%H:%M:%SZ")
                  for w in whens[:n]]
            t0 = ts[0]
            rel_times = [(t - t0).total_seconds() for t in ts]
        except (ValueError, TypeError):
            rel_times = None

    if rel_times is None:
        # 回退：用 Distance / SportAvgSpeed 反推
        dist_meta = float(meta.get("Distance", 0) or meta.get("Mileage", 0))
        avg_speed = float(meta.get("SportAvgSpeed", 0))
        if dist_meta > 0 and avg_speed > 0.1:
            total_active_s = dist_meta / avg_speed
        else:
            begin_ms = int(meta.get("BeginTime", 0))
            end_ms = int(meta.get("EndTime", 0))
            pause_s = float(meta.get("PauseTime", 0)) / 1000.0
            total_active_s = max(0, (end_ms - begin_ms) / 1000.0 - pause_s)
        rel_times = list(np.linspace(0, total_active_s, n)) if n > 1 else [0.0]

    # ── 时长口径（与两步路 UI 对齐，已用 dog.kml 实测校准）──
    #   PauseTime 单位 = 毫秒（dog: 5564027ms=5564s，运动=墙钟-5564=06:01:05 与两步路精确一致）
    #   运动耗时 total_active_s = 墙钟(EndTime-BeginTime) - PauseTime/1000
    #   总耗时   total_wall_s   = 墙钟(EndTime-BeginTime)
    #   <when> = 墙钟轴（含暂停段），仅用于驱动轨迹节奏（标记停顿真实）
    begin_ms = int(meta.get("BeginTime", 0))
    end_ms = int(meta.get("EndTime", 0))
    pause_s = float(meta.get("PauseTime", 0)) / 1000.0   # 毫秒 → 秒
    wall_s = max(0.0, (end_ms - begin_ms) / 1000.0) if begin_ms and end_ms else 0.0
    when_span = rel_times[-1] if rel_times else 0.0
    if wall_s > 0:
        total_active_s = max(0.0, wall_s - pause_s)
        total_wall_s = wall_s
    else:
        # 无墙钟元数据：退回 when 跨度口径
        total_active_s = when_span
        total_wall_s = when_span + pause_s

    # when 时间戳保留真实停顿节奏（标记在停顿时段真实地停在原地）
    points = [(lon, lat, elev, float(t)) for (lon, lat, elev), t in zip(raw, rel_times)]
    extra = {
        "begin_ms": begin_ms,
        "end_ms": end_ms,
        "pause_s": pause_s,
        "total_active_s": total_active_s,
        "total_wall_s": total_wall_s,
        "when_span_s": when_span,
        "distance_m": float(meta.get("Distance", 0)),
        "elev_gain_m": float(meta.get("ElevationGain", 0)),
        "elev_loss_m": float(meta.get("ElevationLoss", 0)),
        "name": meta.get("Name", ""),
        "sport": meta.get("SportTypeName", ""),
        "speeds": speeds,
    }
    return points, extra


def smooth_elevation(points: List[Point], window: int = 7) -> List[Point]:
    if window < 2 or len(points) < window:
        return points
    elevs = np.array([p[2] for p in points])
    kernel = np.ones(window) / window
    smoothed = np.convolve(elevs, kernel, mode="same")
    return [(lon, lat, float(smoothed[i]), t) for i, (lon, lat, _, t) in enumerate(points)]


def compute_cumulative_ascent(points: List[Point]) -> List[float]:
    cum = [0.0]
    for i in range(1, len(points)):
        cum.append(cum[-1] + max(points[i][2] - points[i - 1][2], 0.0))
    return cum


def compute_cumulative_distance(points: List[Point]) -> List[float]:
    cum = [0.0]
    for i in range(1, len(points)):
        lon1, lat1, _, _ = points[i - 1]
        lon2, lat2, _, _ = points[i]
        cum.append(cum[-1] + _haversine(lon1, lat1, lon2, lat2))
    return cum


def find_current_idx(cum_time: List[float], target: float) -> int:
    lo, hi = 0, len(cum_time) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cum_time[mid] <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _find_idx_float(cum_time: List[float], target: float) -> float:
    """线性插值版的 find_current_idx：返回 lo 与 lo+1 之间的浮点 idx。

    用于地图导航面板中心点平滑滑动——地图中心是连续的墨卡托像素坐标，
    不像轨迹标记必须落在离散点上。整数版跳帧造成地图「一卡一卡」。
    """
    i = find_current_idx(cum_time, target)
    n = len(cum_time)
    if i >= n - 1:
        return float(i)
    t0, t1 = cum_time[i], cum_time[i + 1]
    if t1 <= t0:
        return float(i)
    return i + (target - t0) / (t1 - t0)


# ─────────────────────── 坡度 / 配速 ───────────────────────

SLOPE_LIMIT_PCT = 60.0        # 坡度物理合理裁剪区间（±60% ≈ ±31°，覆盖野路台阶）
SLOPE_MIN_DIST_M = 5.0        # 窗口水平距离小于该值不计算坡度（防 GPS 噪声）
PACE_MIN_SPEED = 0.3          # m/s，低于此视为停顿/无效（约 18 分钟/公里）


def compute_slope_deg(points: List[Point], elevations: List[float],
                      half: int = 3) -> List[float]:
    """逐点坡度（角度，单位 °）：对称窗口内 arctan(海拔差 / 水平距离)。

    - 使用平滑后的海拔，窗口 ±half 个点（徒步采样 1~6s → 跨度约 20~40m）
    - 距离 < 5m 时置 0（防除零与静止噪声）
    - 先按百分比裁剪到 ±60%（≈ ±31°），再换算成角度（保留负坡=下坡）
    """
    n = len(points)
    out = [0.0] * n
    for i in range(n):
        a = max(0, i - half)
        b = min(n - 1, i + half)
        dh = elevations[b] - elevations[a]
        dist = _haversine(points[a][0], points[a][1],
                          points[b][0], points[b][1])
        if dist < SLOPE_MIN_DIST_M:
            out[i] = 0.0
        else:
            pct = max(-SLOPE_LIMIT_PCT,
                      min(SLOPE_LIMIT_PCT, dh / dist * 100.0))
            out[i] = math.degrees(math.atan(pct / 100.0))
    return out


def compute_pace_s_per_km(points: List[Point], speeds: List[float],
                          half: int = 2) -> List[float | None]:
    """逐点配速（秒/公里）。

    优先用 KML 自带瞬时速度（窗口内有效速度取均值，天然平滑）；
    无速度数据时回退为「窗口水平距离 / 窗口真实时间差」。
    速度 < 0.3 m/s（停顿）→ None（面板显示 --'--）。
    """
    n = len(points)
    out: List[float | None] = [None] * n
    have_speeds = any(v > PACE_MIN_SPEED for v in speeds)
    for i in range(n):
        a = max(0, i - half)
        b = min(n - 1, i + half)
        v = 0.0
        if have_speeds:
            vals = [speeds[j] for j in range(a, b + 1)
                    if speeds[j] > PACE_MIN_SPEED]
            if vals:
                v = sum(vals) / len(vals)
        if v <= PACE_MIN_SPEED:
            # 回退：窗口距离 / 窗口时间（跳过 dt<=0）
            dt = points[b][3] - points[a][3]
            if dt > 0:
                dist = _haversine(points[a][0], points[a][1],
                                  points[b][0], points[b][1])
                v = dist / dt if dist >= SLOPE_MIN_DIST_M else 0.0
        out[i] = 1000.0 / v if v > PACE_MIN_SPEED else None
    return out


def format_pace(sec_per_km: float | None) -> str:
    """配速格式 m'ss"/km；停顿/无效 → --'--"/km。"""
    if sec_per_km is None or sec_per_km <= 0:
        return "--'--\"/km"
    s = int(round(sec_per_km))
    return f"{s // 60}'{s % 60:02d}\"/km"


# ─────────────────────── 坐标投影 ───────────────────────

def compute_projection(points: List[Point], width: int, height: int,
                       margin: int = 80):
    """返回 project(lon, lat) -> (x, y) 投影函数（含轨迹包围盒 + 边距计算）。"""
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    lon_min, lon_max = min(lons), max(lons)
    lat_min, lat_max = min(lats), max(lats)
    if lon_max == lon_min:
        lon_max += 1e-6
    if lat_max == lat_min:
        lat_max += 1e-6
    cw = width - 2 * margin
    ch = height - 2 * margin
    span_lon = lon_max - lon_min
    span_lat = lat_max - lat_min
    aspect_data = span_lon / span_lat if span_lat else 1.0
    aspect_canvas = cw / ch
    if aspect_data > aspect_canvas:
        px_scale = cw / span_lon
        py_scale = cw / span_lon
    else:
        px_scale = ch / span_lat
        py_scale = ch / span_lat
    drawn_w = span_lon * px_scale
    drawn_h = span_lat * py_scale
    offset_x = margin + (cw - drawn_w) / 2
    offset_y = margin + (ch - drawn_h) / 2

    def project(lon: float, lat: float) -> Tuple[float, float]:
        return (offset_x + (lon - lon_min) * px_scale,
                offset_y + (lat_max - lat) * py_scale)

    def inverse(x: float, y: float) -> Tuple[float, float]:
        """scene 像素 → WGS 经纬度（project 的反函数）。"""
        return ((x - offset_x) / px_scale + lon_min,
                lat_max - (y - offset_y) / py_scale)

    project.inverse = inverse   # 挂在闭包上，需要反投影的调用方可用
    return project


def project_points(points: List[Point], width: int, height: int,
                   margin: int = 80) -> List[Tuple[float, float]]:
    """经纬度 → 画布像素（浮点坐标）。"""
    project = compute_projection(points, width, height, margin)
    return [project(p[0], p[1]) for p in points]


# ─────────────────────── 格式化 ───────────────────────

def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    cands = (["C:/Windows/Fonts/msyhbd.ttc",       # 雅黑 Bold
              "C:/Windows/Fonts/simhei.ttf"]
             if bold else
             ["C:/Windows/Fonts/msyh.ttc",
              "C:/Windows/Fonts/simhei.ttf",
              "C:/Windows/Fonts/simsun.ttc"])
    for p in cands:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


# ─────────────────────── Renderer ───────────────────────

class Renderer:
    """持有所有轨迹数据 + 用户编辑过的布局，负责输出单帧和整个视频。"""

    def __init__(self, kml_path: Path, width: int = 1920, height: int = 1080):
        self.width = width
        self.height = height
        # 投影边距：超采样渲染时按倍率一起放大，保证排版比例不变
        self.proj_margin = 80
        self.render_scale = 1      # 输出倍率：1=原始尺寸；>1 超采样（像素数×N²，文字/线条更清晰）
        self._ss = 1               # 当前渲染的实时超采样因子（仅渲染期间由 render_video 设置）

        points, meta = parse_kml(kml_path)
        self.points = points
        self.meta = meta
        self.points_smoothed = smooth_elevation(points, 15)

        # 累计爬升：脚本计算逐点进度，最终值对齐到 KML 元数据 ElevationGain
        raw_ascent = compute_cumulative_ascent(self.points_smoothed)
        meta_ascent = float(meta.get("elev_gain_m", 0))
        if raw_ascent[-1] > 0 and meta_ascent > 0:
            scale = meta_ascent / raw_ascent[-1]
            self.cum_ascent = [v * scale for v in raw_ascent]
        else:
            self.cum_ascent = raw_ascent

        # 累计里程：脚本计算逐点进度，最终值对齐到 KML 元数据 Distance
        raw_dist = compute_cumulative_distance(points)
        meta_dist = float(meta.get("distance_m", 0))
        if raw_dist[-1] > 0 and meta_dist > 0:
            scale = meta_dist / raw_dist[-1]
            self.cum_dist = [v * scale for v in raw_dist]
        else:
            self.cum_dist = raw_dist

        # 当前坡度（角度°，平滑海拔 + 对称窗口，物理裁剪 ±31°）
        self.slope_deg = compute_slope_deg(
            points, [p[2] for p in self.points_smoothed])
        # 当前配速（优先 KML 瞬时速度，停顿点为 None）
        self.pace_s = compute_pace_s_per_km(points, list(meta.get("speeds", [])))

        self.cum_time = [p[3] for p in points]
        # 时长口径（元数据优先，见 parse_kml 注释）：
        #   total_active_s = 运动耗时（墙钟 - PauseTime）
        #   total_wall_s   = 总耗时（墙钟）
        # cum_time(when) 只驱动节奏，动态显示值按比例映射到两个口径
        self.total_active_s = meta["total_active_s"]
        self.total_wall_s = float(meta.get("total_wall_s", 0.0)) or self.total_active_s
        self.pause_s = float(meta.get("pause_s", 0.0))
        self.when_span_s = float(meta.get("when_span_s", 0.0)) or max(self.cum_time[-1], 1e-9)

        # 投影（基于原始画布的默认布局）
        self._project = compute_projection(points, width, height, self.proj_margin)
        self.base_projected = [(self._project(lon, lat))
                               for lon, lat, _, _ in points]

        # 关键节点：用户通过 GUI 填入的 (名称, 经度, 纬度)
        self.key_nodes: List[Tuple[str, float, float]] = []

        # 用户可编辑状态（默认值）
        self.trajectory_offset = (0.0, 0.0)   # 画布坐标的平移
        self.trajectory_scale = 1.0            # 缩放（以中心为锚）
        self.trajectory_center = (width / 2, height / 2)
        self.trajectory_width: int = 16         # 轨迹基础线宽（紫色；红色 = +1px）
        self.node_font_size: int = 41           # 关键节点名称字号（粗体）
        self.node_color: Tuple[int, int, int] = (255, 200, 60)  # 标注点颜色（默认黄）

        # 数据面板显示项（GUI 可勾选开关；空 = 不画面板）
        self.panel_fields: List[str] = ["当前海拔", "当前坡度", "累计爬升",
                                         "已行里程", "当前配速",
                                         "运动耗时", "总耗时"]

        self.panel_pos = (30.0, 30.0)          # 左上角
        self.panel_scale = 1.0
        # 面板背景（半透明圆角条）：宽度与字号缩放解耦，可独立收窄/拉宽
        self.panel_bg_w: float = 320.0           # 背景条宽度（基准 px，随 panel_scale 等比）
        self.panel_bg_pad: float = 0.0           # 四周外扩边距（基准 px，负值 = 内收）
        self.panel_bg_color: Tuple[int, int, int] = (128, 128, 128)
        self.panel_bg_alpha_pct: float = 45.0    # 不透明度 0-100%（45 ≈ 115/255）

        # 动态坡度仪：独立的直角三角形，斜边倾角随当前坡度平滑变化（可单独拖动/调大小）
        self.show_slope_gauge: bool = True
        self.slope_gauge_pos: Tuple[float, float] = (40.0, float(height) - 200.0)
        self.slope_gauge_size: float = 140.0     # 示意区域边长（基准 px）
        self.gauge_label_size: float = 20.0      # 「坡度」标签字号（px）
        self.gauge_value_size: float = 24.0      # 坡度数值字号（px）
        self.gauge_edge_top_color: Tuple[int, int, int] = (255, 170, 40)     # 上边（斜边）
        self.gauge_edge_bottom_color: Tuple[int, int, int] = (255, 255, 255)  # 下边（水平基线）
        self.gauge_fill_color: Tuple[int, int, int] = (255, 170, 40)          # 内部填充
        self.gauge_fill_alpha_pct: float = 30.0  # 填充不透明度 0-100%
        self.gauge_label_color: Tuple[int, int, int] = (255, 255, 255)        # 标签颜色
        self.gauge_value_color: Tuple[int, int, int] = (255, 255, 255)        # 数值颜色
        self._gauge_series: List[float] = []   # 时间平滑后的角度序列（懒构建）

        # 地图导航面板：以当前位置为中心的小地图，始终北朝上
        self.show_map = False                    # 是否显示
        self.map_pos = (float(width - 350), 30.0)  # 左上角（默认右上）
        self.map_scale = 1.0
        self.map_size = 300                      # 基础边长（正方形）
        self.map_zoom = 15                       # 缩放级别 (3~18)，决定看到的范围
        self.map_shape = "rect"                  # "rect" 方形 / "circle" 圆形
        # 文字图例大小：在范围不变的前提下，取 z+delta 级瓦片再缩放到面板大小
        # delta=+1 文字更小(更多地名) / 0 标准 / -1 较大 / -2 特大
        self.map_text_delta = 0
        self.map_arrow_size = 14               # 位置箭头基础长度(px，scale=1时)
        # 朝向模式：False=正北朝上（地图不转，箭头指运动方向）；
        #           True=运动方向朝上（地图旋转使行进方向朝屏幕上方，指北针指示真北）
        self.map_heading_up = False

        # 分段视频：None = 未设置分段；否则 (cut_points, durations)
        # cut_points = 轨迹进度分割点列表(0<c<1 升序)，durations = 各段视频秒数
        # 段数 = len(cut_points) + 1，总时长 = sum(durations)
        # use_segments = 是否启用分段生成（False=整段均匀）；切换不丢失 segments 数据
        self.segments = None
        self.use_segments = False

        # 字体
        self.font_big = load_font(24)
        self.font_small = load_font(16)
        self.font_title = load_font(22)

    def set_size(self, width: int, height: int):
        """改变画布尺寸时重新计算投影，保持预览与输出一致。"""
        self.width = width
        self.height = height
        self._project = compute_projection(self.points, width, height, self.proj_margin)
        self.base_projected = [(self._project(lon, lat))
                               for lon, lat, _, _ in self.points]
        # 重新对齐缩放中心到新投影的包围盒中心
        xs = [p[0] for p in self.base_projected]
        ys = [p[1] for p in self.base_projected]
        self.trajectory_center = ((min(xs) + max(xs)) / 2,
                                  (min(ys) + max(ys)) / 2)

    # ── 多进程 pickle 支持 ──
    # _project 是闭包（不可 pickle）、PIL 字体含 C 指针（不可 pickle）：
    # 快照时置 None，子进程反序列化后用同一输入参数重建（compute_projection 纯函数，毫秒级）。

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_project"] = None
        state["font_big"] = None
        state["font_small"] = None
        state["font_title"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        # 兼容旧快照：补齐超采样相关属性
        self.proj_margin = getattr(self, "proj_margin", 80)
        self.render_scale = getattr(self, "render_scale", 1)
        self._ss = getattr(self, "_ss", 1)
        self._project = compute_projection(self.points, self.width, self.height,
                                           self.proj_margin)
        self.font_big = load_font(24)
        self.font_small = load_font(16)
        self.font_title = load_font(22)

    # ── 应用用户变换到轨迹 ──

    def get_projected(self) -> List[Tuple[float, float]]:
        cx, cy = self.trajectory_center
        out = []
        for x, y in self.base_projected:
            nx = cx + (x - cx) * self.trajectory_scale + self.trajectory_offset[0]
            ny = cy + (y - cy) * self.trajectory_scale + self.trajectory_offset[1]
            out.append((nx, ny))
        return out

    def get_node_projected(self) -> List[Tuple[str, float, float]]:
        """关键节点（名称 + 画布坐标，已含用户轨迹变换）。"""
        cx, cy = self.trajectory_center
        out = []
        for name, lon, lat in self.key_nodes:
            x, y = self._project(lon, lat)
            nx = cx + (x - cx) * self.trajectory_scale + self.trajectory_offset[0]
            ny = cy + (y - cy) * self.trajectory_scale + self.trajectory_offset[1]
            out.append((name, nx, ny))
        return out

    def get_bounds(self) -> Tuple[float, float, float, float]:
        """当前用户变换下轨迹的包围盒（画布坐标），用于越界检测。"""
        if not self.base_projected:
            return 0.0, 0.0, 0.0, 0.0
        xs = [p[0] for p in self.base_projected]
        ys = [p[1] for p in self.base_projected]
        cx, cy = self.trajectory_center
        s = self.trajectory_scale
        ox, oy = self.trajectory_offset
        x0 = cx + (min(xs) - cx) * s + ox
        x1 = cx + (max(xs) - cx) * s + ox
        y0 = cy + (min(ys) - cy) * s + oy
        y1 = cy + (max(ys) - cy) * s + oy
        return x0, y0, x1, y1

    # ── 渲染单帧 ──

    def render_frame(self, idx: int) -> Image.Image:
        """渲染第 idx 个轨迹点对应的帧。

        idx 由 _build_frame_plan 预计算（浮点，用于地图中心平滑）：
        - 均匀模式：按真实墙钟时间节奏分配（标记按真实速度走，停顿点停住）
        - 分段模式：段端点严格命中 cut 物理位置（点索引归一化），
          段内通过 cum_time 真实时间在段范围内插值找 idx，保留真实节奏。

        轨迹切片/标记/数据面板用 int(idx)（离散点折线，本就该跳）；
        地图导航中心用浮点 idx 插值 lon/lat（连续墨卡托像素，平滑滑动）。
        """
        n = len(self.points)
        idx_f = max(0.0, min(n - 1, float(idx)))
        idx = int(idx_f)              # 整数索引用于切片/标记/数据面板
        projected = self.get_projected()
        # 超采样因子（输出倍率）：画布与投影已按该倍率放大，
        # 这里只负责把「写死的像素尺寸」同步放大，保证构图比例不变、细节更多
        ss = int(getattr(self, "_ss", 1) or 1)

        img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # 线宽/标记随用户缩放同步（GUI 画笔随 item 缩放，视频需一致才是所见即所得）
        # 轨迹粗细可由用户设置：紫 = trajectory_width，红 = +1px 强调
        ts = max(0.25, self.trajectory_scale)
        w_purple = max(2, int(round(self.trajectory_width * ts * ss)))
        w_red = max(2, int(round((self.trajectory_width + 1) * ts * ss)))

        # 1) 完整轨迹 — 紫色 = 未走
        if len(projected) >= 2:
            pts_rounded = [(int(x), int(y)) for x, y in projected]
            d.line(pts_rounded, fill=(180, 100, 255, 200), width=w_purple)

        # 1b) 已走部分 — 红色叠加
        if idx > 0 and len(projected) >= 2:
            traveled = [(int(x), int(y)) for x, y in projected[:idx + 1]]
            if len(traveled) >= 2:
                d.line(traveled, fill=(255, 70, 70, 230), width=w_red)

        # 2) 当前位置标记
        if 0 <= idx < len(projected):
            cx, cy = projected[idx]
            for r, a in [(22 * ss, 60), (16 * ss, 120), (10 * ss, 180)]:
                d.ellipse([cx - r, cy - r, cx + r, cy + r],
                          fill=(255, 80, 80, a))
            d.ellipse([cx - 6 * ss, cy - 6 * ss, cx + 6 * ss, cy + 6 * ss],
                      fill=(255, 255, 255, 255))
            d.ellipse([cx - 4 * ss, cy - 4 * ss, cx + 4 * ss, cy + 4 * ss],
                      fill=(255, 60, 60, 255))

        # 2b) 起点/终点标记（大圆点，带白边；半径随缩放）
        if len(projected) >= 2:
            r = max(5, int(round(14 * ts * ss)))
            for (px, py), color in [(projected[0], (100, 240, 120)),
                                     (projected[-1], (240, 80, 80))]:
                d.ellipse([px - r, py - r, px + r, py + r],
                          outline=(255, 255, 255, 220), width=max(1, int(2 * ss)),
                          fill=color + (255,))

        # 2c) 关键节点（彩色圆点 + 半透明黑底条白字标签，随轨迹缩放）
        if self.key_nodes:
            node_r = max(3, int(round(8 * ts * ss)))
            node_font = load_font(max(9, int(round(self.node_font_size * ts * ss))))
            node_rgb = self.node_color + (255,)
            for name, nx, ny in self.get_node_projected():
                d.ellipse([nx - node_r, ny - node_r, nx + node_r, ny + node_r],
                          outline=(255, 255, 255, 220), width=max(1, int(2 * ss)),
                          fill=node_rgb)
                # 标签：半透明黑底条(胶囊) + 白字，任何背景下高对比
                tx, ty = nx + 12 * ts * ss, ny - 22 * ts * ss
                tb = d.textbbox((0, 0), name, font=node_font)
                pad_x, pad_y = 6 * ts * ss, 4 * ts * ss
                x0, y0 = tx + tb[0] - pad_x, ty + tb[1] - pad_y
                x1, y1 = tx + tb[2] + pad_x, ty + tb[3] + pad_y
                bar_h = y1 - y0
                d.rounded_rectangle([x0, y0, x1, y1],
                                    radius=bar_h / 2,   # 胶囊形
                                    fill=(0, 0, 0, 115))
                d.text((tx, ty), name, fill=(255, 255, 255, 255),
                       font=node_font)

        # 3) 数据面板（带用户平移缩放）
        ascent = self.cum_ascent[idx]
        dist_km = self.cum_dist[idx] / 1000.0
        elapsed = self.cum_time[idx]
        elev = self.points_smoothed[idx][2]
        slope = self.slope_deg[idx]
        pace = self.pace_s[idx]
        self._draw_panel(d, ascent, dist_km, elapsed, elev, slope, pace)

        # 3b) 动态坡度仪（独立于数据面板，可单独拖动/调大小）
        self._draw_slope_gauge(d, idx)

        # 4) 地图导航面板（以当前位置为中心，始终北朝上）
        if self.show_map:
            self._draw_map(img, d, idx_f)   # 传浮点 idx：地图中心平滑插值

        return img

    def _draw_panel(self, d: ImageDraw.ImageDraw, ascent: float,
                    dist_km: float, elapsed: float, elev: float,
                    slope: float = 0.0, pace: float | None = None) -> None:
        px, py = self.panel_pos

        # 未勾选任何数据项时不绘制面板
        if not self.panel_fields:
            return

        big_font = load_font(int(24 * self.panel_scale))
        small_font = load_font(int(20 * self.panel_scale), bold=True)

        # 面板背景：宽度 / 外扩边距 / 颜色 / 不透明度 四项互相独立（pad 负值 = 向内收）
        n = len(self.panel_fields)
        panel_h = (18 + (n - 1) * 32 + 26 + 30) * self.panel_scale
        bg_w = self.panel_bg_w * self.panel_scale
        pad = self.panel_bg_pad * self.panel_scale
        r, g, b = self.panel_bg_color
        a = int(round(max(0.0, min(100.0, self.panel_bg_alpha_pct)) * 255 / 100))
        d.rounded_rectangle(
            (px - pad, py - pad, px + bg_w + pad, py + panel_h + pad),
            radius=max(2, int(round(12 * self.panel_scale))),
            fill=(r, g, b, a))

        row_h = int(32 * self.panel_scale)
        line_start_y = py + 18 * self.panel_scale
        # when 时间轴 → 两个时长口径的线性映射（首 0、末值锚定元数据）
        #   pig(when=压缩运动轴): 比例≈1，运动耗时≈elapsed；总耗时按墙钟放大
        #   cat(when=墙钟):       总耗时≈elapsed；运动耗时按运动/墙钟比例缩小
        k = (elapsed / self.when_span_s) if self.when_span_s > 0 else 0.0
        active_elapsed = self.total_active_s * k
        wall_elapsed = self.total_wall_s * k
        all_rows = [
            ("当前海拔", f"{elev:.0f} m",          (200, 160, 255, 255)),
            ("当前坡度", f"{slope:+.1f}°",         (255, 170, 200, 255)),
            ("累计爬升", f"{ascent:.1f} m",         (80, 220, 120, 255)),
            ("已行里程", f"{dist_km:.2f} km",        (255, 210,  90, 255)),
            ("当前配速", format_pace(pace),          (140, 255, 220, 255)),
            ("运动耗时", format_duration(active_elapsed), (120, 200, 255, 255)),
            ("总耗时",   format_duration(wall_elapsed),  (255, 150, 80, 255)),
        ]
        fields = set(self.panel_fields)
        rows = [r for r in all_rows if r[0] in fields]
        for i, (label, value, color) in enumerate(rows):
            y = line_start_y + i * row_h
            # 数据标题：粗体黑边白字（stroke 随面板缩放）
            d.text((px + 16 * self.panel_scale, y), label,
                   fill=(255, 255, 255, 255), font=small_font,
                   stroke_width=max(1, int(round(self.panel_scale))),
                   stroke_fill=(0, 0, 0, 255))
            lb = d.textbbox((0, 0), label, font=small_font)
            label_w = lb[2] - lb[0]
            # 数值紧跟标签右侧，间距 10*scale
            d.text((px + 16 * self.panel_scale + label_w + 10 * self.panel_scale,
                    y - int(6 * self.panel_scale)),
                   value, fill=color, font=big_font)

    # ── 动态坡度仪 ──

    def _build_gauge_series(self, half: int = 4) -> None:
        """预计算坡度仪的平滑角度序列（±half 个点的滑动平均）。

        视频帧在多进程中独立渲染，角度必须是 idx 的纯函数（不能靠帧间状态
        做缓动），因此对逐点坡度预先做滑动平均，帧序列自然连续无跳变。
        注：窗口按点数而非时间——两步路采样间隔约 4~6s/点，时间窗常只含 1 个点。
        """
        n = len(self.points)
        if n == 0:
            self._gauge_series = []
            return
        half = max(1, int(half))
        s = self.slope_deg
        # 前缀和 O(n) 滑动平均
        pref = [0.0] * (n + 1)
        for i, v in enumerate(s):
            pref[i + 1] = pref[i] + v
        out = [0.0] * n
        for i in range(n):
            lo = max(0, i - half)
            hi = min(n, i + half + 1)
            out[i] = (pref[hi] - pref[lo]) / (hi - lo)
        self._gauge_series = out

    def prepare_gauge_smoothing(self, total_frames: int) -> None:
        """按本次渲染的总帧数重建坡度平滑序列（渲染入口调用）。

        窗口半宽 ≈ 1.5×每帧跨越的点数：相邻帧窗口重叠 ≥50%，
        角度逐帧渐变（实测 cat.kml 30fps 下每帧 ≤1°），三角不会瞬间跳转。
        """
        n = len(self.points)
        if n < 2 or total_frames < 1:
            return
        step = (n - 1) / total_frames
        self._build_gauge_series(half=max(4, int(math.ceil(step * 1.5))))

    def gauge_angle(self, idx: int) -> float:
        """坡度仪显示角度（°，时间平滑后；上坡正/下坡负）。"""
        if not self._gauge_series:
            self._build_gauge_series()
        if not self._gauge_series:
            return 0.0
        idx = max(0, min(len(self._gauge_series) - 1, int(idx)))
        return self._gauge_series[idx]

    def _draw_slope_gauge(self, d: ImageDraw.ImageDraw, idx: int) -> None:
        """动态坡度仪：直角三角形（水平下边 + 竖直闭合边 + 斜边），斜边倾角 = 当前坡度。

        上坡时斜边在基线上方、下坡在下方；角度取自 gauge_angle（帧密度自适应
        平滑序列）。上边/下边颜色、内部填充色+透明度、标签/数值字号与颜色均可配。
        独立于数据面板（panel_* 系列参数），有独立的 pos/size 与显示开关。
        """
        if not self.show_slope_gauge:
            return
        ss = int(getattr(self, "_ss", 1) or 1)
        gx, gy = self.slope_gauge_pos[0] * ss, self.slope_gauge_pos[1] * ss
        size = self.slope_gauge_size * ss

        angle = max(-32.0, min(32.0, self.gauge_angle(idx)))  # 视觉限幅
        lw = max(1, int(round(2 * ss)))

        # 直角三角形：下边（水平基线）+ 右端竖直闭合边 + 斜边（倾角 = 坡度）
        vx, vy = gx + 0.10 * size, gy + 0.40 * size
        arm = 0.55 * size
        h = arm * math.tan(math.radians(angle))
        h_min = 0.02 * size   # 近水平时保留最小可见高度
        if abs(h) < h_min:
            h = h_min if angle >= 0 else -h_min
        x2, y2 = vx + arm, vy - h   # y 向下：正坡度 y2 在基线上方

        # 内部填充（颜色/透明度可调）
        fa = int(round(max(0.0, min(100.0, self.gauge_fill_alpha_pct)) * 255 / 100))
        fc = self.gauge_fill_color
        d.polygon([(vx, vy), (x2, vy), (x2, y2)],
                  fill=(fc[0], fc[1], fc[2], fa))

        # 边线：下边（水平基线）= 下边颜色；竖直闭合边 + 上边（斜边）= 上边颜色
        tc = self.gauge_edge_top_color
        bc = self.gauge_edge_bottom_color
        d.line((vx, vy, x2, vy), fill=(bc[0], bc[1], bc[2], 255), width=lw)
        d.line((x2, vy, x2, y2), fill=(tc[0], tc[1], tc[2], 255), width=lw)
        d.line((vx, vy, x2, y2), fill=(tc[0], tc[1], tc[2], 255), width=lw)

        # 文字：「坡度」标签 + 数值（各自字号/颜色，黑边描边，垂直居中对齐）
        label, value = "坡度", f"{angle:+.1f}°"
        lf = load_font(max(8, int(round(self.gauge_label_size * ss))))
        vf = load_font(max(8, int(round(self.gauge_value_size * ss))))
        lb = d.textbbox((0, 0), label, font=lf)
        vb = d.textbbox((0, 0), value, font=vf)
        lw_, vw_ = lb[2] - lb[0], vb[2] - vb[0]
        gap = 0.05 * size
        x0 = gx + size / 2 - (lw_ + gap + vw_) / 2
        cy = gy + size * 0.84
        stroke = max(1, int(round(ss)))
        lc, vc = self.gauge_label_color, self.gauge_value_color
        d.text((x0 - lb[0], cy - (lb[3] - lb[1]) / 2 - lb[1]), label,
               fill=(lc[0], lc[1], lc[2], 255), font=lf,
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        d.text((x0 + lw_ + gap - vb[0], cy - (vb[3] - vb[1]) / 2 - vb[1]), value,
               fill=(vc[0], vc[1], vc[2], 255), font=vf,
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))

    def _get_tile(self, z: int, x: int, y: int):
        """获取瓦片（带磁盘缓存），返回 PIL Image 或 None。"""
        return _fetch_tile(z, x, y)

    def _map_render_params(self):
        """返回 (显示 zoom z, 取瓦片的 zoom zf, zf 下需裁剪边长 span, 面板像素边长 panel)。

        文字图例缩放原理：取 zf=z+delta 级瓦片，裁剪 span=panel*2^(zf-z) 像素，
        再缩放回 panel —— 显示范围与 zoom=z 完全一致，但文字图例大小改变。

        超采样（输出倍率 ss>1）：只把面板像素边长 panel×ss，z/zf/span 保持不变。
        这样地图的缩放级别与地理范围、文字图例大小都与 1× 完全一致，
        仅中间画布更大（随后视频按同比例显示时观感不变），避免超采样
        意外改变用户已调好的地图外观。
        """
        ss = int(getattr(self, "_ss", 1) or 1)
        panel_base = int(self.map_size * self.map_scale)
        z = max(3, min(18, int(self.map_zoom)))
        zf = max(3, min(18, z + int(self.map_text_delta)))
        span = panel_base * (2.0 ** (zf - z))
        panel = panel_base * ss
        return z, zf, span, panel

    def _interp_lonlat(self, idx_f: float) -> Tuple[float, float]:
        """浮点轨迹索引 → 插值经纬度（与 _draw_map 中心同一套插值）。"""
        n = len(self.points)
        jf = max(0.0, min(n - 1, float(idx_f)))
        j = int(jf)
        f = jf - j
        plon, plat = self.points[j][0], self.points[j][1]
        if j < n - 1 and f > 0.0:
            nxt = self.points[j + 1]
            plon += (nxt[0] - plon) * f
            plat += (nxt[1] - plat) * f
        return plon, plat

    def _map_movement_vec(self, idx_f: float, zf: int,
                          cx_g: float, cy_g: float, look: float = 2.0):
        """计算当前位置的运动方向向量（zf 墨卡托像素，屏幕坐标）。

        idx_f 为浮点：取插值位置 idx_f±look 两点之差，方向随帧连续变化，
        不会再在跨过整数点时台阶式突跳（30fps 下箭头朝向/地图旋转因此丝滑）。
        look 为前后各跨的点数，对称窗口抑制 GPS 抖动；
        返回 (dx, dy, length)，length≈0 表示静止。
        """
        lon0, lat0 = self._interp_lonlat(idx_f - look)
        lon1, lat1 = self._interp_lonlat(idx_f + look)
        x0, y0 = _lonlat_to_webmerc_px(*_wgs84_to_gcj02(lon0, lat0), zf)
        x1, y1 = _lonlat_to_webmerc_px(*_wgs84_to_gcj02(lon1, lat1), zf)
        dx, dy = x1 - x0, y1 - y0
        return dx, dy, math.sqrt(dx * dx + dy * dy)

    def _prefetch_map_tiles(self, idx_list: list):
        """预取视频所需的所有地图瓦片，避免逐帧网络请求。"""
        n = len(self.points)
        z, zf, span, _panel = self._map_render_params()
        self._map_zoom = z
        self._map_zf = zf
        # 运动方向朝上时地图要旋转，需覆盖旋转外接正方形（×√2）
        expand = math.sqrt(2.0) if self.map_heading_up else 1.0

        tiles_needed = set()
        # idx_list 是浮点（地图中心插值用）；预取瓦片用 floor 去重，
        # 集合大小≈离散点数，不会因浮点精度膨胀
        for idx in set(int(i) for i in idx_list):
            idx = max(0, min(n - 1, int(idx)))
            lon0, lat0 = _wgs84_to_gcj02(self.points[idx][0], self.points[idx][1])
            cx_g, cy_g = _lonlat_to_webmerc_px(lon0, lat0, zf)
            half = span / 2 * expand
            tx0 = int((cx_g - half) // 256)
            tx1 = int((cx_g + half) // 256)
            ty0 = int((cy_g - half) // 256)
            ty1 = int((cy_g + half) // 256)
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    tiles_needed.add((zf, tx, ty))

        total = len(tiles_needed)
        print(f"[地图] 预取 {total} 个高德地图瓦片 (zoom={zf})...",
              file=sys.stderr)
        for i, (zz, tx, ty) in enumerate(sorted(tiles_needed)):
            self._get_tile(zz, tx, ty)
            if (i + 1) % 20 == 0 or i + 1 == total:
                print(f"  瓦片 {i+1}/{total}", file=sys.stderr)

    def _draw_map(self, img: Image.Image, d: ImageDraw.ImageDraw, idx) -> None:
        """绘制高德瓦片地图导航面板：实时位置为中心。

        - 高德标准地图瓦片（含路名/村庄名等地理标注，WGS-84→GCJ-02 校正）
        - 方形 / 圆形两种外形（圆形用 alpha 遮罩裁剪）
        - 两种朝向：正北朝上（地图不转，箭头指运动方向）；
                    运动方向朝上（地图旋转使行进方向朝屏幕上方，指北针指示真北）
        - 文字图例大小可在范围不变时调整（取不同 zoom 瓦片再缩放）
        - idx 接受浮点：相邻两点之间线性插值 lon/lat，地图中心平滑滑动
        """
        n = len(self.points)
        idx_f = max(0.0, min(n - 1, float(idx)))
        # 浮点 idx → lon/lat 线性插值（地图中心平滑，不跳整点）
        lon0, lat0 = self._interp_lonlat(idx_f)
        ss = int(getattr(self, "_ss", 1) or 1)

        # z/zf/span/panel 统一由 _map_render_params 给出（已含超采样调整）
        z, zf, span, panel = self._map_render_params()
        mx, my = int(self.map_pos[0] * ss), int(self.map_pos[1] * ss)

        # 当前位置在 zf 全局像素坐标（KML 是 WGS-84，先转 GCJ-02 再投影）
        cx_g, cy_g = _lonlat_to_webmerc_px(*_wgs84_to_gcj02(lon0, lat0), zf)

        # 运动方向向量（浮点 idx 对称窗口，方向随帧连续）；航向朝上时裁剪旋转外接正方形
        mvx, mvy, mlen = self._map_movement_vec(idx_f, zf, cx_g, cy_g)
        heading_up = bool(self.map_heading_up) and mlen > 0.001
        expand = math.sqrt(2.0) if heading_up else 1.0
        half = span / 2 * expand

        # 需要的瓦片范围
        tx0 = int((cx_g - half) // 256)
        tx1 = int((cx_g + half) // 256)
        ty0 = int((cy_g - half) // 256)
        ty1 = int((cy_g + half) // 256)

        # 合成瓦片
        cw = (tx1 - tx0 + 1) * 256
        ch = (ty1 - ty0 + 1) * 256
        composite = Image.new("RGBA", (cw, ch), (240, 240, 240, 255))
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                tile = self._get_tile(zf, tx, ty)
                if tile is not None:
                    composite.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))

        # 裁剪到显示范围（以当前位置为中心）；round 而非 floor 截断，
        # 把亚像素位移误差限制在 ±0.5px，避免连续滑动时周期性 1px 抖动
        ox = int(round(cx_g - tx0 * 256 - half))
        oy = int(round(cy_g - ty0 * 256 - half))
        crop_span = int(round(span * expand))
        cropped = composite.crop((ox, oy, ox + crop_span, oy + crop_span))

        if heading_up:
            # 航向角：真北顺时针（0=北，90=东）；PIL rotate 正角=逆时针，
            # 东行时把地图逆时针转 90° → 东到屏幕上方，即航向朝上
            bearing = math.degrees(math.atan2(mvx, -mvy))
            big = max(panel, int(round(panel * math.sqrt(2.0))))
            if cropped.size != (big, big):
                cropped = cropped.resize((big, big), Image.LANCZOS)
            rotated = cropped.rotate(bearing, resample=Image.BICUBIC)
            # 从旋转后的外接正方形中心裁出面板大小
            bx = (big - panel) // 2
            cropped = rotated.crop((bx, bx, bx + panel, bx + panel))
        elif cropped.size != (panel, panel):
            cropped = cropped.resize((panel, panel), Image.LANCZOS)

        # 贴到视频帧上（圆形用 alpha 遮罩，保持圆外区域透明）
        if self.map_shape == "circle":
            mask = Image.new("L", (panel, panel), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, panel - 1, panel - 1], fill=255)
            img.paste(cropped, (mx, my), mask)
        else:
            img.paste(cropped, (mx, my))

        # 当前位置标记（带运动方向箭头）
        cx_panel = mx + panel // 2
        cy_panel = my + panel // 2
        al = max(4, int(self.map_arrow_size * self.map_scale * ss))  # 箭头长度
        aw = max(2, int(al * 0.5))                              # 箭头半宽

        if heading_up:
            # 地图已转向，箭头恒指屏幕上方
            tip = (cx_panel, cy_panel - al)
            base1 = (cx_panel - aw, cy_panel + al * 0.4)
            base2 = (cx_panel + aw, cy_panel + al * 0.4)
            d.polygon([tip, base1, base2],
                      outline=(255, 255, 255, 255),
                      fill=(100, 240, 120, 255))
            cr = max(2, int(al * 0.2))
            d.ellipse([cx_panel - cr, cy_panel - cr, cx_panel + cr, cy_panel + cr],
                      fill=(255, 255, 255, 255))
        elif mlen > 0.001:
            # 正北朝上：箭头按真实运动方向绘制
            dir_x = mvx / mlen
            dir_y = mvy / mlen
            tip = (cx_panel + dir_x * al, cy_panel + dir_y * al)
            perp_x, perp_y = -dir_y, dir_x
            bx = cx_panel - dir_x * al * 0.4
            by = cy_panel - dir_y * al * 0.4
            base1 = (bx + perp_x * aw, by + perp_y * aw)
            base2 = (bx - perp_x * aw, by - perp_y * aw)
            d.polygon([tip, base1, base2],
                      outline=(255, 255, 255, 255),
                      fill=(100, 240, 120, 255))
            # 中心小白点（精确定位，随箭头大小缩放）
            cr = max(2, int(al * 0.2))
            d.ellipse([cx_panel - cr, cy_panel - cr, cx_panel + cr, cy_panel + cr],
                      fill=(255, 255, 255, 255))
        else:
            # 仅1个点或静止 → 画圆点（大小随箭头设置）
            mr = max(3, int(self.map_arrow_size * self.map_scale * ss * 0.55))
            d.ellipse([cx_panel - mr, cy_panel - mr, cx_panel + mr, cy_panel + mr],
                      outline=(255, 255, 255, 255), width=max(1, int(2 * ss)),
                      fill=(100, 240, 120, 255))

        # 边框（方形=矩形描边，圆形=圆环描边）
        circle = self.map_shape == "circle"
        bw = max(1, int(2 * ss))
        if circle:
            d.ellipse([mx, my, mx + panel - 1, my + panel - 1],
                      outline=(80, 160, 255, 220), width=bw)
        else:
            d.rectangle([mx, my, mx + panel, my + panel],
                        outline=(80, 160, 255, 220), width=bw)

        # 指北针：位置固定在面板右上区域；
        # 正北朝上时指针朝上；运动方向朝上时指针反向旋转，始终指向真北
        na = max(14, int(22 * self.map_scale * ss))
        bg_r = max(10, int(12 * self.map_scale * ss))
        if circle:
            # 圆形时沿 45° 方向收进圆内（0.72×半径处）
            off = 0.72 * panel / 2 * math.sqrt(2) / 2
            ccx = cx_panel + off
            ccy = cy_panel - off
        else:
            ccx = mx + panel - bg_r - 6 * self.map_scale * ss
            ccy = my + bg_r + 6 * self.map_scale * ss
        # 指针屏幕角（自屏幕上方顺时针为正）：航向朝上时 = -bearing
        needle_ang = math.radians(-bearing) if heading_up else 0.0
        tip = (ccx + math.sin(needle_ang) * na / 2,
               ccy - math.cos(needle_ang) * na / 2)
        tail = (ccx - math.sin(needle_ang) * na / 2,
                ccy + math.cos(needle_ang) * na / 2)
        # 半透明背景圆（让指北针在浅色地图上可见）
        d.ellipse([ccx - bg_r, ccy - bg_r, ccx + bg_r, ccy + bg_r],
                  fill=(0, 0, 0, 130))
        d.line([tail, tip], fill=(255, 255, 255, 255),
               width=max(2, int(2 * self.map_scale * ss)))
        head = max(4, int(6 * self.map_scale * ss))
        # 箭头两翼：针尖沿指针方向回退 head，垂直展开
        ux, uy = (tip[0] - tail[0]) / max(1e-6, na / 2), (tip[1] - tail[1]) / max(1e-6, na / 2)
        bx = tip[0] - ux * head
        by = tip[1] - uy * head
        d.polygon([tip,
                   (bx - uy * head * 0.6, by + ux * head * 0.6),
                   (bx + uy * head * 0.6, by - ux * head * 0.6)],
                  fill=(255, 255, 255, 255))
        # N 字保持正立，放在针尖外侧
        nf = load_font(max(10, int(14 * self.map_scale * ss)), bold=True)
        d.text((tip[0] + ux * (head + 2 * ss) - 4 * ss,
                tip[1] + uy * (head + 2 * ss) - 6 * ss),
               "N", fill=(255, 255, 255, 255), font=nf)

    # ── 视频生成 ──

    def _build_frame_plan(self, fps: int, duration_s: int):
        """计算每帧对应的轨迹点索引（idx_list）。

        均匀模式：按真实墙钟时间节奏分配 idx（标记按真实速度走，停顿点停住）。
        分段模式：段端点严格命中 cut 物理位置（按点索引归一化），
                  段内通过 cum_time 在段范围内线性插值找 idx，保留真实节奏。
        """
        cuts, durations = self.segments if (self.use_segments and self.segments) else ([], [])
        if not cuts:
            return self._build_uniform(fps, duration_s)
        return self._build_segmented(fps, cuts, durations)

    def _build_uniform(self, fps: int, duration_s: int):
        """均匀模式：u=f/(N-1) 线性映射到真实时间，_find_idx_float 算浮点 idx。

        浮点 idx 仅用于地图导航中心平滑；轨迹切片/标记/数据面板仍用 int(idx)。
        """
        n = len(self.points)
        total_frames = max(1, fps * duration_s)
        if total_frames == 1:
            return 1, [float(n - 1)]
        when_span = self.when_span_s
        idx_list = []
        for f in range(total_frames):
            u = f / (total_frames - 1)
            t = u * when_span
            idx_list.append(_find_idx_float(self.cum_time, t))
        idx_list[-1] = float(n - 1)   # 兜底：末帧必到终点
        return total_frames, idx_list

    def _build_segmented(self, fps: int, cuts: list, durations: list):
        """分段模式：段端点严格命中 cut 物理位置，段内按真实时间节奏走。"""
        n = len(self.points)
        bounds = [0.0] + [float(c) for c in cuts] + [1.0]
        if len(durations) != len(bounds) - 1:
            return self._build_uniform(fps, durations[0] if durations else 1)

        # 段端点物理索引 + 真实墙钟时间
        seg_end_idx = [max(0, min(n - 1, int(round(b * (n - 1))))) for b in bounds]
        seg_end_time = [self.cum_time[i] for i in seg_end_idx]

        idx_list = []
        for i, dur in enumerate(durations):
            nf = max(1, int(round(dur * fps)))
            idx_a, idx_b = seg_end_idx[i], seg_end_idx[i + 1]
            t_a, t_b = seg_end_time[i], seg_end_time[i + 1]
            for f in range(nf):
                # 段末帧强制到段终点物理位置（保证「段结束→cut」严格命中）
                if f == nf - 1:
                    idx_list.append(float(idx_b))
                    continue
                u = f / nf                       # 左闭右开 [0,1)
                t = t_a + u * (t_b - t_a)        # 段内 u 线性映射到真实时间
                idx = _find_idx_float(self.cum_time, t)
                # 约束到段物理范围（防 cum_time 非严格单调溢出）
                idx = max(float(idx_a), min(float(idx_b), idx))
                idx_list.append(idx)
        idx_list[-1] = float(n - 1)              # 兜底：视频最后一帧必到终点
        return len(idx_list), idx_list

    # ── 超采样（输出倍率）──
    # 思路：把画布/投影/布局整体按 ss 倍放大，所有写死的像素尺寸也随之 ×ss。
    # 构图比例完全不变（因为所有尺寸同比例放大），但像素数 ×ss²，
    # 播放器把 620 宽的视频放大到全屏时不再需要插值拉伸，文字/线条因此更清晰。

    def _apply_render_scale(self, ss: int) -> dict:
        """临时按 ss 倍放大画布与布局，返回原值快照（供 _restore_render_scale 恢复）。"""
        ss = max(1, int(ss or 1))
        saved = {
            "width": self.width,
            "height": self.height,
            "proj_margin": self.proj_margin,
            "panel_scale": self.panel_scale,
            "panel_pos": self.panel_pos,
            "trajectory_offset": self.trajectory_offset,
            "trajectory_center": self.trajectory_center,
            "_ss": self._ss,
        }
        self._ss = ss
        if ss == 1:
            return saved
        self.width = int(self.width * ss)
        self.height = int(self.height * ss)
        self.proj_margin = int(self.proj_margin * ss)
        self.panel_scale = self.panel_scale * ss
        self.panel_pos = (self.panel_pos[0] * ss, self.panel_pos[1] * ss)
        self.trajectory_offset = (self.trajectory_offset[0] * ss,
                                  self.trajectory_offset[1] * ss)
        # map_pos 不在此缩放：_draw_map 内部按 ss 显式换算
        self.set_size(self.width, self.height)
        return saved

    def _restore_render_scale(self, saved: dict) -> None:
        """从快照恢复画布与布局（渲染结束/失败/取消后调用）。"""
        if not saved:
            return
        self.width = saved["width"]
        self.height = saved["height"]
        self.proj_margin = saved["proj_margin"]
        self.panel_scale = saved["panel_scale"]
        self.panel_pos = saved["panel_pos"]
        self.trajectory_offset = saved["trajectory_offset"]
        self._ss = saved["_ss"]
        self.set_size(self.width, self.height)
        self.trajectory_center = saved["trajectory_center"]

    def render_video(self, out_path: Path, fps: int, duration_s: int,
                     progress_cb=None, workers: int = 0,
                     render_scale: int = 1) -> Path:
        """渲染视频。workers: 并行进程数，0=自动(CPU 核数)，1=单进程。

        render_scale: 输出倍率（超采样）。1=按画布原始尺寸；>1 时像素数 ×N²，
        构图不变但文字/线条更清晰（用于「窄画布」视频被放大播放时避免发虚）。
        """
        saved = self._apply_render_scale(render_scale)
        try:
            return self._render_video_inner(out_path, fps, duration_s,
                                            progress_cb, workers)
        finally:
            self._restore_render_scale(saved)

    def _render_video_inner(self, out_path: Path, fps: int, duration_s: int,
                            progress_cb=None, workers: int = 0) -> Path:
        # 越界预警：超出画布的轨迹在视频中会被裁剪，提前写入日志暴露问题
        x0, y0, x1, y1 = self.get_bounds()
        if x0 < 0 or y0 < 0 or x1 > self.width or y1 > self.height:
            print(f"[警告] 轨迹包围盒 ({x0:.0f},{y0:.0f})~({x1:.0f},{y1:.0f}) "
                  f"超出画布 {self.width}x{self.height}，超出部分将被裁剪",
                  file=sys.stderr)
        total_frames, idx_list = self._build_frame_plan(fps, duration_s)
        # 坡度仪：按本次渲染帧密度重建平滑序列（保证逐帧角度渐变，随后 pickle 到子进程）
        if self.show_slope_gauge:
            self.prepare_gauge_smoothing(total_frames)
        # 预取地图瓦片（避免逐帧网络请求）；磁盘缓存共享，子进程直接读缓存
        if self.show_map:
            self._prefetch_map_tiles(idx_list)
        if workers <= 0:
            workers = os.cpu_count() or 2
        workers = max(1, min(workers, total_frames))
        with tempfile.TemporaryDirectory() as tmpdir:
            frames_dir = Path(tmpdir) / "frames"
            frames_dir.mkdir()

            # spawn 子进程有固定启动开销(~2-3s)，帧太少时并行反而更慢（实测阈值约 60 帧）
            if workers == 1 or total_frames <= 64:
                for f in range(total_frames):
                    frame = self.render_frame(idx_list[f])
                    # 中间帧用完即删，低压缩级别（1）大幅加快 PNG 保存
                    frame.save(frames_dir / f"frame_{f:05d}.png", compress_level=1)
                    if progress_cb is not None:
                        progress_cb(f + 1, total_frames)
            else:
                self._render_frames_parallel(
                    idx_list, frames_dir, total_frames, workers, progress_cb)

            cmd = [
                "ffmpeg", "-y",
                "-framerate", str(fps),
                "-i", str(frames_dir / "frame_%05d.png"),
                "-r", str(fps),          # 强制输出帧率，避免剪映/Pr 用默认 24fps 重读
                "-c:v", "png",
                "-compression_level", "1",   # 默认 zlib 级别封装过慢；1 级体积略增、封装耗时约减半
                "-pix_fmt", "rgba",
                str(out_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                raise RuntimeError("ffmpeg failed")
        return out_path

    def _render_frames_parallel(self, idx_list, frames_dir: Path,
                                total_frames: int, workers: int,
                                progress_cb=None):
        """多进程并行渲染帧。子进程通过 pickle 快照重建 Renderer（无 GIL 争抢）。

        帧先落盘 PNG、再统一由 ffmpeg 封装：两条流水线可互相重叠
        （worker 写盘时 ffmpeg 无需空等）。进度按完成数递增（imap 保序，
        进度条平滑）；取消时抛异常，finally 中 terminate 兜底回收所有子进程。
        """
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        pool = None
        try:
            pool = ctx.Pool(workers, initializer=_worker_init,
                            initargs=(self, frames_dir))
            tasks = [(f, idx_list[f]) for f in range(total_frames)]
            done = 0
            for _ in pool.imap(_worker_render_one, tasks, chunksize=4):
                done += 1
                if progress_cb is not None:
                    progress_cb(done, total_frames)
            pool.close()
            pool.join()
        finally:
            if pool is not None:
                pool.terminate()   # 正常结束后无害；异常/取消时回收子进程


# ── 多进程 worker（模块级，spawn 可 pickle；子进程只 import 本模块）──

_WORKER_RENDERER: Renderer | None = None
_WORKER_FRAMES_DIR: Path | None = None


def _worker_init(renderer: Renderer, frames_dir: Path):
    global _WORKER_RENDERER, _WORKER_FRAMES_DIR
    _WORKER_RENDERER = renderer
    _WORKER_FRAMES_DIR = frames_dir


def _worker_render_one(task) -> int:
    f, idx = task
    frame = _WORKER_RENDERER.render_frame(idx)
    # 中间帧用完即删，低压缩级别（1）大幅加快 PNG 保存
    frame.save(_WORKER_FRAMES_DIR / f"frame_{f:05d}.png", compress_level=1)
    return f
