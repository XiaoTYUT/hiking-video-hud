# KML 轨迹视频生成器 — 项目知识库

> 本文件是项目的完整知识总结，供新会话快速恢复上下文。
> 最后更新：2026-10-19

---

## 1. 项目概述

把两步路（流马）App 导出的 KML 徒步轨迹渲染成**带透明通道的视频**（MOV 容器 + PNG 编码 + RGBA），
供剪映等剪辑软件叠加在底层视频上使用。

- PyQt5 GUI，所见即所得：拖拽轨迹/数据面板，画布构图 = 视频构图
- 纯后端数据层（renderer.py）与 GUI（app.py）分离
- 用户画像：有徒步活动经验，主要沟通语言中文

## 2. 运行环境（硬性约束）

| 项 | 值 |
|---|---|
| Python | **必须用 miniconda 环境 `test_skill`**，禁止系统 python |
| 解释器 | `C:\Users\xiao\miniconda3\envs\test_skill\python.exe` |
| conda | `C:\Users\xiao\miniconda3\Scripts\conda.exe run -n test_skill ...` |
| 依赖 | PyQt5、Pillow、numpy（requirements.txt） |
| 外部工具 | ffmpeg / ffprobe 需在 PATH |
| 系统 | Windows，项目目录 `d:\26code\kml_path` |

## 3. 文件结构与职责

| 文件 | 职责 |
|---|---|
| `app.py`（约 124KB） | **GUI 唯一入口**（`python app.py` 启动，内含 `main()` 与 `__main__` 块）+ 全部 GUI：CanvasView、MainWindow、SegmentDialog、KeyNodeEditor、RenderWorker 等 |
| `main.py` | ⚠️ 旧命令行解析 demo（打印 sample.kml 坐标，无 GUI）；sample.kml 不存在时静默退出，**不是应用入口** |
| `renderer.py`（约 57KB） | KML 解析、投影、单帧渲染、多进程帧生产、ffmpeg 封装（纯数据层） |
| `kml2video.py` | 早期 CLI 版本，仅保留参考，未跟进后续功能 |
| `back.png` | GUI 预览背景图（铺满整个预览窗口）；**视频背景仍透明**；缺失时回退白底 |
| `*.kml` | 测试数据（cat/dog/test；pig.kml 已被用户移除） |
| `.kmlproj` | 工程文件（用户数据在 `E:\video\meigui\`） |

## 4. 渲染管线（当前状态）

```
render_video(out_path, fps, duration_s, progress_cb, workers, render_scale)
 ├─ _apply_render_scale(render_scale)      # 超采样：画布/布局临时 ×N
 ├─ _render_video_inner
 │   ├─ get_bounds() 越界预警（超出画布会被裁剪）
 │   ├─ _build_frame_plan → (total_frames, idx_list)   # idx 驱动，保留真实节奏
 │   ├─ show_map 时 _prefetch_map_tiles(idx_list)      # 预取瓦片，避免逐帧网络请求
 │   ├─ 帧生产：
 │   │   ├─ workers==1 或 ≤64 帧：单进程循环，PNG(compress_level=1) 落盘
 │   │   └─ 否则：multiprocessing spawn Pool（16 核）→ worker 落盘 PNG
 │   └─ ffmpeg 封装：
 │       -framerate {fps} -i frame_%05d.png -r {fps}
 │       -c:v png -compression_level 1 -pix_fmt rgba out.mov
 └─ _restore_render_scale(saved)           # finally 恢复原布局
