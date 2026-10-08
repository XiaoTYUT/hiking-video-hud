# KML 轨迹视频生成器 — 功能说明

> 把两步路 App 导出的 KML 徒步轨迹，生成带 **透明 alpha 通道** 的轨迹动画视频，供二次剪辑叠加。
> 工作流：**加载 KML → 拖拽编辑 → 生成视频**（所见即所得）

---

## 一、核心功能

### 1. KML 解析与时间口径校准
- 解析 `<gx:Track>` 中的 `gx:coord` 坐标点和 `<when>` 时间戳
- 提取 `ExtendedData` 元数据：BeginTime / EndTime / PauseTime / Distance / ElevationGain / ElevationLoss 等
- **时长口径（已用 dog.kml 实测校准，与两步路 UI 一致）**
  - PauseTime 单位 = **毫秒**
  - 运动耗时 = 墙钟(EndTime − BeginTime) − PauseTime/1000
  - 总耗时 = 墙钟(EndTime − BeginTime)
  - `<when>` 时间戳 = 含暂停的墙钟轴，仅驱动轨迹节奏（标记真实停顿）

### 2. 数据计算
- **海拔平滑**：移动平均滤波（窗口 15），抑制 GPS 抖动导致的虚假爬升
- **累计爬升**：逐点累加 Δelev（仅取正值），末值按 KML 元数据 ElevationGain 线性校准
- **累计里程**：Haversine 公式逐点累加，末值按 KML 元数据 Distance 线性校准
- **当前点查找**：二分查找 `find_current_idx`，时间 → 索引

### 3. 坐标投影
- 等比缩放 + 居中投影，画布边距 80px
- 数据/画布宽高比自适应（保证轨迹不变形）
- 投影函数复用于关键节点（共享同一变换）

---

## 二、GUI 编辑功能（PyQt5）

### 1. 画布与背景
- 蓝色边框内 = 视频实际输出区域，边框外内容**不会**进入视频
- 项目目录 `back.png` 作为 GUI 预览背景（居中、保持比例），**视频输出保持全透明**
- 背景图缓存避免拖拽时反复缩放
- back.png 缺失时自动回退白底

### 2. 轨迹拖拽编辑 ([DraggablePathItem](file:///d:\26code\kml_path\app.py#L72))
- 左键拖拽平移轨迹
- 滚轮缩放（锚点固定在路径包围盒中心，与视频渲染模型一致）
- Hover 高亮反馈
- 缩放/偏移实时写回 Renderer，所见即所得

### 3. 终点标记
- 半径 14px 圆点 + 白色描边
- 起点 = 绿色 (100, 240, 120)，终点 = 红色 (240, 80, 80)
- 作为轨迹的子项，跟随拖拽/缩放自动同步

### 4. 关键节点标注
- JSON 数组输入，兼容多种键名：`name/名称`、`lon/lng/longitude/经度`、`lat/latitude/纬度`
- 圆点 + **白字 + 半透明黑底胶囊条**（任何背景下高对比）
- **节点字号**可调（10–60 px，默认 41 px）
- **标注颜色**调色板可选（默认黄色 255,200,60；仅影响圆点，文字固定白字）
- 节点作为轨迹子项跟随变换

### 5. 数据面板 ([DataPanelItem](file:///d:\26code\kml_path\app.py#L125))
- **无边框无背景**，仅绘制数据文字（rect 保留用于拖拽命中）
- 5 个可选数据项（GUI 复选框）：
  - 当前海拔（紫）
  - 累计爬升（绿）
  - 已行里程（黄）
  - 运动耗时（蓝）
  - 总耗时（橙）
- 数据标题 = 粗体黑边白字（8 方向偏移近似 PIL stroke）
- 数值紧跟标签右侧，间距 10×scale
- 拖拽平移 + 滚轮缩放（下限 0.5，字号 8px 可读性下限）
- **缩放模型**：内部 `_s` 系数驱动 rect + paint 统一变化，避免 Qt setScale × paint s 双重放大导致文字溢出

### 6. 输出规格
- 宽度 480–4096 px，高度 270–2304 px（默认 1920×1080）
- **轨迹粗细** 2–30 px（紫色未走，红色已走 +1px）
- 时长 1–3600 s，帧率 1–60 fps
- 实时显示总帧数

### 7. 分段视频 ([SegmentDialog](file:///d:\26code\kml_path\app.py#L365))
- 在轨迹预览上**左键点击添加分割点**（60px 屏幕捕捉阈值，最小间隔 0.4%）
- **右键点击删除分割点**（合并相邻两段，时长相加）
- 每段独立视频时长（spinbox）
- 段表格显示：段号、轨迹进度范围、对应墙钟时间范围、视频时长
- 滚轮缩放 + 方向键平移预览
- 段内仍按真实节奏线性压缩，段间拼接连续
- 分段模式下主时长输入禁用（总时长 = 各段时长之和）
- 分段绑定轨迹（切换 KML 自动清除），持久化到 settings.json

