"""双轨批量合成后端。

把「相机实拍素材」整理成一条连续的视频轨道（轨道二），
并为 KML 仪表盘（轨道一）生成严格等长的时间轴。

处理规则：
  1. 按素材内嵌 creation_time 排序，相邻素材之间的空档用纯色空白 MOV 补齐；
  2. 无声音素材 2×，有声音素材 10×（可用参数覆盖）；
  3. 空档按 gap_speed 压缩，且两条轨道总时长不超过 max_total_s；
  4. 轨道一复用 Renderer 的分段模式（cuts + durations），与轨道二逐段对齐。

本模块只依赖 ffmpeg / ffprobe，不导入 GUI。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

# ── 默认参数 ──
DEFAULT_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".avi")
SILENT_DB = -45.0          # 平均音量低于该值视为「没有声音」
AUDIO_SPEED = 10.0         # 有声音素材倍速
SILENT_SPEED = 2.0         # 无声音素材倍速
GAP_SPEED = 10.0           # 空档压缩倍数（空档也按 10× 压缩）
MAX_TOTAL_S = 1800.0       # 两条轨道总时长上限（30 分钟）

_PROGRESS_RE_TIME = re.compile(r"out_time_ms=(\d+)")


class ComposerCancelled(Exception):
    """用户取消合成。"""


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError("找不到 ffmpeg，请确认已安装并在 PATH 中")


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    raise RuntimeError("找不到 ffprobe，请确认已安装并在 PATH 中")


def _parse_time(text: str) -> Optional[datetime]:
    """解析 MP4 creation_time（UTC）→ 带时区 datetime。"""
    text = (text or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _time_from_name(path: Path) -> Optional[datetime]:
    """从 DJI_20261108123421_0003_D.MP4 文件名推断开始时间（本地时间）。"""
    m = re.search(r"(20\d{6})(\d{6})", path.stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").astimezone()
    except ValueError:
        return None


@dataclass
class Clip:
    path: Path
    start: datetime                 # UTC
    duration: float                 # 秒
    has_audio: bool = False
    mean_db: Optional[float] = None
    max_db: Optional[float] = None
    silent: bool = False
    speed: float = AUDIO_SPEED
    excluded: bool = False

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def out_duration(self) -> float:
        return self.duration / max(0.01, self.speed)

    @property
    def start_local(self) -> datetime:
        return self.start.astimezone()


@dataclass
class Segment:
    """最终时间轴上的一段：实拍片段或空白补片。"""
    kind: str                       # "clip" / "blank"
    real_start: datetime
    real_end: datetime
    duration: float = 0.0           # 输出时长（秒，变速/压缩后）
    real_s: float = 0.0             # 原始时长（秒）
    clip: Optional[Clip] = None
    actual_duration: Optional[float] = None   # 编码后实测时长

    @property
    def final_duration(self) -> float:
        return self.actual_duration if self.actual_duration else self.duration


@dataclass
class Timeline:
    segments: List[Segment] = field(default_factory=list)
    total_s: int = 0
    gap_speed: float = GAP_SPEED
    max_total_s: float = MAX_TOTAL_S

    @property
    def clips(self) -> List[Segment]:
        return [s for s in self.segments if s.kind == "clip"]

    @property
    def blanks(self) -> List[Segment]:
        return [s for s in self.segments if s.kind == "blank"]

    def duration_of(self) -> float:
        return sum(s.final_duration for s in self.segments)


# ─────────────────────────── 素材扫描 ───────────────────────────

def _probe_json(path: Path) -> dict:
    cmd = [_ffprobe(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {path.name}\n{res.stderr[-400:]}")
    return json.loads(res.stdout or "{}")


def measure_audio_db(path: Path, positions: Sequence[float] = (0.2, 0.5, 0.8),
                     window: float = 8.0) -> tuple[Optional[float], Optional[float]]:
    """抽样测量平均/最大音量（dB）；没有音轨返回 (None, None)。"""
    info = _probe_json(path)
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        return None, None
    dur = float(info.get("format", {}).get("duration") or 0.0)
    means: List[float] = []
    maxes: List[float] = []
    for p in positions:
        ss = max(0.0, dur * p) if dur > 0 else 0.0
        cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{ss:.3f}",
               "-t", f"{window:.3f}", "-i", str(path), "-map", "0:a:0",
               "-vn", "-af", "volumedetect", "-f", "null", "-"]
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        m1 = re.search(r"mean_volume:\s*(-?[\d.]+)", res.stderr)
        m2 = re.search(r"max_volume:\s*(-?[\d.]+)", res.stderr)
        if m1:
            means.append(float(m1.group(1)))
        if m2:
            maxes.append(float(m2.group(1)))
    if not means:
        return None, None
    return sum(means) / len(means), max(maxes) if maxes else None


def probe_clip(path: Path, analyze_audio: bool = True,
               silent_db: float = SILENT_DB) -> Clip:
    info = _probe_json(path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    duration = 0.0
    if video and video.get("duration"):
        duration = float(video["duration"])
    elif fmt.get("duration"):
        duration = float(fmt["duration"])

    start = None
    tags = fmt.get("tags") or {}
    start = _parse_time(tags.get("creation_time"))
    if start is None:
        for s in streams:
            start = _parse_time((s.get("tags") or {}).get("creation_time"))
            if start is not None:
                break
    if start is None:
        local = _time_from_name(path)
        start = (local.astimezone(timezone.utc) if local
                 else datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    mean_db = max_db = None
    if analyze_audio and has_audio:
        mean_db, max_db = measure_audio_db(path)

    silent = (not has_audio) or (mean_db is not None and mean_db < silent_db)
    return Clip(path=path, start=start, duration=max(0.0, duration),
                has_audio=has_audio, mean_db=mean_db, max_db=max_db,
                silent=silent, speed=SILENT_SPEED if silent else AUDIO_SPEED)


def scan_folder(folder: Path, analyze_audio: bool = True,
                silent_db: float = SILENT_DB,
                progress: Optional[Callable[[int, int, str], None]] = None
                ) -> List[Clip]:
    files = sorted([p for p in Path(folder).iterdir()
                    if p.is_file() and p.suffix.lower() in DEFAULT_VIDEO_EXTS])
    clips: List[Clip] = []
    for i, p in enumerate(files):
        if progress:
            progress(i, len(files), p.name)
        try:
            clips.append(probe_clip(p, analyze_audio=analyze_audio,
                                    silent_db=silent_db))
        except Exception as exc:      # 单个文件损坏不影响整体
            print(f"[跳过] {p.name}: {exc}", file=sys.stderr)
    clips.sort(key=lambda c: c.start)
    if progress:
        progress(len(files), len(files), "完成")
    return clips


# ─────────────────────────── 时间轴 ───────────────────────────

def plan_timeline(clips: Sequence[Clip],
                  kml_start: Optional[datetime] = None,
                  kml_end: Optional[datetime] = None,
                  gap_speed: float = GAP_SPEED,
                  max_total_s: float = MAX_TOTAL_S) -> Timeline:
    """按绝对时间排序，插入空白补片，并把总时长压到 max_total_s 以内。"""
    usable = [c for c in sorted(clips, key=lambda c: c.start) if not c.excluded]
    if not usable:
        return Timeline(segments=[], total_s=0,
                        gap_speed=gap_speed, max_total_s=max_total_s)

    first, last = usable[0], usable[-1]
    t_begin = min(kml_start, first.start) if kml_start else first.start
    t_end = max(kml_end, last.end) if kml_end else last.end

    segs: List[Segment] = []
    cursor = t_begin
    for c in usable:
        if c.start > cursor:
            segs.append(Segment("blank", cursor, c.start,
                                real_s=(c.start - cursor).total_seconds()))
        segs.append(Segment("clip", c.start, c.end,
                            real_s=c.duration, clip=c))
        cursor = max(cursor, c.end)
    if t_end > cursor:
        segs.append(Segment("blank", cursor, t_end,
                            real_s=(t_end - cursor).total_seconds()))

    gap_speed = max(1.0, float(gap_speed))
    for s in segs:
        s.duration = (s.clip.out_duration if s.kind == "clip"
                      else s.real_s / gap_speed)

    clip_total = sum(s.duration for s in segs if s.kind == "clip")
    blank_total = sum(s.duration for s in segs if s.kind == "blank")
    if max_total_s > 0 and clip_total + blank_total > max_total_s and blank_total > 0:
        budget = max(0.0, max_total_s - clip_total)
        factor = budget / blank_total
        for s in segs:
            if s.kind == "blank":
                s.duration *= factor

    # 总时长取整秒：轨道一按整数秒渲染，末段吸收误差保证严格等长
    total = sum(s.duration for s in segs)
    total_s = max(1, int(round(total)))
    delta = total_s - total
    target = segs[-1]
    if target.kind != "blank" and len(segs) > 1 and segs[-2].kind == "blank":
        target = segs[-2]
    target.duration = max(0.1, target.duration + delta)

    return Timeline(segments=segs, total_s=total_s,
                    gap_speed=gap_speed, max_total_s=max_total_s)


def dashboard_segments(timeline: Timeline, kml_start: datetime,
                       kml_end: datetime) -> tuple[List[float], List[float]]:
    """把绝对时间轴换算成 Renderer 的分段参数 (cuts, durations)。

    cuts = 每段结束时刻在 KML 时间跨度内的归一化位置；
    durations = 每段在最终时间轴上的输出时长。
    """
    span = max(1e-6, (kml_end - kml_start).total_seconds())
    cuts: List[float] = []
    durations: List[float] = []
    for s in timeline.segments[:-1]:
        u = (s.real_end - kml_start).total_seconds() / span
        u = min(0.9999, max(0.0001, u))
        if cuts and u <= cuts[-1] + 1e-6:
            u = min(0.9999, cuts[-1] + 1e-6)
        cuts.append(u)
        durations.append(s.final_duration)
    if timeline.segments:
        durations.append(timeline.segments[-1].final_duration)
    return cuts, durations


# ─────────────────────────── ffmpeg 渲染 ───────────────────────────

def _atempo_chain(speed: float) -> str:
    """atempo 单级上限 2.0，拆成多级串联（保音高）。"""
    parts: List[str] = []
    remain = max(0.01, speed)
    while remain > 2.0:
        parts.append("atempo=2.0")
        remain /= 2.0
    if remain > 1.001 or remain < 0.999:
        parts.append(f"atempo={remain:.6f}")
    return ",".join(parts) if parts else "anull"


def _run_ffmpeg(cmd: List[str], expected_s: float,
                progress_cb: Optional[Callable[[float], None]] = None,
                cancel_cb: Optional[Callable[[], bool]] = None) -> None:
    """执行 ffmpeg 并解析 -progress 输出。

    stderr 必须落到临时文件：若用 PIPE 且不读取，ffmpeg 写满管道缓冲区后会永久阻塞
    （实测 4K 转码几十秒后卡死）。
    """
    import tempfile
    err_fp = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_fp,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel_cb and cancel_cb():
                proc.kill()
                raise ComposerCancelled()
            m = _PROGRESS_RE_TIME.search(line)
            if m and progress_cb and expected_s > 0:
                p = min(1.0, (int(m.group(1)) / 1e6) / expected_s)
                progress_cb(p)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
        err_fp.seek(0)
        err = err_fp.read()
        err_fp.close()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败（{proc.returncode}）\n{err[-800:]}")


def render_clip_segment(clip: Clip, out_path: Path, width: int, height: int,
                        fps: int, crf: int = 20,
                        progress_cb: Optional[Callable[[float], None]] = None,
                        cancel_cb: Optional[Callable[[], bool]] = None) -> Path:
    """变速转码单个实拍片段（统一分辨率/帧率/编码，供 concat 拼接）。"""
    speed = max(1.0, clip.speed)
    vf = f"setpts=PTS/{speed:.6f},fps={fps},scale={width}:{height}:flags=lanczos"
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
           "-i", str(clip.path), "-map", "0:v:0"]
    if clip.has_audio:
        cmd += ["-map", "0:a:0", "-af", _atempo_chain(speed)]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", str(fps)]
    if clip.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [str(out_path)]
    _run_ffmpeg(cmd, clip.out_duration, progress_cb, cancel_cb)
    return out_path


def render_blank_segment(out_path: Path, duration: float, width: int, height: int,
                         fps: int, color: str = "black",
                         progress_cb: Optional[Callable[[float], None]] = None,
                         cancel_cb: Optional[Callable[[], bool]] = None) -> Path:
    """生成空白补片 MOV（纯色画面 + 静音音轨）。"""
    dur = max(0.1, float(duration))
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
           "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps}",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-t", f"{dur:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-r", str(fps),
           "-c:a", "aac", "-b:a", "128k",
           str(out_path)]
    _run_ffmpeg(cmd, dur, progress_cb, cancel_cb)
    return out_path


def probe_duration(path: Path) -> float:
    info = _probe_json(path)
    return float(info.get("format", {}).get("duration") or 0.0)


def concat_segments(parts: Sequence[Path], out_path: Path,
                    list_path: Path, limit_s: Optional[float] = None,
                    progress_cb: Optional[Callable[[float], None]] = None,
                    cancel_cb: Optional[Callable[[], bool]] = None) -> Path:
    """用 concat demuxer 无损拼接（所有分段编码参数一致）。

    limit_s：强制输出总时长，用于和仪表盘轨道做到严格等长。
    """
    lines = []
    for p in parts:
        safe = str(p).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
           "-f", "concat", "-safe", "0", "-i", str(list_path),
           "-c", "copy", "-movflags", "+faststart"]
    if limit_s and limit_s > 0:
        cmd += ["-t", f"{limit_s:.3f}"]
    cmd.append(str(out_path))
    total = sum(probe_duration(p) for p in parts)
    _run_ffmpeg(cmd, total, progress_cb, cancel_cb)
    return out_path


def render_track2(timeline: Timeline, out_dir: Path, width: int, height: int,
                  fps: int = 30, color: str = "black",
                  progress_cb: Optional[Callable[[float, str], None]] = None,
                  cancel_cb: Optional[Callable[[], bool]] = None,
                  out_name: str = "track2_footage.mp4") -> Path:
    """渲染轨道二：逐段转码/补空白 → 拼接 → 回填实测时长。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_dir = out_dir / "_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    total_plan = sum(s.duration for s in timeline.segments) or 1.0
    done_plan = 0.0
    parts: List[Path] = []
    for i, seg in enumerate(timeline.segments):
        if cancel_cb and cancel_cb():
            raise ComposerCancelled()

        def _seg_cb(p: float, _seg=seg, _base=done_plan):
            if progress_cb:
                overall = (_base + _seg.duration * p) / total_plan
                label = (_seg.clip.path.name if _seg.kind == "clip"
                         else f"空白补片 {_seg.duration:.0f}s")
                progress_cb(min(0.99, overall), label)

        if seg.kind == "clip":
            out = seg_dir / f"seg_{i:03d}_clip.mp4"
            render_clip_segment(seg.clip, out, width, height, fps,
                                progress_cb=_seg_cb, cancel_cb=cancel_cb)
        else:
            out = seg_dir / f"seg_{i:03d}_blank.mov"
            render_blank_segment(out, seg.duration, width, height, fps,
                                 color=color, progress_cb=_seg_cb,
                                 cancel_cb=cancel_cb)
        seg.actual_duration = probe_duration(out)
        parts.append(out)
        done_plan += seg.duration

    if progress_cb:
        progress_cb(0.995, "拼接轨道二…")
    total_s = int(round(sum(s.final_duration for s in timeline.segments)))
    final = concat_segments(parts, out_dir / out_name, out_dir / "_concat.txt",
                            limit_s=total_s, cancel_cb=cancel_cb)
    if progress_cb:
        progress_cb(1.0, "轨道二完成")
    return final