```

关键点：
- **输入输出帧率都要显式设置**（`-framerate` + `-r`），否则剪映/Pr 按 24fps 重读导致变速
- `-compression_level 1`：2026-10-17 新增，封装耗时约减半（11.8→6.3 ms/帧），体积略增
- 中间帧临时目录由 `TemporaryDirectory` 自动清理
- 取消：RenderWorker.cancel() 置标志 → progress_cb 抛 `_RenderCancelled` → app.py 删半成品视频

### 管道实验结论（重要，勿重试）

用户曾要求"不降帧率提高渲染速度"，实测否决了管道方案：

| 方案 | 端到端（480 帧 @1240×2160） | 结论 |
|---|---|---|
| 落盘 PNG + 默认封装 | 20.2s（生产 14.7s + ffmpeg 5.5s） | **基准，最快** |
| 落盘 PNG + compression_level 1 | 封装减半（5.5→~3s） | **当前采用** |
| raw 直通（worker 返回 tobytes → ffmpeg stdin） | 67.3s | 慢 3.3×，已回退 |
| PNG 管道（worker 返回 PNG bytes → image2pipe） | 34.5s | 慢 1.7×，已回退 |

根因：管道使「渲染」与「ffmpeg 封装」互相阻塞成串行；落盘让两个阶段天然解耦重叠。
IPC 每帧 raw 10.7MB / PNG 约 2MB，主进程单线程解包也是瓶颈。
两种管道方案正确性均验证 PASS（ffprobe: png/rgba/尺寸/480帧/48s）。

### 单帧成本拆解（620×1080@2× → 1240×2160）

| 操作 | ms/帧 |
|---|---|
| 绘制 only（单进程） | 38.6 |
| + PIL PNG 编码 | 111.5（编码占 65%） |
| + raw tobytes（不编码） | 40.5 |
| 多进程 16 + PNG 落盘 | 31.0（并行加速仅 ~3.6×，受磁盘 I/O/内存带宽限制） |
| ffmpeg 封装默认 / level 1 | 11.8 / 6.3 |

## 5. 输出规格与超采样

- **格式硬约束**：MOV 容器 + PNG 编码 + rgba（ProRes 4444 在部分剪辑软件有 alpha 问题，已弃用）
- **透明背景**：视频背景必须透明（供剪辑叠加）；back.png 仅 GUI 预览
- **输出倍率 render_scale（1-4×，默认 1）**：窄画布视频被放大播放会发虚/发淡（像素密度不足），
  倍率提升时画布、投影、所有固定像素尺寸同步 ×N，构图完全不变
  - `_map_render_params` 中超采样只放大 `panel`，瓦片 zoom/zf/span **保持原样**（不改用户地图配置）
  - 持久化在 .kmlproj；GUI 入口在「时长&帧率」面板，`lbl_out_size` 实时显示输出像素与建议提示
- 无进度条（硬约束）
- 轨迹配色：未走紫 `(180,100,255,200)`，已走红 `(255,70,70,230)`；线宽 `trajectory_width`（默认16，GUI 2-30），红=紫+1px
- 线宽/标记半径在视频里要乘 `trajectory_scale`（否则缩放后 GUI 与视频不一致）
- 起终点标记 r=14 白边；位置光晕三层 (22/16/10, alpha 60/120/180)

## 6. 坐标系与地图面板

- KML 轨迹 = WGS-84；高德瓦片 = GCJ-02；用轨迹包围盒中心的 GCJ↔WGS 偏移量对齐
- Web 墨卡托投影（`_lonlat_to_webmerc_px`）；瓦片 URL `webrd0{s}.is.autonavi.com ... style=8`
- 瓦片磁盘缓存 `%APPDATA%\KmlPath\tiles\`（{z}_{x}_{y}.png）
- 文字图例大小：取 zoom+delta 级瓦片同范围裁剪再缩放（`map_text_delta`）
- 地图形状：方形/圆形（alpha 遮罩裁剪，圆环描边）；运动方向箭头（绿填充白边三角，静止圆形），大小 4-80px
- 地图面板不显示轨迹；缩放 3-18 默认 15
- GUI 瓦片刷新：wheel/keyPress/scroll → 150ms 防抖 `_tile_timer`（防瓦片风暴）
- 瓦片选取依赖 m11 与 scene rect，必须在 fitInView + _update_marker_scale 之后
- 放大 ≥2× 时 z_target = base_z + floor(log2(m)) 用更高一级瓦片

## 7. 暂停模型（2026-09-23 定稿）

- `<when>` 时间轴 = **纯运动时间**（流马在暂停时删点，无位置轨迹）
- `PauseTime` 元数据单位**毫秒**；运动耗时 = 墙钟(EndTime-BeginTime) − PauseTime/1000；总耗时 = 墙钟
- 视频进度走「运动+暂停」总轴，暂停**集中**在 `pause_center`（0-1，默认 0.5，GUI「暂停位置」1-99%）
- `active_time_for(progress)` 分段函数：暂停点前 t_total；期间冻结；之后 t_total − pause_s
- 数据面板：运动耗时冻结、总耗时持续走
- format_duration **四舍五入**（截断会与两步路显示差 1 秒）

## 8. 分段模型（durations 驱动）

- `renderer.segments = (cuts[升序0-1], durations[n_cuts+1])`；**durations 是编辑对象**
- 编辑段 i 结束时间只改 durations[i]；编辑总长只改末段；加 cut 对半拆分；删 cut 合并时长；总长恒不变
- 进度语义：**点索引归一化**（idx = round(u×(n-1))），末段末帧强制 u=1.0（保证命中 cut 位置）
- 分段模式禁用主时长 spinbox；总时长 = sum(durations)，duration_s 参数被忽略
- SegmentDialog：滚轮缩放预览、点击加 cut（60px 吸附、不落在同一整数点）、右键删（合并时长）、
  **分割点数字标号**（圆半径 13px，QGraphicsTextItem 反向缩放保持屏幕尺寸）
- cut 绑定轨迹，换 KML 清空；确定后**立即持久化**；settings 存 `{"cuts":[...], "durations":[...]}`（兼容旧 list）

## 9. 关键节点

- WGS-84 坐标 `{name, lon, lat}`（兼容 名称/经度/纬度 键），与 KML 轨迹同坐标系
- 样式：黄点 r=8（颜色可调，调色板只改点）+ 半透明黑胶囊白字标签（fill alpha 115≈0.45，曾 153 被要求调淡）
- `node_font_size` 默认 41（用户工程用 26），regular 字重 msyh.ttc
- 独立编辑窗口 1100×780：高德底图、右键加点（60px 吸附轨迹）、左键删点、JSON 批量编辑、确定回写
- **不要**在主画布直接绑定右键加点（会被轨迹 item 事件拦截，历史教训）

## 10. 数据面板

- `panel_fields` 列表控制显示项（GUI 复选框），空 = 不画面板；**半透明圆角背景四项独立可调**（2026-10-18/19：「面板背景」组——`panel_bg_w` 背景宽度 60~1200px **与字号缩放解耦**（只改条的宽窄，不动文字；随 panel_scale 等比以便缩放时所见即所得）、`panel_bg_pad` 外扩边距 -50~200px 可负内收（GUI boundingRect 取「数据矩形 ∪ 背景条」并集防裁剪）、`panel_bg_color` 调色板、`panel_bg_alpha_pct` 0-100%→alpha×255/100；默认 320px 灰 (128,128,128) 45%，圆角 12×scale 无边框）。注意背景只是底色，**不裁剪文字**：宽度小于文字实际跨度时数值会溢出到条外（用户自行取舍）。边界检查/自动适配用 `_panel_span_w/_panel_span_h(r)` 取文字与背景条的较宽/较高者
- 标签 20px 粗体白字黑边（PIL stroke），值各带彩色；值紧跟标签（gap = 10×panel_scale，左流式）
- 高度 = 18 + (n-1)×32 + 26 + 30（n=5 → 202）
- 可选字段：当前海拔（实时）、累计爬升、已行里程、当前坡度、当前配速、运动耗时、总耗时
- 当前坡度（renderer.py ~282-308，`compute_slope_deg`）：单位**角度°**（2026-10-16 由 % 改为 °），对称窗口 ±3 点 arctan(高差 ÷ 水平距)，15 点平滑海拔，先按 ±60% 裁剪再转角度（≈ ±31°），显示 1 位小数 `+5.7°`；Renderer 属性 `slope_deg`（旧名 slope_pct 已废弃）
- 当前配速（~286-322）：优先 KML 逐点速度（GxTrackExtendedData），窗口时间回退；<0.3m/s 显示 `--'--"/km`