### 8. 渲染前边界检查 ([_check_and_fix_bounds](file:///d:\26code\kml_path\app.py#L1131))
- 检测轨迹/面板是否超出输出画布
- 超出时三选一：自动缩放居中 / 按当前状态渲染（裁剪超界部分）/ 取消渲染
- 自动适配保留 60px 边距

### 9. 会话持久化
- **存储位置**：`%APPDATA%\KmlPath\settings.json`（已从工程目录迁移）
- 启动时**自动迁移**旧工程目录文件并删除
- 持久化内容：kml_path、画布尺寸、line_width、node_font_size、duration、fps、panel_fields、key_nodes、轨迹 offset/scale、面板 pos/scale、标注颜色、last_save_dir、segments
- 关闭时保存，启动时恢复（包括 KML 路径与编辑状态）
- 手动"打开 KML"始终重置布局

### 10. 保存对话框
- 起始目录 = 上次保存目录（无则桌面）
- 文件名默认时间戳 `YYYYMMDD_HHMMSS.mov`

---

## 三、视频渲染 ([Renderer](file:///d:\26code\kml_path\renderer.py#L228))

### 1. 单帧渲染 ([render_frame](file:///d:\26code\kml_path\renderer.py#L353))
- **透明 RGBA** 画布 (0,0,0,0)
- 完整紫线轨迹（一次全展示）
- 已走红线叠加（截至当前 idx）
- 当前位置标记：3 层光晕 + 中心白红圆点
- 起终点：14px×scale 圆点 + 白边
- 关键节点：8px×scale 圆点 + 半透明黑底胶囊条白字标签
- 数据面板：5 行可选数据，时间值按 when→运动/总耗时线性映射（首 0，末值锚定元数据）

### 2. 线宽/半径随缩放同步
- 紫线宽 = trajectory_width × trajectory_scale
- 红线宽 = (trajectory_width + 1) × scale
- 起终点半径、节点半径、节点字号均乘 trajectory_scale
- 保证 GUI 预览与视频输出一致

### 3. 帧计划 ([_build_frame_plan](file:///d:\26code\kml_path\renderer.py#L477))
- **均匀模式**：progress 在 [0,1] 线性分布，总帧数 = fps × duration_s
- **分段模式**：每段帧数 = 段时长 × fps，段内进度左闭右开 [0,1)，最后一帧强制到 1.0
- 分段模式忽略 duration_s，总时长 = Σ durations

### 4. ffmpeg 合成
- 输入 `-framerate fps`，输出 `-r fps`（双重显式，确保剪辑软件读取正确时长）
- PNG 编码 + rgba 像素格式 + MOV 容器（兼容性好，alpha 通道稳定）
- 临时帧目录渲染完成后自动清理
- 越界时 stderr 警告

### 5. 异步渲染 ([RenderWorker](file:///d:\26code\kml_path\app.py#L279))
- QThread 后台渲染，不阻塞 UI
- progress 信号驱动进度条
- 渲染期间锁定画布防拖拽

---

## 四、工程约定

- GUI 与视频渲染**共享同一坐标模型**（固定中心锚点 + offset 拖拽位置）
- Qt5 用 `setScale(scale() * factor)` + `setTransformOriginPoint`，弃用 `scale(sx, sy)`
- 字体优先级：msyh.ttc → simhei.ttf → simsun.ttc → default；粗体用 msyhbd.ttc
- 崩溃日志：faulthandler + excepthook 写入 `crash.log`
- 不使用系统 Python，必须用 miniconda 环境 `test_skill`

---

## 五、文件结构

| 文件 | 作用 |
|---|---|
| [app.py](file:///d:\26code\kml_path\app.py) | PyQt5 GUI 主入口（窗口、Dock、对话框、渲染线程） |
| [renderer.py](file:///d:\26code\kml_path\renderer.py) | 数据层 + 渲染层（KML 解析、投影、单帧、视频生成） |
| [kml2video.py](file:///d:\26code\kml_path\kml2video.py) | 早期 CLI Demo（独立运行，含进度条） |
| [main.py](file:///d:\26code\kml_path\main.py) | 零依赖 KML 坐标解析 Demo |
| [back.png](file:///d:\26code\kml_path\back.png) | GUI 画布预览背景图（不影响视频透明） |
| [requirements.txt](file:///d:\26code\kml_path\requirements.txt) | numpy / pillow / PyQt5 |
| settings.json | 会话持久化（位于 `%APPDATA%\KmlPath\`） |
| crash.log | 崩溃堆栈日志 |

---

## 六、运行

```powershell
# 必须用 miniconda 环境
C:\Users\xiao\miniconda3\Scripts\conda.exe run -n test_skill python app.py
```

外部依赖：`ffmpeg`（需在 PATH 中）
