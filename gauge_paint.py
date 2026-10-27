"""坡度仪共享绘制层（QPainter）：GUI 预览与视频渲染共用同一份绘制代码。

- app.py 的 SlopeGaugeItem.paint 以 ss=1 直接调用 paint_gauge（屏幕预览）
- renderer.Renderer._draw_slope_gauge 以 ss=输出倍率 经 render_gauge_layer
  在离屏 QImage 上绘制后转回 PIL 图层合成（成片输出）
两条路径走同一函数，保证 WYSIWYG（消除历史上 PIL/QP 两套手绘实现漂移走形）。

ss 约定：布局函数 gauge_*_layout(size) 接收已乘 ss 的尺寸（坐标自动放大），
字体大小 / dy 偏移 / 线宽等「写死的像素值」由本模块统一乘 ss。
"""

from __future__ import annotations

import math
import os

from PIL import Image

from renderer import (
    GAUGE_LABEL_RATIO, SLOPE_LIMIT_PCT,
    ELEV_BG, ELEV_BORDER, ELEV_PROGRESS, ELEV_LINE,
    ELEV_AREA_TOP, ELEV_AREA_BOTTOM,
    gauge_footprint, gauge_arc_layout, gauge_card_layout,
    gauge_profile_layout, gauge_gradient_stops,
    gauge_value_color, gauge_format_pct, _fit_font_size,
)

_QAPP = None     # 懒创建的 QGuiApplication（worker / CLI 进程用，见 ensure_qapp）


def ensure_qapp():
    """保证当前进程存在 Qt 应用实例（QFont/QFontMetrics 依赖 QGuiApplication）。

    GUI 进程已有 QApplication → 直接返回；多进程 worker / CLI 渲染进程没有 →
    懒创建一个。注意不能用 offscreen 平台：其字体数据库为空（实测 Qt5/Windows
    families()=0），drawText 静默失败画不出文字；Windows 上改用 windows 平台
    创建 QGuiApplication（不创建窗口即不弹窗，离屏绘制与字体光栅化正常），
    其他平台回退 offscreen（Linux 下 offscreen 走 fontconfig 字体正常）。
    """
    global _QAPP
    import sys
    from PyQt5.QtGui import QGuiApplication
    if QGuiApplication.instance() is not None:
        return
    if _QAPP is None:
        default = "windows" if sys.platform == "win32" else "offscreen"
        os.environ.setdefault("QT_QPA_PLATFORM", default)
        _QAPP = QGuiApplication([])


def _font(px: float, bold: bool = False):
    """坡度仪字体（与 PIL 端 load_font 同用微软雅黑，保证文字度量一致）。"""
    from PyQt5.QtGui import QFont
    f = QFont("Microsoft YaHei")
    f.setPixelSize(max(8, int(round(px))))
    if bold:
        f.setBold(True)
    return f


def _outlined_text(painter, text, rect, color, font, o: int = 1,
                   outline=(0, 0, 0, 255)):
    """黑边描边文字：Qt 无原生描边，用 8 方向偏移近似（o=描边半径 px）。"""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor
    painter.setFont(font)
    painter.setPen(QColor(*outline))
    for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o),
                   (-o, -o), (-o, o), (o, -o), (o, o)):
        painter.drawText(rect.translated(dx, dy), Qt.AlignCenter, text)
    painter.setPen(color)
    painter.drawText(rect, Qt.AlignCenter, text)


def _brightness(r) -> float:
    """整体亮度系数（0..1）：直接缩放绘制时用到的每种颜色的 RGB。

    注意：不能事后用 SourceAtop 叠一层黑——GUI 预览是直接画在场景底上的，
    叠黑会把坡度仪占位矩形整块压暗，看起来像加了一块灰背景。
    """
    return max(0.0, min(100.0, getattr(r, "gauge_brightness_pct", 100.0))) / 100.0


def _c(r, rgb, alpha=None):
    """按整体亮度缩放 RGB 后构造 QColor；alpha 缺省取 rgb[3]（若有），否则 255。"""
    from PyQt5.QtGui import QColor
    b = _brightness(r)
    a = alpha if alpha is not None else (rgb[3] if len(rgb) > 3 else 255)
    return QColor(int(round(rgb[0] * b)), int(round(rgb[1] * b)),
                  int(round(rgb[2] * b)), int(a))