## 11. GUI 架构要点

- CanvasView + QGraphicsScene；Qt5 缩放用 `setScale(scale()*factor)` + `setTransformOriginPoint`
  （`scale(sx,sy)` 已弃用，会 TypeError + 进程崩溃 -1073740791）
- drawBackground：back.png 铺满整个预览窗口（KeepAspectRatioByExpanding，按可视区缓存），
  画布仍画蓝色边框标出输出范围；画布外深色 `#0c0c12`
- `scene.clear()` 后必须 `view.resetTransform()` + `fitInView()`，否则新内容在视口外
- 数据面板缩放：内部 `_s` 单一缩放驱动 setRect+paint（QGraphicsItem.setScale + s 乘布局会双重缩放文字）
- Ctrl+S 保存工程；`_DurSpin`（分段时长输入框）禁用滚轮和上下键
- 工程脏标记 `*`；加载时 `_loading` 抑制 `_sync` 误判
- 渲染在 QThread（RenderWorker），取消按钮深红、渲染时启用、点击后「终止中…」防重复
- 崩溃日志：faulthandler（native）+ excepthook → crash.log

### 右侧「视频参数」Dock 布局（2026-10-19 重构）

- **问题**：原来 7 个分组约 28 行 ≈ 950~1000px 高、无滚动条，小屏会被直接裁掉
- **外层套 `QScrollArea`**（widgetResizable、NoFrame）兜底；进度条 + ⛔终止 放在滚动区**外面**的底部，始终可见
- **外侧只留常用项**（现约 387px 高）：输出规格（宽度/高度）、时长 & 帧率（生成方式/时长/帧率/输出倍率/输出尺寸/总帧数）、🎨 外观与标注设置… 按钮
- **不常用项收进非模态窗口**「外观与标注设置」（`QDialog`，`_build_appearance_dialog()` 建，窗口隐藏待用；`_open_appearance_dialog()` 显示/前置），分 3 页 `QTabWidget`：
  - 数据面板 → `self._page_panel`：数据项显示（`fg`）+ 面板背景（`bg`）
  - 地图导航 → `self._page_map`：`mg`
  - 轨迹与标注 → `self._page_track`：轨迹（`tg`，轨迹粗细）+ 关键节点（`kg`）
