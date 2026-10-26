"""运动轨迹可视化与视频生成 - PyQt5 GUI（v2 修复版）

三步流程：加载 KML → 拖拽编辑 → 生成视频
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import math
from io import BytesIO
from datetime import datetime
from pathlib import Path
from PyQt5.QtCore import Qt, QPointF, QRectF, QThread, pyqtSignal, QStandardPaths, QTimer
from PyQt5.QtGui import (QPen, QColor, QBrush, QPainterPath, QPainter, QFont,
                         QFontMetrics, QPixmap, QPolygonF, QTransform,
                         QLinearGradient)
from PIL import Image, ImageDraw

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGraphicsView, QGraphicsScene,
    QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsEllipseItem,
    QGraphicsTextItem, QGraphicsPolygonItem,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QFileDialog, QMessageBox, QGroupBox, QFormLayout, QTextEdit,
    QProgressBar, QDockWidget, QStatusBar, QFrame, QSizePolicy,
    QCheckBox, QGridLayout, QColorDialog, QDialog, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QToolButton,
    QRadioButton, QButtonGroup, QComboBox, QGraphicsPixmapItem,
    QInputDialog, QScrollArea, QTabWidget,
)

from renderer import (Renderer, format_duration, format_pace,
                      _fetch_tile, _wgs84_to_gcj02, _lonlat_to_webmerc_px,
                      _webmerc_px_to_lonlat,
                      GAUGE_COLD_DEFAULT, GAUGE_WARM_DEFAULT,
                      gauge_footprint, gauge_arc_layout, gauge_card_layout,
                      gauge_gradient_stops, GAUGE_LABEL_RATIO,
                      gauge_value_color, gauge_format_pct,
                      _fit_font_size, SLOPE_LIMIT_PCT,
                      ring_footprint, RING_PROGRESS_DEFAULT)

# ─────────────────────── 工程文件 & 全局设置 持久化 ───────────────────────
# 全局 settings.json 存放在系统用户配置目录（Windows: %APPDATA%\KmlPath），
# 只保留窗口级状态（最近工程列表 / 视频输出目录 / 默认画布 / 上次工程路径）。
# 每份 KML + 渲染参数 = 一个独立 .kmlproj 工程文件，由用户自选位置保存。

# 工程文件字段：从旧 settings.json 抽出的渲染相关键
_PROJECT_FIELDS = (
    "kml_path", "canvas", "line_width", "node_font_size", "node_color",
    "unvisited_color", "visited_color", "start_color", "end_color",
    "duration", "fps", "panel_fields", "key_nodes",
    "use_segments", "segments",
    "trajectory", "panel", "map",
    "show_map", "map_zoom", "map_shape", "map_text_delta",
    "map_arrow_size", "map_heading_up", "map_show_trajectory",
    "map_traj_ahead_m", "map_style", "map_traj_style",
    "show_elev_profile", "elev_profile_pos",
    "elev_profile_w", "elev_profile_h",
    "show_progress_ring", "progress_ring_style", "progress_ring_size",
    "progress_ring_pos", "ring_progress_color",
)


def _settings_path() -> Path:
    appdata = os.environ.get("APPDATA")   # Windows: C:\Users\<user>\AppData\Roaming
    base = Path(appdata) / "KmlPath" if appdata else Path.home() / ".config" / "kml_path"
    return base / "settings.json"


SETTINGS_PATH = _settings_path()


def _convert_legacy_global_to_project(old: dict) -> dict:
    """旧格式全局 settings（含渲染参数） → 新工程文件 dict。

    兼容旧 segments 格式（list [cuts, durs]）→ 新 dict {"cuts":..., "durations":...}。
    返回新工程 dict；调用方负责写入磁盘。
    """
    proj = {"version": 1}
    for k in _PROJECT_FIELDS:
        if k in old:
            proj[k] = old[k]
    seg = proj.get("segments")
    if isinstance(seg, list) and len(seg) == 2:
        proj["segments"] = {"cuts": list(seg[0]), "durations": list(seg[1])}
    return proj


def _migrate_legacy_settings() -> None:
    """一次性迁移（导入时执行）：

    1) 工程目录里的旧 settings.json → 系统目录（历史行为保留）
    2) 全局 settings.json 旧格式（无 version 字段）→ 新格式：
       把渲染参数抽到 .kmlproj 工程文件（KML 同目录，不可写则退到 Documents），
       全局只留 last_project / recent_projects / last_save_dir / default_canvas
    """
    legacy = Path(__file__).with_name("settings.json")
    target = SETTINGS_PATH
    # 1) 工程目录旧文件 → 系统目录
    if legacy.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            cur = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        except (OSError, json.JSONDecodeError):
            cur = {}
        if not isinstance(cur, dict) or "version" not in cur:
            # 全局文件不存在或仍是旧格式 → 用工程目录旧文件覆盖
            target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        legacy.unlink(missing_ok=True)

    # 2) 全局 settings 旧格式 → 新格式 + 抽工程文件
    if not target.exists():
        return
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or "version" in data:
        return   # 已是新格式或损坏 → 不动

    # 旧格式：把渲染字段抽成工程文件
    kml_path = data.get("kml_path")
    proj_path = None
    if kml_path and Path(kml_path).exists():
        proj_data = _convert_legacy_global_to_project(data)
        kml_p = Path(kml_path)
        # 工程文件位置：KML 同目录，文件名 = KML stem + .kmlproj
        candidate = kml_p.with_suffix(".kmlproj")
        try:
            # 避免覆盖已有工程文件：同名时加 _2 _3 后缀
            n, fp = 2, candidate
            while fp.exists():
                fp = kml_p.with_name(f"{kml_p.stem}_{n}.kmlproj")
                n += 1
            fp.write_text(
                json.dumps(proj_data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            proj_path = fp
        except OSError:
            # KML 同目录不可写 → 退到 Documents
            docs = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
            try:
                docs.mkdir(parents=True, exist_ok=True)
                fp = docs / f"{kml_p.stem}.kmlproj"
                n = 2
                while fp.exists():
                    fp = docs / f"{kml_p.stem}_{n}.kmlproj"
                    n += 1
                fp.write_text(
                    json.dumps(proj_data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                proj_path = fp
            except OSError:
                proj_path = None   # 放弃，仅重写全局

    # 重写全局 settings.json 为新格式
    new_global = {
        "version": 1,
        "last_project": (str(proj_path) if proj_path else None),
        "recent_projects": ([str(proj_path)] if proj_path else []),
        "last_save_dir": data.get("last_save_dir"),
        "default_canvas": data.get("canvas") or [1920, 1080],
    }
    try:
        target.write_text(
            json.dumps(new_global, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass


_migrate_legacy_settings()   # 导入时执行一次性迁移


def load_global_settings() -> dict:
    """读取全局 settings（不存在/损坏时返回空 dict）。"""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_global_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass


def load_project(path: Path) -> dict:
    """读取 .kmlproj 工程文件（损坏时抛 ValueError 由调用方处理）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("工程文件不是合法 JSON 对象")
        return data
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"无法读取工程文件: {e}")