# ─────────────────────── 入口分发 ───────────────────────

def paint_gauge(painter, r, s: float, pct: float, idx: float, ss: float = 1.0):
    """按 r.gauge_style 分发给三种样式绘制函数。

    painter 坐标原点 = 坡度仪占位框左上角；s = slope_gauge_size × ss。
    整体亮度在三种样式的颜色构造处统一用 _c() 缩放（预览/成片一致）。
    """
    if r.gauge_style == "card":
        paint_gauge_card(painter, r, s, pct, ss)
    elif r.gauge_style == "profile":
        paint_gauge_profile(painter, r, s, pct, idx, ss)
    else:
        paint_gauge_arc(painter, r, s, pct, ss)


def render_gauge_layer(r, s: float, pct: float, idx: float,
                       ss: float = 1.0) -> Image.Image:
    """视频端用：离屏 QImage 上 QPainter 绘制坡度仪，转回 PIL RGBA 图层。

    Format_ARGB32_Premultiplied 绘制（Qt 默认合成格式）
    → convertToFormat(RGBA8888) 得非预乘 RGBA 字节流 → PIL frombytes。
    """
    ensure_qapp()
    from PyQt5.QtGui import QImage, QPainter
    fw, fh = gauge_footprint(r.gauge_style, s)
    # 右/下各 +1px 余量：卡片边框（1px 笔宽）以 w/h 为路径中心，右/底缘有
    # 0.5px 抗锯齿溢出；不加余量会被图层右/下边界裁掉，而 GUI 预览不裁剪
    # item 外像素 → 预览有右/底框线、成片没有，两端不一致。
    w, h = max(2, int(round(fw))) + 1, max(2, int(round(fh))) + 1
    qimg = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    qimg.fill(0)                                  # 全透明底
    painter = QPainter(qimg)
    painter.setRenderHint(QPainter.Antialiasing, True)
    paint_gauge(painter, r, s, pct, idx, ss)
    painter.end()
    qimg = qimg.convertToFormat(QImage.Format_RGBA8888)
    ptr = qimg.bits()
    ptr.setsize(qimg.byteCount())
    return Image.frombytes("RGBA", (w, h), bytes(ptr),
                           "raw", "RGBA", qimg.bytesPerLine(), 1)


# ─────────────────────── 方案B 半圆弧仪表 ───────────────────────