def render_track1_dashboard(renderer, timeline: Timeline,
                            kml_start: datetime, kml_end: datetime,
                            out_path: Path, fps: int = 10,
                            render_scale: int = 1, workers: int = 0,
                            progress_cb: Optional[Callable[[int, int], None]] = None,
                            cancel_cb: Optional[Callable[[], bool]] = None) -> Path:
    """渲染轨道一：用分段模式让仪表盘与轨道二逐段严格对齐。"""
    cuts, durations = dashboard_segments(timeline, kml_start, kml_end)
    old_use, old_segments = renderer.use_segments, renderer.segments
    renderer.use_segments = bool(cuts)
    renderer.segments = (cuts, durations) if cuts else None
    try:
        def _cb(done: int, total: int):
            if cancel_cb and cancel_cb():
                raise ComposerCancelled()
            if progress_cb:
                progress_cb(done, total)
        total_s = int(round(sum(s.final_duration for s in timeline.segments)))
        return renderer.render_video(Path(out_path), fps, total_s,
                                     progress_cb=_cb, workers=workers,
                                     render_scale=render_scale)
    finally:
        renderer.use_segments = old_use
        renderer.segments = old_segments


# ─────────────────────────── CLI（仅扫描调试用）───────────────────────────

def _cli_scan(folder: Path) -> None:
    clips = scan_folder(folder, progress=lambda i, n, name: print(f"[{i+1}/{n}] {name}"))
    for c in clips:
        print(f"{c.path.name}  start={c.start_local:%Y-%m-%d %H:%M:%S}  "
              f"dur={c.duration:7.1f}s  audio={c.has_audio}  "
              f"mean={c.mean_db}  silent={c.silent}  speed={c.speed:g}x  "
              f"out={c.out_duration:7.1f}s")
    tl = plan_timeline(clips)
    print(f"\n片段 {len(tl.clips)} 段，空档 {len(tl.blanks)} 段，"
          f"总时长 {tl.total_s/60:.2f} 分钟")
    for s in tl.segments:
        tag = s.clip.path.name if s.clip else "空白补片"
        print(f"  {s.kind:5s} {s.real_start:%H:%M:%S}→{s.real_end:%H:%M:%S} "
              f"原始{s.real_s:7.1f}s → 输出{s.duration:7.1f}s  {tag}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python composer.py <素材目录>")
        raise SystemExit(1)
    _cli_scan(Path(sys.argv[1]))