- 各页 layout 用 `setAlignment(Qt.AlignTop)`（**不要**用 addStretch，分组是后加的会被挤到 stretch 之后）
- **非模态**才能边调边看主画布实时预览；所有 `self.<控件名>` 保持不变，settings/.kmlproj 存取逻辑零改动
- 页面容器在 `_build_dock` 开头就创建，之后再 `self._page_*.addWidget(分组)`

## 12. 工程持久化

- **.kmlproj**（JSON，每工程独立）：version 1、kml_path 引用、全部渲染参数、segments、render_scale
- **全局 settings.json**（`%APPDATA%\KmlPath\settings.json`，不在项目目录）：仅存
  `recent_projects`（10 个）、视频输出目录、default_canvas、last_project
- 启动自动打开 last_project；无则空画布提示；KML 失效提示重选并标脏
- 旧全局 settings 自动迁移为 .kmlproj（放 KML 同目录，名 = KML名+.kmlproj；不可写退到 Documents）
- 保存对话框默认目录 = 工程目录 → 上次输出目录 → 桌面
- 修改 settings.json 前先备份（曾被验证逻辑误覆盖）

## 13. 多进程渲染细节

- `mp.get_context("spawn")`；Renderer 通过 `__getstate__/__setstate` 支持 pickle
  （`_project` 闭包不可 pickle，置 None 由子进程重建投影与字体资源）
- workers=0 → cpu_count；帧数 ≤64 单进程（spawn 启动开销 2-3s）
- 中间帧 PNG `compress_level=1`；任务 chunksize=4
- 临时帧目录 TemporaryDirectory 自动清理

## 14. KML 解析要点（renderer.py parse_kml）

- 轨迹点：`.//gx:coord`（lon lat elev）；速度：`GxTrackExtendedData` 第一列 m/s
- 时间：优先 `<gx:Track><when>` 时间戳（与 coord 一一对应）；回退 Distance/SportAvgSpeed 反推
- meta：BeginTime/EndTime（毫秒）、PauseTime（毫秒）、Distance、ElevationGain/Loss、SportAvgSpeed
- 海拔平滑 `smooth_elevation`（7 点卷积）；累计爬升/距离逐点累加（haversine）

## 15. 经验教训（踩坑集）

1. QGraphicsView 会渲染 sceneRect 外的内容（letterbox 区），导致"预览完整、视频被裁"——
   drawBackground 画画布边界 + resizeEvent 重新 fitInView + 渲染前越界预警
2. 视频线宽/标记半径必须乘 trajectory_scale，否则 GUI 缩放预览与视频不一致
3. 面板缩放双重缩放问题（见 §11）
4. 硬解析状态栏字符串取路径不可靠——用专门变量 `current_kml_path`
5. 修改场景后需显式 `scene.update()` / `viewport().update()`
6. 修改 spinbox 不同步 renderer 会导致 dock 行为不一致——直接改 renderer 尺寸（`_set_size`）
7. 分段时序不一致（索引 vs 时间归一化）导致 cut 不命中——统一为点索引语义
8. 连续事件触发瓦片刷新会瓦片风暴——防抖
9. **管道方案（raw/PNG 经 stdin 喂 ffmpeg）实测均慢于落盘**——见 §4，勿重试
10. 基准测试必须先预热瓦片缓存（首次运行会被网络下载污染，401ms/帧 vs 真实 88ms/帧）
11. PowerShell 不支持 `&&`，用 `;` 分隔