def paint_gauge_arc(painter, r, s: float, pct: float, ss: float = 1.0):
    """半圆弧仪表：暗色轨道 + 渐变刻度弧 + 当前进度弧 + 光点 + 数值。"""
    from PyQt5.QtCore import Qt, QPointF, QRectF
    from PyQt5.QtGui import QPen, QColor, QFontMetrics

    cold, warm = r.gauge_edge_bottom_color, r.gauge_edge_top_color
    vc = gauge_value_color(pct, cold, warm)
    vcol = _c(r, vc)
    value = gauge_format_pct(pct)
    a = max(0.0, min(100.0, r.gauge_fill_alpha_pct)) / 100.0
    o = max(1, int(round(ss)))                    # 文字描边偏移
    L = gauge_arc_layout(s)
    cx, cy, R, W = L["cx"], L["cy"], L["R"], L["W"]
    rect = QRectF(cx - R, cy - R, 2 * R, 2 * R)

    # 1) 暗色轨道（左→顶→右半圆，圆头端点；透明度随 a）
    #    注意 Qt drawArc：0°=3点钟、正角逆时针（y 向下）→ 0..180 才是上半圆。
    #    旧 PIL 版 ld.arc(start=180,end=360) 画的也是上半圆；
    #    迁移时若写成 180..360 会画到下半圆，裸露成一条灰带（已修正）。
    painter.setPen(QPen(_c(r, (16, 22, 35), int(round(0.55 * a * 255))),
                        W, Qt.SolidLine, Qt.RoundCap))
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(rect, 0, 180 * 16)

    # 2) 彩色刻度弧：48 段小弧拼成连续渐变（左=冷端 → 顶=绿 → 右=暖端）
    seg_pen = QPen()
    seg_pen.setWidthF(W)
    seg_pen.setCapStyle(Qt.FlatCap)
    seg = 180.0 / 48
    for i in range(48):
        fa = 180 + i * seg
        mid = math.radians(fa + seg / 2)
        ov = (1 + math.cos(mid)) / 2              # 0=左端 → 1=右端
        c = gauge_value_color(ov * 120 - 60, cold, warm)
        seg_pen.setColor(_c(r, c, int(round(0.35 * a * 255))))
        painter.setPen(seg_pen)
        # PIL 角 φ（顺时针，y 向下）→ Qt 角 = 360 − φ（逆时针）
        painter.drawArc(rect, int(round((360 - fa - seg) * 16)) - 32,
                        int(round(seg * 16)) + 64)
    painter.setPen(Qt.NoPen)
    for ex, ov in ((cx - R, -60.0), (cx + R, 60.0)):
        c = gauge_value_color(ov, cold, warm)
        painter.setBrush(_c(r, c, int(round(0.35 * a * 255))))
        painter.drawEllipse(QPointF(ex, cy), W / 2, W / 2)

    # 3) 当前进度弧：从顶部 0% 展开到当前坡度（平头笔 + 手动圆帽）
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
    painter.setBrush(_c(r, vc, 90))
    painter.drawEllipse(tip, gr, gr)
    painter.setPen(QPen(QColor(0, 0, 0, 110), max(1.0, float(ss))))
    painter.setBrush(_c(r, (255, 255, 255)))
    painter.drawEllipse(tip, dr, dr)

    # 4) 数值 + 「坡度」标签（居中，均在弧内空白区）
    vfs = _fit_font_size(
        lambda fs: QFontMetrics(_font(fs)).horizontalAdvance(value),
        int(round(r.gauge_value_size * ss)), 0.56 * s)
    vf = _font(vfs)
    fm_v = QFontMetrics(vf)
    vw = fm_v.horizontalAdvance(value)
    _outlined_text(
        painter, value,
        QRectF(cx - vw / 2 - 4,
               L["value_cy"] + r.gauge_value_dy * ss - fm_v.height() / 2,
               vw + 8, fm_v.height()), vcol, vf, o)
    lfs = max(8, int(round(r.gauge_label_size * GAUGE_LABEL_RATIO * ss)))
    lf = _font(lfs)
    fm_l = QFontMetrics(lf)
    lw = fm_l.horizontalAdvance("坡度")
    _outlined_text(
        painter, "坡度",
        QRectF(cx - lw / 2 - 4,
               L["label_cy"] + r.gauge_label_dy * ss - fm_l.height() / 2,
               lw + 8, fm_l.height()), _c(r, (230, 236, 244), 210), lf, o)


# ─────────────────────── 方案D HUD 深色卡片 ───────────────────────

