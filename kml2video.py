"""KML → 透明背景轨迹视频 Demo

解析两步路 KML 的 gx:Track 格式，渲染运动轨迹动画 + 实时数据，输出带 alpha 通道的视频。
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────── KML 解析 ───────────────────────

KML_NS = {"kml": "http://www.opengis.net/kml/2.2",
          "gx": "http://www.google.com/kml/ext/2.2"}

# 轨迹点: (lon, lat, elev, rel_time)
Point = Tuple[float, float, float, float]


def parse_kml(kml_path: Path) -> tuple[List[Point], dict]:
    """解析 KML，返回 (轨迹点列表, 元数据字典)"""
    tree = ET.parse(kml_path)
    root = tree.getroot()

    # 1) 找所有 gx:coord
    coords_elems = root.findall(".//gx:coord", KML_NS)
    raw = []
    for elem in coords_elems:
        parts = elem.text.strip().split()
        if len(parts) >= 3:
            raw.append((float(parts[0]), float(parts[1]), float(parts[2])))

    # 2) ExtendedData 里的时间 / 里程 / 爬升
    meta = {}
    for data in root.findall(".//kml:ExtendedData/kml:Data", KML_NS):
        name = data.get("name")
        val_elem = data.find("kml:value", KML_NS)
        if name and val_elem is not None and val_elem.text:
            meta[name] = val_elem.text.strip()

    begin_ms = int(meta.get("BeginTime", 0))
    end_ms = int(meta.get("EndTime", 0))
    pause_s = float(meta.get("PauseTime", 0))          # 暂停秒数
    total_active_s = max(0, (end_ms - begin_ms) / 1000.0 - pause_s)

    # 3) 给每个点分配相对时间（0 ~ total_active_s）
    n = len(raw)
    # 先给初始均匀分布
    if n > 1:
        rel_times = np.linspace(0, total_active_s, n)
    else:
        rel_times = [0.0]

    points = [(lon, lat, elev, float(t)) for (lon, lat, elev), t in zip(raw, rel_times)]

    extra = {
        "begin_ms": begin_ms,
        "end_ms": end_ms,
        "pause_s": pause_s,
        "total_active_s": total_active_s,
        "distance_m": float(meta.get("Distance", 0)),
        "elev_gain_m": float(meta.get("ElevationGain", 0)),
        "elev_loss_m": float(meta.get("ElevationLoss", 0)),
    }
    return points, extra


def smooth_elevation(points: List[Point], window: int = 5) -> List[Point]:
    """对海拔做移动平均平滑，抑制 GPS 抖动造成的虚假爬升。"""
    if window < 2 or len(points) < window:
        return points
    elevs = np.array([p[2] for p in points])
    kernel = np.ones(window) / window
    smoothed = np.convolve(elevs, kernel, mode="same")
    out = []
    for i, (lon, lat, _, t) in enumerate(points):
        out.append((lon, lat, float(smoothed[i]), t))
    return out


def compute_cumulative_ascent(points: List[Point]) -> list[float]:
    """累计爬升（米），每到一个点时的累计值。"""
    cum = [0.0]
    for i in range(1, len(points)):
        delta = points[i][2] - points[i - 1][2]
        cum.append(cum[-1] + max(delta, 0.0))
    return cum


def compute_cumulative_distance(points: List[Point]) -> list[float]:
    """累计里程（米），Haversine 近似。"""
    cum = [0.0]
    R = 6_371_000.0
    for i in range(1, len(points)):
        lon1, lat1, _, _ = points[i - 1]
        lon2, lat2, _, _ = points[i]
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        d = 2 * R * math.asin(math.sqrt(a))
        cum.append(cum[-1] + d)
    return cum


# ─────────────────────── 坐标投影 ───────────────────────

def project_points(points: List[Point], width: int, height: int,
                   margin: int = 80) -> List[Tuple[int, int]]:
    """经纬度 → 画布像素（等比缩放，居中）。"""
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

    # 宽高比
    span_lon = lon_max - lon_min
    span_lat = lat_max - lat_min
    aspect_data = span_lon / span_lat if span_lat else 1.0
    aspect_canvas = cw / ch

    if aspect_data > aspect_canvas:
        # 数据更宽 → 宽度撑满，高度留黑
        px_scale = cw / span_lon
        py_scale = cw / span_lon  # 等比
    else:
        px_scale = ch / span_lat
        py_scale = ch / span_lat

    # 等比缩放后居中
    drawn_w = span_lon * px_scale
    drawn_h = span_lat * py_scale
    offset_x = margin + (cw - drawn_w) / 2
    offset_y = margin + (ch - drawn_h) / 2

    out = []
    for lon, lat, _, _ in points:
        x = offset_x + (lon - lon_min) * px_scale
        y = offset_y + (lat_max - lat) * py_scale  # Y 翻转
        out.append((int(x), int(y)))
    return out


# ─────────────────────── 渲染 ───────────────────────

def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """尝试加载一个中文字体，失败则回退默认。"""
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
        "C:/Windows/Fonts/simhei.ttf",    # 黑体
        "C:/Windows/Fonts/simsun.ttc",    # 宋体
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def render_frame(width: int, height: int,
                 projected: List[Tuple[int, int]],
                 cum_ascent: List[float],
                 cum_dist: List[float],
                 cum_time: List[float],
                 progress: float,        # 0 ~ 1，视频进度
                 current_idx: int,
                 font_big, font_small) -> Image.Image:
    """渲染单帧（透明 RGBA）。"""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 1) 画完整轨迹（一次全部展示）
    if len(projected) >= 2:
        d.line(projected, fill=(60, 180, 255, 200), width=4)

    # 2) 画当前位置标记
    if 0 <= current_idx < len(projected):
        cx, cy = projected[current_idx]
        # 外光晕
        for r, a in [(18, 60), (12, 120), (8, 180)]:
            d.ellipse([cx - r, cy - r, cx + r, cy + r],
                      fill=(255, 80, 80, a))
        # 中心实心
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5],
                  fill=(255, 255, 255, 255))
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3],
                  fill=(255, 60, 60, 255))

    # 3) 左上角数据面板
    ascent = cum_ascent[current_idx]
    dist = cum_dist[current_idx] / 1000.0  # km
    elapsed = cum_time[current_idx]

    panel_x, panel_y = 30, 30
    panel_w, panel_h = 320, 170
    # 半透明背景
    d.rounded_rectangle([panel_x, panel_y, panel_x + panel_w, panel_y + panel_h],
                         radius=12, fill=(0, 0, 0, 120), outline=(255, 255, 255, 60), width=1)

    # 标题
    d.text((panel_x + 16, panel_y + 14), "徒步轨迹",
           fill=(255, 255, 255, 230), font=font_big)

    # 数据行
    line_start_y = panel_y + 60
    row_h = 32
    rows = [
        ("累计爬升", f"{ascent:.1f} m",        (80, 220, 120, 255)),
        ("已行里程", f"{dist:.2f} km",          (255, 210,  90, 255)),
        ("运动耗时", format_duration(elapsed), (120, 200, 255, 255)),
    ]
    for i, (label, value, color) in enumerate(rows):
        y = line_start_y + i * row_h
        d.text((panel_x + 16, y), label,
               fill=(220, 220, 220, 220), font=font_small)
        # 右对齐 value
        bbox = d.textbbox((0, 0), value, font=font_big)
        vw = bbox[2] - bbox[0]
        d.text((panel_x + panel_w - 16 - vw, y - 6), value,
               fill=color, font=font_big)

    # 4) 底部进度条
    bar_x, bar_y = 30, height - 50
    bar_w, bar_h = width - 60, 8
    d.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                         radius=4, fill=(255, 255, 255, 60))
    filled = int(bar_w * progress)
    d.rounded_rectangle([bar_x, bar_y, bar_x + max(filled, 8), bar_y + bar_h],
                         radius=4, fill=(255, 120, 120, 220))

    return img


# ─────────────────────── 时间插值 ───────────────────────

def find_current_idx(cum_time: List[float], target: float) -> int:
    """二分找到当前时间对应的点索引。"""
    lo, hi = 0, len(cum_time) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cum_time[mid] <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ─────────────────────── 主流程 ───────────────────────

def main() -> None:
    kml_path = Path(r"d:\26code\kml_path\test.kml")
    out_video = kml_path.with_suffix(".mov")

    width, height = 1280, 720
    fps = 2
    video_duration_s = 20
    total_frames = fps * video_duration_s

    print(f"解析: {kml_path}")
    points, meta = parse_kml(kml_path)
    print(f"  轨迹点: {len(points)}")
    print(f"  总时长: {meta['total_active_s']:.0f}s "
          f"({format_duration(meta['total_active_s'])})")
    print(f"  累计爬升(KML): {meta['elev_gain_m']:.1f} m")
    print(f"  总里程(KML):   {meta['distance_m']/1000:.2f} km")

    cum_ascent = compute_cumulative_ascent(points)
    cum_dist = compute_cumulative_distance(points)
    cum_time = [p[3] for p in points]

    # 用平滑海拔重算爬升，使结果接近 KML 元数据（抑制 GPS 抖动）
    points_smoothed = smooth_elevation(points, window=7)
    cum_ascent = compute_cumulative_ascent(points_smoothed)

    # 投影
    projected = project_points(points, width, height)

    # 字体
    font_big = load_font(24)
    font_small = load_font(16)

    # 渲染到临时目录
    with tempfile.TemporaryDirectory() as tmpdir:
        frames_dir = Path(tmpdir) / "frames"
        frames_dir.mkdir()

        print(f"渲染 {total_frames} 帧 ...")
        for f in range(total_frames):
            progress = f / (total_frames - 1)
            # 视频进度 → 轨迹时间
            target_time = progress * meta["total_active_s"]
            idx = find_current_idx(cum_time, target_time)

            frame = render_frame(
                width, height, projected, cum_ascent, cum_dist, cum_time,
                progress, idx, font_big, font_small
            )
            frame.save(frames_dir / f"frame_{f:05d}.png")
            if (f + 1) % 30 == 0 or f == total_frames - 1:
                print(f"  {f+1}/{total_frames}")

        # ffmpeg 合成带 alpha 的视频
        # 用 PNG 编码（无损、全 alpha、兼容性好），mov 容器
        print("ffmpeg 合成视频 (PNG alpha) ...")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", str(frames_dir / "frame_%05d.png"),
            "-c:v", "png",
            str(out_video),
        ]
        print("  " + " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr)
            sys.exit(1)

    # 输出统计
    print(f"\n✅ 完成: {out_video}")
    print(f"   大小: {out_video.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"   分辨率: {width}x{height}, {fps}fps, {video_duration_s}s")


if __name__ == "__main__":
    main()