## 10a. 动态坡度仪（2026-10-19 新增）

- 独立可拖动**直角三角形**坡度示意，与数据面板完全解耦（2026-10-19 三版定稿：用户先后否决旋转三角、几何角，最终要求直角三角形）；水平下边 + 右端竖直闭合边 + 斜边（倾角 = 当前坡度，上坡在基线上方），h = arm·tan(角度)（±32° 视觉限幅，近水平保留最小高度 0.02×size 防退化消失）；顶点在左 (0.10, 0.40)×size，臂长 0.55×size。
- **样式全可配**（Renderer 属性 → GUI「动态坡度仪」组 → .kmlproj 持久化）：`gauge_edge_top_color` 上边（斜边，竖直闭合边同色）、`gauge_edge_bottom_color` 下边（基线）、`gauge_fill_color`+`gauge_fill_alpha_pct` 内部填充色/不透明度 0-100%、`gauge_label_size`/`gauge_label_color`「坡度」标签字号与颜色、`gauge_value_size`/`gauge_value_color` 数值字号与颜色；文字白/彩字黑边，垂直居中并排于 0.84×size 处；GUI 用 `self._gauge_colors` dict + 5 个色块按钮（`_on_pick_gauge_color`/`_refresh_gauge_color_btns`，样式表背景即预览），样式统一经 `_on_gauge_style_changed` 同步。
- **平滑方案**：渲染前按帧密度重建点数滑动平均序列，`half = max(4, ceil(1.5 × step))`（step = 每帧跨越轨迹点数）。相邻帧窗口重叠 ≥50%，30fps/30s 下每帧角度变化均值 ~1.5°，逐帧渐进无跳变。GUI 预览用默认 half=4。
- Renderer：`show_slope_gauge`（bool，默认 True）、`slope_gauge_pos`（Tuple）、`slope_gauge_size`（float，默认 140px，范围 60-600）。方法 `_build_gauge_series(half)`、`prepare_gauge_smoothing(total_frames)`、`gauge_angle(idx)`、`_draw_slope_gauge(d, idx)`。
- app.py：`SlopeGaugeItem(QGraphicsRectItem)`：rect 作拖拽命中区，paint 完全自绘（透明底 + QPolygonF 填充三角 + 三条边线 + 黑边描边文字），滚轮调大小，hover/selected 虚线框指示边界。控件组在「数据面板」页，handlers `_on_slope_gauge_toggled` / `_on_gauge_style_changed` / `_on_pick_gauge_color`。
- 持久化：工程文件含 `show_slope_gauge`、`slope_gauge_size`、`slope_gauge_pos` 及 gauge_* 样式 8 键；_load_kml 自动创建/移除；_apply_state_to_gui 恢复控件，_apply_state_dict 恢复位置大小。

## 16. 当前状态与待办（2026-10-19）

- [x] renderer.py 已回退到「落盘 PNG + `-compression_level 1`」最终方案，py_compile 通过
- [x] app.py 取消分支注释已还原
- [x] 右侧面板重构完成（QScrollArea + 非模态三页对话框），387px 高
- [x] 面板背景 4 参数完成（宽度独立可调 / 外扩边距 / 颜色 / 不透明度）
- [x] 动态坡度仪完成（独立拖动三角 + 自适应帧密度平滑 + 持久化）
- [ ] 用户原始诉求"不降帧率提速"的最终答案 = compression_level 1（封装减半）+ 用户可对不重要的视频手动用低倍率（1×）渲染；管道方案已实测否决
- [ ] 临时文件可清理：`%TEMP%\_bench_e2e.py`、`%TEMP%\_bench_ffmpeg.py`、`%TEMP%\_verify_rawpipe.py`

## 17. 用户工程实例（E:\video\meigui\2026-10-14 10 19 59.kmlproj）

画布 620×1080、render_scale 1（建议用户 ≥2×）、fps 10、轨迹宽 6、节点字号 26、
地图圆形 zoom 18、图例 delta −1、箭头 34、面板 scale 1.32 pos (8,511)、
字段 [累计爬升/已行里程/当前配速/总耗时]、6 个关键节点（林海亭/九溪公交站/九溪烟树/龙井村/翁家山村/茶博馆）、
18 个分割点 19 段时长（合计约 1328s）