def save_project(path: Path, data: dict) -> None:
    """写入 .kmlproj 工程文件。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8")


# 兼容旧调用名（部分位置仍用 self.settings = load_settings()）
def load_settings() -> dict:
    return load_global_settings()


def _num(settings: dict, key: str, default):
    """安全读取数值型设置，损坏时回退默认值。"""
    try:
        return type(default)(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def _notify_main_dirty(item) -> None:
    """Item._sync 内调用：通知 MainWindow 标工程变脏。
    scene 的 QObject parent = MainWindow（QGraphicsScene(self) 设定）。
    加载期间 MainWindow._loading=True，_mark_dirty 内部会早返回。
    """
    p = item.scene().parent() if item.scene() else None
    if p is not None and hasattr(p, "_mark_dirty"):
        p._mark_dirty()


def _notify_main_scale(item, kind: str, value: float) -> None:
    """预览框内组件被滚轮缩放后：把最新数值回写到设置面板对应控件。
    kind: "trajectory" / "panel" / "map" / "gauge"
    """
    p = item.scene().parent() if item.scene() else None
    if p is not None and hasattr(p, "_on_preview_scaled"):
        p._on_preview_scaled(kind, value)


# ─────────────────────── 可拖拽轨迹 Item ───────────────────────

class DraggablePathItem(QGraphicsPathItem):
    def __init__(self, path: QPainterPath, renderer: Renderer):
        super().__init__(path)
        self.renderer = renderer
        self._apply_pen()  # 未走轨迹色，粗细由 renderer.trajectory_width 决定
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        # 固定缩放锚点 = 路径包围盒中心（与 renderer 模型一致，保证视频输出同所见）
        self._anchor = self.boundingRect().center()
        self.setTransformOriginPoint(self._anchor)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        # Qt5 必须用 setScale（scale(sx,sy) 旧 API 已移除）
        self.setScale(self.scale() * factor)
        self._sync()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._sync()

    def _sync(self):
        """把当前布局写回 renderer。
        GUI 映射: x -> anchor + s*(x - anchor) + pos
        renderer: x -> center + s*(x - center) + offset
        两者对齐: center = 固定 anchor, offset = pos
        """
        self.renderer.trajectory_scale = self.scale()
        self.renderer.trajectory_offset = (self.pos().x(), self.pos().y())
        self.renderer.trajectory_center = (self._anchor.x(), self._anchor.y())
        self.scene().update()
        _notify_main_dirty(self)
        _notify_main_scale(self, "trajectory", self.scale())

    def _apply_pen(self, hover: bool = False):
        """按 renderer.trajectory_width 设置画笔（与视频线宽同源，所见即所得）。"""
        w = self.renderer.trajectory_width + (2 if hover else 0)
        base = self.renderer.unvisited_color
        if hover:
            # 悬停提亮（混合白色）
            base = (min(255, base[0] + 30), min(255, base[1] + 60),
                    min(255, base[2] + 0))
        color = QColor(base[0], base[1], base[2], 250 if hover else 220)
        self.setPen(QPen(color, w))

    def hoverEnterEvent(self, event):
        self._apply_pen(hover=True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._apply_pen()
        super().hoverLeaveEvent(event)


# ─────────────────────── 可拖拽数据面板 Item ───────────────────────

def _panel_span_w(r) -> float:
    """数据面板在画布上占用的实际宽度（取「数据矩形」与「背景条」的较宽者）。"""
    s = r.panel_scale
    pad = r.panel_bg_pad * s
    text_w = DataPanelItem.BASE_W * s
    bg_w = r.panel_bg_w * s + 2 * pad
    return max(text_w, bg_w, 1.0)


def _panel_span_h(r) -> float:
    """数据面板在画布上占用的实际高度（数据矩形或背景条取高者）。"""
    s = r.panel_scale
    pad = r.panel_bg_pad * s
    text_h = DataPanelItem._base_h(r) * s
    return max(text_h, text_h + 2 * pad, 1.0)


class DataPanelItem(QGraphicsRectItem):
    BASE_W = 320
    # 布局基准（与 renderer._draw_panel 同构）：顶 18 + 行距 32 + 行高 26 + 底 30
    TOP_PAD, ROW_H, LAST_H, BOTTOM_PAD = 18, 32, 26, 30

    def __init__(self, renderer: Renderer):
        super().__init__(0, 0, self.BASE_W, self._base_h(renderer))
        self.renderer = renderer
        self._s = renderer.panel_scale
        self.setPos(renderer.panel_pos[0], renderer.panel_pos[1])
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        # 缩放锚点=左上角：rect 从原点向右下生长，与视频从 panel_pos 向右下画一致
        self._apply_scale()

    @classmethod
    def _base_h(cls, renderer: Renderer) -> float:
        """按当前勾选的数据项数计算面板基准高度（4 项 = 170）。"""
        n = len(renderer.panel_fields)
        if n == 0:
            return 30.0   # 全不勾选时保留小矩形作拖拽命中区
        return cls.TOP_PAD + (n - 1) * cls.ROW_H + cls.LAST_H + cls.BOTTOM_PAD

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        # 不用 Qt setScale：缩放由 _s 驱动 rect+paint 统一变化，
        # 避免“Qt 变换 × paint 内 s”双重放大导致文字溢出、标签被数值遮挡
        # 下限 0.5：字号有 8px 最小可读下限，再小会导致标签与数值重叠
        self._s = max(0.5, min(6.0, self._s * factor))
        self._apply_scale()
        self._sync()

    def mousePressEvent(self, event):
        self._raise_to_top()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._sync()

    def _raise_to_top(self):
        """最后点击的仪表盘压在最上层（zValue 最高）。"""
        if self.scene() is not None:
            max_z = max((it.zValue() for it in self.scene().items()), default=0)
            self.setZValue(max_z + 1)

    def _apply_scale(self):
        """按内部缩放系数重建 rect（paint 中所有尺寸 = 基准值 * _s）。"""
        self.setRect(0, 0, self.BASE_W * self._s, self._base_h(self.renderer) * self._s)
        self.update()

    def boundingRect(self):
        """背景条宽度/外扩边距可独立设置，可能超出数据矩形；
        包围矩形取两者并集，避免绘制被裁剪。"""
        rect = self.rect()
        s = self._s
        pad = self.renderer.panel_bg_pad * s
        bg_w = self.renderer.panel_bg_w * s
        x0 = min(0.0, -pad)
        y0 = min(0.0, -pad)
        x1 = max(rect.width(), bg_w + pad)
        y1 = max(rect.height(), rect.height() + pad)
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _sync(self):
        pos = self.pos()
        self.renderer.panel_pos = (pos.x(), pos.y())
        self.renderer.panel_scale = self._s
        self.scene().update()
        _notify_main_dirty(self)
        _notify_main_scale(self, "panel", self._s)

    def paint(self, painter, option, widget=None):
        # 布局与 renderer._draw_panel 完全同构：背景/字号/间距全部 = 基准值 * s，
        # 缩放时等比例变化，标签与数值永不互相遮挡
        s = self._s

        # 半透明圆角背景（宽度/边距/颜色/不透明度独立可调）+ 数据文本
        # rect 本身仍作拖拽命中区域

        ascent = self.renderer.cum_ascent[-1]
        dist_km = self.renderer.cum_dist[-1] / 1000.0
        elev = self.renderer.points_smoothed[-1][2]
        slope = self.renderer.slope_pct[-1]
        pace = self.renderer.pace_s[-1]

        # setPixelSize 与视频端 PIL 像素字号一致，保证所见即所得
        big_font = QFont("Microsoft YaHei")
        big_font.setPixelSize(max(8, int(24 * s)))
        small_font = QFont("Microsoft YaHei")
        small_font.setPixelSize(max(8, int(20 * s)))
        small_font.setBold(True)   # 粗体标题：与视频端 msyhbd 一致

        # 预览显示终值：运动耗时/总耗时按元数据口径（与视频末帧一致）
        rows_all = [
            ("当前海拔", f"{elev:.0f} m",          QColor(200, 160, 255)),
            ("当前坡度", f"{slope:+.0f}%",         QColor(255, 170, 200)),
            ("累计爬升", f"{ascent:.1f} m",         QColor(80, 220, 120)),
            ("已行里程", f"{dist_km:.2f} km",        QColor(255, 210, 90)),
            ("当前配速", format_pace(pace),          QColor(140, 255, 220)),
            ("运动耗时", format_duration(self.renderer.total_active_s), QColor(120, 200, 255)),
            ("总耗时",   format_duration(self.renderer.total_wall_s), QColor(255, 150, 80)),
        ]
        fields = set(self.renderer.panel_fields)
        rows = [r for r in rows_all if r[0] in fields]
        if not rows:
            return
        row_h = 32 * s
        fm_small = QFontMetrics(small_font)
        fm_big = QFontMetrics(big_font)
        ow = max(1, int(round(s)))   # 黑边宽度（与视频端 stroke 一致）

        # 面板背景：宽度 / 外扩边距 / 颜色 / 不透明度 四项互相独立（与视频端一致）
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        pad = self.renderer.panel_bg_pad * s
        bg_w = self.renderer.panel_bg_w * s
        c = self.renderer.panel_bg_color
        a = int(round(max(0.0, min(100.0, self.renderer.panel_bg_alpha_pct)) * 255 / 100))
        painter.setBrush(QColor(int(c[0]), int(c[1]), int(c[2]), a))
        painter.drawRoundedRect(
            QRectF(-pad, -pad,
                   bg_w + 2 * pad,
                   self.rect().height() + 2 * pad),
            12 * s, 12 * s)
        painter.setBrush(Qt.NoBrush)

        for i, (label, value, color) in enumerate(rows):
            y = 18 * s + i * row_h
            # 数据标题：粗体黑边白字（8 方向偏移近似 PIL stroke）
            label_rect = QRectF(16 * s, y, 150 * s, 24 * s)
            painter.setFont(small_font)
            for dx, dy in [(-ow, 0), (ow, 0), (0, -ow), (0, ow),
                          (-ow, -ow), (ow, -ow), (-ow, ow), (ow, ow)]:
                painter.setPen(QColor(0, 0, 0, 255))
                painter.drawText(label_rect.translated(dx, dy),
                                 Qt.AlignLeft | Qt.AlignTop, label)
            painter.setPen(QColor(255, 255, 255, 255))
            painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignTop, label)
            painter.setPen(color)
            painter.setFont(big_font)
            # 数值紧跟标签右侧，间距 10*s（与视频端一致）
            lx = 16 * s + fm_small.horizontalAdvance(label) + 10 * s
            vw = fm_big.horizontalAdvance(value)
            painter.drawText(QRectF(lx, y - 6 * s, vw + 8, 32 * s),
                             Qt.AlignLeft | Qt.AlignTop, value)


# ─────────────────────── 可拖拽地图导航面板 Item ───────────────────────

class MapPanelItem(QGraphicsRectItem):
    """地图导航面板的 GUI 预览：直接复用视频渲染代码绘制「视频首帧」的完整地图
    （瓦片底图 + 位置箭头 + 指北针 + 边框 + 可选轨迹），
    调整缩放级别/形状/朝向/文字图例/箭头等设置都会即时刷新预览。
    可拖拽定位 + 滚轮缩放（与视频输出 WYSIWYG）。"""

    def __init__(self, renderer: Renderer):
        s = renderer.map_scale
        size = renderer.map_size * s
        super().__init__(0, 0, size, size)
        self.renderer = renderer
        self._s = s
        self._hover = False
        self.setPos(renderer.map_pos[0], renderer.map_pos[1])
        self.setBrush(QBrush(QColor(10, 10, 20, 120)))
        self.setPen(QPen(QColor(80, 160, 255, 200), 2))
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        # 首帧预览图（含边框/箭头/指北针，铺满整个面板）
        self._pix_item = QGraphicsPixmapItem(self)
        self._pix_item.setAcceptedMouseButtons(Qt.NoButton)
        # 占位文字：未加载轨迹 / 预览尚未生成时可见
        self._label = QGraphicsTextItem("地图导航", self)
        self._label.setDefaultTextColor(QColor(200, 200, 220, 200))
        f = QFont("Microsoft YaHei", 10)
        self._label.setFont(f)
        self._label.setPos(10, 10)
        # 防抖：连续调整数值时合并刷新，避免反复拉瓦片
        self._preview_timer = QTimer()
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._render_preview)

    def refresh_preview(self, delay_ms: int = 120):
        """请求刷新首帧预览（默认 120ms 防抖，连续调数值只渲染最后一次）。"""
        self._preview_timer.start(max(0, int(delay_ms)))

    def _preview_ss(self) -> int:
        """预览倍率：按屏幕像素比放大渲染，屏幕显示尺寸不变、文字边框更清晰。"""
        dpr = 1.0
        sc = self.scene()
        views = sc.views() if sc is not None else []
        if views:
            dpr = float(views[0].devicePixelRatioF())
        return max(1, min(3, int(round(dpr))))

    def _render_preview(self):
        """用视频同一套 _draw_map 绘制首帧（idx=0），转成 QPixmap 显示。

        只临时改写 renderer.map_pos/_ss（把面板摆到临时图片左上角），
        其余参数全部原样复用，因此预览与成片首帧像素级一致。
        """
        r = self.renderer
        if r is None or self.scene() is None or len(r.points) < 2:
            return
        ss = self._preview_ss()
        panel = int(r.map_size * r.map_scale)
        if panel <= 0:
            return
        pad = ss                                    # 四周留 1px（×ss）给边框描边
        img = Image.new("RGBA", (panel + 2 * pad, panel + 2 * pad), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        saved_pos = r.map_pos
        saved_ss = int(getattr(r, "_ss", 1) or 1)
        r._ss = ss
        r.map_pos = (1.0, 1.0)                      # _draw_map 内部会 ×ss
        try:
            r._draw_map(img, d, 0)                  # 视频首帧 = 轨迹索引 0
        finally:
            r.map_pos = saved_pos
            r._ss = saved_ss
        buf = BytesIO()
        img.save(buf, format="PNG")
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        if pix.isNull():
            return
        pix.setDevicePixelRatio(float(ss))
        self._pix_item.setPixmap(pix)
        self._pix_item.setPos(-1.0, -1.0)            # 抵消 1px 边框留白
        self._label.hide()
        # 预览图已自带形状与蓝色边框，去掉占位矩形
        self.setBrush(QBrush(Qt.NoBrush))
        self.setPen(QPen(Qt.NoPen))
        self.update()

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._s = max(0.3, min(6.0, self._s * factor))
        size = self.renderer.map_size * self._s
        self.setRect(0, 0, size, size)
        self.renderer.map_scale = self._s
        self.refresh_preview(60)
        self.scene().update()
        _notify_main_dirty(self)
        _notify_main_scale(self, "map", self._s)

    def paint(self, painter, option, widget=None):
        """有预览图时只画悬停高亮轮廓；无预览时按形状画占位框。"""
        if not self._pix_item.pixmap().isNull():
            if self._hover:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(120, 200, 255, 240), 2))
                if getattr(self.renderer, "map_shape", "rect") == "circle":
                    painter.drawEllipse(self.rect())
                else:
                    painter.drawRect(self.rect())
            return
        if getattr(self.renderer, "map_shape", "rect") == "circle":
            painter.setBrush(self.brush())
            painter.setPen(self.pen())
            painter.drawEllipse(self.rect())
        else:
            super().paint(painter, option, widget)

    def mousePressEvent(self, event):
        self._raise_to_top()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._sync()

    def _raise_to_top(self):
        if self.scene() is not None:
            max_z = max((it.zValue() for it in self.scene().items()), default=0)
            self.setZValue(max_z + 1)

    def _sync(self):
        pos = self.pos()
        self.renderer.map_pos = (pos.x(), pos.y())
        self.renderer.map_scale = self._s
        self.scene().update()
        _notify_main_dirty(self)
        _notify_main_scale(self, "map", self._s)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)


# ─────────────────────── 动态坡度仪 Item ───────────────────────

def _ui_font(px: int) -> QFont:
    """GUI 预览字体（与视频端 msyh 一致，保证所见即所得）。"""
    f = QFont("Microsoft YaHei")
    f.setPixelSize(max(8, int(round(px))))
    return f


def _paint_outlined_text(painter, text, rect, color, font):
    """黑边描边文字（Qt 无原生描边，用 8 方向偏移近似，与视频端 PIL 描边一致）。"""
    painter.setFont(font)
    painter.setPen(QColor(0, 0, 0, 255))
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                   (-1, -1), (-1, 1), (1, -1), (1, 1)):
        painter.drawText(rect.translated(dx, dy), Qt.AlignCenter, text)
    painter.setPen(color)
    painter.drawText(rect, Qt.AlignCenter, text)


class SlopeGaugeItem(QGraphicsRectItem):
    """动态坡度仪的 GUI 预览：样式二选一（renderer.gauge_style）。
    - "arc"  方案B 半圆弧仪表：暗色轨道 + 渐变刻度弧 + 当前进度弧 + 光点 + 数值
    - "card" 方案D HUD 深色卡片：圆角卡片 + 大号数值 + 渐变指示条 + 白色游标
    可拖拽定位 + 滚轮调大小；布局复用 renderer 模块级 gauge_* 布局函数，
    与视频端 renderer._draw_slope_gauge / _paint_gauge_* WYSIWYG。"""

    def __init__(self, renderer: Renderer):
        fw, fh = gauge_footprint(renderer.gauge_style,
                                 renderer.slope_gauge_size)
        super().__init__(0, 0, fw, fh)
        self.renderer = renderer
        self._hover = False
        self.setPos(renderer.slope_gauge_pos[0], renderer.slope_gauge_pos[1])
        # rect 只作拖拽命中区，paint 完全自绘（不画默认矩形）
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        size = max(60.0, min(600.0, self.renderer.slope_gauge_size * factor))
        self.renderer.slope_gauge_size = size
        fw, fh = gauge_footprint(self.renderer.gauge_style, size)
        self.prepareGeometryChange()
        self.setRect(0, 0, fw, fh)
        self.scene().update()
        _notify_main_dirty(self)
        _notify_main_scale(self, "gauge", size)

    def mousePressEvent(self, event):
        self._raise_to_top()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._sync()

    def _raise_to_top(self):
        if self.scene() is not None:
            max_z = max((it.zValue() for it in self.scene().items()), default=0)
            self.setZValue(max_z + 1)

    def _sync(self):
        pos = self.pos()
        self.renderer.slope_gauge_pos = (pos.x(), pos.y())
        self.scene().update()
        _notify_main_dirty(self)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter, option, widget=None):
        # 透明底：按样式自绘（与视频端同构）
        painter.setRenderHint(QPainter.Antialiasing, True)

        r = self.renderer
        n = len(r.points)
        idx = r.preview_index() if n else 0.0
        pct = r.gauge_pct(idx) if n else 0.0
        size = r.slope_gauge_size
        a = max(0.0, min(100.0, r.gauge_fill_alpha_pct)) / 100.0
        cold, warm = r.gauge_edge_bottom_color, r.gauge_edge_top_color
        vc = gauge_value_color(pct, cold, warm)
        vcol = QColor(vc[0], vc[1], vc[2])
        value = gauge_format_pct(pct)

        # 选中/悬停时用虚线框标出命中区范围（不参与视频输出）
        if self.isSelected() or self._hover:
            painter.setPen(QPen(QColor(120, 200, 255, 160), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.rect())

        if r.gauge_style == "card":
            self._paint_card(painter, size, pct, a, cold, warm, vcol, value)
        else:
            self._paint_arc(painter, size, pct, a, cold, warm, vcol, value)

    def _paint_arc(self, painter, size, pct, a, cold, warm, vcol, value):
        """方案B 半圆弧仪表（与 renderer._paint_gauge_arc 同构）。"""
        L = gauge_arc_layout(size)
        cx, cy, R, W = L["cx"], L["cy"], L["R"], L["W"]
        rect = QRectF(cx - R, cy - R, 2 * R, 2 * R)

        # 1) 暗色轨道（半圆，圆头端点；透明度随 a）
        painter.setPen(QPen(QColor(16, 22, 35, int(round(0.55 * a * 255))),
                            W, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(rect, 180 * 16, 180 * 16)

        # 2) 彩色刻度弧：48 段小弧拼成连续渐变（左=冷端 → 顶=绿 → 右=暖端）
        seg_pen = QPen()
        seg_pen.setWidthF(W)
        seg_pen.setCapStyle(Qt.FlatCap)
        seg = 180.0 / 48
        for i in range(48):
            fa = 180 + i * seg
            mid = math.radians(fa + seg / 2)
            o = (1 + math.cos(mid)) / 2          # 0=左端 → 1=右端
            c = gauge_value_color(o * 120 - 60, cold, warm)
            seg_pen.setColor(QColor(c[0], c[1], c[2], int(round(0.35 * a * 255))))
            painter.setPen(seg_pen)
            # PIL 角 φ（顺时针，y 向下）→ Qt 角 = 360 − φ（逆时针）
            painter.drawArc(rect, int(round((360 - fa - seg) * 16)) - 32,
                            int(round(seg * 16)) + 64)
        painter.setPen(Qt.NoPen)
        for ex, o in ((cx - R, -60.0), (cx + R, 60.0)):
            c = gauge_value_color(o, cold, warm)
            painter.setBrush(QColor(c[0], c[1], c[2], int(round(0.35 * a * 255))))
            painter.drawEllipse(QPointF(ex, cy), W / 2, W / 2)

        # 3) 当前进度弧：从顶部 0% 展开到当前坡度（平头笔 + 手动圆帽，与 PIL 一致）
        painter.setBrush(Qt.NoBrush)
        pg = QPen(vcol, W, Qt.SolidLine, Qt.FlatCap)
        painter.setPen(pg)
        if pct >= 0.75:
            mdeg = 90 * (1 - pct / 60.0)
            painter.drawArc(rect, int(round(mdeg * 16)),
                            int(round((90 - mdeg) * 16)))
        elif pct <= -0.75:
            mdeg = 90 * (1 + abs(pct) / 60.0)
            painter.drawArc(rect, 90 * 16, int(round((mdeg - 90) * 16)))
        else:
            mdeg = 90.0
        tip = QPointF(cx + R * math.cos(math.radians(mdeg)),
                      cy - R * math.sin(math.radians(mdeg)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(vcol)
        painter.drawEllipse(QPointF(cx, cy - R), W / 2, W / 2)   # 顶部 0% 圆帽
        painter.drawEllipse(tip, W / 2, W / 2)                   # 弧端圆帽
        gr, dr = L["glow_r"], L["dot_r"]
        painter.setBrush(QColor(vc[0], vc[1], vc[2], 90))
        painter.drawEllipse(tip, gr, gr)
        painter.setPen(QPen(QColor(0, 0, 0, 110), 1))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(tip, dr, dr)

        # 4) 数值 + 「坡度」标签（居中）
        vfs = _fit_font_size(
            lambda fs: QFontMetrics(_ui_font(fs)).horizontalAdvance(value),
            int(round(self.renderer.gauge_value_size)), 0.84 * size)
        vf = _ui_font(vfs)
        fm_v = QFontMetrics(vf)
        vw = fm_v.horizontalAdvance(value)
        _paint_outlined_text(
            painter, value,
            QRectF(cx - vw / 2 - 4, L["value_cy"] - fm_v.height() / 2,
                   vw + 8, fm_v.height()), vcol, vf)
        lf = _ui_font(int(round(self.renderer.gauge_label_size
                                * GAUGE_LABEL_RATIO)))
        fm_l = QFontMetrics(lf)
        lw = fm_l.horizontalAdvance("坡度")
        _paint_outlined_text(
            painter, "坡度",
            QRectF(cx - lw / 2 - 4, L["label_cy"] - fm_l.height() / 2,
                   lw + 8, fm_l.height()), QColor(230, 236, 244, 210), lf)

    def _paint_card(self, painter, size, pct, a, cold, warm, vcol, value):
        """方案D HUD 深色卡片（与 renderer._paint_gauge_card 同构）。"""
        L = gauge_card_layout(size)
        w, h, pad, rx = L["w"], L["h"], L["pad"], L["rx"]

        # 1) 卡片底：深色半透明圆角矩形 + 细白边
        painter.setPen(QPen(QColor(255, 255, 255, 46), 1))
        painter.setBrush(QColor(13, 17, 26, int(round(0.62 * a * 255))))
        painter.drawRoundedRect(QRectF(0, 0, w, h), rx, rx)

        # 2) 渐变指示条：横向胶囊（左=冷端 → 中=绿 → 右=暖端）
        strip_h = L["strip_h"]
        sy0 = L["strip_cy"] - strip_h / 2
        sx0, sx1 = pad, w - pad
        grad = QLinearGradient(sx0, 0, sx1, 0)
        for off, c in gauge_gradient_stops(cold, warm):
            grad.setColorAt(off, QColor(c[0], c[1], c[2],
                                        int(round(a * 255))))
        painter.setPen(Qt.NoPen)
        painter.setBrush(grad)
        painter.drawRoundedRect(QRectF(sx0, sy0, sx1 - sx0, strip_h),
                                strip_h / 2, strip_h / 2)

        # 3) 游标：深色晕圈 + 白色圆点
        mx = sx0 + (max(-SLOPE_LIMIT_PCT, min(SLOPE_LIMIT_PCT, pct)) + 60) \
            / 120 * (sx1 - sx0)
        my = L["strip_cy"]
        hr, dr = L["halo_r"], L["dot_r"]
        painter.setBrush(QColor(0, 0, 0, 115))
        painter.drawEllipse(QPointF(mx, my), hr, hr)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QPointF(mx, my), dr, dr)

        # 4) 文本：左侧灰色「坡度」标签 + 右对齐大号彩色数值
        lf = _ui_font(int(round(self.renderer.gauge_label_size
                                * GAUGE_LABEL_RATIO)))
        fm_l = QFontMetrics(lf)
        lw = fm_l.horizontalAdvance("坡度")
        _paint_outlined_text(
            painter, "坡度",
            QRectF(pad - 4, L["label_cy"] - fm_l.height() / 2, lw + 8,
                   fm_l.height()), QColor(169, 180, 196), lf)
        max_w = max(20.0, (w - 2 * pad) - lw - 8)
        vfs = _fit_font_size(
            lambda fs: QFontMetrics(_ui_font(fs)).horizontalAdvance(value),
            int(round(self.renderer.gauge_value_size)), max_w)
        vf = _ui_font(vfs)
        fm_v = QFontMetrics(vf)
        vw = fm_v.horizontalAdvance(value)
        _paint_outlined_text(
            painter, value,
            QRectF(w - pad - vw - 4, L["value_cy"] - fm_v.height() / 2,
                   vw + 8, fm_v.height()), vcol, vf)


# ─────────────────────── 海拔一览图 Item ───────────────────────

class ElevProfileItem(QGraphicsRectItem):
    """海拔一览图的 GUI 预览：直接调用视频渲染端 _draw_elev_profile 生成位图，
    与成片像素级一致（WYSIWYG）。可拖拽定位 + 滚轮等比缩放。"""

    def __init__(self, renderer: Renderer):
        w = float(renderer.elev_profile_w)
        h = float(renderer.elev_profile_h)
        super().__init__(0, 0, w, h)
        self.renderer = renderer
        self._hover = False
        self.setPos(renderer.elev_profile_pos[0], renderer.elev_profile_pos[1])
        self.setBrush(QBrush(Qt.NoBrush))
        self.setPen(QPen(Qt.NoPen))
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self._pix_item = QGraphicsPixmapItem(self)
        self._pix_item.setAcceptedMouseButtons(Qt.NoButton)
        self.refresh_preview()

    def _preview_ss(self) -> int:
        """预览倍率：按屏幕像素比放大渲染，屏幕显示尺寸不变、文字曲线更清晰。"""
        dpr = 1.0
        sc = self.scene()
        views = sc.views() if sc is not None else []
        if views:
            dpr = float(views[0].devicePixelRatioF())
        return max(1, min(3, int(round(dpr))))

    def refresh_preview(self):
        """用视频同一套 _draw_elev_profile 渲染当前预览索引的一帧。"""
        r = self.renderer
        if r is None or self.scene() is None or len(r.points) < 2:
            return
        ss = self._preview_ss()
        w = max(40.0, float(r.elev_profile_w))
        h = max(24.0, float(r.elev_profile_h))
        pad = ss
        img = Image.new(
            "RGBA",
            (int(round(w * ss)) + 2 * pad, int(round(h * ss)) + 2 * pad),
            (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        saved_pos = r.elev_profile_pos
        saved_ss = int(getattr(r, "_ss", 1) or 1)
        r._ss = ss
        r.elev_profile_pos = (1.0, 1.0)      # _draw_elev_profile 内部 ×ss → 1px 留白
        try:
            r._draw_elev_profile(img, d, r.preview_index())
        finally:
            r.elev_profile_pos = saved_pos
            r._ss = saved_ss
        buf = BytesIO()
        img.save(buf, format="PNG")
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        if pix.isNull():
            return
        pix.setDevicePixelRatio(float(ss))
        self._pix_item.setPixmap(pix)
        self._pix_item.setPos(-1.0, -1.0)    # 抵消 1px 边框留白
        self.update()

    def sync_layout(self):
        """按 renderer 的尺寸/位置重建 item（恢复工程布局时调用）。"""
        r = self.renderer
        self.prepareGeometryChange()
        self.setRect(0, 0, float(r.elev_profile_w), float(r.elev_profile_h))
        self.setPos(*r.elev_profile_pos)
        self.refresh_preview()
        if self.scene() is not None:
            self.scene().update()

    def wheelEvent(self, event):
        r = self.renderer
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        ratio = r.elev_profile_h / max(1.0, r.elev_profile_w)
        w = max(220.0, min(1400.0, r.elev_profile_w * factor))
        h = max(90.0, min(800.0, w * ratio))
        r.elev_profile_w = w
        r.elev_profile_h = h
        self.prepareGeometryChange()
        self.setRect(0, 0, w, h)
        self.refresh_preview()
        if self.scene() is not None:
            self.scene().update()
        _notify_main_dirty(self)
        _notify_main_scale(self, "elev", w)

    def mousePressEvent(self, event):
        self._raise_to_top()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._sync()

    def _raise_to_top(self):
        if self.scene() is not None:
            max_z = max((it.zValue() for it in self.scene().items()), default=0)
            self.setZValue(max_z + 1)

    def _sync(self):
        pos = self.pos()
        self.renderer.elev_profile_pos = (pos.x(), pos.y())
        if self.scene() is not None:
            self.scene().update()
        _notify_main_dirty(self)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.isSelected() or self._hover:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(120, 200, 255, 160), 1, Qt.DashLine))
            painter.drawRect(self.rect())
            return
        if self._pix_item.pixmap().isNull():
            painter.setBrush(QBrush(QColor(14, 18, 26, 150)))
            painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
            painter.drawRoundedRect(self.rect(), 12, 12)
            painter.setPen(QColor(200, 200, 220, 180))
            painter.drawText(self.rect(), Qt.AlignCenter, "海拔一览")


# ─────────────────────── 进度环 Item ───────────────────────

class ProgressRingItem(QGraphicsRectItem):
    """进度环的 GUI 预览：直接调用视频渲染端 _draw_progress_ring 生成位图，
    与成片像素级一致（WYSIWYG）。可拖拽定位 + 滚轮调大小；
    样式（经典满环 / 紧凑胶囊）切换时按 ring_footprint 重建命中区。"""

    def __init__(self, renderer: Renderer):
        fw, fh = ring_footprint(renderer.progress_ring_style,
                                float(renderer.progress_ring_size))
        super().__init__(0, 0, fw, fh)
        self.renderer = renderer
        self._hover = False
        self.setPos(renderer.progress_ring_pos[0], renderer.progress_ring_pos[1])
        self.setBrush(QBrush(Qt.NoBrush))
        self.setPen(QPen(Qt.NoPen))
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self._pix_item = QGraphicsPixmapItem(self)
        self._pix_item.setAcceptedMouseButtons(Qt.NoButton)
        self.refresh_preview()

    def _preview_ss(self) -> int:
        """预览倍率：按屏幕像素比放大渲染，屏幕显示尺寸不变、文字更清晰。"""
        dpr = 1.0
        sc = self.scene()
        views = sc.views() if sc is not None else []
        if views:
            dpr = float(views[0].devicePixelRatioF())
        return max(1, min(3, int(round(dpr))))

    def refresh_preview(self):
        """用视频同一套 _draw_progress_ring 渲染当前预览索引的一帧。"""
        r = self.renderer
        if r is None or self.scene() is None or len(r.points) < 2:
            return
        ss = self._preview_ss()
        s = float(r.progress_ring_size) * ss
        fw, fh = ring_footprint(r.progress_ring_style, s)
        pad = max(2, int(round(0.06 * s)))     # 与渲染端一致的环帽余量
        img = Image.new("RGBA", (int(round(fw)) + 2 * pad,
                                 int(round(fh)) + 2 * pad), (0, 0, 0, 0))
        saved_pos = r.progress_ring_pos
        saved_ss = int(getattr(r, "_ss", 1) or 1)
        r._ss = ss
        r.progress_ring_pos = (pad / ss, pad / ss)   # 绘制内部 ×ss → 留 pad 边
        try:
            r._draw_progress_ring(img, int(round(r.preview_index())))
        finally:
            r.progress_ring_pos = saved_pos
            r._ss = saved_ss
        buf = BytesIO()
        img.save(buf, format="PNG")
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        if pix.isNull():
            return
        pix.setDevicePixelRatio(float(ss))
        self._pix_item.setPixmap(pix)
        self._pix_item.setPos(-pad / ss, -pad / ss)   # 抵消环帽留白，内容对齐命中区
        self.update()

    def _relayout(self):
        """样式/大小变化后按 renderer 重建命中区并重渲预览。"""
        r = self.renderer
        fw, fh = ring_footprint(r.progress_ring_style,
                                float(r.progress_ring_size))
        self.prepareGeometryChange()
        self.setRect(0, 0, fw, fh)
        self.refresh_preview()
        if self.scene() is not None:
            self.scene().update()

    def sync_layout(self):
        """按 renderer 的样式/尺寸/位置重建 item（恢复工程布局时调用）。"""
        self.setPos(*self.renderer.progress_ring_pos)
        self._relayout()

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        r = self.renderer
        size = max(60.0, min(600.0, float(r.progress_ring_size) * factor))
        r.progress_ring_size = size
        self._relayout()
        _notify_main_dirty(self)
        _notify_main_scale(self, "ring", size)

    def mousePressEvent(self, event):
        self._raise_to_top()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._sync()

    def _raise_to_top(self):
        if self.scene() is not None:
            max_z = max((it.zValue() for it in self.scene().items()), default=0)
            self.setZValue(max_z + 1)

    def _sync(self):
        pos = self.pos()
        self.renderer.progress_ring_pos = (pos.x(), pos.y())
        if self.scene() is not None:
            self.scene().update()
        _notify_main_dirty(self)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.isSelected() or self._hover:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(120, 200, 255, 160), 1, Qt.DashLine))
            painter.drawRect(self.rect())
            return
        if self._pix_item.pixmap().isNull():
            painter.setBrush(QBrush(QColor(0, 0, 0, 115)))
            painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
            painter.drawRoundedRect(self.rect(), 12, 12)
            painter.setPen(QColor(200, 200, 220, 180))
            painter.drawText(self.rect(), Qt.AlignCenter, "进度环")


# ─────────────────────── CanvasView ───────────────────────

class CanvasView(QGraphicsView):
    """画布视图：蓝色边框内 = 视频实际输出区域；边框外内容不会进入视频。"""

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setAlignment(Qt.AlignCenter)
        # 画布背景图（仅 GUI 预览；视频输出保持全透明）
        self._bg = QPixmap(str(Path(__file__).with_name("back.png")))
        self._bg_scaled = None
        self._bg_size = None

    def _scaled_bg(self, w: int, h: int):
        """按目标尺寸缓存缩放后的背景图（铺满：保持比例、超出部分裁掉）。

        避免拖拽时反复缩放开销。
        """
        if self._bg.isNull():
            return None
        if self._bg_scaled is None or self._bg_size != (w, h):
            self._bg_scaled = self._bg.scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self._bg_size = (w, h)
        return self._bg_scaled

    def drawBackground(self, painter, rect):
        # 背景图铺满整个预览窗口（含画布以外的留白区）；
        # 画布（输出区域）仍用蓝色边框标出，避免背景铺满后分不清输出范围。
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        bg = self._scaled_bg(int(vis.width()), int(vis.height()))
        if bg is not None:
            painter.drawPixmap(
                QPointF(vis.center().x() - bg.width() / 2,
                        vis.center().y() - bg.height() / 2), bg)
        else:
            # back.png 缺失/改名：回退深色窗口底 + 画布白底
            painter.fillRect(rect, QColor("#0c0c12"))
            painter.fillRect(self.scene().sceneRect(), QColor("#ffffff"))
        sr = self.scene().sceneRect()
        painter.setPen(QPen(QColor(90, 130, 255, 230), 0))
        painter.drawRect(sr)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 窗口尺寸变化时重新适配视野，防止留边区扩大让画布外内容“看起来在画布里”
        if not self.scene().sceneRect().isEmpty():
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)


# ─────────────────────── 渲染线程 ───────────────────────

class _RenderCancelled(Exception):
    """用户主动终止渲染。"""


class RenderWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, renderer: Renderer, out_path: Path, fps: int, duration_s: int,
                 render_scale: int = 1):
        super().__init__()
        self.renderer = renderer
        self.out_path = out_path
        self.fps = fps
        self.duration_s = duration_s
        self.render_scale = max(1, int(render_scale or 1))
        self._cancel = False            # 终止标志：progress_cb 检测后抛 _RenderCancelled

    def cancel(self):
        """主线程调用：请求终止。下一帧回调时生效。"""
        self._cancel = True

    def run(self):
        try:
            def _cb(done, total):
                self.progress.emit(done, total)
                if self._cancel:
                    raise _RenderCancelled()
            self.renderer.render_video(
                self.out_path, self.fps, self.duration_s,
                progress_cb=_cb,
                render_scale=self.render_scale,
            )
            self.finished_ok.emit(str(self.out_path))
        except _RenderCancelled:
            # 已写入的临时帧由 render_video 内 TemporaryDirectory 自动清理
            try:
                self.out_path.unlink(missing_ok=True)   # 删半成品视频
            except Exception:
                pass
            self.cancelled.emit()
        except Exception as e:
            self.failed.emit(str(e))


# ─────────────────────── 分段视频设置对话框 ───────────────────────

class SegmentView(QGraphicsView):
    """轨迹预览视图：滚轮缩放、方向键平移、左键添加分割点、右键删除分割点。"""

    def __init__(self, scene, dlg):
        super().__init__(scene)
        self.dlg = dlg
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, ev):
        f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)
        self.dlg._update_marker_scale()   # 标记反向补偿，屏幕尺寸不变
        # 缩放后视区与缩放级别都变了，延迟刷新瓦片（防抖）
        self.dlg._tile_timer.start(150)
        ev.accept()

    def keyPressEvent(self, ev):
        """上下左右方向键平移视图。"""
        step = max(40, int(self.viewport().width() * 0.08))
        vstep = max(40, int(self.viewport().height() * 0.08))
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        key = ev.key()
        if key == Qt.Key_Left:
            hb.setValue(hb.value() - step)
        elif key == Qt.Key_Right:
            hb.setValue(hb.value() + step)
        elif key == Qt.Key_Up:
            vb.setValue(vb.value() - vstep)
        elif key == Qt.Key_Down:
            vb.setValue(vb.value() + vstep)
        else:
            super().keyPressEvent(ev)
            return
        # 平移后可见区域改变，延迟刷新瓦片（防抖）
        self.dlg._tile_timer.start(150)
        ev.accept()

    def scrollContentsBy(self, dx, dy):
        """滚动条拖动时刷新瓦片（平移后可见区域改变）。"""
        super().scrollContentsBy(dx, dy)
        self.dlg._tile_timer.start(150)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            sp = self.mapToScene(ev.pos())
            self.dlg.try_add_cut_at(sp)
            ev.accept()
        elif ev.button() == Qt.RightButton:
            sp = self.mapToScene(ev.pos())
            self.dlg.try_remove_cut_at(sp)
            ev.accept()
        else:
            super().mousePressEvent(ev)


class CutPointItem(QGraphicsEllipseItem):
    """轨迹上的分割点标记（橙色序号球）。"""

    def __init__(self, cut: float, num: int, x: float, y: float, dlg):
        r = 13
        super().__init__(x - r, y - r, 2 * r, 2 * r)
        self._cut = cut
        self._cx, self._cy = x, y
        self.setTransformOriginPoint(x, y)   # 绕圆心反向缩放，位置不动
        self.setBrush(QBrush(QColor(255, 150, 40, 235)))
        self.setPen(QPen(QColor(255, 255, 255, 235), 2))
        self.setZValue(10)
        # 数字标号：白色粗体居中于圆心，随父项反向缩放保持屏幕尺寸
        label = QGraphicsTextItem(str(num), self)
        label.setDefaultTextColor(QColor(255, 255, 255))
        f = QFont("Microsoft YaHei")
        f.setPixelSize(14)
        f.setBold(True)
        label.setFont(f)
        lw = label.boundingRect().width()
        lh = label.boundingRect().height()
        label.setPos(x - lw / 2, y - lh / 2)
        self.setToolTip(f"分割点 {num}（进度 {cut*100:.1f}%）——右键删除")


class _NoWheelSpin(QSpinBox):
    """禁用滚轮与上下键的 spinbox（方向键留给轨迹预览平移用）。"""

    def wheelEvent(self, e):
        e.ignore()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Up, Qt.Key_Down,
                       Qt.Key_PageUp, Qt.Key_PageDown):
            return
        super().keyPressEvent(e)


class TimeFrameEdit(QWidget):
    """时:分:秒:帧 组合输入框，内部以「帧数」为唯一数据模型。

    帧数 = (h*3600 + m*60 + s) * fps + frame
    改 fps 时帧数不变，仅刷新 4 个 spinbox 的显示与帧范围。
    """

    valueChanged = pyqtSignal(int)   # 参数：总帧数

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fps = 30
        self._frames = 0
        self._block = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self.sp_h = _NoWheelSpin(); self.sp_h.setRange(0, 23)
        self.sp_m = _NoWheelSpin(); self.sp_m.setRange(0, 59)
        self.sp_s = _NoWheelSpin(); self.sp_s.setRange(0, 59)
        self.sp_f = _NoWheelSpin(); self.sp_f.setRange(0, self._fps - 1)

        for sp in (self.sp_h, self.sp_m, self.sp_s, self.sp_f):
            sp.setKeyboardTracking(False)
            sp.setFixedWidth(42)
            sp.setAlignment(Qt.AlignCenter)

        lay.addWidget(self.sp_h)
        lay.addWidget(QLabel(":"))
        lay.addWidget(self.sp_m)
        lay.addWidget(QLabel(":"))
        lay.addWidget(self.sp_s)
        lay.addWidget(QLabel(":"))
        lay.addWidget(self.sp_f)

        self.sp_h.valueChanged.connect(self._on_changed)
        self.sp_m.valueChanged.connect(self._on_changed)
        self.sp_s.valueChanged.connect(self._on_changed)
        self.sp_f.valueChanged.connect(self._on_changed)

    def set_fps(self, fps: int):
        fps = max(1, int(fps))
        if fps == self._fps:
            return
        self._fps = fps
        self.sp_f.setRange(0, fps - 1)
        # 帧数不变，刷新显示
        self._update_spins_from_frames()

    def total_frames(self) -> int:
        h = self.sp_h.value()
        m = self.sp_m.value()
        s = self.sp_s.value()
        f = self.sp_f.value()
        return (h * 3600 + m * 60 + s) * self._fps + f

    def set_total_frames(self, frames: int):
        self._frames = max(0, int(frames))
        self._update_spins_from_frames()

    def _update_spins_from_frames(self):
        self._block = True
        fps = self._fps
        total_s, f = divmod(self._frames, fps)
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        self.sp_h.setValue(h)
        self.sp_m.setValue(m)
        self.sp_s.setValue(s)
        self.sp_f.setValue(f)
        self._block = False

    def _on_changed(self):
        if self._block:
            return
        self._frames = self.total_frames()
        self.valueChanged.emit(self._frames)


class SegmentDialog(QDialog):
    """分段视频设置：每段填写「截止时间（时:分:秒:帧）」，各段独立，互不联动。

    输入模型：
      - 数据模型 = end_frames（每段在视频中的截止帧数，int），是唯一编辑对象。
      - 表格「截止时间」列（时:分:秒:帧）直接编辑 end_frames[i]，其它段完全不受影响。
      - 视频总长（秒）= 末段截止帧数 / fps（自动）。
      - 校验：截止帧数必须严格递增；若不满足（如前段 ≥ 后段），
        不自动修改任何值，仅标红并禁用「确定」，无法保存。
      - 轨迹进度（cuts）由轨迹预览点击决定，与时间独立。
      - fps 可在对话框顶部修改；改 fps 时帧数不变，仅时分秒帧显示刷新。
    段内仍按真实节奏线性压缩；段与段拼接连续。
    """

    def __init__(self, renderer: Renderer, default_duration: int, fps: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("分段视频设置")
        self.resize(1120, 760)
        self.renderer = renderer
        self.fps = max(1, int(fps))
        self._orig_fps = self.fps   # 用于确定时判断是否需要同步回主界面
        # 数据模型：end_frames = 每段截止帧数（唯一编辑对象，各段独立）
        # durations_frames / cut_frames / total_frames 由 end_frames 推导。
        if renderer.segments:
            self.cuts = [float(c) for c in renderer.segments[0]]
            durs = [max(1, float(d)) for d in renderer.segments[1]]   # 秒（可能是 float）
            # durations(秒) → end_frames（累加）
            self.end_frames = []
            acc = 0
            for d in durs:
                acc += max(1, int(round(d * self.fps)))
                self.end_frames.append(acc)
        else:
            self.cuts = []
            self.end_frames = [max(1, int(round(default_duration * self.fps)))]
        self._default_frames = max(1, int(round(default_duration * self.fps)))
        # 派生量
        self.durations_frames = []
        self.cut_frames = []
        self.total_frames = 0
        self._recompute()

        # 动态瓦片地图状态（_build_map_layer 初始化）
        self._map_tiles = {}              # {(z, tx, ty): QGraphicsPixmapItem}
        self._map_base_z = None           # 基准 zoom（按轨迹包围盒选）
        self._map_zoom_current = None     # 当前已渲染的 zoom
        self._map_gcj_offset = (0.0, 0.0) # GCJ-WGS 偏移（近似常数）
        # 滚轮/平移后延迟刷新瓦片（防抖）
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.timeout.connect(self._refresh_map_tiles)

        lay = QVBoxLayout(self)
        tip = QLabel("滚轮缩放 · 方向键（↑↓←→）平移 · 左键点击轨迹添加分割点 · 右键删除\n"
                     "「截止时间」逐段独立填写（时:分:秒:帧），互不影响；"
                     "若前段截止时间 ≥ 后段，将标红并禁止保存")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # 顶部：帧率设置 + 视频总长
        row_top = QHBoxLayout()
        row_top.addWidget(QLabel("帧率"))
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(1, 120)
        self.spin_fps.setValue(self.fps)
        self.spin_fps.setSuffix(" fps")
        self.spin_fps.valueChanged.connect(self._on_fps_changed)
        row_top.addWidget(self.spin_fps)
        row_top.addSpacing(30)
        self.lbl_total_top = QLabel()
        self.lbl_total_top.setStyleSheet("font-weight: bold;")
        row_top.addWidget(self.lbl_total_top)
        row_top.addStretch()
        lay.addLayout(row_top)

        # 轨迹预览
        self.scene = QGraphicsScene()
        self.view = SegmentView(self.scene, self)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor(0x10, 0x10, 0x18)))
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        lay.addWidget(self.view, 1)

        # 段表格（5 列：段 / 轨迹进度 / 对应墙钟时间 / 视频时间范围 / 截止时间）
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["段", "轨迹进度", "对应墙钟时间", "视频时间范围", "截止时间 (时:分:秒:帧)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, 1)

        # 校验提示（默认隐藏，非法时显示）
        self.lbl_warn = QLabel()
        self.lbl_warn.setStyleSheet("color: #ff7070; font-weight: bold;")
        self.lbl_warn.hide()
        lay.addWidget(self.lbl_warn)

        # 底部
        row2 = QHBoxLayout()
        btn_clear = QPushButton("清除全部分段")
        btn_clear.clicked.connect(self._clear_all)
        row2.addWidget(btn_clear)
        row2.addStretch()
        self.lbl_total = QLabel()
        row2.addWidget(self.lbl_total)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._btn_ok = bb.button(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row2.addWidget(bb)
        lay.addLayout(row2)

        self._build_scene()
        self._refresh_table()

    # ── 帧 ↔ 时:分:秒:帧 转换 ──

    def _frames_to_hmsf(self, frames: int):
        """帧数 → (h, m, s, f)。"""
        total_s, f = divmod(max(0, int(frames)), self.fps)
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        return h, m, s, f

    def _fmt_hmsf(self, frames: int) -> str:
        h, m, s, f = self._frames_to_hmsf(frames)
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    def _recompute(self):
        """由 end_frames 推算 durations_frames / cut_frames / total_frames（派生量）。"""
        prev = 0
        self.durations_frames = []
        for t in self.end_frames:
            self.durations_frames.append(max(1, t - prev))
            prev = t
        self.cut_frames = list(self.end_frames[:-1])
        self.total_frames = self.end_frames[-1] if self.end_frames else 0

    def _validate(self):
        """校验 end_frames 严格递增；返回 (ok, [非法行号...])。不修改任何值。"""
        bad = []
        for i, t in enumerate(self.end_frames):
            if t < 1 or (i > 0 and t <= self.end_frames[i - 1]):
                bad.append(i)
        return (not bad, bad)

    def _apply_validation(self):
        """根据校验结果刷新：标红非法行 + 提示 + 禁用确定。"""
        ok, bad = self._validate()
        bad_set = set(bad)
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 4)
            if w is not None:
                w.setStyleSheet(
                    "QSpinBox { border: 2px solid #ff5050; }"
                    if i in bad_set else "")
        if ok:
            self.lbl_warn.hide()
        else:
            rows = "、".join(f"第 {i + 1} 段" for i in bad)
            self.lbl_warn.setText(
                f"⚠ {rows} 的截止时间无效：必须大于 0 且严格大于前一段，否则无法保存")
            self.lbl_warn.show()
        if self._btn_ok is not None:
            self._btn_ok.setEnabled(ok)

    def _on_fps_changed(self, val: int):
        """帧率变化：帧数不变，仅刷新所有 时:分:秒:帧 显示与帧范围。"""
        self.fps = max(1, int(val))
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 4)
            if isinstance(w, TimeFrameEdit):
                w.set_fps(self.fps)
        # 帧数不变 → 时分秒帧显示刷新；时间范围列也需刷新（秒数 = 帧数/fps 变了）
        self._refresh_time_cols()
        self._update_total()

    def accept(self):
        """保存前最终校验：非法时阻止关闭（双保险，正常已被禁用确定）。"""
        ok, bad = self._validate()
        if not ok:
            QMessageBox.warning(
                self, "无法保存",
                "存在无效的截止时间（必须大于 0 且严格递增）。\n"
                "请修正标红的段后再保存。")
            return
        super().accept()

    # ── 预览场景 ──

    def _build_scene(self):
        r = self.renderer
        self.scene.setSceneRect(0, 0, r.width, r.height)
        path = QPainterPath()
        for i, (x, y) in enumerate(r.base_projected):
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        pi = QGraphicsPathItem(path)
        uc = r.unvisited_color
        pen = QPen(QColor(uc[0], uc[1], uc[2], 220), 3)
        pen.setCosmetic(True)                    # 屏幕恒 3px，放大不变粗
        pi.setPen(pen)
        self.scene.addItem(pi)
        # 起终点
        self._end_items = []
        if len(r.base_projected) >= 2:
            for (px, py), color in [(r.base_projected[0], r.start_color),
                                     (r.base_projected[-1], r.end_color)]:
                it = QGraphicsEllipseItem(px - 10, py - 10, 20, 20)
                it.setTransformOriginPoint(px, py)
                it.setBrush(QBrush(QColor(*color)))
                it.setPen(QPen(QColor(255, 255, 255, 220), 2))
                self.scene.addItem(it)
                self._end_items.append(it)
        self._cut_items = []
        self._rebuild_cut_items()
        # fitInView 必须先于 _build_map_layer：瓦片选取依赖正确的视图缩放与可见区域
        self.view.fitInView(self.scene.sceneRect().adjusted(-40, -40, 40, 40),
                            Qt.KeepAspectRatio)
        self._update_marker_scale()
        self._build_map_layer()          # 底图（z=-1，随视图缩放平移）

    def _update_marker_scale(self):
        """视图缩放时反向补偿标记（分割点/起终点），保持屏幕尺寸不变。"""
        m = self.view.transform().m11()
        inv = 1.0 / m if m > 0 else 1.0
        for it in self._cut_items + getattr(self, "_end_items", []):
            it.setScale(inv)

    def _build_map_layer(self):
        """初始化动态瓦片地图：算基准 zoom + GCJ 偏移，再调 _refresh_map_tiles 渲染。

        视图缩放超过 2 倍自动切到更高 zoom 瓦片（地名更详细），
        平移到新区域自动取该区域瓦片。瓦片是独立 QGraphicsPixmapItem，
        scene→WGS→GCJ→瓦片坐标变换，局部区域内 GCJ 偏移近似常数。
        """
        r = self.renderer
        pts = r.points
        if not pts:
            return
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        lon_min, lon_max = min(lons), max(lons)
        lat_min, lat_max = min(lats), max(lats)

        # GCJ-02 包围盒（用于选基准 zoom）
        glons, glats = [], []
        for lo, la in ((lon_min, lat_min), (lon_min, lat_max),
                       (lon_max, lat_min), (lon_max, lat_max)):
            glo, gla = _wgs84_to_gcj02(lo, la)
            glons.append(glo)
            glats.append(gla)
        glon0, glon1 = min(glons), max(glons)

        # 基准 zoom：合成图宽度目标 ~2048px
        span_lon = max(1e-6, glon1 - glon0)
        z = int(math.log2(2048.0 * 360.0 / (256.0 * span_lon)))
        self._map_base_z = max(3, min(17, z))

        # GCJ↔WGS 偏移（局部常数近似）：取包围盒中心的偏移
        c_lon = (lon_min + lon_max) / 2
        c_lat = (lat_min + lat_max) / 2
        g_lon, g_lat = _wgs84_to_gcj02(c_lon, c_lat)
        self._map_gcj_offset = (g_lon - c_lon, g_lat - c_lat)

        # 首次渲染（按当前视图缩放选 z_target）
        self._refresh_map_tiles()

    def _refresh_map_tiles(self):
        """按当前视图缩放 + 可见区域，刷新瓦片图层。

        - 视图缩放 m，z_target = base_z + max(0, floor(log2(m)))，夹到 [base_z, 18]
        - 算可见 scene rect → WGS bbox → GCJ bbox → 该 z 的瓦片范围
        - 取瓦片、定位、入 scene；删掉旧 zoom 的瓦片
        """
        if self._map_base_z is None or self.renderer is None:
            return
        m = self.view.transform().m11()
        if m <= 0:
            return
        # 目标 zoom：每放大 2 倍提升 1 级，不放大时维持基准
        z_target = self._map_base_z + max(0, int(math.floor(math.log2(max(m, 1e-6)))))
        z_target = max(self._map_base_z, min(18, z_target))

        # 可见 scene rect → WGS bbox → GCJ bbox
        vis_poly = self.view.mapToScene(self.view.viewport().rect())
        vis = vis_poly.boundingRect()
        inv = getattr(self.renderer._project, "inverse", None)
        if inv is None:
            return
        corners = [(vis.left(), vis.top()), (vis.right(), vis.top()),
                   (vis.left(), vis.bottom()), (vis.right(), vis.bottom())]
        wgs_pts = [inv(x, y) for x, y in corners]
        w_lons = [p[0] for p in wgs_pts]
        w_lats = [p[1] for p in wgs_pts]
        # 可见 WGS bbox → GCJ bbox
        glons, glats = [], []
        for lo, la in ((min(w_lons), min(w_lats)), (min(w_lons), max(w_lats)),
                       (max(w_lons), min(w_lats)), (max(w_lons), max(w_lats))):
            glo, gla = _wgs84_to_gcj02(lo, la)
            glons.append(glo)
            glats.append(gla)
        glon0, glon1 = min(glons), max(glons)
        glat0, glat1 = min(glats), max(glats)

        # 该 z 下覆盖可见 GCJ bbox 的瓦片范围
        px0, py_top = _lonlat_to_webmerc_px(glon0, glat1, z_target)
        px1, py_bot = _lonlat_to_webmerc_px(glon1, glat0, z_target)
        tx0, tx1 = int(px0 // 256), int(px1 // 256)
        ty0, ty1 = int(py_top // 256), int(py_bot // 256)
        # 上限保护：异常大可见区域（如未 fitInView）不一次拉太多瓦片
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 64:
            return

        # 清掉旧 zoom 的瓦片
        for key, it in list(self._map_tiles.items()):
            if key[0] != z_target:
                self.scene.removeItem(it)
                del self._map_tiles[key]
        self._map_zoom_current = z_target

        off_lon, off_lat = self._map_gcj_offset
        r = self.renderer
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                key = (z_target, tx, ty)
                if key in self._map_tiles:
                    continue   # 已渲染
                t = _fetch_tile(z_target, tx, ty)
                if t is None:
                    continue
                pix = QPixmap()
                # 瓦片 RGBA → QPixmap（保留半透明边缘，便于无背景融合）
                buf = BytesIO()
                t.save(buf, format="PNG")
                pix.loadFromData(buf.getvalue(), "PNG")
                # 瓦片 GCJ 经纬度四角 → 减偏移得 WGS → project 得 scene 坐标
                gcj_lon_l, gcj_lat_t = _webmerc_px_to_lonlat(tx * 256, ty * 256, z_target)
                gcj_lon_r, gcj_lat_b = _webmerc_px_to_lonlat((tx + 1) * 256, (ty + 1) * 256, z_target)
                w_lon_l = gcj_lon_l - off_lon
                w_lon_r = gcj_lon_r - off_lon
                w_lat_t = gcj_lat_t - off_lat
                w_lat_b = gcj_lat_b - off_lat
                sx0, sy0 = r._project(w_lon_l, w_lat_t)
                sx1, sy1 = r._project(w_lon_r, w_lat_b)
                item = QGraphicsPixmapItem(pix)
                item.setPos(sx0, sy0)
                # 非均匀缩放：墨卡托瓦片横向跨 Δlon、纵向只跨 Δlon·cos(lat)，
                # 而场景投影是线性度数（横纵同比例）。必须横纵各自缩放到投影后
                # 矩形，否则瓦片纵向被拉高 1/cos(lat)≈1.2 倍，相邻瓦片对不齐
                tr = QTransform()
                tr.scale(max(sx1 - sx0, 1e-6) / 256.0,
                         max(sy1 - sy0, 1e-6) / 256.0)
                item.setTransform(tr)
                item.setZValue(-1)
                self.scene.addItem(item)
                self._map_tiles[key] = item

    def _rebuild_cut_items(self):
        for it in self._cut_items:
            self.scene.removeItem(it)
        self._cut_items = []
        n = len(self.renderer.base_projected)
        for i, c in enumerate(self.cuts):
            idx = max(0, min(n - 1, round(c * (n - 1))))
            x, y = self.renderer.base_projected[idx]
            self._cut_items.append(CutPointItem(c, i + 1, x, y, self))
            self.scene.addItem(self._cut_items[-1])
        self._update_marker_scale()

    # ── 分割点编辑 ──

    def try_add_cut_at(self, scene_pos) -> bool:
        r = self.renderer
        n = len(r.base_projected)
        if n < 2:
            return False
        # 距离阈值：屏幕 60px 折算为场景坐标
        m = self.view.transform().m11()
        thr = (60.0 / m if m > 0 else 60.0) ** 2
        best, bd = -1, float("inf")
        for i, (x, y) in enumerate(r.base_projected):
            d2 = (x - scene_pos.x()) ** 2 + (y - scene_pos.y()) ** 2
            if d2 < bd:
                bd, best = d2, i
        if best < 0 or bd > thr:
            return False
        c = best / (n - 1)
        if not (0.005 < c < 0.995):
            return False
        # 只要求不落在同一个整数轨迹点上（相邻点也可各设一个分割点）
        if any(round(e * (n - 1)) == best for e in self.cuts):
            return False
        # 插入新 cut：原段时间范围拆为两半（新段截止 = 原区间中点），总长不变
        import bisect
        pos = bisect.bisect(self.cuts, c)
        self.cuts.insert(pos, c)
        prev_end = self.end_frames[pos - 1] if pos > 0 else 0
        this_end = self.end_frames[pos] if pos < len(self.end_frames) else self._default_frames
        mid = max(prev_end + 1, (prev_end + this_end) // 2)
        self.end_frames.insert(pos, mid)   # 原段截止=mid，新段截止=原截止
        self._recompute()
        self._rebuild_cut_items()
        self._refresh_table()
        return True

    def try_remove_cut_at(self, scene_pos) -> bool:
        """右键：删除 scene_pos 附近（视图 25px 内）的分割点。"""
        m = self.view.transform().m11()
        thr = (25.0 / m if m > 0 else 25.0) ** 2
        for it in self._cut_items:
            d2 = (it._cx - scene_pos.x()) ** 2 + (it._cy - scene_pos.y()) ** 2
            if d2 <= thr:
                self.remove_cut(it._cut)
                return True
        return False

    def remove_cut(self, cut: float):
        """删除一个 cut：合并相邻两段，合并后截止 = 后一段的截止，总长不变。"""
        if cut in self.cuts:
            pos = self.cuts.index(cut)
            self.cuts.pop(pos)
            # 合并段 pos 与 pos+1：删除前段的截止（保留后段截止）
            if pos < len(self.end_frames) - 1:
                del self.end_frames[pos]
            self._recompute()
            self._rebuild_cut_items()
            self._refresh_table()

    def _clear_all(self):
        self.cuts = []
        self.end_frames = [self._default_frames]
        self._recompute()
        self._rebuild_cut_items()
        self._refresh_table()

    # ── 段表格 ──

    def _fmt_time(self, ratio: float) -> str:
        # ratio = 点索引归一化（与 cuts 一致），反查该点的真实墙钟时间
        # 这样表格显示的时间与轨迹该位置的真实时间一致，与视频渲染口径相同
        n = len(self.renderer.points)
        idx = max(0, min(n - 1, int(round(ratio * (n - 1)))))
        s = self.renderer.cum_time[idx]
        h, rem = divmod(int(s), 3600)
        m, sec = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{sec:02d}"

    def _refresh_table(self):
        bounds = [0.0] + self.cuts + [1.0]
        nseg = len(bounds) - 1

        self.table.setRowCount(nseg)
        for i in range(nseg):
            a, b = bounds[i], bounds[i + 1]
            # 段视频帧边界（cut_frames 已由 durations_frames 推导）
            f_start = self.cut_frames[i - 1] if i > 0 else 0
            f_end = self.cut_frames[i] if i < len(self.cut_frames) else self.total_frames

            it = QTableWidgetItem(f"第 {i + 1} 段")
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 0, it)
            it = QTableWidgetItem(f"{a*100:.1f}% → {b*100:.1f}%")
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 1, it)
            it = QTableWidgetItem(f"{self._fmt_time(a)} → {self._fmt_time(b)}")
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 2, it)
            it = QTableWidgetItem(f"{self._fmt_hmsf(f_start)} → {self._fmt_hmsf(f_end)}")
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 3, it)
            # 截止时间 时:分:秒:帧 编辑器：直接编辑 end_frames[i]，各段独立
            edit = TimeFrameEdit()
            edit.set_fps(self.fps)
            edit.set_total_frames(self.end_frames[i])
            edit.setToolTip("该段在视频中截止的时:分:秒:帧；必须大于前一段，否则无法保存")
            edit.valueChanged.connect(lambda v, idx=i: self._on_end_time_changed(idx, v))
            self.table.setCellWidget(i, 4, edit)
        self._update_total()
        self._apply_validation()

    def _on_end_time_changed(self, idx: int, val: int):
        """段idx截止帧数变化：只改 end_frames[idx]，其它段不受影响；校验并联动 UI。"""
        if 0 <= idx < len(self.end_frames):
            self.end_frames[idx] = int(val)
        self._recompute()
        # 只刷新时间范围/总长/校验，不重建输入框（避免打断继续输入）
        self._refresh_time_cols()
        self._apply_validation()

    def _refresh_time_cols(self):
        """只更新「视频时间范围」只读列与总长标签（不动输入框）。"""
        for i in range(self.table.rowCount()):
            f_start = self.cut_frames[i - 1] if i > 0 else 0
            f_end = self.cut_frames[i] if i < len(self.cut_frames) else self.total_frames
            it = self.table.item(i, 3)
            if it is not None:
                it.setText(f"{self._fmt_hmsf(f_start)} → {self._fmt_hmsf(f_end)}")
        self._update_total()

    def _update_total(self):
        nseg = len(self.cuts) + 1
        total_s = self.total_frames / self.fps
        self.lbl_total.setText(
            f"共 {nseg} 段 · 视频总长 {self._fmt_hmsf(self.total_frames)}（{total_s:.2f} s）")
        self.lbl_total_top.setText(
            f"视频总长： {self._fmt_hmsf(self.total_frames)}（= 末段截止时间，{total_s:.2f} s）")

    def result_segments(self):
        """返回 (cuts, durations) 供 Renderer 使用；cuts 为空表示未分段。
        durations 单位为秒（float，精度到帧 = 1/fps），renderer 端 round(dur*fps) 还原帧数。
        仅在 accept() 校验通过后调用（end_frames 严格递增）。"""
        if not self.cuts:
            return None
        durations = []
        prev = 0
        for t in self.end_frames:
            durations.append(max(1, t - prev) / self.fps)
            prev = t
        return (list(self.cuts), durations)

    def fps_changed(self) -> bool:
        """对话框内帧率是否被修改过（确定时需同步回主界面）。"""
        return self.fps != self._orig_fps


# ─────────────────────── 关键节点编辑对话框 ───────────────────────

class NodeView(QGraphicsView):
    """关键节点编辑器视图：滚轮缩放、方向键平移、右键添加节点、左键删除节点。"""

    def __init__(self, scene, dlg):
        super().__init__(scene)
        self.dlg = dlg
        self.setFocusPolicy(Qt.StrongFocus)
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor(0x10, 0x10, 0x18)))
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def wheelEvent(self, ev):
        f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)
        self.dlg._update_marker_scale()
        self.dlg._tile_timer.start(150)
        ev.accept()

    def keyPressEvent(self, ev):
        step = max(40, int(self.viewport().width() * 0.08))
        vstep = max(40, int(self.viewport().height() * 0.08))
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        key = ev.key()
        if key == Qt.Key_Left:
            hb.setValue(hb.value() - step)
        elif key == Qt.Key_Right:
            hb.setValue(hb.value() + step)
        elif key == Qt.Key_Up:
            vb.setValue(vb.value() - vstep)
        elif key == Qt.Key_Down:
            vb.setValue(vb.value() + vstep)
        else:
            super().keyPressEvent(ev)
            return
        self.dlg._tile_timer.start(150)
        ev.accept()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.dlg._tile_timer.start(150)

    def mousePressEvent(self, ev):
        sp = self.mapToScene(ev.pos())
        if ev.button() == Qt.RightButton:
            self.dlg.try_add_node_at(sp)
            ev.accept()
        elif ev.button() == Qt.LeftButton:
            self.dlg.try_remove_node_at(sp)
            ev.accept()
        else:
            super().mousePressEvent(ev)


class NodeMarkItem(QGraphicsEllipseItem):
    """关键节点标记（带名称标签），右键添加/左键删除。"""

    def __init__(self, name: str, x: float, y: float, dlg):
        r = 10
        super().__init__(x - r, y - r, 2 * r, 2 * r)
        self._name = name
        self._cx, self._cy = x, y
        self.dlg = dlg
        self.setTransformOriginPoint(x, y)
        c = dlg._node_color
        self.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), 235)))
        self.setPen(QPen(QColor(255, 255, 255, 235), 2))
        self.setZValue(10)
        # 名称标签：半透明黑底胶囊 + 白字
        f = QFont("Microsoft YaHei")
        f.setPixelSize(14)
        label = QGraphicsTextItem(name, self)
        label.setDefaultTextColor(QColor(255, 255, 255))
        label.setFont(f)
        fm = QFontMetrics(f)
        adv = fm.horizontalAdvance(name)
        bx = x + 12 - 6
        by = y - 22 - 4
        # 黑底胶囊
        bg_path = QPainterPath()
        bg_path.addRoundedRect(QRectF(bx, by, adv + 12, fm.height() + 8),
                               (fm.height() + 8) / 2, (fm.height() + 8) / 2)
        bg = QGraphicsPathItem(bg_path, self)
        bg.setPen(QPen(Qt.NoPen))
        bg.setBrush(QBrush(QColor(0, 0, 0, 180)))
        bg.setAcceptedMouseButtons(Qt.NoButton)
        # 白字
        tp = QPainterPath()
        tp.addText(0, 0, f, name)
        tp_item = QGraphicsPathItem(tp, self)
        tp_item.setPen(QPen(Qt.NoPen))
        tp_item.setBrush(QBrush(QColor(255, 255, 255, 255)))
        tp_item.setPos(bx + 6, by + 4 + fm.ascent())
        tp_item.setAcceptedMouseButtons(Qt.NoButton)
        self.setToolTip(f"节点「{name}」——左键删除")


class NodeEditorDialog(QDialog):
    """关键节点编辑器：JSON 输入 + 右键点击轨迹添加 + 地图背景。

    - 数据模型：nodes = [(name, lon, lat), ...]
    - 右键点击轨迹附近 → 吸附最近轨迹点 → 弹窗命名 → 添加
    - 左键点击已有节点 → 删除
    - JSON 输入框：实时解析并刷新预览
    - 确定：写回 renderer.key_nodes
    """

    def __init__(self, renderer: Renderer, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关键节点编辑器")
        self.resize(1100, 780)
        self.renderer = renderer
        # 节点数据（编辑副本，确定时写回）
        self.nodes = list(renderer.key_nodes) if renderer.key_nodes else []
        # 节点颜色（与主窗口一致）
        nc = getattr(renderer, "node_color", (255, 200, 60))
        self._node_color = QColor(int(nc[0]), int(nc[1]), int(nc[2]))

        # 动态瓦片地图状态（复用 SegmentDialog 逻辑）
        self._map_tiles = {}
        self._map_base_z = None
        self._map_zoom_current = None
        self._map_gcj_offset = (0.0, 0.0)
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.timeout.connect(self._refresh_map_tiles)

        lay = QVBoxLayout(self)
        tip = QLabel("滚轮缩放 · 方向键平移 · **右键点击轨迹添加节点** · 左键点击节点删除\n"
                     "下方 JSON 框可直接编辑（修改后点「应用 JSON」刷新预览）")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # 轨迹预览（带地图）
        self.scene = QGraphicsScene()
        self.view = NodeView(self.scene, self)
        lay.addWidget(self.view, 1)

        # JSON 编辑
        jg = QGroupBox("节点 JSON")
        jl = QVBoxLayout(jg)
        self.txt_json = QTextEdit()
        self.txt_json.setPlaceholderText(
            '[{"name": "山脚", "lon": 113.05, "lat": 22.55},\n'
            ' {"name": "垭口", "lon": 113.08, "lat": 22.58}]')
        self.txt_json.setFixedHeight(120)
        jl.addWidget(self.txt_json)
        row_j = QHBoxLayout()
        btn_apply_json = QPushButton("应用 JSON → 刷新预览")
        btn_apply_json.clicked.connect(self._apply_json_to_preview)
        row_j.addWidget(btn_apply_json)
        row_j.addStretch()
        self.lbl_count = QLabel()
        row_j.addWidget(self.lbl_count)
        jl.addLayout(row_j)
        lay.addWidget(jg)

        # 底部
        row2 = QHBoxLayout()
        row2.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row2.addWidget(bb)
        lay.addLayout(row2)

        self._node_items = []
        self._build_scene()
        self._sync_json_from_nodes()

    # ── 预览场景 ──

    def _build_scene(self):
        r = self.renderer
        self.scene.setSceneRect(0, 0, r.width, r.height)
        # 轨迹
        path = QPainterPath()
        for i, (x, y) in enumerate(r.base_projected):
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        pi = QGraphicsPathItem(path)
        uc = r.unvisited_color
        pen = QPen(QColor(uc[0], uc[1], uc[2], 220), 3)
        pen.setCosmetic(True)
        pi.setPen(pen)
        self.scene.addItem(pi)
        # 方向小箭头：沿轨迹等弧长分布，指示行进方向（仅编辑器预览）
        self._arrow_items = []
        pts = r.base_projected
        if len(pts) >= 2:
            seg_len = []
            total = 0.0
            for i in range(1, len(pts)):
                d = math.hypot(pts[i][0] - pts[i - 1][0],
                               pts[i][1] - pts[i - 1][1])
                seg_len.append(d)
                total += d
            if total > 0:
                interval = max(50.0, total / 120)  # 等弧长间隔，最多约120个
                tri = QPolygonF([QPointF(6.0, 0.0), QPointF(-4.0, 3.8),
                                 QPointF(-4.0, -3.8)])
                target, acc = interval, 0.0
                for i in range(1, len(pts)):
                    x0, y0 = pts[i - 1]
                    x1, y1 = pts[i]
                    d = seg_len[i - 1]
                    if d <= 0:
                        continue
                    while target <= acc + d and target < total - 1e-6:
                        t = (target - acc) / d
                        ar = QGraphicsPolygonItem(tri)
                        ar.setPos(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
                        ar.setRotation(math.degrees(math.atan2(y1 - y0, x1 - x0)))
                        ar.setBrush(QBrush(QColor(255, 255, 255, 235)))
                        ap = QPen(QColor(0, 0, 0, 130), 1)
                        ap.setCosmetic(True)
                        ar.setPen(ap)
                        ar.setZValue(5)
                        ar.setAcceptedMouseButtons(Qt.NoButton)
                        self.scene.addItem(ar)
                        self._arrow_items.append(ar)
                        target += interval
                    acc += d
        # 起终点
        self._end_items = []
        if len(r.base_projected) >= 2:
            for (px, py), color in [(r.base_projected[0], r.start_color),
                                     (r.base_projected[-1], r.end_color)]:
                it = QGraphicsEllipseItem(px - 10, py - 10, 20, 20)
                it.setTransformOriginPoint(px, py)
                it.setBrush(QBrush(QColor(*color)))
                it.setPen(QPen(QColor(255, 255, 255, 220), 2))
                self.scene.addItem(it)
                self._end_items.append(it)
        # 节点
        self._node_items = []
        self._rebuild_node_items()
        # fitInView 必须先于地图瓦片初始化
        self.view.fitInView(self.scene.sceneRect().adjusted(-40, -40, 40, 40),
                            Qt.KeepAspectRatio)
        self._update_marker_scale()
        self._build_map_layer()

    def _update_marker_scale(self):
        """视图缩放时反向补偿标记（节点/起终点），保持屏幕尺寸不变。"""
        m = self.view.transform().m11()
        inv = 1.0 / m if m > 0 else 1.0
        for it in (self._node_items + getattr(self, "_end_items", [])
                   + getattr(self, "_arrow_items", [])):
            it.setScale(inv)

    # ── 地图层（复用 SegmentDialog 逻辑）──

    def _build_map_layer(self):
        r = self.renderer
        pts = r.points
        if not pts:
            return
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        lon_min, lon_max = min(lons), max(lons)
        lat_min, lat_max = min(lats), max(lats)
        glons, glats = [], []
        for lo, la in ((lon_min, lat_min), (lon_min, lat_max),
                       (lon_max, lat_min), (lon_max, lat_max)):
            glo, gla = _wgs84_to_gcj02(lo, la)
            glons.append(glo)
            glats.append(gla)
        glon0, glon1 = min(glons), max(glons)
        span_lon = max(1e-6, glon1 - glon0)
        z = int(math.log2(2048.0 * 360.0 / (256.0 * span_lon)))
        self._map_base_z = max(3, min(17, z))
        c_lon = (lon_min + lon_max) / 2
        c_lat = (lat_min + lat_max) / 2
        g_lon, g_lat = _wgs84_to_gcj02(c_lon, c_lat)
        self._map_gcj_offset = (g_lon - c_lon, g_lat - c_lat)
        self._refresh_map_tiles()

    def _refresh_map_tiles(self):
        if self._map_base_z is None or self.renderer is None:
            return
        m = self.view.transform().m11()
        if m <= 0:
            return
        z_target = self._map_base_z + max(0, int(math.floor(math.log2(max(m, 1e-6)))))
        z_target = max(self._map_base_z, min(18, z_target))
        vis_poly = self.view.mapToScene(self.view.viewport().rect())
        vis = vis_poly.boundingRect()
        inv = getattr(self.renderer._project, "inverse", None)
        if inv is None:
            return
        corners = [(vis.left(), vis.top()), (vis.right(), vis.top()),
                   (vis.left(), vis.bottom()), (vis.right(), vis.bottom())]
        wgs_pts = [inv(x, y) for x, y in corners]
        w_lons = [p[0] for p in wgs_pts]
        w_lats = [p[1] for p in wgs_pts]
        glons, glats = [], []
        for lo, la in ((min(w_lons), min(w_lats)), (min(w_lons), max(w_lats)),
                       (max(w_lons), min(w_lats)), (max(w_lons), max(w_lats))):
            glo, gla = _wgs84_to_gcj02(lo, la)
            glons.append(glo)
            glats.append(gla)
        glon0, glon1 = min(glons), max(glons)
        glat0, glat1 = min(glats), max(glats)
        px0, py_top = _lonlat_to_webmerc_px(glon0, glat1, z_target)
        px1, py_bot = _lonlat_to_webmerc_px(glon1, glat0, z_target)
        tx0, tx1 = int(px0 // 256), int(px1 // 256)
        ty0, ty1 = int(py_top // 256), int(py_bot // 256)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 64:
            return
        for key, it in list(self._map_tiles.items()):
            if key[0] != z_target:
                self.scene.removeItem(it)
                del self._map_tiles[key]
        self._map_zoom_current = z_target
        off_lon, off_lat = self._map_gcj_offset
        r = self.renderer
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                key = (z_target, tx, ty)
                if key in self._map_tiles:
                    continue
                t = _fetch_tile(z_target, tx, ty)
                if t is None:
                    continue
                pix = QPixmap()
                buf = BytesIO()
                t.save(buf, format="PNG")
                pix.loadFromData(buf.getvalue(), "PNG")
                gcj_lon_l, gcj_lat_t = _webmerc_px_to_lonlat(tx * 256, ty * 256, z_target)
                gcj_lon_r, gcj_lat_b = _webmerc_px_to_lonlat((tx + 1) * 256, (ty + 1) * 256, z_target)
                w_lon_l = gcj_lon_l - off_lon
                w_lon_r = gcj_lon_r - off_lon
                w_lat_t = gcj_lat_t - off_lat
                w_lat_b = gcj_lat_b - off_lat
                sx0, sy0 = r._project(w_lon_l, w_lat_t)
                sx1, sy1 = r._project(w_lon_r, w_lat_b)
                item = QGraphicsPixmapItem(pix)
                item.setPos(sx0, sy0)
                # 非均匀缩放（同 SegmentDialog）：纵向按投影后高度缩放，
                # 否则瓦片纵向拉高 1/cos(lat) 倍导致相邻瓦片对不齐
                tr = QTransform()
                tr.scale(max(sx1 - sx0, 1e-6) / 256.0,
                         max(sy1 - sy0, 1e-6) / 256.0)
                item.setTransform(tr)
                item.setZValue(-1)
                self.scene.addItem(item)
                self._map_tiles[key] = item

    # ── 节点编辑 ──

    def _rebuild_node_items(self):
        """重建节点图形标记。"""
        for it in self._node_items:
            self.scene.removeItem(it)
        self._node_items = []
        r = self.renderer
        for name, lon, lat in self.nodes:
            x, y = r._project(lon, lat)
            item = NodeMarkItem(name, x, y, self)
            self.scene.addItem(item)
            self._node_items.append(item)
        self._update_marker_scale()
        if hasattr(self, "lbl_count"):
            self.lbl_count.setText(f"共 {len(self.nodes)} 个节点")

    def try_add_node_at(self, scene_pos) -> bool:
        """右键：吸附最近轨迹点 → 弹窗命名 → 添加节点。"""
        r = self.renderer
        projected = r.base_projected
        if not projected:
            return False
        m = self.view.transform().m11()
        thr2 = (60.0 / m if m > 0 else 60.0) ** 2
        best, bd = -1, float("inf")
        for i, (x, y) in enumerate(projected):
            d2 = (x - scene_pos.x()) ** 2 + (y - scene_pos.y()) ** 2
            if d2 < bd:
                bd, best = d2, i
        if best < 0 or bd > thr2:
            return False
        sx, sy = projected[best]
        inv = getattr(r._project, "inverse", None)
        if inv is None:
            return False
        lon, lat = inv(sx, sy)
        default_name = f"节点{len(self.nodes) + 1}"
        name, ok = QInputDialog.getText(
            self, "新增关键节点", "节点名称：", text=default_name)
        if not ok:
            return False
        name = name.strip() or default_name
        self.nodes.append((name, lon, lat))
        self._rebuild_node_items()
        self._sync_json_from_nodes()
        return True

    def try_remove_node_at(self, scene_pos) -> bool:
        """左键：删除点击位置附近的节点。"""
        m = self.view.transform().m11()
        thr2 = (25.0 / m if m > 0 else 25.0) ** 2
        for i, it in enumerate(self._node_items):
            d2 = (it._cx - scene_pos.x()) ** 2 + (it._cy - scene_pos.y()) ** 2
            if d2 <= thr2:
                if i < len(self.nodes):
                    del self.nodes[i]
                self._rebuild_node_items()
                self._sync_json_from_nodes()
                return True
        return False

    # ── JSON 同步 ──

    def _sync_json_from_nodes(self):
        """nodes → JSON 文本框。"""
        self.txt_json.setPlainText(json.dumps(
            [{"name": a, "lon": b, "lat": c} for a, b, c in self.nodes],
            ensure_ascii=False, indent=1))

    def _apply_json_to_preview(self):
        """JSON 文本框 → nodes → 刷新预览。"""
        text = self.txt_json.toPlainText().strip()
        if not text:
            self.nodes = []
            self._rebuild_node_items()
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON 解析失败", str(e))
            return
        if not isinstance(data, list):
            QMessageBox.warning(self, "格式错误", "请输入 JSON 数组")
            return
        def _pick(d, keys):
            for k in keys:
                if k in d and d[k] is not None:
                    return d[k]
            return None
        nodes = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                QMessageBox.warning(self, "格式错误", f"第 {i+1} 项不是 JSON 对象")
                return
            lon_v = _pick(item, ["lon", "lng", "longitude", "经度"])
            lat_v = _pick(item, ["lat", "latitude", "纬度"])
            name_v = _pick(item, ["name", "名称"])
            if lon_v is None or lat_v is None:
                QMessageBox.warning(self, "格式错误",
                                    f"第 {i+1} 项缺少有效的 lon/lat")
                return
            try:
                lon, lat = float(lon_v), float(lat_v)
            except (TypeError, ValueError):
                QMessageBox.warning(self, "格式错误",
                                    f"第 {i+1} 项 lon/lat 不是数字")
                return
            name = str(name_v) if name_v is not None else f"节点{i+1}"
            nodes.append((name, lon, lat))
        self.nodes = nodes
        self._rebuild_node_items()

    def result_nodes(self):
        """确定时调用：返回编辑后的节点列表。"""
        return list(self.nodes)


# ─────────────────────── 主窗口 ───────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("运动轨迹可视化 & 视频生成")
        self.resize(1320, 780)

        self.renderer: Renderer | None = None
        self.current_kml_path: Path | None = None
        # 当前工程文件路径（None = 未保存的新工程）
        self.current_project_path: Path | None = None
        # 工程是否有未保存的改动（拖拽/改参数/改节点/改分段等置 True）
        self._project_dirty: bool = False
        # 加载工程期间置 True：抑制 _sync/_mark_dirty 把"恢复布局"误判为用户改动
        self._loading: bool = False
        self.trajectory_item: DraggablePathItem | None = None
        self.panel_item: DataPanelItem | None = None
        self.map_item: MapPanelItem | None = None
        self.gauge_item: SlopeGaugeItem | None = None
        self.elev_item: ElevProfileItem | None = None
        self.ring_item: ProgressRingItem | None = None
        self.worker: RenderWorker | None = None

        # 全局设置（仅窗口级状态：最近工程列表 / 视频输出目录 / 默认画布 / 上次工程路径）
        self.settings = load_global_settings()
        # 新建工程的默认画布尺寸
        cs = self.settings.get("default_canvas") or [1920, 1080]
        try:
            self._cw, self._ch = int(cs[0]), int(cs[1])
        except (TypeError, ValueError, IndexError):
            self._cw, self._ch = 1920, 1080
        # 上次保存视频的目录（跨工程共享；保存对话框起始位置；None = 桌面）
        self._last_save_dir = self.settings.get("last_save_dir") or None

        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, self._cw, self._ch)
        self.view = CanvasView(self.scene)
        self.setCentralWidget(self.view)

        self._build_toolbar()
        self._build_dock()
        self.statusBar().showMessage("就绪 — 请新建工程或打开已有工程")
        self._update_title()

    # ── 工具栏 ──

    def _build_toolbar(self):
        tb = self.addToolBar("主工具栏")
        tb.setMovable(False)

        # ── 工程操作组 ──
        act_new = tb.addAction("📄 新建工程")
        act_new.triggered.connect(self.on_new_project)

        act_open = tb.addAction("📂 打开工程")
        act_open.triggered.connect(self.on_open_project)

        act_save = tb.addAction("💾 保存")
        act_save.setShortcut("Ctrl+S")          # Ctrl+S 快速保存当前工程
        act_save.setToolTip("保存当前工程（Ctrl+S）")
        act_save.triggered.connect(self.on_save_project)

        act_save_as = tb.addAction("💾 另存为…")
        act_save_as.triggered.connect(self.on_save_project_as)

        # 最近工程下拉（QToolButton + QMenu）
        self.btn_recent = QToolButton()
        self.btn_recent.setText("🕐 最近 ▾")
        self.btn_recent.setPopupMode(QToolButton.InstantPopup)
        self._recent_menu = QMenu(self.btn_recent)
        self.btn_recent.setMenu(self._recent_menu)
        self.btn_recent.clicked.connect(self._rebuild_recent_menu)
        tb.addWidget(self.btn_recent)
        self._rebuild_recent_menu()

        tb.addSeparator()

        act_reset = tb.addAction("↺ 重置布局")
        act_reset.triggered.connect(self.on_reset_layout)

        tb.addSeparator()

        self.btn_render = QPushButton("🎬 生成视频")
        self.btn_render.setEnabled(False)
        self.btn_render.setStyleSheet(
            "QPushButton{background:#2d7ef7;color:#fff;padding:6px 14px;border-radius:6px;font-weight:bold;}"
            "QPushButton:hover{background:#1a6ae5;}"
            "QPushButton:disabled{background:#555;color:#888;}"
        )
        self.btn_render.clicked.connect(self.on_render)
        tb.addWidget(self.btn_render)

    def _rebuild_recent_menu(self):
        """根据全局 settings 的 recent_projects 重建最近工程下拉菜单。"""
        self._recent_menu.clear()
        recents = self.settings.get("recent_projects") or []
        if not recents:
            self._recent_menu.addAction("（无最近工程）").setEnabled(False)
            return
        for p in recents[:10]:
            fp = Path(p)
            act = self._recent_menu.addAction(fp.name)
            act.setToolTip(p)
            act.triggered.connect(lambda _=False, path=fp: self._open_recent(Path(path)))

    def _update_title(self):
        """窗口标题 = 工程名（或未命名）+ dirty 标记 *。"""
        if self.current_project_path is not None:
            name = self.current_project_path.name
        else:
            name = "未命名工程"
        dirty = " *" if self._project_dirty else ""
        self.setWindowTitle(f"{name}{dirty} — 运动轨迹可视化 & 视频生成")

    def _mark_dirty(self):
        """标记工程有未保存改动并刷新标题。加载期间不触发。"""
        if self._loading:
            return
        if not self._project_dirty:
            self._project_dirty = True
            self._update_title()

    # ── 右侧 Dock ──

    def _build_appearance_dialog(self):
        """外观与标注设置（非模态）：数据面板 / 地图导航 / 轨迹与标注 三页。

        非模态才能边调边看主画布实时预览；各页排版容器存到
        self._page_*，由 _build_dock 在创建分组时按页挂载。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("外观与标注设置")
        dlg.setModal(False)
        dlg.setMinimumSize(460, 430)

        tabs = QTabWidget()

        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setAlignment(Qt.AlignTop)       # 控件始终靠上，不随窗口高度拉伸
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setAlignment(Qt.AlignTop)
        p3 = QWidget()
        l3 = QVBoxLayout(p3)
        l3.setAlignment(Qt.AlignTop)

        tabs.addTab(p1, "数据面板")
        tabs.addTab(p2, "地图导航")
        tabs.addTab(p3, "轨迹与标注")

        v = QVBoxLayout(dlg)
        v.addWidget(tabs)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.hide)
        v.addWidget(bb)

        self._page_panel, self._page_map, self._page_track = l1, l2, l3
        self.appearance_dlg = dlg

    def _open_appearance_dialog(self):
        """打开（或前置）外观设置窗口。"""
        self.appearance_dlg.show()
        self.appearance_dlg.raise_()
        self.appearance_dlg.activateWindow()

    def _build_dock(self):
        dock = QDockWidget("视频参数", self)
        dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)

        w = QWidget()
        w.setFixedWidth(272)
        outer = QVBoxLayout(w)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(8)

        # 内容区套滚动条：屏幕矮时内容可滚动，不会被裁掉
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        # 外观设置窗口先建好各页的排版容器，下面的分组按页分别挂载
        self._build_appearance_dialog()

        # 输出规格（spin 初值从上次会话恢复，首次运行为默认值）
        sg = QGroupBox("输出规格")
        sf = QFormLayout(sg)
        self.spin_width = QSpinBox(); self.spin_width.setRange(480, 4096); self.spin_width.setValue(self._cw)
        self.spin_height = QSpinBox(); self.spin_height.setRange(270, 2304); self.spin_height.setValue(self._ch)
        self.spin_width.valueChanged.connect(lambda v: self._on_size_changed())
        self.spin_height.valueChanged.connect(lambda v: self._on_size_changed())
        sf.addRow("宽度", self.spin_width)
        sf.addRow("高度", self.spin_height)
        layout.addWidget(sg)

        # 数据项显示（勾选 = 面板中显示该行数据）
        fg = QGroupBox("数据项显示")
        fgrid = QGridLayout(fg)
        self.PANEL_FIELDS = ("当前海拔", "当前坡度", "累计爬升", "已行里程",
                             "当前配速", "运动耗时", "总耗时")
        saved_fields = (set(self.settings.get("panel_fields"))
                        if "panel_fields" in self.settings else None)
        self.panel_checks = {}
        for i, name in enumerate(self.PANEL_FIELDS):
            chk = QCheckBox(name)
            # settings 里存过 → 按存档恢复；否则默认全开
            chk.setChecked(saved_fields is None or name in saved_fields)
            chk.toggled.connect(lambda *_: self._on_panel_fields_changed())
            self.panel_checks[name] = chk
            fgrid.addWidget(chk, i // 2, i % 2)
        self._page_panel.addWidget(fg)

        # 面板背景（半透明圆角条：宽度 / 外扩边距 / 不透明度 / 颜色）
        bg = QGroupBox("面板背景")
        bf = QFormLayout(bg)
        self.spin_panel_scale = QSpinBox()
        self.spin_panel_scale.setRange(50, 600)
        self.spin_panel_scale.setSingleStep(5)
        self.spin_panel_scale.setValue(100)
        self.spin_panel_scale.setSuffix(" %")
        self.spin_panel_scale.setToolTip(
            "数据面板整体缩放（基准 100%）；也可在画布上对面板滚轮缩放")
        self.spin_panel_scale.valueChanged.connect(self._on_panel_scale_changed)
        bf.addRow("面板缩放", self.spin_panel_scale)
        self.spin_panel_bg_w = QSpinBox()
        self.spin_panel_bg_w.setRange(60, 1200)
        self.spin_panel_bg_w.setValue(320)
        self.spin_panel_bg_w.setSuffix(" px")
        self.spin_panel_bg_w.setToolTip(
            "背景条宽度（基准 px，随面板缩放等比变化）\n"
            "与字号无关，可单独收窄/拉宽：调小 = 右边缘往里收")
        self.spin_panel_bg_w.valueChanged.connect(
            lambda *_: self._on_panel_bg_changed())
        bf.addRow("背景宽度", self.spin_panel_bg_w)
        self.spin_panel_bg_pad = QSpinBox()
        self.spin_panel_bg_pad.setRange(-50, 200)
        self.spin_panel_bg_pad.setValue(0)
        self.spin_panel_bg_pad.setSuffix(" px")
        self.spin_panel_bg_pad.setToolTip(
            "背景相对数据四周的外扩边距（随面板缩放等比变化），负值 = 向内收")
        self.spin_panel_bg_pad.valueChanged.connect(
            lambda *_: self._on_panel_bg_changed())
        bf.addRow("外扩边距", self.spin_panel_bg_pad)
        self.spin_panel_bg_alpha = QSpinBox()
        self.spin_panel_bg_alpha.setRange(0, 100)
        self.spin_panel_bg_alpha.setValue(45)
        self.spin_panel_bg_alpha.setSuffix(" %")
        self.spin_panel_bg_alpha.setToolTip("背景不透明度：0 = 全透明，100 = 不透明")
        self.spin_panel_bg_alpha.valueChanged.connect(
            lambda *_: self._on_panel_bg_changed())
        bf.addRow("不透明度", self.spin_panel_bg_alpha)
        self._panel_bg_color = QColor(128, 128, 128)
        self.btn_panel_bg_color = QPushButton("背景颜色…")
        self.btn_panel_bg_color.setToolTip("面板背景条的颜色")
        self.btn_panel_bg_color.clicked.connect(self._on_pick_panel_bg_color)
        self._refresh_panel_bg_btn()
        bf.addRow("背景颜色", self.btn_panel_bg_color)
        self._page_panel.addWidget(bg)

        # 动态坡度仪（样式二选一：半圆弧仪表 / HUD 深色卡片，随坡度实时变化）
        gg = QGroupBox("动态坡度仪")
        gf = QFormLayout(gg)
        self.chk_slope_gauge = QCheckBox("显示动态坡度仪")
        self.chk_slope_gauge.setChecked(bool(self.settings.get("show_slope_gauge", True)))
        self.chk_slope_gauge.setToolTip(
            "动态坡度仪（半圆弧仪表 / HUD 卡片），独立于数据面板，可单独拖动位置")
        self.chk_slope_gauge.toggled.connect(self._on_slope_gauge_toggled)
        gf.addRow(self.chk_slope_gauge)
        self.chk_gauge_preview = QCheckBox("画布预览播放（循环）")
        self.chk_gauge_preview.setChecked(False)
        self.chk_gauge_preview.setToolTip(
            "让坡度仪在画布上循环播放整段轨迹的坡度变化，便于查看过渡效果（不影响视频输出）")
        self.chk_gauge_preview.toggled.connect(self._on_gauge_preview_toggled)
        gf.addRow(self.chk_gauge_preview)
        self.combo_gauge_style = QComboBox()
        self.combo_gauge_style.addItem("半圆弧仪表（方案B）", "arc")
        self.combo_gauge_style.addItem("HUD 深色卡片（方案D）", "card")
        self.combo_gauge_style.setToolTip(
            "坡度仪外观样式，切换后画布立即预览（半圆弧 = 正方形占位，HUD卡片 = 横宽占位）")
        self.combo_gauge_style.currentIndexChanged.connect(
            self._on_gauge_style_changed)
        gf.addRow("仪表样式", self.combo_gauge_style)
        self.spin_gauge_size = QSpinBox()
        self.spin_gauge_size.setRange(60, 600)
        self.spin_gauge_size.setValue(_num(self.settings, "slope_gauge_size", 140))
        self.spin_gauge_size.setSuffix(" px")
        self.spin_gauge_size.setToolTip("坡度仪的边长（基准 px，也可在画布上滚轮调整）")
        self.spin_gauge_size.valueChanged.connect(self._on_gauge_style_changed)
        gf.addRow("仪表大小", self.spin_gauge_size)
        self.spin_gauge_label_size = QSpinBox()
        self.spin_gauge_label_size.setRange(8, 60)
        self.spin_gauge_label_size.setValue(20)
        self.spin_gauge_label_size.setSuffix(" px")
        self.spin_gauge_label_size.setToolTip("「坡度」标签文字的字号（基准 px）")
        self.spin_gauge_label_size.valueChanged.connect(self._on_gauge_style_changed)
        gf.addRow("标签字号", self.spin_gauge_label_size)
        self.spin_gauge_value_size = QSpinBox()
        self.spin_gauge_value_size.setRange(8, 72)
        self.spin_gauge_value_size.setValue(24)
        self.spin_gauge_value_size.setSuffix(" px")
        self.spin_gauge_value_size.setToolTip("坡度百分比数值的字号（基准 px）")
        self.spin_gauge_value_size.valueChanged.connect(self._on_gauge_style_changed)
        gf.addRow("数值字号", self.spin_gauge_value_size)
        self.spin_gauge_alpha = QSpinBox()
        self.spin_gauge_alpha.setRange(0, 100)
        self.spin_gauge_alpha.setValue(85)
        self.spin_gauge_alpha.setSuffix(" %")
        self.spin_gauge_alpha.setToolTip(
            "彩色刻度弧 / 渐变指示条 / 卡片底的不透明度：0 = 全透明，100 = 不透明")
        self.spin_gauge_alpha.valueChanged.connect(self._on_gauge_style_changed)
        gf.addRow("彩色不透明度", self.spin_gauge_alpha)
        # 颜色配置：色阶冷端（下坡）/ 暖端（上坡）；数值颜色由 5 档色阶自动连续渐变
        self._gauge_colors = {
            "edge_top": QColor(*GAUGE_WARM_DEFAULT),
            "edge_bottom": QColor(*GAUGE_COLD_DEFAULT),
        }
        self.btn_gauge_color = {}
        for key, text in [("edge_top", "暖端颜色"),
                          ("edge_bottom", "冷端颜色")]:
            btn = QPushButton(f"{text}…")
            btn.clicked.connect(lambda _, k=key: self._on_pick_gauge_color(k))
            self.btn_gauge_color[key] = btn
            gf.addRow(text, btn)
        self._refresh_gauge_color_btns()
        self._page_panel.addWidget(gg)

        # 海拔一览图（整条路线海拔剖面 + 跟随视频位置移动的标记）
        eg = QGroupBox("海拔一览图")
        ef = QFormLayout(eg)
        self.chk_elev_profile = QCheckBox("显示海拔一览图")
        self.chk_elev_profile.setChecked(
            bool(self.settings.get("show_elev_profile", True)))
        self.chk_elev_profile.setToolTip(
            "整条路线的海拔剖面（山形剪影），标记随视频当前位置移动，\n"
            "可单独拖动位置、滚轮等比缩放")
        self.chk_elev_profile.toggled.connect(self._on_elev_profile_toggled)
        ef.addRow(self.chk_elev_profile)
        self.spin_elev_w = QSpinBox()
        self.spin_elev_w.setRange(220, 1400)
        self.spin_elev_w.setValue(_num(self.settings, "elev_profile_w", 360))
        self.spin_elev_w.setSuffix(" px")
        self.spin_elev_w.setToolTip("面板宽度（也可在画布上滚轮等比缩放）")
        self.spin_elev_w.valueChanged.connect(self._on_elev_profile_style_changed)
        ef.addRow("面板宽度", self.spin_elev_w)
        self.spin_elev_h = QSpinBox()
        self.spin_elev_h.setRange(90, 800)
        self.spin_elev_h.setValue(_num(self.settings, "elev_profile_h", 190))
        self.spin_elev_h.setSuffix(" px")
        self.spin_elev_h.setToolTip("面板高度（滚轮缩放时按宽高比同步变化）")
        self.spin_elev_h.valueChanged.connect(self._on_elev_profile_style_changed)
        ef.addRow("面板高度", self.spin_elev_h)
        self._page_panel.addWidget(eg)

        # 进度环（当前里程 / 全程里程；样式：经典满环 / 紧凑胶囊）
        rg = QGroupBox("进度环")
        rf = QFormLayout(rg)
        self.chk_progress_ring = QCheckBox("显示进度环")
        self.chk_progress_ring.setChecked(
            bool(self.settings.get("show_progress_ring", True)))
        self.chk_progress_ring.setToolTip(
            "当前里程 / 全程里程的环形进度仪表，\n"
            "可单独拖动位置、滚轮调整大小")
        self.chk_progress_ring.toggled.connect(self._on_progress_ring_toggled)
        rf.addRow(self.chk_progress_ring)
        self.combo_ring_style = QComboBox()
        self.combo_ring_style.addItem("经典满环", "circle")
        self.combo_ring_style.addItem("紧凑胶囊", "capsule")
        _sty = self.settings.get("progress_ring_style", "circle")
        self.combo_ring_style.setCurrentIndex(1 if _sty == "capsule" else 0)
        self.combo_ring_style.setToolTip("经典满环：圆环 + 中心里程数字；\n"
                                         "紧凑胶囊：横向小环 + 里程，占位最小")
        self.combo_ring_style.currentIndexChanged.connect(
            self._on_ring_style_changed)
        rf.addRow("样式", self.combo_ring_style)
        self.spin_ring_size = QSpinBox()
        self.spin_ring_size.setRange(60, 600)
        self.spin_ring_size.setValue(_num(self.settings, "progress_ring_size", 140))
        self.spin_ring_size.setSuffix(" px")
        self.spin_ring_size.setToolTip("进度环的基准尺寸（也可在画布上滚轮调整）")
        self.spin_ring_size.valueChanged.connect(self._on_ring_style_changed)
        rf.addRow("仪表大小", self.spin_ring_size)
        self._ring_color = QColor(*RING_PROGRESS_DEFAULT)
        self.btn_ring_color = QPushButton("进度颜色…")
        self.btn_ring_color.setToolTip("进度弧颜色（轨道固定为半透明白）")
        self.btn_ring_color.clicked.connect(self._on_pick_ring_color)
        rf.addRow("进度颜色", self.btn_ring_color)
        self._refresh_ring_color_btn()
        self._page_panel.addWidget(rg)

        # 轨迹（设置窗口「轨迹与标注」页）
        tg = QGroupBox("轨迹")
        tf = QFormLayout(tg)
        self.spin_line_width = QSpinBox()
        self.spin_line_width.setRange(2, 30)
        self.spin_line_width.setValue(_num(self.settings, "line_width", 16))
        self.spin_line_width.setSuffix(" px")
        self.spin_line_width.setToolTip("轨迹基础线宽（未走段），已走过段会粗 1px")
        self.spin_line_width.valueChanged.connect(self._on_line_width_changed)
        tf.addRow("轨迹粗细", self.spin_line_width)
        # 轨迹缩放：与画布滚轮双向联动
        self.spin_traj_scale = QSpinBox()
        self.spin_traj_scale.setRange(10, 1000)
        self.spin_traj_scale.setSingleStep(5)
        self.spin_traj_scale.setValue(100)
        self.spin_traj_scale.setSuffix(" %")
        self.spin_traj_scale.setToolTip(
            "轨迹整体缩放（基准 100%）；也可在画布上对轨迹滚轮缩放")
        self.spin_traj_scale.valueChanged.connect(self._on_traj_scale_changed)
        tf.addRow("轨迹缩放", self.spin_traj_scale)
        # 轨迹颜色：未走 / 已走 / 起点 / 终点
        self._traj_colors = {
            "unvisited": QColor(180, 100, 255),
            "visited": QColor(255, 70, 70),
            "start": QColor(100, 240, 120),
            "end": QColor(240, 80, 80),
        }
        self.btn_traj_color = {}
        for key, text in [("unvisited", "未走轨迹颜色"),
                          ("visited", "已走轨迹颜色"),
                          ("start", "起点颜色"),
                          ("end", "终点颜色")]:
            btn = QPushButton(f"{text}…")
            btn.setToolTip(f"{text}（起点/终点为带白边的圆点）")
            btn.clicked.connect(lambda _, k=key: self._on_pick_traj_color(k))
            self.btn_traj_color[key] = btn
            tf.addRow(text, btn)
        self._refresh_traj_color_btns()
        self._page_track.addWidget(tg)

        # 关键节点（设置窗口「轨迹与标注」页）
        kg = QGroupBox("关键节点")
        kv = QVBoxLayout(kg)
        nk_form = QFormLayout()
        self.spin_node_font = QSpinBox()
        self.spin_node_font.setRange(10, 60)
        self.spin_node_font.setValue(_num(self.settings, "node_font_size", 41))
        self.spin_node_font.setSuffix(" px")
        self.spin_node_font.setToolTip("关键节点名称的字号（白字 + 半透明黑底条，随轨迹缩放）")
        self.spin_node_font.valueChanged.connect(self._on_node_font_size_changed)
        nk_form.addRow("节点字号", self.spin_node_font)
        # 标注颜色（调色板）：标记圆点与名称同色
        try:
            nc = self.settings.get("node_color") or [255, 200, 60]
            self._node_color = QColor(int(nc[0]), int(nc[1]), int(nc[2]))
        except (TypeError, ValueError, IndexError):
            self._node_color = QColor(255, 200, 60)
        self.btn_node_color = QPushButton("标注颜色…")
        self.btn_node_color.setToolTip("标注圆点的颜色（文字固定为白字 + 半透明黑底条）")
        self.btn_node_color.clicked.connect(self._on_pick_node_color)
        self._refresh_node_color_btn()
        nk_form.addRow("标注颜色", self.btn_node_color)
        kv.addLayout(nk_form)
        btn_edit_nodes = QPushButton("📍 添加关键节点…")
        btn_edit_nodes.setToolTip("打开节点编辑器：右键点击轨迹添加 / JSON 编辑 / 地图背景")
        btn_edit_nodes.clicked.connect(self._open_node_editor)
        kv.addWidget(btn_edit_nodes)
        self._page_track.addWidget(kg)

        # 地图导航
        mg = QGroupBox("地图导航")
        mf = QFormLayout(mg)
        self.chk_map = QCheckBox("显示地图导航面板")
        self.chk_map.setChecked(bool(self.settings.get("show_map", False)))
        self.chk_map.toggled.connect(self._on_map_toggled)
        mf.addRow(self.chk_map)
        self.chk_map_traj = QCheckBox("在地图上显示轨迹")
        self.chk_map_traj.setChecked(
            bool(self.settings.get("map_show_trajectory", False)))
        self.chk_map_traj.setToolTip(
            "在地图导航面板上叠加轨迹折线（未走色 + 已走色）\n"
            "画布预览与视频输出同步生效")
        self.chk_map_traj.toggled.connect(self._on_map_traj_toggled)
        mf.addRow(self.chk_map_traj)
        # 轨迹前视距离：0 = 全部轨迹；>0 只显示当前位置前方该距离内的轨迹
        self.spin_map_traj_ahead = QSpinBox()
        self.spin_map_traj_ahead.setRange(0, 20000)
        self.spin_map_traj_ahead.setSingleStep(10)
        self.spin_map_traj_ahead.setSuffix(" 米")
        self.spin_map_traj_ahead.setSpecialValueText("全部")
        self.spin_map_traj_ahead.setValue(
            int(_num(self.settings, "map_traj_ahead_m", 0)))
        self.spin_map_traj_ahead.setToolTip(
            "只显示当前位置前方多远内的轨迹\n"
            "「全部」= 显示完整轨迹；填 50 = 只显示前方 50 米内的轨迹\n"
            "画布预览与视频输出同步生效")
        self.spin_map_traj_ahead.setEnabled(self.chk_map_traj.isChecked())
        self.spin_map_traj_ahead.valueChanged.connect(
            self._on_map_traj_ahead_changed)
        mf.addRow("轨迹显示距离", self.spin_map_traj_ahead)
        self.spin_map_zoom = QSpinBox()
        self.spin_map_zoom.setRange(3, 18)
        self.spin_map_zoom.setSingleStep(1)
        self.spin_map_zoom.setValue(_num(self.settings, "map_zoom", 15))
        self.spin_map_zoom.setToolTip("地图缩放级别（3=世界，18=街道）；越大越放大")
        self.spin_map_zoom.valueChanged.connect(self._on_map_zoom_changed)
        mf.addRow("缩放级别", self.spin_map_zoom)
        self.spin_map_scale = QSpinBox()
        self.spin_map_scale.setRange(30, 600)
        self.spin_map_scale.setSingleStep(5)
        self.spin_map_scale.setValue(100)
        self.spin_map_scale.setSuffix(" %")
        self.spin_map_scale.setToolTip(
            "地图面板整体缩放（基准 100%）；也可在画布上对地图面板滚轮缩放")
        self.spin_map_scale.valueChanged.connect(self._on_map_scale_changed)
        mf.addRow("面板缩放", self.spin_map_scale)
        # 地图形状：方形 / 圆形
        self.combo_map_shape = QComboBox()
        self.combo_map_shape.addItems(["方形", "圆形"])
        self.combo_map_shape.setCurrentIndex(
            1 if self.settings.get("map_shape", "rect") == "circle" else 0)
        self.combo_map_shape.currentIndexChanged.connect(self._on_map_shape_changed)
        mf.addRow("地图形状", self.combo_map_shape)
        # 底图样式：标准矢量 / 卫星影像
        self.combo_map_style = QComboBox()
        self.combo_map_style.addItem("标准地图", "standard")
        self.combo_map_style.addItem("卫星影像", "satellite")
        self.combo_map_style.setCurrentIndex(
            1 if self.settings.get("map_style", "standard") == "satellite" else 0)
        self.combo_map_style.setToolTip(
            "标准地图：浅绿矢量底图，含路名/村庄名等地名标注\n"
            "卫星影像：真实航拍地形纹理，无地名标注，更贴近实景")
        self.combo_map_style.currentIndexChanged.connect(self._on_map_style_changed)
        mf.addRow("底图样式", self.combo_map_style)
        # 地图上轨迹样式：高对比 / 经典
        self.combo_map_traj_style = QComboBox()
        self.combo_map_traj_style.addItem("高对比（白+主色）", "contrast")
        self.combo_map_traj_style.addItem("经典（同主画布配色）", "classic")
        self.combo_map_traj_style.setCurrentIndex(
            0 if self.settings.get("map_traj_style", "contrast") == "contrast" else 1)
        self.combo_map_traj_style.setToolTip(
            "高对比：未走=白色、已走=主色，行进语义最强\n"
            "经典：未走/已走沿用主画布轨迹配色（默认紫/红）")
        self.combo_map_traj_style.currentIndexChanged.connect(
            self._on_map_traj_style_changed)
        mf.addRow("轨迹样式", self.combo_map_traj_style)
        # 朝向模式：正北朝上 / 运动方向朝上
        self.combo_map_orient = QComboBox()
        self.combo_map_orient.addItems(["正北朝上", "运动方向朝上"])
        self.combo_map_orient.setCurrentIndex(
            1 if self.settings.get("map_heading_up", False) else 0)
        self.combo_map_orient.setToolTip(
            "正北朝上：地图固定不转，箭头指运动方向\n"
            "运动方向朝上：地图旋转，行进方向始终朝屏幕上方，指北针指示真北")
        self.combo_map_orient.currentIndexChanged.connect(self._on_map_orient_changed)
        mf.addRow("朝向模式", self.combo_map_orient)
        # 文字图例大小：范围不变，取不同 zoom 瓦片再缩放实现
        self._map_text_deltas = [1, 0, -1, -2]
        self.combo_map_text = QComboBox()
        self.combo_map_text.addItems(["较小（更多地名）", "标准", "较大", "特大"])
        try:
            self.combo_map_text.setCurrentIndex(
                self._map_text_deltas.index(
                    int(self.settings.get("map_text_delta", 0))))
        except ValueError:
            self.combo_map_text.setCurrentIndex(1)
        self.combo_map_text.setToolTip(
            "地图显示范围不变，只改变文字和图例大小\n（较大/特大会略微降低清晰度）")
        self.combo_map_text.currentIndexChanged.connect(self._on_map_text_changed)
        mf.addRow("文字图例", self.combo_map_text)
        # 位置箭头大小
        self.spin_map_arrow = QSpinBox()
        self.spin_map_arrow.setRange(4, 80)
        self.spin_map_arrow.setSingleStep(2)
        self.spin_map_arrow.setValue(_num(self.settings, "map_arrow_size", 14))
        self.spin_map_arrow.setSuffix(" px")
        self.spin_map_arrow.setToolTip("地图上当前位置方向箭头的大小")
        self.spin_map_arrow.valueChanged.connect(self._on_map_arrow_changed)
        mf.addRow("箭头大小", self.spin_map_arrow)
        self._page_map.addWidget(mg)

        # 时长 & 帧率（常用，留在外侧）
        pg = QGroupBox("时长 & 帧率")
        pf = QFormLayout(pg)
        self.spin_duration = QSpinBox()
        self.spin_duration.setRange(1, 3600); self.spin_duration.setValue(_num(self.settings, "duration", 20))
        self.spin_duration.setSuffix(" s")
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(1, 60); self.spin_fps.setValue(_num(self.settings, "fps", 2))
        self.spin_fps.setSuffix(" fps")
        # 输出倍率（超采样）：画布像素 × 倍率，构图比例不变但文字/线条更清晰
        self.combo_render_scale = QComboBox()
        self.combo_render_scale.addItem("1×（原始尺寸）", 1)
        self.combo_render_scale.addItem("2×（更清晰）", 2)
        self.combo_render_scale.addItem("3×（更清晰）", 3)
        self.combo_render_scale.addItem("4×（最清晰）", 4)
        _rs = int(_num(self.settings, "render_scale", 1) or 1)
        _rsi = self.combo_render_scale.findData(_rs)
        self.combo_render_scale.setCurrentIndex(_rsi if _rsi >= 0 else 0)
        self.combo_render_scale.setToolTip(
            "输出视频的像素密度倍率（超采样）\n"
            "构图与 1× 完全一致，但像素数 ×N²\n"
            "窄画布视频在播放器里被放大时不再发虚，文字/线条更清晰\n"
            "倍率越高渲染越慢、文件越大")
        self.combo_render_scale.currentIndexChanged.connect(self._update_frames_label)
        # 输出实际像素尺寸（画布 × 倍率）：让「清晰度」可见，避免忘记设倍率
        self.lbl_out_size = QLabel()
        self.lbl_out_size.setStyleSheet("color:#8fd0ff; font-weight:bold;")
        self.lbl_frames = QLabel()
        self._update_frames_label()
        self.spin_duration.valueChanged.connect(self._update_frames_label)
        self.spin_fps.valueChanged.connect(self._update_frames_label)
        # 画布尺寸变化时同步刷新「输出尺寸」
        self.spin_width.valueChanged.connect(self._update_frames_label)
        self.spin_height.valueChanged.connect(self._update_frames_label)
        # 生成方式：整段 vs 分段（分段数据始终保留，切换不丢失）
        self.rb_uniform = QRadioButton("整段")
        self.rb_segments = QRadioButton("分段")
        self._seg_mode_group = QButtonGroup(self)
        self._seg_mode_group.addButton(self.rb_uniform)
        self._seg_mode_group.addButton(self.rb_segments)
        self.rb_uniform.blockSignals(True)
        self.rb_segments.blockSignals(True)
        self.rb_uniform.setChecked(not self.settings.get("use_segments", False))
        self.rb_segments.setChecked(bool(self.settings.get("use_segments", False)))
        self.rb_uniform.blockSignals(False)
        self.rb_segments.blockSignals(False)
        self.rb_uniform.toggled.connect(self._on_seg_mode_changed)
        self.rb_segments.toggled.connect(self._on_seg_mode_changed)
        self.btn_segments = QPushButton("✂️ 分段设置…")
        self.btn_segments.clicked.connect(self._open_segment_dialog)
        self.lbl_seg_status = QLabel("未分段（整段均匀）")
        self.lbl_seg_status.setWordWrap(True)
        seg_row = QHBoxLayout()
        seg_row.addWidget(self.rb_uniform)
        seg_row.addWidget(self.rb_segments)
        seg_row.addWidget(self.btn_segments)
        seg_row.addWidget(self.lbl_seg_status, 1)
        _seg_w = QWidget(); _seg_w.setLayout(seg_row)
        pf.addRow("生成方式", _seg_w)
        pf.addRow("时长", self.spin_duration)
        pf.addRow("帧率", self.spin_fps)
        pf.addRow("输出倍率", self.combo_render_scale)
        pf.addRow("输出尺寸", self.lbl_out_size)
        pf.addRow("", self.lbl_frames)
        layout.addWidget(pg)

        # 外观与标注设置入口（数据面板 / 地图导航 / 轨迹与标注 三页，非模态）
        self.btn_appearance = QPushButton("🎨 外观与标注设置…")
        self.btn_appearance.setToolTip(
            "数据项与面板背景、地图导航、轨迹与标注等配置项\n"
            "（非模态窗口，可边调边看画布预览）")
        self.btn_appearance.clicked.connect(self._open_appearance_dialog)
        layout.addWidget(self.btn_appearance)

        layout.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # 进度 + 终止按钮（固定在底部，不随内容滚动）
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        outer.addWidget(self.progress)
        self.btn_abort = QPushButton("⛔ 终止")
        self.btn_abort.setEnabled(False)
        self.btn_abort.setStyleSheet(
            "QPushButton{background:#3a1a1a; color:#ff8080; padding:4px 10px;}"
            "QPushButton:hover{background:#5a2020;}"
            "QPushButton:disabled{color:#666; background:#222;}")
        self.btn_abort.clicked.connect(self._on_abort_render)
        outer.addWidget(self.btn_abort)

        dock.setWidget(w)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    def _update_frames_label(self, *_):
        if self.renderer is not None and self.renderer.use_segments and self.renderer.segments:
            total = int(round(sum(self.renderer.segments[1]) * self.spin_fps.value()))
            self.lbl_frames.setText(f"总帧数: {total}（分段模式）")
        else:
            total = self.spin_duration.value() * self.spin_fps.value()
            self.lbl_frames.setText(f"总帧数: {total}")
        # 输出实际像素尺寸 = 画布 × 倍率；窄画布倍率=1 时给出提示
        rs = int(self.combo_render_scale.currentData() or 1)
        ow, oh = self._cw * rs, self._ch * rs
        self.lbl_out_size.setText(f"{ow} × {oh} px（{ow * oh / 1e6:.2f} MP）")
        if rs == 1 and self._cw < 1280:
            self.lbl_out_size.setToolTip(
                "画布较窄且倍率为 1×，输出像素偏少；\n"
                "导入剪辑软件放大后容易发虚/发淡，建议把「输出倍率」调到 ≥2×")
        else:
            self.lbl_out_size.setToolTip("")

    def _on_seg_mode_changed(self):
        """生成方式切换：整段/分段；分段数据始终保留，不删除。"""
        use_seg = self.rb_segments.isChecked()
        if self.renderer is not None:
            self.renderer.use_segments = use_seg
        # 整段模式：时长可用、分段设置按钮禁用；分段模式反之
        self.spin_duration.setEnabled(not use_seg)
        self.btn_segments.setEnabled(use_seg)
        self.spin_duration.setToolTip(
            "分段模式下总时长由各段时长之和决定" if use_seg else "")
        self._apply_segments_to_ui()
        self._save_current()
        self._mark_dirty()

    def _open_segment_dialog(self):
        """打开分段设置对话框；确定后应用并立即持久化。"""
        if self.renderer is None:
            QMessageBox.information(self, "提示", "请先加载 KML 轨迹")
            return
        dlg = SegmentDialog(self.renderer, self.spin_duration.value(),
                            self.spin_fps.value(), self)
        if dlg.exec_() == QDialog.Accepted:
            # 若对话框内改了帧率，同步回主界面
            if dlg.fps_changed():
                self.spin_fps.setValue(dlg.fps)
            self.renderer.segments = dlg.result_segments()
            self._apply_segments_to_ui()
            self._save_current()   # 立即保存分段设置，防止崩溃丢失
            self._mark_dirty()

    def _apply_segments_to_ui(self):
        """按 renderer.use_segments + segments 更新主界面状态（标签/按钮可用性）。"""
        segs = self.renderer.segments if self.renderer else None
        use_seg = self.renderer.use_segments if self.renderer else False
        if segs:
            total = sum(segs[1])
            self.lbl_seg_status.setText(
                f"已分 {len(segs[1])} 段 · 共 {total:.2f} s"
                + ("" if use_seg else "（当前整段模式，分段数据已保留）"))
        else:
            self.lbl_seg_status.setText("未设置分段")
        self.spin_duration.setEnabled(not use_seg)
        self.btn_segments.setEnabled(use_seg)
        self._update_frames_label()

    def _on_line_width_changed(self):
        """轨迹粗细：同步 renderer 与 GUI 画笔（视频输出用同一数值）。"""
        if self.renderer is not None:
            self.renderer.trajectory_width = self.spin_line_width.value()
            self.trajectory_item._apply_pen()
            self.scene.update()
        self._mark_dirty()

    # ── 面板背景 ──

    def _on_preview_scaled(self, kind: str, value: float):
        """画布组件被滚轮缩放 → 回写设置面板数值（屏蔽信号，避免回环）。"""
        if kind == "elev" and self.renderer is not None:
            for name, v in (("spin_elev_w", self.renderer.elev_profile_w),
                            ("spin_elev_h", self.renderer.elev_profile_h)):
                spin = getattr(self, name, None)
                if spin is None:
                    continue
                spin.blockSignals(True)
                spin.setValue(int(round(v)))
                spin.blockSignals(False)
            return
        mapping = {"trajectory": ("spin_traj_scale", 100.0),
                   "panel": ("spin_panel_scale", 100.0),
                   "map": ("spin_map_scale", 100.0),
                   "gauge": ("spin_gauge_size", 1.0),
                   "ring": ("spin_ring_size", 1.0)}
        entry = mapping.get(kind)
        if entry is None:
            return
        spin = getattr(self, entry[0], None)
        if spin is None:
            return
        v = int(round(value * entry[1]))
        v = max(spin.minimum(), min(spin.maximum(), v))
        spin.blockSignals(True)
        spin.setValue(v)
        spin.blockSignals(False)

    def _on_traj_scale_changed(self, *_):
        """轨迹缩放设置项 → 同步画布上的轨迹 item。"""
        if self.trajectory_item is not None:
            self.trajectory_item.setScale(self.spin_traj_scale.value() / 100.0)
            self.trajectory_item._sync()
        self._mark_dirty()

    def _on_panel_scale_changed(self, *_):
        """面板缩放设置项 → 同步画布上的数据面板 item。"""
        if self.panel_item is not None:
            self.panel_item._s = self.spin_panel_scale.value() / 100.0
            self.panel_item._apply_scale()
            self.panel_item._sync()
        self._mark_dirty()

    def _on_map_scale_changed(self, *_):
        """地图面板缩放设置项 → 同步 renderer 与画布地图 item。"""
        if self.renderer is not None:
            self.renderer.map_scale = self.spin_map_scale.value() / 100.0
        if self.map_item is not None:
            self.map_item._s = self.renderer.map_scale
            size = self.renderer.map_size * self.map_item._s
            self.map_item.setRect(0, 0, size, size)
            self.map_item._sync()
            self.map_item.refresh_preview()
        self.scene.update()
        self._mark_dirty()

    def _on_panel_bg_changed(self):
        """面板背景宽度/外扩边距/不透明度：同步 renderer 并刷新预览。"""
        if self.renderer is not None:
            self.renderer.panel_bg_w = float(self.spin_panel_bg_w.value())
            self.renderer.panel_bg_pad = float(self.spin_panel_bg_pad.value())
            self.renderer.panel_bg_alpha_pct = float(
                self.spin_panel_bg_alpha.value())
            if self.panel_item is not None:
                self.panel_item.prepareGeometryChange()
                self.panel_item.update()
            self.scene.update()
        self._mark_dirty()

    def _refresh_panel_bg_btn(self):
        """颜色按钮背景 = 当前背景色（按钮即色块预览）。"""
        c = self._panel_bg_color
        txt = "#000" if (c.red() + c.green() + c.blue()) > 420 else "#fff"
        self.btn_panel_bg_color.setStyleSheet(
            f"background-color: {c.name()}; color: {txt};"
            "border: 1px solid #666; padding: 3px 8px;")

    def _on_pick_panel_bg_color(self):
        """调色板选面板背景色：同步 renderer 并刷新预览。"""
        c = QColorDialog.getColor(self._panel_bg_color, self, "选择面板背景颜色")
        if not c.isValid():
            return
        self._panel_bg_color = c
        self._refresh_panel_bg_btn()
        if self.renderer is not None:
            self.renderer.panel_bg_color = (c.red(), c.green(), c.blue())
            if self.panel_item is not None:
                self.panel_item.update()
            self.scene.update()
        self._mark_dirty()

    # ── 关键节点 ──

    def _on_node_font_size_changed(self):
        """节点字号：同步 renderer 并即时重建 GUI 节点图形。"""
        if self.renderer is not None:
            self.renderer.node_font_size = self.spin_node_font.value()
            self._rebuild_node_items()
        self._mark_dirty()

    def _refresh_node_color_btn(self):
        """颜色按钮背景 = 当前标注色（按钮即色块预览）。"""
        c = self._node_color
        # 依据亮度选黑/白文字，保证浅色背景下按钮文字可读
        txt = "#000" if (c.red() + c.green() + c.blue()) > 420 else "#fff"
        self.btn_node_color.setStyleSheet(
            f"background-color: {c.name()}; color: {txt};"
            "border: 1px solid #666; padding: 3px 8px;")

    def _on_pick_node_color(self):
        """调色板选色：同步 renderer 并重建节点（圆点+文字同色）。"""
        c = QColorDialog.getColor(self._node_color, self, "选择标注颜色")
        if not c.isValid():
            return
        self._node_color = c
        self._refresh_node_color_btn()
        if self.renderer is not None:
            self.renderer.node_color = (c.red(), c.green(), c.blue())
            self._rebuild_node_items()
        self._mark_dirty()

    def _refresh_traj_color_btns(self):
        """轨迹颜色按钮背景 = 当前色（按钮即色块预览）。"""
        for key, btn in self.btn_traj_color.items():
            c = self._traj_colors[key]
            txt = "#000" if (c.red() + c.green() + c.blue()) > 420 else "#fff"
            btn.setStyleSheet(
                f"background-color: {c.name()}; color: {txt};"
                "border: 1px solid #666; padding: 3px 8px;")

    def _on_pick_traj_color(self, key: str):
        """轨迹/起终点颜色选择：同步 renderer 并即时刷新画布预览。"""
        c = QColorDialog.getColor(self._traj_colors[key], self, "选择颜色")
        if not c.isValid():
            return
        self._traj_colors[key] = c
        self._refresh_traj_color_btns()
        if self.renderer is not None:
            rgb = (c.red(), c.green(), c.blue())
            attr = {"unvisited": "unvisited_color",
                    "visited": "visited_color",
                    "start": "start_color",
                    "end": "end_color"}[key]
            setattr(self.renderer, attr, rgb)
            # 未走轨迹颜色 → 刷新轨迹画笔；起点/终点颜色 → 刷新起终点圆点
            if key == "unvisited" and self.trajectory_item is not None:
                self.trajectory_item._apply_pen()
            elif key in ("start", "end"):
                self._refresh_start_end_dots()
            # 地图导航面板叠加了轨迹时，颜色变化也要刷新首帧预览
            if (self.map_item is not None
                    and getattr(self.renderer, "map_show_trajectory", False)):
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _refresh_start_end_dots(self):
        """按 renderer.start_color/end_color 刷新画布上的起终点圆点刷色。"""
        if self.renderer is None or self.trajectory_item is None:
            return
        dots = getattr(self, "_start_end_dots", None)
        if not dots:
            return
        for (item, _which) in dots:
            if _which == "start":
                item.setBrush(QBrush(QColor(*self.renderer.start_color)))
            else:
                item.setBrush(QBrush(QColor(*self.renderer.end_color)))

    def _checked_fields(self):
        """当前勾选的数据项列表（保持固定顺序）。"""
        return [f for f in self.PANEL_FIELDS if self.panel_checks[f].isChecked()]

    def _on_panel_fields_changed(self):
        """数据项勾选变化：同步 renderer、面板高度（rect）与画布重绘。"""
        if self.renderer is None:
            return
        self.renderer.panel_fields = self._checked_fields()
        if self.panel_item is not None:
            self.panel_item._apply_scale()   # rect 高度随行数变化
        self.scene.update()
        self._mark_dirty()

    def _on_map_toggled(self, on: bool):
        """地图导航开关：同步 renderer + 创建/移除 GUI 面板。"""
        if self.renderer is None:
            return
        self.renderer.show_map = on
        if on and self.map_item is None:
            # 按设置项中的面板缩放创建，保证数值与画布一致
            self.renderer.map_scale = self.spin_map_scale.value() / 100.0
            self.map_item = MapPanelItem(self.renderer)
            self.scene.addItem(self.map_item)
            self.map_item.refresh_preview(0)     # 立即渲出首帧预览
            _notify_main_scale(self.map_item, "map", self.map_item._s)
        elif not on and self.map_item is not None:
            self.scene.removeItem(self.map_item)
            self.map_item = None
        self.scene.update()
        self._mark_dirty()

    def _on_slope_gauge_toggled(self, on: bool):
        """动态坡度仪开关：同步 renderer + 创建/移除 GUI 仪表。"""
        if self.renderer is None:
            return
        self.renderer.show_slope_gauge = on
        if on and self.gauge_item is None:
            self.gauge_item = SlopeGaugeItem(self.renderer)
            self.scene.addItem(self.gauge_item)
        elif not on and self.gauge_item is not None:
            self.scene.removeItem(self.gauge_item)
            self.gauge_item = None
        self.scene.update()
        self._mark_dirty()

    def _on_elev_profile_toggled(self, on: bool):
        """海拔一览图开关：同步 renderer + 创建/移除 GUI 面板。"""
        if self.renderer is None:
            return
        self.renderer.show_elev_profile = on
        if on and self.elev_item is None:
            self.renderer.elev_profile_w = float(self.spin_elev_w.value())
            self.renderer.elev_profile_h = float(self.spin_elev_h.value())
            self.elev_item = ElevProfileItem(self.renderer)
            self.scene.addItem(self.elev_item)
            self.elev_item.refresh_preview()
        elif not on and self.elev_item is not None:
            self.scene.removeItem(self.elev_item)
            self.elev_item = None
        self.scene.update()
        self._mark_dirty()

    def _on_elev_profile_style_changed(self, *_):
        """海拔一览图宽/高变化 → 同步 renderer + 重建预览。"""
        if self.renderer is None:
            return
        r = self.renderer
        r.elev_profile_w = float(self.spin_elev_w.value())
        r.elev_profile_h = float(self.spin_elev_h.value())
        if self.elev_item is not None:
            self.elev_item.prepareGeometryChange()
            self.elev_item.setRect(0, 0, r.elev_profile_w, r.elev_profile_h)
            self.elev_item.refresh_preview()
        self.scene.update()
        self._mark_dirty()

    def _on_progress_ring_toggled(self, on: bool):
        """进度环开关：同步 renderer + 创建/移除 GUI 仪表。"""
        if self.renderer is None:
            return
        self.renderer.show_progress_ring = on
        if on and self.ring_item is None:
            self.ring_item = ProgressRingItem(self.renderer)
            self.scene.addItem(self.ring_item)
            self.ring_item.refresh_preview()
        elif not on and self.ring_item is not None:
            self.scene.removeItem(self.ring_item)
            self.ring_item = None
        self.scene.update()
        self._mark_dirty()

    def _on_ring_style_changed(self, *_):
        """进度环样式/大小/颜色变化 → 同步 renderer + 重建 GUI 预览。"""
        if self.renderer is None:
            return
        r = self.renderer
        r.progress_ring_style = self.combo_ring_style.currentData() or "circle"
        r.progress_ring_size = float(self.spin_ring_size.value())
        c = self._ring_color
        r.ring_progress_color = (c.red(), c.green(), c.blue())
        if self.ring_item is not None:
            self.ring_item._relayout()
        self.scene.update()
        self._mark_dirty()

    def _on_pick_ring_color(self):
        """进度环进度颜色按钮：调色板选择 → 写回 renderer。"""
        c = QColorDialog.getColor(self._ring_color, self, "选择进度颜色")
        if c.isValid():
            self._ring_color = c
            self._refresh_ring_color_btn()
            self._on_ring_style_changed()

    def _refresh_ring_color_btn(self):
        """进度颜色按钮背景 = 当前色（按钮即色块预览）。"""
        c = self._ring_color
        txt = "#000" if (c.red() + c.green() + c.blue()) > 420 else "#fff"
        self.btn_ring_color.setStyleSheet(
            f"background-color: {c.name()}; color: {txt};"
            "border: 1px solid #666; padding: 3px 8px;")

    def _on_gauge_style_changed(self, *_):
        """坡度仪样式/大小/字号/透明度变化 → 同步 renderer + 刷新 GUI。"""
        if self.renderer is None:
            return
        r = self.renderer
        r.gauge_style = self.combo_gauge_style.currentData() or "arc"
        r.slope_gauge_size = float(self.spin_gauge_size.value())
        r.gauge_label_size = float(self.spin_gauge_label_size.value())
        r.gauge_value_size = float(self.spin_gauge_value_size.value())
        r.gauge_fill_alpha_pct = float(self.spin_gauge_alpha.value())
        mapping = {"edge_top": "gauge_edge_top_color",
                   "edge_bottom": "gauge_edge_bottom_color"}
        for key, attr in mapping.items():
            c = self._gauge_colors[key]
            setattr(r, attr, (c.red(), c.green(), c.blue()))
        if self.gauge_item is not None:
            fw, fh = gauge_footprint(r.gauge_style, r.slope_gauge_size)
            self.gauge_item.prepareGeometryChange()
            self.gauge_item.setRect(0, 0, fw, fh)
            self.gauge_item.update()
        self.scene.update()
        self._mark_dirty()

    def _on_gauge_preview_toggled(self, on: bool):
        """画布预览播放开关：定时循环推进轨迹索引，让坡度仪过渡效果可见。"""
        if not hasattr(self, "_preview_timer"):
            self._preview_timer = QTimer(self)
            self._preview_timer.setInterval(60)
            self._preview_timer.timeout.connect(self._on_preview_tick)
        if on and self.renderer is not None:
            self._preview_pos = 0.0
            self._preview_timer.start()
        else:
            self._preview_timer.stop()
            if self.renderer is not None:
                self.renderer.preview_idx = -1.0
            if self.gauge_item is not None:
                self.gauge_item.update()
            if self.elev_item is not None:
                self.elev_item.refresh_preview()
            if self.ring_item is not None:
                self.ring_item.refresh_preview()
            self.scene.update()

    def _on_preview_tick(self):
        """预览推进一帧：索引循环前进（约 5s 走完整段），刷新坡度仪 / 进度环 / 海拔标记。"""
        if self.renderer is None:
            return
        n = len(self.renderer.points)
        if n <= 0:
            self._preview_timer.stop()
            return
        self._preview_pos = (getattr(self, "_preview_pos", 0.0)
                             + max(0.25, n / 83.0)) % n
        self.renderer.preview_idx = self._preview_pos
        if self.gauge_item is not None:
            self.gauge_item.update()
        if self.elev_item is not None:
            self.elev_item.refresh_preview()
        if self.ring_item is not None:
            self.ring_item.refresh_preview()

    def _on_pick_gauge_color(self, key: str):
        """坡度仪颜色按钮：调色板选择 → 写回 renderer。"""
        c = QColorDialog.getColor(self._gauge_colors[key], self, "选择颜色")
        if c.isValid():
            self._gauge_colors[key] = c
            self._refresh_gauge_color_btns()
            self._on_gauge_style_changed()

    def _refresh_gauge_color_btns(self):
        """颜色按钮背景 = 当前色（按钮即色块预览）。"""
        for key, btn in self.btn_gauge_color.items():
            c = self._gauge_colors[key]
            txt = "#000" if (c.red() + c.green() + c.blue()) > 420 else "#fff"
            btn.setStyleSheet(
                f"background-color: {c.name()}; color: {txt};"
                "border: 1px solid #666; padding: 3px 8px;")

    def _on_map_zoom_changed(self):
        """地图缩放级别变化 → 同步 renderer + 刷新首帧预览。"""
        if self.renderer is not None:
            self.renderer.map_zoom = self.spin_map_zoom.value()
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _on_map_shape_changed(self):
        """地图形状（方形/圆形）变化 → 同步 renderer + 刷新预览。"""
        if self.renderer is not None:
            self.renderer.map_shape = ("circle"
                                       if self.combo_map_shape.currentIndex() == 1
                                       else "rect")
            if self.map_item is not None:
                self.map_item.refresh_preview()
            self.scene.update()
        self._mark_dirty()

    def _on_map_style_changed(self):
        """底图样式（标准/卫星）变化 → 同步 renderer + 刷新首帧预览。"""
        if self.renderer is not None:
            self.renderer.map_style = (self.combo_map_style.currentData()
                                       or "standard")
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _on_map_traj_style_changed(self):
        """地图上轨迹样式（高对比/经典）变化 → 同步 renderer + 刷新预览。"""
        if self.renderer is not None:
            self.renderer.map_traj_style = (self.combo_map_traj_style.currentData()
                                            or "contrast")
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _on_map_text_changed(self):
        """文字图例大小变化 → 同步 renderer + 刷新首帧预览。"""
        if self.renderer is not None:
            self.renderer.map_text_delta = self._map_text_deltas[
                self.combo_map_text.currentIndex()]
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _on_map_orient_changed(self):
        """朝向模式（正北朝上/运动方向朝上）变化 → 同步 renderer + 刷新预览。"""
        if self.renderer is not None:
            self.renderer.map_heading_up = (self.combo_map_orient.currentIndex() == 1)
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _on_map_arrow_changed(self):
        """位置箭头大小变化 → 同步 renderer + 刷新首帧预览。"""
        if self.renderer is not None:
            self.renderer.map_arrow_size = self.spin_map_arrow.value()
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _on_map_traj_toggled(self, on: bool):
        """地图上是否叠加轨迹 → 同步 renderer + 刷新首帧预览。"""
        if self.renderer is not None:
            self.renderer.map_show_trajectory = on
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self.spin_map_traj_ahead.setEnabled(on)
        self._mark_dirty()

    def _on_map_traj_ahead_changed(self):
        """轨迹前视距离（米）→ 同步 renderer；0 表示显示完整轨迹。"""
        if self.renderer is not None:
            self.renderer.map_traj_ahead_m = float(
                self.spin_map_traj_ahead.value())
            if self.map_item is not None:
                self.map_item.refresh_preview()
        self._mark_dirty()

    def _open_node_editor(self):
        """打开关键节点编辑器对话框。"""
        if self.renderer is None:
            QMessageBox.warning(self, "提示", "请先加载 KML 文件")
            return
        dlg = NodeEditorDialog(self.renderer, self)
        if dlg.exec_() == QDialog.Accepted:
            self.renderer.key_nodes = dlg.result_nodes()
            self._rebuild_node_items()
            self.statusBar().showMessage(
                f"已更新关键节点（共 {len(self.renderer.key_nodes)} 个）", 5000)
            self._mark_dirty()

    def _rebuild_node_items(self):
        """重建关键节点图形：彩色标记 + 名称标签，作为轨迹子项跟随拖拽缩放。"""
        for it in getattr(self, "_node_items", []):
            self.scene.removeItem(it)
        self._node_items = []
        if self.renderer is None or self.trajectory_item is None:
            return
        node_c = QColor(*self.renderer.node_color)
        f = QFont("Microsoft YaHei")
        f.setPixelSize(self.renderer.node_font_size)   # 常规字重（与视频端 msyh 一致，WYSIWYG）
        fm = QFontMetrics(f)
        for name, lon, lat in self.renderer.key_nodes:
            x, y = self.renderer._project(lon, lat)
            dot = QGraphicsEllipseItem(x - 8, y - 8, 16, 16, self.trajectory_item)
            dot.setBrush(QBrush(node_c))
            dot.setPen(QPen(QColor(255, 255, 255, 220), 2))
            dot.setAcceptedMouseButtons(Qt.NoButton)
            dot.setFlag(QGraphicsItem.ItemIsSelectable, False)
            # 标签：半透明黑底条(胶囊) + 白字（与视频端一致，任何背景高对比）
            adv = fm.horizontalAdvance(name)
            bar = QRectF(x + 12 - 6, y - 22 - 4, adv + 12, fm.height() + 8)
            bp = QPainterPath()
            bp.addRoundedRect(bar, bar.height() / 2, bar.height() / 2)
            bg = QGraphicsPathItem(bp, self.trajectory_item)
            bg.setPen(QPen(Qt.NoPen))
            bg.setBrush(QBrush(QColor(0, 0, 0, 115)))
            bg.setAcceptedMouseButtons(Qt.NoButton)
            bg.setFlag(QGraphicsItem.ItemIsSelectable, False)
            # 白字（路径填充，无描边）
            tp = QPainterPath()
            tp.addText(0, 0, f, name)
            label = QGraphicsPathItem(tp, self.trajectory_item)
            label.setPen(QPen(Qt.NoPen))
            label.setBrush(QBrush(QColor(255, 255, 255, 255)))
            # addText 以基线为 y=0；对齐视频端左上角锚点 (x+12, y-22)
            label.setPos(x + 12, y - 22 + fm.ascent())
            label.setAcceptedMouseButtons(Qt.NoButton)
            label.setFlag(QGraphicsItem.ItemIsSelectable, False)
            self._node_items.extend([dot, bg, label])
        self.scene.update()

    def _on_size_changed(self):
        self._cw = self.spin_width.value()
        self._ch = self.spin_height.value()
        if self.renderer is not None:
            # 保存用户编辑状态
            saved_pos = self.trajectory_item.pos()
            saved_scale = self.trajectory_item.scale()
            saved_panel_pos = self.panel_item.pos()
            saved_panel_scale = self.panel_item._s

            self.renderer.set_size(self._cw, self._ch)
            self.scene.setSceneRect(0, 0, self._cw, self._ch)

            # 重建轨迹 path（用 base_projected 原始坐标，Qt 变换由 setPos/setScale 处理）
            path_item = QPainterPath()
            projected = self.renderer.base_projected
            for i, (x, y) in enumerate(projected):
                if i == 0:
                    path_item.moveTo(x, y)
                else:
                    path_item.lineTo(x, y)
            self.trajectory_item.setPath(path_item)
            # 新的 anchor = 新 path 包围盒中心
            self.trajectory_item._anchor = path_item.boundingRect().center()
            self.trajectory_item.setTransformOriginPoint(self.trajectory_item._anchor)
            # 恢复用户编辑状态
            self.trajectory_item.setPos(saved_pos)
            self.trajectory_item.setScale(saved_scale)
            self.trajectory_item._sync()

            # 重建起终点圆点
            for child in self.trajectory_item.childItems():
                self.scene.removeItem(child)
            sx, sy = projected[0]
            ex, ey = projected[-1]
            self._start_end_dots = []
            for x, y, which in [(sx, sy, "start"), (ex, ey, "end")]:
                rgb = (self.renderer.start_color if which == "start"
                       else self.renderer.end_color)
                dot = QGraphicsEllipseItem(x - 14, y - 14, 28, 28, self.trajectory_item)
                dot.setBrush(QBrush(QColor(*rgb)))
                dot.setPen(QPen(QColor(255, 255, 255, 200), 2))
                dot.setAcceptedMouseButtons(Qt.NoButton)
                dot.setFlag(QGraphicsItem.ItemIsSelectable, False)
                self._start_end_dots.append((dot, which))

            # 重建关键节点（投影已随尺寸变化）
            self._node_items = []   # 旧节点子项已随上面 childItems 循环移除
            self._rebuild_node_items()

            # 恢复面板状态
            self.panel_item.setPos(saved_panel_pos)
            self.panel_item._s = saved_panel_scale
            self.panel_item._apply_scale()
            self.panel_item._sync()

            self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
            self.scene.update()
        self._mark_dirty()   # 画布尺寸变化 = 工程变脏（即便无 KML 也存 canvas）

    # ── 工程加载 ──

    def on_new_project(self):
        """新建工程：选 KML → 全新加载（restore=False）→ 标记未保存。"""
        if not self._maybe_save_dirty():
            return   # 用户取消
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 KML 文件", str(Path.home()), "KML (*.kml)")
        if not path:
            return
        path = path.strip()
        if not path:
            return
        try:
            self._load_kml(Path(path))
        except Exception as e:
            QMessageBox.critical(self, "错误", f"解析 KML 失败:\n{e}")
            return
        self.current_project_path = None
        self._project_dirty = True
        self._update_title()
        self.statusBar().showMessage(
            "新工程已创建（未保存）— 建议点「保存」存为 .kmlproj", 5000)

    def on_open_project(self):
        """打开已有工程文件。"""
        if not self._maybe_save_dirty():
            return
        start = (str(self.current_project_path.parent)
                 if self.current_project_path else str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(
            self, "打开工程", start, "KML 工程 (*.kmlproj)")
        if not path:
            return
        self._load_project_file(Path(path))

    def _open_recent(self, path: Path):
        """从最近列表打开工程。"""
        if not self._maybe_save_dirty():
            return
        self._load_project_file(path)

    def _load_project_file(self, path: Path):
        """加载 .kmlproj 工程文件（含 KML 路径 + 全部渲染设置）。"""
        try:
            data = load_project(path)
        except ValueError as e:
            QMessageBox.critical(self, "打开工程失败", str(e))
            return
        kml_path = data.get("kml_path")
        if not kml_path or not Path(kml_path).exists():
            # KML 路径失效 → 提示重新指定
            QMessageBox.warning(
                self, "KML 文件未找到",
                f"工程引用的 KML 文件不存在：\n{kml_path}\n\n请重新指定 KML 文件。")
            new_kml, _ = QFileDialog.getOpenFileName(
                self, "重新指定 KML 文件", str(Path.home()), "KML (*.kml)")
            if not new_kml:
                return   # 用户放弃
            data["kml_path"] = new_kml.strip()
            kml_path = data["kml_path"]
        # 同步 GUI 控件初值 + 恢复布局（_loading 抑制 _sync/_mark_dirty 误触）
        self._loading = True
        try:
            self._apply_state_to_gui(data)
            try:
                self._load_kml(Path(kml_path))
            except Exception as e:
                QMessageBox.critical(self, "错误", f"解析 KML 失败:\n{e}")
                return
            # 恢复轨迹/面板/地图布局 + 关键节点 + 分段
            self._apply_state_dict(data)
        finally:
            self._loading = False
        self.current_project_path = path
        self._project_dirty = False
        self._update_title()
        self._add_recent_project(path)
        self.statusBar().showMessage(f"已打开工程：{path.name}", 5000)

    def _apply_state_to_gui(self, s: dict):
        """从工程数据 dict 同步 GUI 控件初值（屏蔽信号，避免连锁）。"""
        # 画布尺寸
        cs = s.get("canvas")
        if cs and len(cs) >= 2:
            self.spin_width.blockSignals(True)
            self.spin_height.blockSignals(True)
            try:
                self.spin_width.setValue(int(cs[0]))
                self.spin_height.setValue(int(cs[1]))
                self._cw, self._ch = int(cs[0]), int(cs[1])
            except (TypeError, ValueError):
                pass
            self.spin_width.blockSignals(False)
            self.spin_height.blockSignals(False)
        # 轨迹粗细 / 节点字号
        for spin, key, default in [(self.spin_line_width, "line_width", 16),
                                   (self.spin_node_font, "node_font_size", 41)]:
            spin.blockSignals(True)
            spin.setValue(_num(s, key, default))
            spin.blockSignals(False)
        # 标注颜色
        nc = s.get("node_color")
        if nc and len(nc) >= 3:
            try:
                self._node_color = QColor(int(nc[0]), int(nc[1]), int(nc[2]))
                self._refresh_node_color_btn()
            except (TypeError, ValueError, IndexError):
                pass
        # 轨迹与起终点颜色（未走 / 已走 / 起点 / 终点）
        for key, default in [("unvisited", (180, 100, 255)),
                             ("visited", (255, 70, 70)),
                             ("start", (100, 240, 120)),
                             ("end", (240, 80, 80))]:
            c = s.get(f"{key}_color")
            if c and len(c) >= 3:
                try:
                    self._traj_colors[key] = QColor(int(c[0]), int(c[1]), int(c[2]))
                except (TypeError, ValueError, IndexError):
                    self._traj_colors[key] = QColor(*default)
            else:
                self._traj_colors[key] = QColor(*default)
        self._refresh_traj_color_btns()
        # 时长 / 帧率
        self.spin_duration.blockSignals(True)
        self.spin_fps.blockSignals(True)
        self.spin_duration.setValue(_num(s, "duration", 20))
        self.spin_fps.setValue(_num(s, "fps", 2))
        self.spin_duration.blockSignals(False)
        self.spin_fps.blockSignals(False)
        # 输出倍率（超采样）
        _rsi = self.combo_render_scale.findData(int(_num(s, "render_scale", 1) or 1))
        if _rsi >= 0:
            self.combo_render_scale.setCurrentIndex(_rsi)
        # 数据项勾选
        saved_fields = s.get("panel_fields")
        if saved_fields is not None:
            saved_set = set(saved_fields)
            for name, chk in self.panel_checks.items():
                chk.blockSignals(True)
                chk.setChecked(name in saved_set)
                chk.blockSignals(False)
        # 面板背景（宽度 / 外扩边距 / 不透明度 / 颜色）
        self.spin_panel_bg_w.blockSignals(True)
        self.spin_panel_bg_w.setValue(int(_num(s, "panel_bg_w", 320)))
        self.spin_panel_bg_w.blockSignals(False)
        self.spin_panel_bg_pad.blockSignals(True)
        self.spin_panel_bg_pad.setValue(int(_num(s, "panel_bg_pad", 0)))
        self.spin_panel_bg_pad.blockSignals(False)
        self.spin_panel_bg_alpha.blockSignals(True)
        self.spin_panel_bg_alpha.setValue(
            int(_num(s, "panel_bg_alpha_pct", 45)))
        self.spin_panel_bg_alpha.blockSignals(False)
        bc = s.get("panel_bg_color")
        if bc and len(bc) >= 3:
            try:
                self._panel_bg_color = QColor(int(bc[0]), int(bc[1]),
                                              int(bc[2]))
                self._refresh_panel_bg_btn()
            except (TypeError, ValueError, IndexError):
                pass
        if self.renderer is not None:
            self.renderer.panel_bg_w = float(_num(s, "panel_bg_w", 320))
            self.renderer.panel_bg_pad = float(_num(s, "panel_bg_pad", 0))
            self.renderer.panel_bg_alpha_pct = float(
                _num(s, "panel_bg_alpha_pct", 45))
            if bc and len(bc) >= 3:
                self.renderer.panel_bg_color = (
                    self._panel_bg_color.red(), self._panel_bg_color.green(),
                    self._panel_bg_color.blue())
        # 动态坡度仪控件
        self.chk_slope_gauge.blockSignals(True)
        self.chk_slope_gauge.setChecked(bool(s.get("show_slope_gauge", True)))
        self.chk_slope_gauge.blockSignals(False)
        self.spin_gauge_size.blockSignals(True)
        self.spin_gauge_size.setValue(_num(s, "slope_gauge_size", 140))
        self.spin_gauge_size.blockSignals(False)
        self.spin_gauge_label_size.blockSignals(True)
        self.spin_gauge_label_size.setValue(_num(s, "gauge_label_size", 20))
        self.spin_gauge_label_size.blockSignals(False)
        self.spin_gauge_value_size.blockSignals(True)
        self.spin_gauge_value_size.setValue(_num(s, "gauge_value_size", 24))
        self.spin_gauge_value_size.blockSignals(False)
        self.spin_gauge_alpha.blockSignals(True)
        self.spin_gauge_alpha.setValue(_num(s, "gauge_fill_alpha_pct", 85))
        self.spin_gauge_alpha.blockSignals(False)
        # 仪表样式（arc 半圆弧 / card HUD卡片；旧工程无此键 → 默认半圆弧）
        self.combo_gauge_style.blockSignals(True)
        _gsi = self.combo_gauge_style.findData(str(s.get("gauge_style", "arc")))
        self.combo_gauge_style.setCurrentIndex(_gsi if _gsi >= 0 else 0)
        self.combo_gauge_style.blockSignals(False)
        # 色阶两端色（暖端/冷端）
        for _gk in ("edge_top", "edge_bottom"):
            _gv = s.get(f"gauge_{_gk}_color")
            if _gv and len(_gv) >= 3:
                try:
                    self._gauge_colors[_gk] = QColor(int(_gv[0]), int(_gv[1]),
                                                     int(_gv[2]))
                except (TypeError, ValueError):
                    pass
        # 旧工程色阶两端色（橙/白）自动迁移为新的暖端/冷端默认色
        if (self._gauge_colors["edge_top"] == QColor(255, 170, 40)
                and self._gauge_colors["edge_bottom"] == QColor(255, 255, 255)):
            self._gauge_colors["edge_top"] = QColor(*GAUGE_WARM_DEFAULT)
            self._gauge_colors["edge_bottom"] = QColor(*GAUGE_COLD_DEFAULT)
        self._refresh_gauge_color_btns()
        if self.renderer is not None:
            self.renderer.gauge_fill_alpha_pct = float(self.spin_gauge_alpha.value())
        # 海拔一览图控件
        self.chk_elev_profile.blockSignals(True)
        self.chk_elev_profile.setChecked(
            bool(s.get("show_elev_profile", True)))
        self.chk_elev_profile.blockSignals(False)
        self.spin_elev_w.blockSignals(True)
        self.spin_elev_w.setValue(int(_num(s, "elev_profile_w", 360)))
        self.spin_elev_w.blockSignals(False)
        self.spin_elev_h.blockSignals(True)
        self.spin_elev_h.setValue(int(_num(s, "elev_profile_h", 190)))
        self.spin_elev_h.blockSignals(False)
        if self.renderer is not None:
            self.renderer.show_elev_profile = self.chk_elev_profile.isChecked()
            self.renderer.elev_profile_w = float(self.spin_elev_w.value())
            self.renderer.elev_profile_h = float(self.spin_elev_h.value())
        # 进度环控件
        self.chk_progress_ring.blockSignals(True)
        self.chk_progress_ring.setChecked(
            bool(s.get("show_progress_ring", True)))
        self.chk_progress_ring.blockSignals(False)
        self.combo_ring_style.blockSignals(True)
        self.combo_ring_style.setCurrentIndex(
            1 if s.get("progress_ring_style", "circle") == "capsule" else 0)
        self.combo_ring_style.blockSignals(False)
        self.spin_ring_size.blockSignals(True)
        self.spin_ring_size.setValue(_num(s, "progress_ring_size", 140))
        self.spin_ring_size.blockSignals(False)
        _rc = s.get("ring_progress_color")
        if _rc and len(_rc) >= 3:
            try:
                self._ring_color = QColor(int(_rc[0]), int(_rc[1]), int(_rc[2]))
            except (TypeError, ValueError, IndexError):
                self._ring_color = QColor(*RING_PROGRESS_DEFAULT)
        else:
            self._ring_color = QColor(*RING_PROGRESS_DEFAULT)
        self._refresh_ring_color_btn()
        if self.renderer is not None:
            self.renderer.show_progress_ring = self.chk_progress_ring.isChecked()
            self.renderer.progress_ring_style = \
                self.combo_ring_style.currentData() or "circle"
            self.renderer.progress_ring_size = float(self.spin_ring_size.value())
            self.renderer.ring_progress_color = (
                self._ring_color.red(), self._ring_color.green(),
                self._ring_color.blue())
        # 关键节点（无文本框，仅写回 renderer）
        nodes = s.get("key_nodes")
        if nodes is not None and self.renderer is not None:
            try:
                self.renderer.key_nodes = [
                    (str(a), float(b), float(c)) for a, b, c in nodes]
            except (TypeError, ValueError):
                self.renderer.key_nodes = []
        # 地图导航控件
        self.chk_map.blockSignals(True)
        self.chk_map.setChecked(bool(s.get("show_map", False)))
        self.chk_map.blockSignals(False)
        self.spin_map_zoom.blockSignals(True)
        self.spin_map_zoom.setValue(_num(s, "map_zoom", 15))
        self.spin_map_zoom.blockSignals(False)
        self.combo_map_shape.blockSignals(True)
        self.combo_map_shape.setCurrentIndex(
            1 if s.get("map_shape", "rect") == "circle" else 0)
        self.combo_map_shape.blockSignals(False)
        self.combo_map_style.blockSignals(True)
        self.combo_map_style.setCurrentIndex(
            1 if s.get("map_style", "standard") == "satellite" else 0)
        self.combo_map_style.blockSignals(False)
        self.combo_map_traj_style.blockSignals(True)
        self.combo_map_traj_style.setCurrentIndex(
            0 if s.get("map_traj_style", "contrast") == "contrast" else 1)
        self.combo_map_traj_style.blockSignals(False)
        self.combo_map_orient.blockSignals(True)
        self.combo_map_orient.setCurrentIndex(
            1 if s.get("map_heading_up", False) else 0)
        self.combo_map_orient.blockSignals(False)
        self.combo_map_text.blockSignals(True)
        try:
            self.combo_map_text.setCurrentIndex(
                self._map_text_deltas.index(int(s.get("map_text_delta", 0))))
        except ValueError:
            self.combo_map_text.setCurrentIndex(1)
        self.combo_map_text.blockSignals(False)
        self.spin_map_arrow.blockSignals(True)
        self.spin_map_arrow.setValue(_num(s, "map_arrow_size", 14))
        self.spin_map_arrow.blockSignals(False)
        self.chk_map_traj.blockSignals(True)
        self.chk_map_traj.setChecked(bool(s.get("map_show_trajectory", False)))
        self.chk_map_traj.blockSignals(False)
        self.spin_map_traj_ahead.blockSignals(True)
        self.spin_map_traj_ahead.setValue(
            int(_num(s, "map_traj_ahead_m", 0)))
        self.spin_map_traj_ahead.blockSignals(False)
        self.spin_map_traj_ahead.setEnabled(self.chk_map_traj.isChecked())
        # 生成方式（整段/分段）
        use_seg = bool(s.get("use_segments", False))
        self.rb_uniform.blockSignals(True)
        self.rb_segments.blockSignals(True)
        self.rb_uniform.setChecked(not use_seg)
        self.rb_segments.setChecked(use_seg)
        self.rb_uniform.blockSignals(False)
        self.rb_segments.blockSignals(False)

    def _apply_state_dict(self, s: dict):
        """从状态 dict 恢复轨迹/面板/地图位置 + 关键节点 + 分段（_load_kml 后调用）。"""
        if self.renderer is None:
            return
        te = s.get("trajectory")
        if te and self.trajectory_item is not None:
            off = te.get("offset") or [0, 0]
            self.trajectory_item.setPos(float(off[0]), float(off[1]))
            self.trajectory_item.setScale(float(te.get("scale", 1.0)))
            self.trajectory_item._sync()
        pn = s.get("panel")
        if pn and self.panel_item is not None:
            pos = pn.get("pos") or [30, 30]
            self.panel_item.setPos(float(pos[0]), float(pos[1]))
            self.panel_item._s = float(pn.get("scale", 1.0))
            self.panel_item._apply_scale()
            self.panel_item._sync()
        mp = s.get("map")
        if mp and self.map_item is not None:
            pos = mp.get("pos") or [0, 0]
            self.map_item.setPos(float(pos[0]), float(pos[1]))
            self.map_item._s = float(mp.get("scale", 1.0))
            size = self.renderer.map_size * self.map_item._s
            self.map_item.setRect(0, 0, size, size)
            self.map_item._sync()
            self.map_item.refresh_preview(0)     # 恢复工程布局后重渲首帧预览
        # 动态坡度仪位置/大小恢复
        gp = s.get("slope_gauge_pos")
        if gp:
            try:
                self.renderer.slope_gauge_pos = (float(gp[0]), float(gp[1]))
            except (TypeError, ValueError, IndexError):
                pass
        if self.gauge_item is not None:
            gfw, gfh = gauge_footprint(self.renderer.gauge_style,
                                       self.renderer.slope_gauge_size)
            self.gauge_item.prepareGeometryChange()
            self.gauge_item.setRect(0, 0, gfw, gfh)
            self.gauge_item.setPos(*self.renderer.slope_gauge_pos)
            self.gauge_item._sync()
        # 海拔一览图位置/大小恢复
        ep = s.get("elev_profile_pos")
        if ep:
            try:
                self.renderer.elev_profile_pos = (float(ep[0]), float(ep[1]))
            except (TypeError, ValueError, IndexError):
                pass
        if self.elev_item is not None:
            self.elev_item.sync_layout()
        # 进度环位置/大小恢复
        rp = s.get("progress_ring_pos")
        if rp:
            try:
                self.renderer.progress_ring_pos = (float(rp[0]), float(rp[1]))
            except (TypeError, ValueError, IndexError):
                pass
        if self.ring_item is not None:
            self.ring_item.sync_layout()
        nodes = s.get("key_nodes") or []
        if nodes:
            try:
                self.renderer.key_nodes = [(str(a), float(b), float(c))
                                           for a, b, c in nodes]
                self._rebuild_node_items()
            except (TypeError, ValueError):
                self.renderer.key_nodes = []
        # 分段恢复（兼容新 dict 格式与旧 list 格式）
        segs = s.get("segments")
        cuts = durs = None
        if isinstance(segs, dict):
            cuts = segs.get("cuts")
            durs = segs.get("durations")
        elif isinstance(segs, list) and len(segs) == 2:
            cuts, durs = segs[0], segs[1]
        if cuts and durs:
            try:
                self.renderer.segments = (
                    [float(c) for c in cuts], [int(d) for d in durs])
            except (TypeError, ValueError):
                self.renderer.segments = None
        if s.get("use_segments") and self.renderer.segments:
            self.renderer.use_segments = True
            self.rb_uniform.blockSignals(True)
            self.rb_segments.blockSignals(True)
            self.rb_segments.setChecked(True)
            self.rb_uniform.blockSignals(False)
            self.rb_segments.blockSignals(False)
        else:
            self.renderer.use_segments = False
            self.rb_uniform.blockSignals(True)
            self.rb_segments.blockSignals(True)
            self.rb_uniform.setChecked(True)
            self.rb_uniform.blockSignals(False)
            self.rb_segments.blockSignals(False)
        self._apply_segments_to_ui()
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def on_save_project(self):
        """保存工程：已保存过则覆盖，否则弹另存为。"""
        if self.current_project_path is None:
            self.on_save_project_as()
            return
        self._save_project_to(self.current_project_path)

    def on_save_project_as(self):
        """另存为：选位置存 .kmlproj。"""
        if self.renderer is None:
            QMessageBox.information(self, "提示", "请先加载 KML 轨迹")
            return
        # 默认目录 = 当前工程所在目录，否则上次视频输出目录，否则桌面
        if self.current_project_path is not None:
            start_dir = str(self.current_project_path.parent)
        elif self._last_save_dir and Path(self._last_save_dir).is_dir():
            start_dir = self._last_save_dir
        else:
            start_dir = (QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
                         or str(Path.home()))
        # 默认名 = KML stem + .kmlproj
        kml_name = (self.current_kml_path.stem
                    if self.current_kml_path else "未命名")
        default = Path(start_dir) / f"{kml_name}.kmlproj"
        out, _ = QFileDialog.getSaveFileName(
            self, "保存工程", str(default), "KML 工程 (*.kmlproj)")
        if not out:
            return
        out = out.strip()
        if not out.lower().endswith(".kmlproj"):
            out += ".kmlproj"
        self._save_project_to(Path(out))

    def _save_project_to(self, path: Path):
        """把当前状态写入 .kmlproj 工程文件。"""
        data = self._collect_project_data()
        try:
            save_project(path, data)
        except OSError as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self.current_project_path = path
        self._project_dirty = False
        self._update_title()
        self._add_recent_project(path)
        self.statusBar().showMessage(f"已保存工程：{path.name}", 5000)

    def _add_recent_project(self, path: Path):
        """更新全局 settings 的最近工程列表 + last_project。"""
        p = str(path)
        recents = list(self.settings.get("recent_projects") or [])
        # 前置、去重、截断 10
        if p in recents:
            recents.remove(p)
        recents.insert(0, p)
        recents = recents[:10]
        self.settings["recent_projects"] = recents
        self.settings["last_project"] = p
        save_global_settings(self.settings)
        self._rebuild_recent_menu()

    def _maybe_save_dirty(self) -> bool:
        """当前工程有未保存改动时提示用户。返回是否可继续操作。"""
        if not self._project_dirty:
            return True
        ret = QMessageBox.question(
            self, "保存当前工程？",
            "当前工程有未保存的改动，是否保存？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if ret == QMessageBox.Save:
            self.on_save_project()
            # 保存可能被取消（如另存为对话框点取消）→ 再检查 dirty
            return not self._project_dirty
        if ret == QMessageBox.Discard:
            return True
        return False   # Cancel

    def _load_kml(self, path: Path):
        self.current_kml_path = path
        w, h = self._cw, self._ch
        self.renderer = Renderer(path, width=w, height=h)
        # 保留用户已设置的轨迹粗细 / 节点字号 / 数据项勾选 / 标注颜色
        self.renderer.trajectory_width = self.spin_line_width.value()
        self.renderer.node_font_size = self.spin_node_font.value()
        self.renderer.panel_fields = self._checked_fields()
        c = self._node_color
        self.renderer.node_color = (c.red(), c.green(), c.blue())
        # 轨迹与起终点颜色
        for key, attr in [("unvisited", "unvisited_color"),
                          ("visited", "visited_color"),
                          ("start", "start_color"),
                          ("end", "end_color")]:
            _tc = self._traj_colors[key]
            setattr(self.renderer, attr, (_tc.red(), _tc.green(), _tc.blue()))
        # 数据面板背景（宽度/外扩边距/颜色/不透明度，从 GUI 控件同步）
        self.renderer.panel_bg_w = float(self.spin_panel_bg_w.value())
        self.renderer.panel_bg_pad = float(self.spin_panel_bg_pad.value())
        self.renderer.panel_bg_alpha_pct = float(self.spin_panel_bg_alpha.value())
        _pbc = self._panel_bg_color
        self.renderer.panel_bg_color = (_pbc.red(), _pbc.green(), _pbc.blue())
        # 地图导航设置（从 GUI 控件同步，确保 _load_kml 内能正确创建 map_item）
        self.renderer.show_map = self.chk_map.isChecked()
        self.renderer.map_zoom = self.spin_map_zoom.value()
        self.renderer.map_shape = ("circle"
                                   if self.combo_map_shape.currentIndex() == 1
                                   else "rect")
        self.renderer.map_style = (self.combo_map_style.currentData()
                                   or "standard")
        self.renderer.map_traj_style = (self.combo_map_traj_style.currentData()
                                        or "contrast")
        self.renderer.map_text_delta = self._map_text_deltas[
            self.combo_map_text.currentIndex()]
        self.renderer.map_arrow_size = self.spin_map_arrow.value()
        self.renderer.map_heading_up = (self.combo_map_orient.currentIndex() == 1)
        self.renderer.map_show_trajectory = self.chk_map_traj.isChecked()
        self.renderer.map_traj_ahead_m = float(
            self.spin_map_traj_ahead.value())
        # 动态坡度仪设置（从 GUI 控件同步）
        self.renderer.show_slope_gauge = self.chk_slope_gauge.isChecked()
        self.renderer.slope_gauge_size = float(self.spin_gauge_size.value())
        self.renderer.gauge_label_size = float(self.spin_gauge_label_size.value())
        self.renderer.gauge_value_size = float(self.spin_gauge_value_size.value())
        self.renderer.gauge_fill_alpha_pct = float(self.spin_gauge_alpha.value())
        self.renderer.gauge_style = (self.combo_gauge_style.currentData()
                                     or "arc")
        _gmap = {"edge_top": "gauge_edge_top_color",
                 "edge_bottom": "gauge_edge_bottom_color"}
        for _gk, _ga in _gmap.items():
            _gc = self._gauge_colors[_gk]
            setattr(self.renderer, _ga, (_gc.red(), _gc.green(), _gc.blue()))
        # 海拔一览图设置（从 GUI 控件同步）
        self.renderer.show_elev_profile = self.chk_elev_profile.isChecked()
        self.renderer.elev_profile_w = float(self.spin_elev_w.value())
        self.renderer.elev_profile_h = float(self.spin_elev_h.value())
        # 进度环设置（从 GUI 控件同步）
        self.renderer.show_progress_ring = self.chk_progress_ring.isChecked()
        self.renderer.progress_ring_style = \
            self.combo_ring_style.currentData() or "circle"
        self.renderer.progress_ring_size = float(self.spin_ring_size.value())
        _rc = self._ring_color
        self.renderer.ring_progress_color = (_rc.red(), _rc.green(),
                                             _rc.blue())
        self.renderer.preview_idx = -1.0
        # 分段生成模式（从 GUI 单选按钮同步）
        self.renderer.use_segments = self.rb_segments.isChecked()

        # 清空并重建 scene
        self.scene.clear()
        self.scene.setSceneRect(0, 0, w, h)

        # 轨迹（用 base_projected 原始坐标，Qt 变换由 setPos/setScale 处理）
        path_item = QPainterPath()
        projected = self.renderer.base_projected
        for i, (x, y) in enumerate(projected):
            if i == 0:
                path_item.moveTo(x, y)
            else:
                path_item.lineTo(x, y)
        self.trajectory_item = DraggablePathItem(path_item, self.renderer)
        self.scene.addItem(self.trajectory_item)
        # 初始 center 精确对齐到路径锚点（保证 scale=1 时画布与视频一致）
        a = self.trajectory_item._anchor
        self.renderer.trajectory_center = (a.x(), a.y())

        # 面板
        self.panel_item = DataPanelItem(self.renderer)
        self.scene.addItem(self.panel_item)

        # 地图导航面板（仅当用户勾选时显示）
        self.map_item = None
        if self.renderer.show_map:
            self.map_item = MapPanelItem(self.renderer)
            self.scene.addItem(self.map_item)

        # 动态坡度仪（仅当用户勾选时显示）
        self.gauge_item = None
        if self.renderer.show_slope_gauge:
            self.gauge_item = SlopeGaugeItem(self.renderer)
            self.scene.addItem(self.gauge_item)

        # 海拔一览图（仅当用户勾选时显示）
        self.elev_item = None
        if self.renderer.show_elev_profile:
            self.elev_item = ElevProfileItem(self.renderer)
            self.scene.addItem(self.elev_item)
            self.elev_item.refresh_preview()

        # 进度环（仅当用户勾选时显示）
        self.ring_item = None
        if self.renderer.show_progress_ring:
            self.ring_item = ProgressRingItem(self.renderer)
            self.scene.addItem(self.ring_item)
            self.ring_item.refresh_preview()

        # 新建 item 的缩放值与设置项对齐（工程恢复时随后由 _apply_state_dict 覆盖）
        self._on_preview_scaled("trajectory", self.trajectory_item.scale())
        self._on_preview_scaled("panel", self.panel_item._s)
        if self.map_item is not None:
            self._on_preview_scaled("map", self.map_item._s)
            self.map_item.refresh_preview(0)     # 加载 KML 后渲出首帧预览

        # 起/终点：作为轨迹的子项，拖拽/缩放自动跟随（放大到半径14）
        sx, sy = projected[0]
        ex, ey = projected[-1]
        self._start_end_dots = []
        for x, y, which in [(sx, sy, "start"), (ex, ey, "end")]:
            rgb = (self.renderer.start_color if which == "start"
                   else self.renderer.end_color)
            dot = QGraphicsEllipseItem(x - 14, y - 14, 28, 28, self.trajectory_item)
            dot.setBrush(QBrush(QColor(*rgb)))
            dot.setPen(QPen(QColor(255, 255, 255, 200), 2))
            # 不拦截鼠标事件，滚轮/拖拽穿透到轨迹本身
            dot.setAcceptedMouseButtons(Qt.NoButton)
            dot.setFlag(QGraphicsItem.ItemIsSelectable, False)
            self._start_end_dots.append((dot, which))

        # 关键节点（scene.clear 已移除旧图形，重置列表即可）
        self._node_items = []
        self._rebuild_node_items()

        # 重置视图 + 刷新
        self.view.resetTransform()
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
        self.scene.update()
        self.view.viewport().update()

        self.btn_render.setEnabled(True)
        self._apply_segments_to_ui()   # 新轨迹 → 分段按 renderer.segments 刷新状态
        self.statusBar().showMessage(
            f"已加载: {path.name}  |  轨迹点: {len(self.renderer.points)}  |  "
            f"里程: {self.renderer.cum_dist[-1]/1000:.2f} km  |  "
            f"爬升: {self.renderer.cum_ascent[-1]:.1f} m  |  "
            f"时长: {format_duration(self.renderer.total_active_s)}"
        )

    def on_reset_layout(self):
        if self.current_kml_path is None:
            return
        try:
            self._load_kml(self.current_kml_path)
        except Exception as e:
            QMessageBox.warning(self, "警告", f"重置失败:\n{e}")
            return
        self._mark_dirty()   # 重置布局 = 丢弃用户编辑，工程变脏

    # ── 渲染前边界检查 ──

    def _check_and_fix_bounds(self) -> bool:
        """轨迹/面板超出输出画布时提醒。返回 False 表示取消渲染。"""
        r = self.renderer
        w, h = r.width, r.height
        pad = 20  # 线宽/抗锯齿的视觉容差

        x0, y0, x1, y1 = r.get_bounds()
        traj_bad = x0 < -pad or y0 < -pad or x1 > w + pad or y1 > h + pad

        pw = _panel_span_w(r)
        ph = _panel_span_h(r)
        px, py = r.panel_pos
        # 背景条向外扩的部分也占画布空间，故起点按边距左/上偏移后判定
        panel_pad = r.panel_bg_pad * r.panel_scale
        panel_bad = (px - panel_pad < -2 or py - panel_pad < -2
                     or px - panel_pad + pw > w + 2
                     or py - panel_pad + ph > h + 2)

        if not (traj_bad or panel_bad):
            return True

        msgs = []
        if traj_bad:
            msgs.append(f"轨迹范围 ({x0:.0f},{y0:.0f})~({x1:.0f},{y1:.0f}) "
                        f"超出画布 {w}×{h}")
        if panel_bad:
            msgs.append(f"数据面板 ({px:.0f},{py:.0f})+{pw:.0f}×{ph:.0f} "
                        f"超出画布 {w}×{h}")
        ret = QMessageBox.warning(
            self, "内容超出输出画布",
            "\n".join(msgs) + "\n\n超出部分在生成的视频中会被裁剪。\n"
            "是否自动调整以完整显示？\n\n"
            "[是] 自动缩放居中  [否] 按当前状态渲染  [取消] 停止渲染",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if ret == QMessageBox.Yes:
            if traj_bad:
                self._auto_fit_trajectory()
            if panel_bad:
                self._auto_fit_panel()
            return True
        return ret == QMessageBox.No

    def _auto_fit_trajectory(self):
        """把轨迹缩放/平移到画布内居中（保留 60px 边距），并同步 GUI。"""
        r = self.renderer
        if not r.base_projected:
            return
        xs = [p[0] for p in r.base_projected]
        ys = [p[1] for p in r.base_projected]
        bx0, bx1 = min(xs), max(xs)
        by0, by1 = min(ys), max(ys)
        bw, bh = bx1 - bx0, by1 - by0
        if bw <= 0 or bh <= 0:
            return
        m = 60
        s = max(0.05, min((r.width - 2 * m) / bw, (r.height - 2 * m) / bh))
        # 映射 x -> C + s*(x-C) + offset，令 base 包围盒中心映射到画布中心
        bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2
        Cx, Cy = r.trajectory_center
        off_x = r.width / 2 - (Cx + s * (bcx - Cx))
        off_y = r.height / 2 - (Cy + s * (bcy - Cy))
        r.trajectory_scale = s
        r.trajectory_offset = (off_x, off_y)
        # 同步 GUI item（Qt 映射 scene = pos + O + s*(x-O)，O=anchor=C）
        self.trajectory_item.setScale(s)
        self.trajectory_item.setPos(off_x, off_y)
        self.trajectory_item._sync()
        self.scene.update()
        self.view.viewport().update()

    def _auto_fit_panel(self):
        """把数据面板平移回画布内（左上优先）。"""
        r = self.renderer
        pw = _panel_span_w(r)
        ph = _panel_span_h(r)
        px = min(max(r.panel_pos[0], 8), max(8, r.width - pw - 8))
        py = min(max(r.panel_pos[1], 8), max(8, r.height - ph - 8))
        r.panel_pos = (px, py)
        self.panel_item.setPos(px, py)
        self.panel_item._sync()

    # ── 视频生成 ──

    def on_render(self):
        if self.renderer is None:
            return
        # 渲染前边界检查：超出画布的内容会被裁掉，提前提醒并可一键适配
        if not self._check_and_fix_bounds():
            return

        # 起始目录 = 上次保存目录（无则桌面），文件名用时间戳
        start_dir = self._last_save_dir
        if not start_dir or not Path(start_dir).is_dir():
            start_dir = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        if not start_dir:
            start_dir = str(Path.home() / "Desktop")
        default_name = datetime.now().strftime("%Y%m%d_%H%M%S") + ".mov"
        out_str, _ = QFileDialog.getSaveFileName(
            self, "保存视频",
            str(Path(start_dir) / default_name),
            "MOV with alpha (*.mov)")
        out_str = out_str.strip()
        if not out_str:
            return
        out_path = Path(out_str)
        # 记住本次目录，作为下次对话框的起始位置
        self._last_save_dir = str(out_path.parent)

        self.btn_render.setEnabled(False)
        self.btn_abort.setEnabled(True)          # 渲染中允许终止
        self.progress.setValue(0)
        fps = self.spin_fps.value()
        dur = self.spin_duration.value()
        rscale = self.combo_render_scale.currentData() or 1
        self.worker = RenderWorker(self.renderer, out_path, fps, dur, rscale)
        self.worker.progress.connect(
            lambda done, t: self.progress.setValue(int(done / t * 100)))
        self.worker.finished_ok.connect(self._on_render_done)
        self.worker.failed.connect(self._on_render_fail)
        self.worker.cancelled.connect(self._on_render_cancelled)
        self.statusBar().showMessage("正在渲染视频...")
        # 渲染期间锁定画布，防止中途拖拽导致前后帧不一致
        self.view.setEnabled(False)
        self.worker.start()

    def _on_abort_render(self):
        """用户点击「终止」：置标志 + 立即禁用按钮防重复点击，等待 worker 取消回调。"""
        if self.worker is None:
            return
        self.btn_abort.setEnabled(False)
        self.btn_abort.setText("终止中…")
        self.worker.cancel()

    def _on_render_cancelled(self):
        self.btn_abort.setEnabled(False)
        self.btn_abort.setText("⛔ 终止")
        self.btn_render.setEnabled(True)
        self.view.setEnabled(True)
        self.statusBar().showMessage("⛔ 渲染已终止")

    def _on_render_done(self, path: str):
        self.btn_abort.setEnabled(False)
        self.btn_render.setEnabled(True)
        self.view.setEnabled(True)
        self.progress.setValue(100)
        size_mb = Path(path).stat().st_size / 1024 / 1024
        self.statusBar().showMessage(f"✅ 完成: {path}  ({size_mb:.1f} MB)")
        QMessageBox.information(
            self, "完成", f"视频已保存\n{path}\n\n大小: {size_mb:.1f} MB")

    def _on_render_fail(self, err: str):
        self.btn_abort.setEnabled(False)
        self.btn_render.setEnabled(True)
        self.view.setEnabled(True)
        self.statusBar().showMessage("❌ 渲染失败")
        QMessageBox.critical(self, "渲染失败", err)

    # ── 会话保存 ──

    def _collect_project_data(self):
        """收集当前会话的全部渲染状态 → 工程数据 dict。"""
        data = {
            "version": 1,
            "kml_path": (str(self.current_kml_path)
                         if self.current_kml_path else None),
            "canvas": [self._cw, self._ch],
            "line_width": self.spin_line_width.value(),
            "node_font_size": self.spin_node_font.value(),
            "node_color": [self._node_color.red(), self._node_color.green(),
                           self._node_color.blue()],
            "unvisited_color": [self._traj_colors["unvisited"].red(),
                                self._traj_colors["unvisited"].green(),
                                self._traj_colors["unvisited"].blue()],
            "visited_color": [self._traj_colors["visited"].red(),
                              self._traj_colors["visited"].green(),
                              self._traj_colors["visited"].blue()],
            "start_color": [self._traj_colors["start"].red(),
                            self._traj_colors["start"].green(),
                            self._traj_colors["start"].blue()],
            "end_color": [self._traj_colors["end"].red(),
                          self._traj_colors["end"].green(),
                          self._traj_colors["end"].blue()],
            "duration": self.spin_duration.value(),
            "fps": self.spin_fps.value(),
            "render_scale": (self.combo_render_scale.currentData() or 1),
            "panel_fields": self._checked_fields(),
            "panel_bg_w": (self.renderer.panel_bg_w
                           if self.renderer else 320.0),
            "panel_bg_pad": (self.renderer.panel_bg_pad
                             if self.renderer else 0.0),
            "panel_bg_color": (list(self.renderer.panel_bg_color)
                               if self.renderer else [128, 128, 128]),
            "panel_bg_alpha_pct": (self.renderer.panel_bg_alpha_pct
                                   if self.renderer else 45.0),
            "key_nodes": (list(self.renderer.key_nodes)
                          if self.renderer else []),
            # segments 用新 dict 格式（cuts / durations）
            "segments": ({"cuts": list(self.renderer.segments[0]),
                           "durations": list(self.renderer.segments[1])}
                         if (self.renderer and self.renderer.segments) else None),
            "show_map": (self.renderer.show_map if self.renderer else False),
            "map_zoom": (self.renderer.map_zoom if self.renderer else 15),
            "map_shape": (self.renderer.map_shape if self.renderer else "rect"),
            "map_style": (self.renderer.map_style
                          if self.renderer else "standard"),
            "map_traj_style": (self.renderer.map_traj_style
                               if self.renderer else "contrast"),
            "map_text_delta": (self.renderer.map_text_delta
                               if self.renderer else 0),
            "map_arrow_size": (self.renderer.map_arrow_size
                               if self.renderer else 14),
            "map_heading_up": (self.renderer.map_heading_up
                               if self.renderer else False),
            "map_show_trajectory": (self.renderer.map_show_trajectory
                                    if self.renderer else False),
            "map_traj_ahead_m": (self.renderer.map_traj_ahead_m
                                 if self.renderer else 0.0),
            "show_slope_gauge": (self.renderer.show_slope_gauge
                                 if self.renderer else False),
            "slope_gauge_size": (self.renderer.slope_gauge_size
                                 if self.renderer else 140.0),
            "slope_gauge_pos": (list(self.renderer.slope_gauge_pos)
                                if self.renderer else [40.0, 880.0]),
            "gauge_label_size": (self.renderer.gauge_label_size
                                 if self.renderer else 20.0),
            "gauge_value_size": (self.renderer.gauge_value_size
                                 if self.renderer else 24.0),
            "gauge_fill_alpha_pct": (self.renderer.gauge_fill_alpha_pct
                                     if self.renderer else 85.0),
            "gauge_edge_top_color": (list(self.renderer.gauge_edge_top_color)
                                     if self.renderer else list(GAUGE_WARM_DEFAULT)),
            "gauge_edge_bottom_color": (list(self.renderer.gauge_edge_bottom_color)
                                        if self.renderer else list(GAUGE_COLD_DEFAULT)),
            "gauge_style": (self.renderer.gauge_style
                            if self.renderer else "arc"),
            "show_elev_profile": (self.renderer.show_elev_profile
                                  if self.renderer else True),
            "elev_profile_w": (self.renderer.elev_profile_w
                               if self.renderer else 360.0),
            "elev_profile_h": (self.renderer.elev_profile_h
                               if self.renderer else 190.0),
            "elev_profile_pos": (list(self.renderer.elev_profile_pos)
                                 if self.renderer else [1536.0, 866.0]),
            "show_progress_ring": (self.renderer.show_progress_ring
                                   if self.renderer else False),
            "progress_ring_style": (self.renderer.progress_ring_style
                                    if self.renderer else "circle"),
            "progress_ring_size": (self.renderer.progress_ring_size
                                   if self.renderer else 140.0),
            "progress_ring_pos": (list(self.renderer.progress_ring_pos)
                                  if self.renderer else [200.0, 350.0]),
            "ring_progress_color": (list(self.renderer.ring_progress_color)
                                    if self.renderer
                                    else list(RING_PROGRESS_DEFAULT)),
            "use_segments": (self.renderer.use_segments if self.renderer else False),
        }
        if self.trajectory_item is not None:
            p = self.trajectory_item.pos()
            data["trajectory"] = {"offset": [p.x(), p.y()],
                                  "scale": self.trajectory_item.scale()}
        if self.panel_item is not None:
            p = self.panel_item.pos()
            data["panel"] = {"pos": [p.x(), p.y()],
                             "scale": self.panel_item._s}
        if self.map_item is not None:
            p = self.map_item.pos()
            data["map"] = {"pos": [p.x(), p.y()],
                           "scale": self.map_item._s}
        return data

    def _save_current(self):
        """即时持久化当前工程：
        - 已保存工程（current_project_path 非空）→ 写盘到 .kmlproj
        - 未保存工程 → 仅置 dirty 标记（等用户手动保存）
        同时把 last_save_dir（跨工程）写回全局 settings。
        """
        if self.current_project_path is not None:
            try:
                save_project(self.current_project_path,
                             self._collect_project_data())
            except OSError:
                pass   # 保存失败不阻止操作
        else:
            self._mark_dirty()
        # last_save_dir 跨工程共享，写回全局 settings
        self.settings["last_save_dir"] = self._last_save_dir
        save_global_settings(self.settings)

    def closeEvent(self, event):
        """关闭时：若工程有未保存改动提示用户；最后写全局 settings。"""
        if self._project_dirty:
            if not self._maybe_save_dirty():
                event.ignore()   # 用户取消 → 不关闭
                return
        # 更新全局 settings（last_save_dir / default_canvas）
        self.settings["last_save_dir"] = self._last_save_dir
        self.settings["default_canvas"] = [self._cw, self._ch]
        save_global_settings(self.settings)
        super().closeEvent(event)


def _setup_crash_log():
    """未捕获异常 / 原生崩溃写入 crash.log，便于事后定位。"""
    import faulthandler
    # 打包版 __file__ 指向临时解压目录，crash.log 写到 exe 旁边才留得住
    if getattr(sys, "frozen", False):
        log_path = Path(sys.executable).with_name("crash.log")
    else:
        log_path = Path(__file__).with_name("crash.log")
    f = open(log_path, "w", encoding="utf-8")
    faulthandler.enable(f)
    sys.excepthook = lambda t, v, tb: (
        traceback.print_exception(t, v, tb),
        traceback.print_exception(t, v, tb, file=f),
    )
    # PyQt5 事件回调里抛的异常不走 excepthook，qFatal 前会打印 stderr；
    # faulthandler 已兜底原生栈，足够定位


def main():
    import multiprocessing
    multiprocessing.freeze_support()   # 打包后多进程渲染子进程必需
    _setup_crash_log()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    # 启动时自动打开上次工程（若有）；否则保持空画布等用户操作
    last_proj = win.settings.get("last_project")
    if last_proj and Path(last_proj).exists():
        try:
            win._load_project_file(Path(last_proj))
        except Exception as e:
            print(f"自动打开工程失败: {e}", file=sys.stderr)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