def paint_gauge_card(painter, r, s: float, pct: float, ss: float = 1.0):
    """HUD 深色卡片：圆角卡片 + 大号数值 + 渐变指示条 + 白色游标。"""
    from PyQt5.QtCore import Qt, QPointF, QRectF
    from PyQt5.QtGui import QPen, QColor, QFontMetrics, QLinearGradient

    cold, warm = r.gauge_edge_bottom_color, r.gauge_edge_top_color
    vc = gauge_value_color(pct, cold, warm)
    vcol = _c(r, vc)
    value = gauge_format_pct(pct)
    a = max(0.0, min(100.0, r.gauge_fill_alpha_pct)) / 100.0
    o = max(1, int(round(ss)))
    L = gauge_card_layout(s)
    w, h, pad, rx = L["w"], L["h"], L["pad"], L["rx"]

    # 1) 卡片底：深色半透明圆角矩形 + 细白边
    painter.setPen(QPen(_c(r, (255, 255, 255), 46), max(1.0, float(ss))))
    painter.setBrush(_c(r, (13, 17, 26), int(round(0.62 * a * 255))))
    painter.drawRoundedRect(QRectF(0, 0, w, h), rx, rx)

    # 2) 渐变指示条：横向胶囊（左=冷端 → 中=绿 → 右=暖端）
    strip_h = L["strip_h"]
    sy0 = L["strip_cy"] - strip_h / 2
    sx0, sx1 = pad, w - pad
    grad = QLinearGradient(sx0, 0, sx1, 0)
    for off, c in gauge_gradient_stops(cold, warm):
        grad.setColorAt(off, _c(r, c, int(round(a * 255))))
    painter.setPen(Qt.NoPen)
    painter.setBrush(grad)
    painter.drawRoundedRect(QRectF(sx0, sy0, sx1 - sx0, strip_h),
                            strip_h / 2, strip_h / 2)

    # 3) 游标：深色晕圈 + 白色圆点（位置 = 当前坡度映射到条上）
    mx = sx0 + (max(-SLOPE_LIMIT_PCT, min(SLOPE_LIMIT_PCT, pct)) + 60) \
        / 120 * (sx1 - sx0)
    my = L["strip_cy"]
    hr, dr = L["halo_r"], L["dot_r"]
    painter.setBrush(QColor(0, 0, 0, 115))
    painter.drawEllipse(QPointF(mx, my), hr, hr)
    painter.setBrush(_c(r, (255, 255, 255)))
    painter.drawEllipse(QPointF(mx, my), dr, dr)

    # 4) 文本：左侧灰色「坡度」标签 + 右对齐大号彩色数值
    lfs = max(8, int(round(r.gauge_label_size * GAUGE_LABEL_RATIO * ss)))
    lf = _font(lfs)
    fm_l = QFontMetrics(lf)
    lw = fm_l.horizontalAdvance("坡度")
    _outlined_text(
        painter, "坡度",
        QRectF(pad - 4,
               L["label_cy"] + r.gauge_label_dy * ss - fm_l.height() / 2,
               lw + 8, fm_l.height()), _c(r, (169, 180, 196)), lf, o,
        outline=(0, 0, 0, 160))
    max_w = max(20.0, (w - 2 * pad) - lw - 8 * ss)
    vfs = _fit_font_size(
        lambda fs: QFontMetrics(_font(fs)).horizontalAdvance(value),
        int(round(r.gauge_value_size * ss)), max_w)
    vf = _font(vfs)
    fm_v = QFontMetrics(vf)
    vw = fm_v.horizontalAdvance(value)
    _outlined_text(
        painter, value,
        QRectF(w - pad - vw - 4,
               L["value_cy"] + r.gauge_value_dy * ss - fm_v.height() / 2,
               vw + 8, fm_v.height()), vcol, vf, o)


# ─────────────────────── 方案A 坡度剖面卡 ───────────────────────

def paint_gauge_profile(painter, r, s: float, pct: float, idx: float,
                        ss: float = 1.0):
    """坡度剖面卡：海拔计同款底 + 大字 + 全程坡度剖面迷你曲线 + 当前点。"""
    from PyQt5.QtCore import Qt, QPointF, QRectF
    from PyQt5.QtGui import (QPen, QColor, QFontMetrics, QPainterPath,
                             QPolygonF, QLinearGradient)

    a = max(0.0, min(100.0, r.gauge_fill_alpha_pct)) / 100.0
    value = gauge_format_pct(pct)
    o = max(1, int(round(ss)))
    L = gauge_profile_layout(s)
    w, h, rx = L["w"], L["h"], L["rx"]

    # 1) 卡片底（海拔计同款：半透明深色 + 细白边框）
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), rx, rx)
    painter.fillPath(path, _c(r, ELEV_BG[:3], int(round(ELEV_BG[3] * a))))
    painter.setPen(QPen(_c(r, ELEV_BORDER[:3], int(round(ELEV_BORDER[3] * a))),
                        max(1.0, float(ss))))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)

    # 2) 右上「坡度」标签（沿用 gauge_label_size / gauge_label_dy）
    lfs = max(8, int(round(r.gauge_label_size * GAUGE_LABEL_RATIO * ss)))
    lf = _font(lfs)
    fm_l = QFontMetrics(lf)
    lw = fm_l.horizontalAdvance("坡度")
    _outlined_text(
        painter, "坡度",
        QRectF(w - L["pad_x"] - lw - 4,
               L["label_cy"] + r.gauge_label_dy * ss - fm_l.height() / 2,
               lw + 8, fm_l.height()),
        _c(r, (230, 236, 244), 184), lf, o, outline=(0, 0, 0, 160))

    # 3) 左上大字数值（暖黄粗体，沿用 gauge_value_size / gauge_value_dy；
    #    超宽时收缩避开右上标签）
    max_tw = max(20.0, w - 2 * L["pad_x"] - lw - 8 * ss)
    vfs = _fit_font_size(
        lambda fs: QFontMetrics(_font(fs, bold=True)).horizontalAdvance(value),
        int(round(r.gauge_value_size * ss)), max_tw)
    vfs = min(vfs, max(8, int(round(
        (L["plot_y0"] - L["value_y"]) * 1.1))))   # 高度上限：不压到剖面区
    vf = _font(vfs, bold=True)
    fm_v = QFontMetrics(vf)
    _outlined_text(
        painter, value,
        QRectF(L["pad_x"] - 4,
               L["value_y"] + r.gauge_value_dy * ss,
               fm_v.horizontalAdvance(value) + 8, fm_v.height()),
        _c(r, ELEV_PROGRESS[:3]), vf, o,
        outline=(0, 0, 0, 200))

    # 4) 全程坡度剖面迷你曲线
    series = r._gauge_series
    if len(series) >= 2:
        x0, x1 = L["plot_x0"], L["plot_x1"]
        y0, y1 = L["plot_y0"], L["plot_y1"]
        mid = (y0 + y1) / 2.0
        amp = (y1 - y0) / 2.0
        n = len(series)
        # 显示降采样：序列常达数千点，直接画会在 ~200px 内糊成噪声。
        # 按 x 像素桶取均值，目标 ~每 2 个布局像素一个显示点，与 ss 无关。
        buckets = max(24, int(round((x1 - x0) / (2.0 * ss))))
        pts = []
        for b in range(buckets):
            i0 = int(b * n / buckets)
            i1 = max(i0 + 1, int((b + 1) * n / buckets))
            seg = series[i0:i1]
            vv = max(-60.0, min(60.0, sum(seg) / len(seg)))
            pts.append(QPointF(x0 + (x1 - x0) * (b + 0.5) / buckets,
                               mid - vv / 60.0 * amp))

        # 4a) 渐变面积（曲线与 0% 基线围成区域，暖橙→紫蓝，海拔计同款）
        poly = QPolygonF(pts + [QPointF(x1, mid), QPointF(x0, mid)])
        grad = QLinearGradient(0, y0, 0, y1)
        ga = int(round(0.88 * a * 255))
        grad.setColorAt(0.0, _c(r, ELEV_AREA_TOP[:3], ga))
        grad.setColorAt(1.0, _c(r, ELEV_AREA_BOTTOM[:3], ga))
        painter.setPen(Qt.NoPen)
        painter.setBrush(grad)
        painter.drawPolygon(poly)

        # 4b) 0% 基线（虚线）
        painter.setPen(QPen(_c(r, (255, 255, 255), 46),
                            max(1.0, float(ss)), Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(x0, mid), QPointF(x1, mid))

        # 4c) 白色曲线
        cpath = QPainterPath(pts[0])
        for p in pts[1:]:
            cpath.lineTo(p)
        painter.setPen(QPen(_c(r, ELEV_LINE[:3], ELEV_LINE[3]),
                            max(1.0, 1.6 * ss), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(cpath)

        # 4d) 当前位置圆点（白圈 + 暖黄芯，随进度在剖面上移动）
        frac = max(0.0, min(1.0, idx / max(1, n - 1)))
        cur_x = x0 + (x1 - x0) * frac
        cur_y = mid - max(-60.0, min(60.0, pct)) / 60.0 * amp
        ro = L["dot_r"] * 1.5
        ri = L["dot_r"] * 0.82
        painter.setPen(Qt.NoPen)
        painter.setBrush(_c(r, (255, 255, 255), 245))
        painter.drawEllipse(QPointF(cur_x, cur_y), ro, ro)
        painter.setBrush(_c(r, ELEV_PROGRESS[:3]))
        painter.drawEllipse(QPointF(cur_x, cur_y), ri, ri)
