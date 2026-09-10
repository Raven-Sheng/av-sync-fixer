# 音画同步修复器

V2 阶段更新见 [更新日志](CHANGELOG.md)；已验证范围与发布限制见 [V2 Release Readiness Report](V2_RELEASE_READINESS.md)。

本地 Python 视频工具，支持**音画同步风险诊断、多策略修复与 Bilibili 输出预设**，提供命令行和 PySide6 桌面界面。
输入一个文件，检查 ffmpeg 和 ffprobe 是否能够运行，然后显示文件名、文件大小、容器格式、总时长、视频和音频编码器、分辨率、avg_frame_rate、r_frame_rate、各轨道时长及 time_base、音频采样率。

## 环境与安装

- Python 3.11+。
- FFmpeg 和 ffprobe。当前生产审查实际验证的是 **9.0.1 essentials**；建议使用同一发行包中的两者。保持帧率路径使用 `-enc_time_base filter`，命令语法至少需要 FFmpeg **6.1**，这不是对 6.1 及所有更新版本的兼容性认证（[6.0 文档](https://github.com/FFmpeg/FFmpeg/blob/n6.0/doc/ffmpeg.texi)、[6.1 文档](https://github.com/FFmpeg/FFmpeg/blob/n6.1/doc/ffmpeg.texi)）。优先复用 PATH 中的安装；未找到时使用项目内 `tools/ffmpeg/bin/` 下的同名可执行文件（Windows 为 `.exe`）。路径相对于项目位置，与运行时工作目录无关。启动检查只验证工具可运行，不代表全部编码器、滤镜和参数都可用。
- Windows 使用原生 `.exe`。如果 PATH 命中 `.bat`/`.cmd` 包装器，会寻找原生 `.exe` 或使用项目内工具；无原生工具时明确报错。批处理在 Windows 下可能经 shell 解释文件名，因此执行层也会拒绝它们，依据 [Python subprocess 安全说明](https://docs.python.org/3/library/subprocess.html#security-considerations)。
- CLI 和核心无需第三方 Python 包；桌面界面需要 PySide6（`requirements.txt`），pytest 仅供开发测试。

Windows 可从 [FFmpeg 官方下载页](https://ffmpeg.org/download.html)列出的 [Gyan 构建](https://www.gyan.dev/ffmpeg/builds/)下载 essentials ZIP。解压后将 `bin`、许可证和说明文件放入项目的 `tools/ffmpeg/`，例如 `tools/ffmpeg/bin/ffprobe.exe`。程序不会自动下载工具或修改系统 PATH。

在项目目录创建本地虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --cache-dir tmp/pip-cache -r requirements.txt
.\.venv\Scripts\python.exe main.py "D:\视频\游戏录屏.mp4"
```

也可以激活虚拟环境后运行任务书中的命令：

```powershell
.\.venv\Scripts\Activate.ps1
python main.py "video.mp4"
```

分析并修复：

```powershell
python main.py "video.mp4" --fix
# 根据诊断自动选择策略（与上行默认行为相同）：
python main.py "video.mp4" --fix --mode safe
# 使用 Bilibili 输出预设：
python main.py "video.mp4" --fix --preset bilibili
# 只分析并预览命令：
python main.py "video.mp4" --fix --mode safe --dry-run
# 或不激活虚拟环境直接运行：
.\.venv\Scripts\python.exe main.py "samples/示例 视频.mp4" --fix --dry-run
```

若 PowerShell 的执行策略阻止激活，直接使用上面的虚拟环境解释器即可，无需修改系统策略。Linux/macOS 使用 `.venv/bin/python`。

```text
python main.py --help
```

本次环境验证已在项目中准备 `.venv`、`tools/ffmpeg/` 和一个 2 秒的合成示例，可直接试运行（这些本地文件不纳入版本控制）：

```powershell
.\.venv\Scripts\python.exe main.py "samples/示例 视频.mp4"
```

含空格路径请使用引号。支持中文、Unicode、相对路径和绝对路径；Windows 的标准输出和错误输出统一使用 UTF-8。成功退出码为 0；文件、工具或探测错误为 1；参数错误为 2；用户中断为 130。

## 桌面 GUI

选择视频后会显示媒体兼容性摘要（视频编码、尺寸、帧率、像素格式、颜色和全部音轨），并根据当前模式与预设展示核心 RepairPlan 的处理建议。点击“为什么需要修复？”查看原因；完整画像、时间基、PTS/DTS 和计划决定放在默认折叠的“技术详情”。预览仅解释已有元数据，转码前仍由后台线程重新探测；核心拒绝的输入会提前显示原因并禁用开始修复。

修复结果并列显示实际 Before/After，并展示同步、颜色、编码、音频、平台兼容五项验证状态。缺少分项证据显示 NOT TESTED，WARNING 与 FAIL 不会改写为 PASS。同步 PASS 表示时间信息满足输出计划，不证明声音和画面内容对齐。Steam 提示仅在软件/编码器元数据明确提及时显示“可能来自 Steam”，不根据文件名、HEVC 或 Full Range 猜测来源。

轨道列表不完整时，各摘要字段和处理建议会标明信息未知，不直接断言原文件无音轨。已有深度分析结果时，帧率说明会区分抽样帧间隔与元数据 FPS 的证据；抽样没有发现变化也不代表整段已确认 CFR。窗口暂时隐藏不会丢失输出验证失败的状态。

安装依赖后，在项目目录启动：

```powershell
python gui.py
# 或直接使用项目虚拟环境：
.\.venv\Scripts\python.exe gui.py
```

1. 点击“选择视频”，支持 MP4、MKV、MOV、WebM。选择后自动在后台分析。
2. 摘要显示编码、分辨率、平均 FPS、像素格式、颜色和音频。标称 FPS、疑似 VFR、轨道时长、长度差及完整风险依据可在“技术详情”查看。缺失字段显示“未知”，缺失音轨明确提示。
3. 选择修复模式：**自动 → safe、CFR → cfr、时间戳 → timestamp、音频同步 → audio-sync**。没有音轨时音频同步模式不可执行。
4. 选择输出预设：**通用 → general（默认）、Bilibili → bilibili**。预设与修复模式可以组合；Bilibili 始终启用 CFR。
5. 点击“开始修复”，可在技术详情查看日志；完成后显示实际修复前后对比、五项验证状态及输出路径。完整输出验证报告保留在日志中，可点击“打开文件所在目录”。输出规则与 CLI 相同。

分析、工具检查、FFmpeg 转码和输出复查都在 `QThread.run()` 中调用既有核心，使用 Qt 信号将日志、进度、结果和错误交回主线程；后台线程不访问控件。依据 [Qt QThread 文档](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html)。修复开始时清除旧诊断并重新分析输入，避免沿用选择文件后已经过时的元数据；重新分析失败时禁用修复按钮，重新选择有效文件后恢复。

进度解析集中在 `ffmpeg_utils.progress_seconds()`，支持传统 `time=HH:MM:SS.xx` 和机器输出 `out_time` / `out_time_us`。现有转码继续使用 `-progress pipe:1` 的机器输出；GUI 接收已处理秒数，以容器总时长（缺失时回退到已知轨道时长）估算百分比，不重复解析 FFmpeg 日志。CLI 和 GUI 共用 `progress_percent()`，兼容未知及异常数值，运行进度最多显示 99%；100% 仅在转码、复查和安全发布全部成功后显示。时长未知时 GUI 显示活动进度条及已处理秒数。

没有 FFmpeg/ffprobe 时分别显示“未检测到 FFmpeg”或“未检测到 ffprobe”和安装位置提示，可以准备好工具后重新选择文件。其他错误显示在状态栏与处理日志中，不向用户弹出 Python traceback。Windows 子进程统一使用 `CREATE_NO_WINDOW`，避免从 GUI 探测或转码时弹出额外控制台。

当前 GUI 每次处理一个文件；任务运行时禁用重新选择与重复修复，暂不提供取消或批处理。为避免销毁运行中的线程，处理过程中关闭窗口会被暂时阻止并提示等待，界面仍可响应；任务完成后可关闭。日志最多保留 1000 行。GUI 采用系统原生控件，没有自定义主题或动画。

## 输出解释与边界

- 文件大小来自本地文件状态，以字节和 MiB 显示，不依赖 ffprobe 是否返回 `size`。总时长使用容器 `duration`；缺失时显示“未知”。
- 编码器一栏展示 ffprobe 的 `codec_name`（如 h264、aac），表示编码格式，不能据此确定原始编码软件。音频采样率使用 `sample_rate`，以 Hz 显示；缺失或无效时显示“未知”。
- 每条音频、视频轨道单独列出，使用 ffprobe 原始轨道编号；视频流总数包含封面，另列可处理视频和封面数量，封面不参与同步诊断与修复。轨道列表完整且无音轨或无视频轨时明确显示“无”；列表信息不足时说明未知。
- 视频帧率分别显示 `avg_frame_rate` 与 `r_frame_rate`，包含 fps 小数及精确分数。字段缺失、`0/0` 或无效值显示“未知”。两字段不同并不足以确认 VFR。
- 轨道时长优先读取轨道 `duration`；缺失时尝试 `duration_ts × time_base` 并标注来源。仍不可用时显示“未知”，不使用容器时长代替轨道时长，也不将未知值当作 0。
- MKV/WebM 等文件可能仅提供容器时长，因此轨道时长显示“未知”是预期行为。快速模式不逐帧扫描，也不从 metadata 标签猜测轨道时长；深度模式另外报告已扫描的帧时间戳范围。
- 容器格式可能显示多个别名，例如 `mov,mp4,m4a,3gp,3g2,mj2`，这是 ffprobe 返回的格式名称。
- 工具启动检查超时为 10 秒，媒体探测超时为 60 秒。损坏、无法读取或超时会输出错误，不向正常用户显示 Python traceback。
- CLI 不带 `--fix` 时只分析。诊断仅根据元数据提示潜在同步风险，不能确认实际音画不同步；源视频始终不被覆盖。

## V2 第一阶段：输入媒体画像

CLI 的分析报告和 GUI 的处理日志共用 `app/media_profile.py`，展示输入的编码、颜色、帧率、音频和时间信息。直接运行 `python main.py "video.mp4"` 即可查看，不会转码。

本阶段继续使用现有 ffprobe JSON 调用，扩展模型和解析，不改变修复策略、FFmpeg 转码参数或输出验证规则。

| 范围 | 读取的字段 |
| --- | --- |
| 视频编码与几何 | `codec_name`、`codec_long_name`、`profile`、`level`、`pix_fmt`、`width`、`height`、`sample_aspect_ratio`、`display_aspect_ratio` |
| 视频帧率与时间 | `r_frame_rate`、`avg_frame_rate`、`time_base`、`start_pts`、`start_time`、`duration_ts`、`duration`、`nb_frames` |
| 视频颜色及附加信息 | `color_range`、`color_space`、`color_transfer`、`color_primaries`、`chroma_location`、`bits_per_raw_sample`、`field_order`、`side_data_list` |
| 音频 | `codec_name`、`profile`、`sample_fmt`、`sample_rate`、`channels`、`channel_layout`、`time_base`、`start_time`、`duration`、`bit_rate` |
| 容器 | `format_name`、`duration`、`start_time`、`bit_rate`、`tags` |
| 轨道组织 | 视频/音频数量、字幕、封面、其他流、各轨 `disposition.default`、`tags`、旋转角度及来源 |

为兼容 V1，模型继续用 `codec` 表示 `codec_name`、`pixel_format` 表示 `pix_fmt`、`container` 表示 `format_name`。新增容器属性为 `container_start_time`、`container_bit_rate`，容器和轨道标签保存在 `metadata`。

每个解析字段同时保存 `probe_fields` 证据：`state` 区分以下情况，`raw` 保留原始值，`present` 表示字段是否存在。原有可选类型仍用 `None` 表示没有可用的规范化值，调用方可以进一步检查证据区分原因。

| 状态 | 含义 |
| --- | --- |
| `missing` | 字段缺失，未知 |
| `empty` | 字段存在，但为 `null`、空字符串或空集合 |
| `unknown` | ffprobe 明确给出 `N/A` / `unknown`；位深字段的 `0` 也表示未指定 |
| `invalid` | 字段存在但不能按预期类型解析，例如 FPS 为 `0/0` |
| `value` | 存在有效值，包括合法的零起点、零时长和非默认轨标记 |

例如 `stream.probe_fields["color_range"].raw` 保留原始标记，`stream.color_range_name` 则将 `pc` / `jpeg` 统一显示为 **Full**，将 `tv` / `mpeg` 显示为 **Limited**。这只是输入解释，不执行颜色范围转换，也不根据 `yuvj420p` 猜测缺失的范围标签。

`parse_rational()` 使用 `Fraction` 保存 FPS 和时间基的精确值，兼容 `60000/1001`、`60/1`；零分母、非正值和无效类型不会引发崩溃。宽高比额外接受 `1:1` 等冒号形式。帧率置信度说明当前证据是缺失、只有一个有效字段，还是仅能比较平均/标称 FPS；不会把两字段接近当作 CFR 已确认。

旋转优先读取 Display Matrix 的有效 `rotation`，其次读取旧 `tags.rotate`，保留原始符号及两处原始证据；未提供有效角度时不假定为零。默认轨道标记只展示，不改变 V1 的轨道选择。

轨道列表存在未知、无效或未识别的类型时，不据此断言某类轨道不存在。字幕、视频和音频的缺席判断共用列表完整性检查；已经解析出的字幕仍明确显示存在，空列表或只有已知数据/附件流的完整列表则可以确认没有音视频轨。

元数据可能缺失或标注不准确。原始画像不从像素格式回填缺失位深，不解析码流以确认位深或 HDR，也不通过像素统计确认实际颜色范围；没有逐帧扫描时不能可靠确认 CFR/VFR、丢帧和中途时间戳异常。轨道时长和起点仍不能证明实际内容同步。

## V2 第二阶段：输入兼容性诊断

媒体画像和原有同步报告后追加 **Input Compatibility Report**；GUI 在日志中显示同一份报告，切换输出预设时根据已有画像重新解释，无需再次探测或转码。

职责保持分离：`analyzer` 读取事实，`compatibility` 解释风险，`fixer` 决定转换。新增诊断不被 fixer 或输出 validator 消费，不修改 FFmpeg 参数，也不因为诊断等级自动禁止、开启或组合修复策略。

| 等级 | 含义 |
| --- | --- |
| `INFO` | 事实、常见特征或未发现该项明显线索；不表示文件已全面验证通过 |
| `WARNING` | 信息不足、证据冲突或需要核实的兼容性风险 |
| `REPAIR_RECOMMENDED` | 建议评估专项处理；不是已实施的修复，也不是必须转换的指令 |
| `UNSUPPORTED` | 当前处理流程没有相应能力，例如受控 HDR→SDR 或没有可处理视频轨；不代表输入损坏或 FFmpeg 无法解码 |

规则覆盖如下：

| 输入特征 | 诊断解释 |
| --- | --- |
| HEVC/H.265、H.264 | 合法常见编码，`INFO`；未运行解码能力测试，未知或其他编码只警告，不直接拒绝 |
| 8-bit、10-bit 及更高位深 | 8-bit 为 `INFO`，高位深提示当前 8-bit 输出的精度损失；位深证据冲突时 `WARNING` |
| yuv420p、yuvj420p、4:2:2、4:4:4、RGB 等 | yuv420p 为常见输出格式；其余提示采样/范围/精度兼容性。**yuvj420p 是合法表示，不等于损坏** |
| Full / Limited Range | Full 为 `REPAIR_RECOMMENDED`，Limited 为 `INFO`；yuvj 与 Limited 标签冲突或范围未知时提醒核实 |
| BT.709、BT.2020 | 一致 BT.709 矩阵/原色为 `INFO`；BT.2020 提示广色域处理边界，不单独判为 HDR |
| HDR / SDR 线索 | PQ/HLG 标记提示当前受控 HDR 转换能力 `UNSUPPORTED`；HDR 附加信息而无一致传递标记时仅警告；SDR 传递标记也只是线索 |
| VFR 风险 | 比较实际平均/标称 FPS，复用 0.1 FPS 容差；明显差异建议评估 CFR，接近不等于确认 CFR |
| 起点差、时长差 | 每条音轨与第一条非封面视频比较，复用现有阈值；起点差超过 0.05 秒、长度差达到 0.2 秒时建议评估处理 |
| 负时间戳 | 检查容器/各轨 start_time 及可用 start_pts；负值不等于损坏，未检查中途 PTS/DTS 或负 DTS |
| 多视频、多音频 | 提醒当前仅输出首条非封面视频、保留全部音轨，以及播放/上传端的音轨选择问题 |
| 非 48 kHz 音频 | 通用预设警告但保留合法原采样率；Bilibili 预设提示重采样需求 |
| 非常见声道布局 | mono/stereo 与声道数一致为 `INFO`；缺失、冲突或其他布局为 `WARNING`，5.1/7.1 等仍是合法布局 |
| 旋转元数据 | 有角度或 Display Matrix 时提醒核对方向；不把缺失角度当作零，也不把单个角度当作完整矩阵变换 |

位深解释只使用有效 `bits_per_raw_sample` 或已知像素格式的分量位深，记录 `source` 和两处证据；不会根据 `Main 10` 字样猜测实际位深，不修改 analyzer 的缺失字段。不能将 RGB24 的每像素 24 位误当作每分量 24-bit。

精度变化按现有输出像素格式的位深评估，并记录目标格式和目标位深：低位深扩展不会被误报为精度降低，也不会增加原始细节；只有输入位深高于目标时才提示降精度风险。目标位深未知时保持警告，不默认按 8-bit 判断。

Full Range 提示的含义是：面向 Limited 输出需显式转换范围，不能只改标签。本阶段新增独立 `ColorConversionPlan` 完成受支持 SDR YUV 输入的范围转换；兼容性诊断仍不直接决定命令。HDR、BT.2020、10-bit、Full Range 是不同特征，不能互相等同。范围/像素格式语义依据 [FFmpeg 像素格式定义](https://ffmpeg.org/doxygen/trunk/pixfmt_8h_source.html)，色调映射属于独立处理，参见 [FFmpeg tonemap 文档](https://ffmpeg.org/ffmpeg-filters.html#tonemap)。

兼容性诊断本身不调用 subprocess，复用已有 `AnalysisResult`。下面示例中只有 `analyze()` 负责探测输入：

```python
from dataclasses import asdict
import json
from app.analyzer import analyze
from app.compatibility import diagnose_compatibility, format_compatibility_report

source = analyze("video.mp4")
report = diagnose_compatibility(source, preset="bilibili")
print(format_compatibility_report(report))
print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
```

每项包含 `code`、`severity`、`summary`、`reason`、`evidence`、`scope` 和 `stream_index`。`scope` 使用从零开始的分类内位置（如 `audio:1`），`stream_index` 保留 ffprobe 原始编号；证据保留字段状态和原值，缺失与空值不混淆。不产生单一“通过/不通过”结论。

CLI 不带 `--fix` 时按通用预设解释；原有 `--preset` 仍须配合 `--fix`。需要只查看 Bilibili 相关报告及原有命令预览时，运行 `python main.py "video.mp4" --fix --preset bilibili --dry-run`。本阶段没有新增 CLI 参数。

## 音画同步诊断

原有媒体信息下方会追加诊断报告，包含实际 FPS、帧率模式、轨道长度差、起始时间差、风险等级、潜在原因及信息限制。

- `avg_frame_rate` 和 `r_frame_rate` 会转换为浮点 FPS 后比较。绝对差 **大于 0.1 FPS** 时提示“疑似可变帧率 VFR”；例如 `60/1` 与 `120/2` 数值相等，`60000/1001` 与 `60/1` 的差也在容差内。未触发提示不等于已经确认 CFR。
- 音视频长度差为两条轨道时长的绝对差。同步风险等级暂时**仅按长度差**划分，VFR 与起始偏移独立列为提示，不进行未经定义的综合评分。

| 轨道长度差 | 风险等级 |
| --- | --- |
| 小于 0.05 秒 | 低 |
| 大于等于 0.05、小于 0.2 秒 | 轻微 |
| 大于等于 0.2、小于等于 0.5 秒 | 中等 |
| 大于 0.5 秒 | 较高 |

- 起始时间直接读取轨道 `start_time`（单位为秒），保留合法负值。绝对差 **大于 0.05 秒** 时提示明显起始偏移。`time_base` 继续按轨展示；两轨的 time_base 不同本身不代表异常。
- 帧率、时长和起始时间阈值均集中定义在 `app/analyzer.py` 顶部。差值通过十进制相减计算；阈值比较还统一使用 `1e-9` 的绝对舍入容差（对应秒或 FPS），将此范围内的差异视为等于阈值，避免分数 FPS 和 `duration_ts × time_base` 转浮点后的舍入误差造成误判。此数值容差与业务风险阈值分别定义。
- 没有音频/视频轨或轨道时长不足时，长度差及风险等级显示“未知”，不当作低风险。缺失的帧率和起始时间会另行说明。
- 多轨文件保留全部轨道元数据，诊断只比较第一条非封面视频轨与第一条音频轨，并显示所选轨道编号和范围限制。
- 长度差可能源于尾部长度不同，起始偏移可能为固定偏移。快速模式不扫描帧/包时间戳；可使用 `--deep-analysis`，但仍没有声音与画面内容比对，因此不能仅凭元数据线索确认累计漂移；低长度差风险也不保证实际播放同步。

调用方式依据 [ffprobe 官方文档](https://ffmpeg.org/ffprobe.html)，通过 `subprocess.run` 传入参数列表，不经过 shell 拼接：

```text
ffprobe -v quiet -print_format json -show_format -show_streams <video_path>
```

`quiet` 模式可能抑制错误日志；调用失败时仍检查退出码并展示可读提示。

## 修复模式

`--fix` 默认使用 **safe 模式与 general 预设**。通用预设需要强制 CFR 时，请显式指定 `--mode cfr`。`--mode`、`--preset` 和 `--dry-run` 都必须与 `--fix` 一起使用。

| 模式 | 用途和行为 |
| --- | --- |
| `safe` | 根据诊断动态组合下面的策略，报告选择依据；无专项风险线索时只做兼容性转码。 |
| `cfr` | 使用 `fps` 滤镜补帧/丢帧，生成稳定帧间隔；适合疑似 VFR 或明确需要固定帧率的剪辑流程。 |
| `timestamp` | 补充可推导的缺失 PTS、归一化起点并消除开头负时间戳，保留原有帧间隔。 |
| `audio-sync` | 按音频采样数与音频 PTS 的偏差补缺口或裁重叠；适合音频采样与时间戳逐渐偏离的情况，保留视频帧间隔。需要音轨。 |

上表描述通用预设。显式指定 `cfr`、`timestamp` 或 `audio-sync` 时开启对应专项策略；组合 Bilibili 预设时，还会强制 CFR。所有模式都会重新编码，不直接复制流。

```powershell
python main.py "video.mp4" --fix --mode cfr
python main.py "video.mp4" --fix --mode timestamp
python main.py "video.mp4" --fix --mode audio-sync
python main.py "video.mp4" --fix --mode audio-sync --dry-run
```

### safe 的选择规则

- 第一条非封面视频轨疑似 VFR（实际 FPS 差大于 0.1）：开启 CFR。
- 长度差只用于风险提示，不再据此自动开启 async。只有 `--deep-analysis` 提供完整、稳定且不超过 **500 ppm** 的 PTS／采样时钟漂移证据时，才为对应音轨选择有限软补偿。
- 所选轨道存在负起点、起点缺失，或音视频起点差大于 0.05 秒：开启 timestamp。起点不同可能是正常剪辑偏移，保留已知相对偏移，不盲目对齐声音内容。
- 信息未知时不凭空判断 VFR 或长度差；相关限制会显示在报告中。只有多个条件同时触发时才组合多个策略，不会始终加入全部参数。

普通诊断报告仍比较第一条视频/音频轨；safe 的音频选择会逐轨检查并列出启用同步的音轨序号。

### CFR 与目标帧率

目标 FPS 由 `app/fixer.py` 中的 `select_target_fps()` 单独选择：优先有效的平均 FPS，其次标称 FPS，再从 **24、25、30、50、60、120** 中取最近值；等距离取较低值。29.97→30、59.94→60、119.88→120；都未知时使用默认 30 FPS 并在命令行提示。未启用 CFR 时不选目标 FPS。

视频先归一化起点，由 `fps` 滤镜沿原播放时间轴补/丢帧，再按输出帧序号重建时间戳。后级使用 `-fps_mode:v passthrough` 保留滤镜结果，避免重复同步把晚开始的视频补帧至零。未启用 CFR 时使用 `-enc_time_base:v filter` 保留滤镜的时间基，避免编码器按平均 FPS 量化时间戳。参见 [fps 文档](https://ffmpeg.org/ffmpeg-filters.html#fps)、[setpts/asetpts 文档](https://ffmpeg.org/ffmpeg-filters.html#setpts_002c-asetpts)和 [FFmpeg 视频选项](https://ffmpeg.org/ffmpeg.html#Video-Options)。

### timestamp 参数依据

- 输入选项 `-fflags +genpts`：在 DTS 可用时生成缺失 PTS；它不保证纠正已经存在的错误 PTS。
- 用 `setpts/asetpts` 移除轨道绝对起点，并恢复已知的相对偏移；保留中间帧间隔。
- 输出选项 `-avoid_negative_ts make_non_negative`：必要时整体平移各轨，消除开头负 PTS/DTS。编码器延迟也可能产生负 DTS，因此该选项保留在 timestamp 策略中。输出首个 PTS 不一定等于零。
- 不添加 `-start_at_zero`：它配合 `-copyts` 才有意义；当前流程通过滤镜归一化起点，不保留原绝对时间戳。不使用 `make_zero` 对已有非负起点再做额外归零。

`genpts` 和 `avoid_negative_ts` 仅在 timestamp 被选中时加入。它们不能修复所有中途不单调时间戳或恢复丢失内容。依据 [FFmpeg 容器选项](https://ffmpeg.org/ffmpeg-formats.html#Format-Options)与 [copyts/start_at_zero 文档](https://ffmpeg.org/ffmpeg.html#Advanced-options)。

### audio-sync 的处理边界

未提供深度分析时，显式 `audio-sync` 沿用 `aresample=async=1:min_hard_comp=0.1:max_soft_comp=0`，常量集中在 `app/ffmpeg_utils.py`。已有深度证据时，`audio-sync` 和 safe 一样遵守下文的微小补偿门槛，不能通过切换模式绕过证据不足或较大漂移限制。旧参数的用途是：

- 保留输入音频 PTS 间隔供滤镜与采样数比较，不在滤镜前用 `asetpts=N/SR/TB` 抹掉偏差。
- `async=1` 采用补静音/裁重叠；禁用软伸缩，不使用 `asetrate` 或按两轨总时长计算变速比例。保留声音片段的音调，但较大的缺口/重叠修补可能可闻，AAC 转码本身也有损。
- 偏差不足 0.1 秒时避免频繁硬补偿；不设置 `first_pts=0` 强制抹掉合法起始偏移。
- 音频 PTS 与采样数一致时，即使音视频总长度不同，也不为追齐视频长度而拉伸或裁短音频。

参数含义见 [FFmpeg 重采样文档](https://ffmpeg.org/ffmpeg-resampler.html#Resampler-Options)。**如果两轨各自的时间戳与内容内部一致，只是录制时钟速率不同，async 无法单靠这些元数据推断正确速度。** 因此该模式不能保证修复所有“越播偏差越大”的视频；仍需播放确认实际效果。

## Bilibili 输出预设

用于生成稳定、兼容性高、便于视频平台再次转码的文件，不代表平台官方限制清单，也不绕过上传限制。

```powershell
python main.py "input.mp4" --fix --preset bilibili
python main.py "input.mp4" --fix --preset bilibili --dry-run
python main.py "input.mp4" --fix --mode timestamp --preset bilibili
```

| 项目 | 通用 `general`（默认） | `bilibili` |
| --- | --- | --- |
| 容器 | MP4，faststart | MP4，faststart |
| 视频 | libx264，medium，CRF 18，yuv420p | 相同 |
| 帧率 | 根据修复模式决定是否 CFR | 始终 CFR，复用现有目标 FPS 选择函数 |
| 音频 | 全部音轨 AAC，192 kbps，保留采样率 | 全部音轨 AAC，192 kbps，48 kHz |
| 分辨率 | 奇数宽高补最多 1 像素黑边 | 严格保持原编码宽高，不缩放、裁剪或补边 |

预设决定输出规格，模式决定专项处理。`safe + bilibili` 在强制 CFR 的基础上，仍只按诊断为对应音轨启用 async、按起始时间线索启用 timestamp；不会始终打开全部修复参数。音轨采样率转换不使用 `asetrate` 或修改播放速度。无音轨输入保持无音轨，不额外生成静音。

**奇数宽高无法同时满足原尺寸与 libx264/yuv420p 编码要求**，Bilibili 预设会在转码前明确报错；原分辨率缺失时也会提示无法验证。带旋转标记的输入使用 `-noautorotate` 保留存储宽高，并由 MP4 旋转元数据保持显示方向，避免自动旋转像素改变编码宽高。依据 [FFmpeg 视频选项](https://ffmpeg.org/ffmpeg.html#Video-Options)。

现有的 `-movflags +faststart` 适合本工具先完成本地 MP4 再播放或上传的流程，因此保留：FFmpeg 在写完后将 moov 索引移到媒体数据之前，让支持逐步下载的播放器较早读取索引；这不是修复同步的参数，也不保证平台免转码。依据 [FFmpeg MP4 muxer 文档](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv)。

### 转码后验证

V2 在生成 `RepairPlan` 时同步生成不可变的 `ExpectedOutputSpec`，包含目标编码、FPS 策略、像素格式、颜色范围和已知色彩标记、尺寸、逐音轨采样率、时长基线、相对起点、封装及验证容差。`app/output_validation.py` 比较实际输出与该规格；通用和 Bilibili 共享这条验证路径。输入的 HEVC、yuvj420p、Full-range 特征不作为输出失败依据。

发布前检查文件存在且非空、ffprobe 可读取、视频/音轨数量、编码、像素格式、颜色范围、尺寸、FPS、逐轨采样率和时序。要求 faststart 时，只读取 MP4 顶层 box 头并 seek 跳过媒体数据，检查 `moov` 在 `mdat` 之前；支持 64-bit box size，限制扫描 box 数量，不加载整个视频。

通用预设未显式指定 `-ar` 时，规格遵循原生 AAC 编码器的采样率协商：支持的输入率保持，否则选最近的支持值，等距时沿用编码器顺序。例如 192 kHz → 96 kHz、10 kHz → 11.025 kHz；Bilibili 仍明确要求 48 kHz。采样率能力表集中在 `repair_plan.py`，与本机 `ffmpeg -h encoder=aac` 的集成测试对照；没有为通过验证而接受任意采样率。依据 [FFmpeg AAC 采样率表](https://ffmpeg.org/doxygen/2.3/aacenc_8c.html) 和 [采样率协商实现](https://ffmpeg.org/doxygen/trunk/avfiltergraph_8c_source.html)。

验证按 `Video / Audio / Color / Timing / Compatibility` 汇总为 `PASS / WARNING / FAIL`，CLI 和 GUI 展示相同的 `Post Repair Report`，包含实际值、计划要求、容差和不符合项。任一 FAIL 阻止最终文件发布并清理临时输出；WARNING 可发布，但明确列出未确认项。文件缺失或 ffprobe 失败也会先生成失败报告。

- 目标 codec、pix_fmt、采样率、范围不符，或已知颜色标记发生冲突：FAIL。输出只有 yuv420p、却缺少 range 证据，仍不能认定 Limited，必须 FAIL。
- 输出像素格式和 Limited 范围已确认，但矩阵、传递特性或原色标记缺失：WARNING；不把缺失直接等同于错误，也不声称已经验证像素数值。输入没有指定的颜色体系不会凭空要求 BT.709。
- 逐轨时长发生明显变化、非正或无效时长、容器时长与输出轨道时间轴明显矛盾：FAIL。时长字段缺失且无法核对：WARNING；明确无效的原始时长或起点字段为 FAIL。两轨同时被截短也不会因为长度差为零而通过。输出容器与输出轨道的一致性独立检查；输入视频时长未知时，若计划省略了其他视频、字幕或数据轨道，不会把完整输入容器长度当作输出长度要求。
- 音视频长度差比输入明显恶化：FAIL。例如 `0.2 秒 → 3 秒`，即使所有编码字段正确也不发布。原有且未恶化的差异达到 0.2 秒仍为 WARNING，不自动截断或拉伸来隐藏问题。
- 时间戳验证对照计划的相对起点，允许编码边界误差和 timestamp 模式的共同封装平移；未经计划的相对偏移：FAIL。起点未知：WARNING。

单轨时长容差取 `max(0.1 秒, 两个视频帧间隔, 两个 AAC 帧时长)`；长度差恶化容差在此基础上最多允许 0.5 秒，避免低 FPS 掩盖秒级同步退化。相对起点容差为 0.05–0.1 秒。软补偿计划对音频单轨时长额外允许其计划补偿上限对应的变化，但不放宽长度差恶化检查。所有阈值在规格生成时确定，并显示在报告中。

CFR 复查平均/标称 FPS 均匹配目标；保持帧率模式检查平均 FPS 是否在取整容差内。生产输出验证不额外逐帧解码，不能证明实际画面/声音事件同步，也不能仅凭颜色标签证明像素转换正确；真实 FFmpeg 集成测试继续检查帧间隔、解码像素、音调和内容时长。

预设不会改变输出命名规则：`output/<原文件名>_fixed.mp4`。若已有同名输出，请先自行重命名或移走；程序始终拒绝覆盖。

## 共同编码、输出与预览

参数统一由 `app/ffmpeg_utils.py` 的 `build_fix_command()` 构建：

- 视频：`libx264`、`preset medium`、`crf 18`、`yuv420p`。通用预设对奇数宽/高在右侧/底部补最多 1 像素黑边；Bilibili 预设严格保持原尺寸，相关冲突见上文。
- 音频：全部音轨编码为 AAC、每轨目标码率 192 kbps，保留声道数。通用预设保留采样率，Bilibili 统一为 48 kHz。不添加 `-shortest`，不按视频长度截断音频；AAC 编码边界可能有少量填充误差。
- 专项处理需要归一化起点时，所有起点已知则保留相对于最早轨道的偏移；部分起点未知则各轨从零归一化并提示限制。
- MP4 容器开启 `+faststart`。处理第一条非封面视频轨及全部音轨；额外视频轨、字幕、附件和章节不写入输出。

多视频轨输入会在 CLI 和 GUI 日志中明确提示其余视频轨不会导出。多音轨仍逐轨编码和保留，不自动混音；播放器或上传平台如何选择音轨不由本工具控制。

`--fix` 会先显示分析报告、选择策略及依据，再显示输入/输出路径、目标 FPS、实际 FFmpeg 命令和转码进度。转码后重新调用 ffprobe，检查视频编码、音轨数量/编码；启用 CFR 时还检查 FPS。最终展示输入与输出的视频时长、音频时长、时长差及平均/标称 FPS。

`--dry-run` 仍检查源文件、工具可用性并调用 ffprobe 分析输入，随后仅显示策略与命令；**不转码、不创建输出目录或文件、不进行输出复查**。预览命令使用最终输出路径；真正执行时只把最后的输出路径参数替换为临时文件，其余参数相同。已有同名输出也可以预览，但实际执行会拒绝覆盖。

Windows 命令预览采用 PowerShell 语法（`&` 加单引号参数），其他平台采用 POSIX shell 语法；滤镜及包含空格、引号、`$` 等字符的路径按字面值传递，规则见 [PowerShell 引号文档](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules)。程序内部仍使用 subprocess 参数列表执行，不通过 shell。手动运行预览命令会直接写最终路径，不包含 Python 的临时文件发布及复查流程。

输出固定放在**项目根目录**的 `output/<输入文件 stem>_fixed.mp4`，与启动命令所在目录无关。若目标已存在，直接提示并退出，不覆盖，也不自动重复转码。

转码先写到 `output/.avsync-.../` 临时目录，输入帧检查及输出复查通过后发布最终文件。Windows 使用同文件系统的 `os.rename`，已有同名目标时包括并发创建均拒绝覆盖，不再要求硬链接；其他系统仍使用排他创建的硬链接。相关行为依据 [Python os.rename 文档](https://docs.python.org/3/library/os.html#os.rename)。本项目实际测试盘为 Windows NTFS，尚未在真实 FAT/exFAT/网络盘上回归；取消对硬链接的依赖不等于认证这些文件系统，FAT32 的文件大小限制仍存在。临时目录在成功、失败和 Ctrl+C 取消后清理，失败不会留下冒充成功的最终文件。强杀/断电可能留下临时目录；大型视频需要足够空间存储重新编码后的文件。

进度使用 FFmpeg 的 `-progress pipe:1`；已知总时长时显示估计百分比，未知时显示已处理秒数。100% 仅在转码、输出复查和发布全部完成后显示。stderr 由独立读取线程持续排空，失败时显示末尾关键内容；Ctrl+C 会停止子进程并清理临时输出。

全部修复模式启用 `-xerror`：遇到解码或损坏包错误就失败并清理临时输出，避免 FFmpeg 容错跳过内容后仍返回成功。已用“保留 MP4 头部、截断媒体数据”的样本验证。该选项不改变正常修复策略，也不表示能检测所有内容缺失；严格失败意味着本工具不用于尽可能抢救损坏视频。参数依据 [FFmpeg 文档](https://ffmpeg.org/ffmpeg.html#Advanced-options)。

强制结束进程或断电不会保证执行 Python 清理代码，可能残留临时目录；强杀 GUI 时子进程也可能尚未结束。确认没有相关任务运行后再手动处理残留文件，程序不自动删除来源不明的临时目录。

修复后时长差可能仍存在，元数据复查通过不代表已确认实际音画同步。HDR 色调映射和其他高级色彩管理不在当前范围内。

生产审查补充：快速探测在 stream metadata 之外，只解码开头最多 32 个 packet 的有限帧，提取 transfer 和 HDR side-data 类型，避免把只存在于帧 SEI 的 HDR 信息漏掉。抽样原值与 stream 字段分别显示，未观察到不代表全片没有 HDR。转码时使用 `showinfo@avsync_input=checksum=0` 在颜色/CFR 滤镜之前逐帧输出属性，由后台读取线程检查其与 RepairPlan 的一致性；只保存基线、计数和有界日志，不构造全片帧列表、不二次全片解码。发现中途范围、色彩体系、像素格式或尺寸变化，以及 HDR 证据时停止并清理临时输出；记录缺失或无法解析同样不能发布。此检查只核对解码器报告的属性，无法证明错误标记像素的真实颜色，也不测量声音与画面内容是否同步。

### V2：Full Range / yuvj420p 颜色处理

V1 的 `-pix_fmt yuv420p` 约束像素布局，却没有完整指定数值范围变换和颜色标记。Full 范围可能继续被编码、标记为 `pc`，ffprobe 因而仍报告 `yuvj420p`；原验证器拒绝这种输出是正确的。只把范围标签改成 Limited 也会让播放器用错误的范围解释数值。

现在通用与 Bilibili 预设共同要求 **yuv420p + Limited**。`app/color.py` 根据输入事实选择独立颜色策略；四种同步模式保持原来的选择逻辑，CLI 和 GUI 共用计划与验证报告。

| 输入证据 | 颜色策略 |
| --- | --- |
| 已知 Full YUV，包括 `yuvj420p / pc`、`yuv420p / pc` | 显式执行 Full → Limited 数值转换，再同步帧和码流标记 |
| 已知 Limited，包括 `yuv420p / tv` | 不添加范围转换的 scale，避免重复压缩黑白位；保留正确的 Limited 标记 |
| 缺失 range，但像素格式为已知 `yuvj*` | 根据该格式的 Full 语义制定转换计划，记录证据来自 pix_fmt；原始画像不回填字段 |
| 其他未知/无效范围，或 `yuvj* + Limited` 矛盾标签 | 转码前明确停止，不根据分辨率猜测、不默认 Full/Limited；分析功能仍可用 |
| PQ/HLG、无法可靠处理的颜色标记、未知像素格式或非受支持 YUV 输入（包括 RGB） | 明确停止；Full/Limited 标签均不能代替像素格式检查，不冒充已经完成 HDR 色调映射或 RGB 矩阵转换 |

在本次验证环境 FFmpeg **9.0.1** 中检查了 `scale`、`zscale`、`format`、`setparams` 和 `h264_metadata` 的可用性。此任务不需要色域转换或色调映射，采用 `scale` 显式设置输入/输出范围即可，无需额外依赖 zscale。对于已知 BT.709 Full YUV，颜色部分为：

```text
scale=w=iw:h=ih:in_range=full:out_range=limited:in_color_matrix=bt709:out_color_matrix=bt709,
format=yuv420p,
setparams=range=limited:colorspace=bt709:color_trc=bt709:color_primaries=bt709

-pix_fmt yuv420p -color_range tv
-colorspace bt709 -color_trc bt709 -color_primaries bt709
-bsf:v h264_metadata=video_full_range_flag=0
```

上面的滤镜换行仅方便阅读；真实命令由 builder 生成连续的 `-vf` 参数。`scale` 保持宽高和原有矩阵，只转换范围。8-bit 亮度的 0／128／255 对应约 16／126／235，色度映射到 16～240；播放器依据 Limited 标记正确展开黑白位。范围转换放在通用预设补黑边之前，避免将 Limited 黑边按 Full 再压缩成灰边。依据 [FFmpeg scale 文档](https://www.ffmpeg.org/ffmpeg-filters.html#scale)。

`setparams` 仅同步已经正确处理的帧标签，不转换像素。当前 FFmpeg 的实测表明，只传编码器颜色选项可能丢失帧的原色/传递标签；原色、传递、矩阵均已知时同时传给帧与编码器。若这些标签缺失，保留未知并提示，不编造 BT.709。x264 还可能省略全为默认值的范围 VUI，因此在编码完成后用 `h264_metadata` 明确写入 Limited 范围位；它同样不修改像素、不替代 scale。依据 [setparams 文档](https://www.ffmpeg.org/ffmpeg-filters.html#setparams) 和 [h264_metadata 文档](https://www.ffmpeg.org/ffmpeg-bitstream-filters.html#h264_005fmetadata)。

转码后重新 ffprobe：`pix_fmt` 必须为 `yuv420p`，`color_range` 必须为 `tv`（或等价 Limited 标记）；`color_space`、`color_transfer`、`color_primaries` 必须保留计划中已知的输入值。五项全部显示在输出报告，任何必需标记缺失或不符均拒绝发布并清理临时输出，未放宽验证器。

这些规则解决受支持 SDR 输入的范围转换，不保证修复错误标注的源文件，也不扫描逐帧颜色变化。HDR 色调映射、受控广色域转换、完整 ICC/Dolby Vision 管线尚未实现。高位深 SDR 最终仍为 8-bit；范围量化、色度降采样和 CRF 18 编码有损，不能宣称逐像素无损或对所有播放器绝对色彩一致。

## 开发与测试

完整审查的分级发现、逐项检查和使用边界见 [CODE_REVIEW.md](CODE_REVIEW.md)。

```powershell
.\.venv\Scripts\python.exe -m pip install --cache-dir tmp/pip-cache -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

单元测试无需安装 FFmpeg；覆盖多轨道、音频长短差、字段缺失、无效帧率、时长换算、FPS 容差、风险阈值边界、负起始时间、缺少音轨、中文和空格路径、缺失工具、执行失败、超时及 CLI 错误展示。集成测试在工具可用时生成短 MP4、MKV、无音轨视频、WAV 及音视频长度不同的样本，验证真实元数据和风险报告、中文/空格/emoji 路径、从其他目录运行、损坏文件报错，以及源文件哈希保持不变；缺少工具时跳过集成测试。pytest 临时文件放在项目的 `tmp/pytest`，该目录仅供测试使用。

V2 输入分析测试位于 `tests/test_media_profile.py` 和 `tests/test_profile_integration.py`，覆盖字段状态及原始值、精确分数解析、颜色范围别名、多轨与默认标记、字幕/封面、旋转来源、缺失位深、帧率置信度，以及无关画像字段不改变四种模式与两种预设生成的命令。真实集成测试在临时目录生成 Full/Limited 两类短 HEVC/AAC 视频，验证颜色标签、编码、帧率、音频和输入哈希；没有工具或 `libx265` 编码器时明确跳过。

范围转换测试位于 `tests/test_color.py` 和 `tests/test_color_integration.py`：生成带已知 Y/U/V 色阶的无损 HEVC Full/Limited 样本，实际经过两种预设重编码，再检查五项颜色元数据和解码后的亮度/色度数值（CRF 18 容差为 3 个码值），覆盖黑位、白位、重复范围压缩、已知范围但缺少色彩体系标签。另有仅改标签的错误对照，证明元数据合格仍可能存在错误像素；测试必须识别该对照。原样本哈希、输出尺寸也会复查。所有视频留在忽略的临时目录，不提交二进制。

兼容性规则测试位于 `tests/test_compatibility.py`，覆盖上述风险、阈值边界、空/无效字段、标签冲突、多轨定位，以及诊断前后输入模型、策略和命令不变。CLI/GUI 测试验证当前预设传递和报告展示。HEVC 集成测试另生成 10-bit 且带 SDR/PQ/HLG 标签的短视频；这些合成测试验证元数据解释，不验证真实 HDR 场景、像素颜色准确性或色调映射质量。

修复测试还覆盖 FPS 选择、四种模式的参数独立性、safe 条件组合和逐音轨选择、dry-run 无输出副作用、重名与并发保护、转码/复查失败的清理、取消后子进程回收、stderr 大量输出。真实转码测试使用 VFR、无音轨、多音轨、音频更长/更短、起始偏移和奇数分辨率样本；逐帧检查 CFR 间隔恒定及非 CFR 模式保留原帧间隔，检查 timestamp 消除开头负 DTS。累计音频 PTS 偏差样本验证 async 补偿有效、正弦波音调保持，以及正常 PTS 下不强行匹配两轨长度。

GUI 测试使用 Qt 自带的 QtTest 和无屏幕渲染平台，无需额外安装 pytest-qt；覆盖模式映射、自动分析、后台线程与主界面响应、进度、缺少工具/字段/音轨、失败后恢复、打开目录、运行中关闭保护，以及真实 FFmpeg 修复和重名输出保护。未安装 PySide6 时 GUI 测试跳过，CLI 测试仍可运行。

预设测试覆盖四种模式组合、条件式参数、字段缺失、逐音轨要求、验证失败不发布、CLI dry-run 和 GUI 预设传递。真实 FFmpeg 测试将 VFR/yuv444p/44.1 kHz 输入转换为目标格式，逐帧检查恒定间隔，并验证 MP4 顶层 moov 位于 mdat 之前、旋转元数据及原编码尺寸保持、多音轨/无音轨、残留时长差警告与源文件哈希不变。

### 生成可手动查看的测试样本

在项目根目录运行（无需下载外部媒体）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m tests.generate_samples
# 可选：写入另一目录；不覆盖已有同名文件。
.\.venv\Scripts\python.exe -X utf8 -m tests.generate_samples --output-dir "tmp/另一组样本"
```

默认输出到 `tests/generated_samples/`。五个 MP4 的视频均为 **6 秒、320 × 180**，以测试图和 440 Hz 正弦音生成，采用低体积编码参数；这些编码参数仅用于测试样本，不改变产品修复设置。

| 文件 | 特征 |
| --- | --- |
| `cfr.mp4` | 30 FPS CFR，音视频均为 6 秒 |
| `vfr.mp4` | 前 3 秒保留一半帧、后 3 秒保留全部帧；保留真实时间间隔，平均约 22.5 FPS、标称 30 FPS |
| `audio_longer.mp4` | 视频 6 秒、音频 6.3 秒 |
| `audio_shorter.mp4` | 视频 6 秒、音频 5.7 秒 |
| `no_audio.mp4` | 30 FPS CFR，不包含音轨 |

工具先在临时子目录生成并用 ffprobe 探测全部样本，再发布正式文件；生成/探测失败会清理临时数据，已存在的同名样本会被拒绝覆盖。重复手动生成时，请先自行移走旧样本，或指定新目录。样本生成工具仍使用同文件系统硬链接，需要文件系统支持。

样本工具与主 CLI 共用 Windows UTF-8 输出处理，控制台或重定向采用 GBK 等编码时，中文及 emoji 目录也能正常显示；重复生成时的拒绝覆盖提示同样适用。

`tests/generated_samples/` 整个目录已加入 `.gitignore`，只提交生成代码，不提交视频二进制文件。无需将生成的样本加入版本控制即可运行测试。

### 样本集成验证

```powershell
# 只运行五类样本的集成验证：
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_generated_samples.py
# 运行全部测试，并记录每个修复用例的前后时长差、FPS 等属性：
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -o junit_family=xunit1 --junitxml=tmp/test-results.xml
```

每次集成验证都会在 `tests/generated_samples/test-run-.../` 新建这五类样本，结束后自动清理；不读取或改写默认目录中供人工查看的 MP4。修复结果使用 pytest 独立临时目录，因此重复运行不会因旧输出重名而失败。

验证内容包括 analyzer/diagnose 正常运行、CFR 与疑似 VFR 识别、无音轨处理、四种修复模式和 Bilibili 预设成功输出、最终文件重新经 ffprobe 读取及完整解码、CFR 输出的平均/标称 FPS 和实际逐帧间隔、源文件哈希不变。无音轨的 `audio-sync` 单独验证为可读错误，不将其当作应成功修复的用例。

“时长差没有明显恶化”定义为：**修复后的音视频绝对时长差不超过修复前的差值 + 0.1 秒**。同时检查视频和音频各自时长偏差不超过 0.1 秒，避免两轨被一起截短却误判通过。容差在测试中定义为常量，用于容纳帧取整和 AAC 编码边界误差；不表示已证明内容同步，无音轨的时长差仍为未知。缺少 FFmpeg/ffprobe 时这组集成测试明确跳过；已安装但不可运行、样本生成失败或断言失败则直接报错。

这些样本验证元数据与处理流程，音频略长/略短是尾部长度差，不能代表所有录制时钟漂移。原有累计音频 PTS 偏差测试继续保留；长时录屏、严重时间戳损坏和实际声音/画面内容同步仍需专门素材或人工播放确认。

当前文件职责：

```text
main.py                 命令行入口
gui.py                  桌面入口（依赖缺失时显示安装提示）
gui/main_window.py      窗口、诊断摘要与交互状态
gui/workers.py          QThread 调用核心，通过信号返回结果
app/models.py           媒体、诊断、修复策略和执行计划数据结构
app/ffmpeg_utils.py     工具查找、集中构建 FFmpeg 命令与子进程调用
app/analyzer.py         轨道与容器信息解析、同步风险分析及阈值
app/deep_sync.py        流式时间戳统计、采样时钟拟合、同步模式与补偿资格
app/timestamp_probe.py  有时限、记录上限和有限缓冲的 ffprobe 子进程读取
app/media_profile.py    CLI/GUI 共用的输入媒体画像及字段状态展示
app/compatibility.py    独立的输入兼容性诊断规则与共享报告，不控制修复
app/color.py            独立颜色范围计划、颜色复查与共享计划说明
app/repair_plan.py      策略/FPS、颜色、时序、逐音轨和容器决策与原因报告
app/fixer.py            只读执行计划生成、执行、复查与安全发布
app/output_validation.py  按 ExpectedOutputSpec 验证输出、时序退化与发布前文件检查
app/presets.py          CLI/GUI 共用的分级报告与旧验证入口适配
app/cli.py              命令行参数与中文报告
tests/                  单元测试与真实 FFmpeg 集成测试
tests/generate_samples.py 五类 6 秒样本的独立生成工具
tests/media_helpers.py  新旧集成测试共用的媒体生成与逐帧读取
tests/generated_samples/ 本地合成样本与短期测试子目录（不纳入版本控制）
tools/ffmpeg/           本地 FFmpeg（不纳入版本控制）
tmp/                    本地缓存和测试临时文件
```

尚未实现声音与画面内容比对或自动估算时钟速率差。

### V2：RepairPlan 与 FFmpeg builder

核心流程为 `AnalysisResult → select_repair_plan → RepairPlan → build_repair_command → FixPlan`。
`RepairPlan` 是不可变的数据结构，描述视频编码、目标 FPS、帧率模式、像素格式、颜色转换、时间戳策略、各轨相对起点、逐音轨编码/采样率/同步策略，以及容器和 faststart。
补边和旋转处理也由计划层确定。builder 不再读取 analyzer、选择颜色策略或按模式/预设分支；只把计划翻译成参数列表，并拒绝不支持或相互冲突的计划。

自动选择（safe）沿用现有分析阈值：疑似 VFR 才启用 CFR；起点缺失、负值或明显偏移才启用 genpts；逐音轨 async 改由完整深度时间戳证据控制，长度差不再作为开启依据。Full → Limited 只在输入范围已知为 Full 时执行，Limited 不重复压缩。未知范围和不支持的 HDR 仍拒绝转换。

为兼容现有正常用法，显式 `cfr`、`timestamp`、`audio-sync` 保留专项请求含义，Bilibili 保留 CFR / AAC 48 kHz 输出要求。这些决定分别标为 `explicit`、`preset`，不会伪装成 analyzer 发现的问题，也不会顺带打开其余专项修复。音轨长度差仍只是风险线索，不代表已确认累计漂移。

`--fix --dry-run` 的 `Repair decisions` 报告逐项列出启用/禁用状态、Reason 和依据来源。CFR 所需的时间轴归一化与 genpts 单独说明；genpts 仅补缺失 PTS，不能重建所有损坏时间戳。

`build_fix_command` 与 `select_strategy` 暂保留为旧调用的迁移接口，实际修复统一使用 `RepairPlan`。新功能应在计划层加入决策，在 builder 中加入对应表达；不要向 CLI/GUI 或旧接口继续添加模式参数拼接。

`tests/test_repair_plan.py` 覆盖计划决策、逐轨偏移、禁用原因、非法计划、参数独立性和中文空格路径。`tests/snapshots/repair_commands.json` 保存 54 组完整 argv（只将输入/输出绝对路径替换为占位符）。本阶段明确更新其中两组 combined/safe，移除仅凭长度差启用的 async，其余 52 组保持原命令。运行测试不会自动更新快照。


### V2：深度同步分析与渐进漂移

```powershell
# 保留快速元数据分析作为默认；深度分析也可以独立使用。
.\.venv\Scripts\python.exe main.py "录屏 视频.mp4" --deep-analysis
# 先查看估算漂移、每条音轨的实际策略与完整命令。
.\.venv\Scripts\python.exe main.py "录屏 视频.mp4" --deep-analysis --fix --dry-run
# 大型输入可调整资源预算；达到上限返回 partial，不自动软补偿。
.\.venv\Scripts\python.exe main.py "录屏 视频.mp4" --deep-analysis --deep-timeout 180 --deep-max-records 2000000
```

普通模式只读取 stream/container metadata，仍保留 duration、start_time、time_base、average/nominal FPS 的风险提示。深度模式将证据附加到 `AnalysisResult.deep_sync`，分别读取：

- 全文件 packet 的 PTS/DTS：识别缺失与 DTS 回退、明显长间隔；视频 packet PTS 在 B 帧解码顺序中可以合法回退，不能据此判损坏。
- 全文件音频 decoded frames 的 PTS 与 `nb_samples`：以整数 ticks × time_base 优先恢复秒数，缺少 ticks 时才用 `pts_time`。采样时钟采用已知 sample_rate；缺失或变化的采样率不作为自动补偿依据。缺失 PTS 的帧仍计入已解码采样数，避免把缺少时间戳误算为采样时钟漂移。
- 第一条非封面视频在开头、中间和末尾等五个位置各约 4 秒的 decoded frame timestamps：排除 seek 提前落到关键帧的 preroll，窗口边界不算突跳，检查真实展示间隔是否变化。短视频或未知时长只检查可确定的开头窗口。

渐进漂移的拟合信号是 `x = 累计已解码采样数 / sample_rate` 与 `y = (当前 PTS − 首帧 PTS) − x`。使用在线协方差计算斜率、R² 和拟合 RMS，并保存有限数量的观测点。`drift_ppm = slope × 1e6`；末端估算漂移为斜率乘已观察采样时长，绝不使用音视频 duration 比例计算速度。正值表示 PTS 时间轴比采样时钟更长；它不是直接测出的“声音比画面晚多少”。报告中的分钟／毫秒列表也是相对采样时钟的观测。

`Sync pattern` 可同时包含多个候选：

| 候选 | 证据及边界 |
| --- | --- |
| STATIC_OFFSET | 快速分析只能提示起始偏移候选；深度分析中还要求完整、至少 60 秒且无明显斜率的音频时钟证据。正 offset 表示音频起点较晚，不能证明全程内容偏移量。 |
| PROGRESSIVE_DRIFT | 至少 60 秒、累计拟合漂移至少 50 ms、R² ≥ 0.98、低拟合残差，且没有缺失/突跳。仅指 PTS 与采样时钟的渐进分离。 |
| VFR_SUSPECTED | 元数据差异或抽样视频帧间隔变化；未抽样区间不能排除 VFR。 |
| TIMESTAMP_ANOMALY | 缺失 PTS、DTS 回退或时间间隔突跳；不把一个阶跃拟合成时钟漂移。 |
| UNKNOWN | 没有足够证据；不等于文件健康或实际播放同步。 |

软补偿额外要求扫描成功、视频抽样有足够帧且无已知时间戳异常、对应音轨证据完整，并且绝对漂移不超过 **500 ppm（0.05%）**。采用 `aresample` 的持续软补偿，最大比例 0.0005，禁止硬补静音和硬裁剪；保留音频 PTS 间隔供 resampler 比较。视频帧、播放速度与时长不按音轨长度改变，也不使用 `atempo`、`asetrate` 或 `-shortest`。RepairPlan 记录每条音轨的估算漂移、ppm、补偿上限与实际选择，dry-run 展示同一组决定。超过上限、明显突跳或 partial 扫描只提示检查，不自动做较大变速。

`async=1` 的 fill/trim 与大于 1 时可用的持续补偿不同；`min_hard_comp` 划分硬/软补偿，`max_soft_comp` 控制软补偿比例。实现参考 [FFmpeg 重采样文档](https://ffmpeg.org/ffmpeg-resampler.html#Resampler-Options)。seek 可能提前定位，帧数与时间窗口不能假定精确，参考 [ffprobe read_intervals 文档](https://ffmpeg.org/ffprobe.html#Main-options)。

**资源边界：**默认总预算 120 秒、1,000,000 条输出记录、最多 32 条选中轨道；分别为 packet、音频 frame 和视频窗口预留时间，前面未耗用的预算可由后续扫描复用。超时、超限、ffprobe 解码错误或无有效抽样帧会显式报告覆盖不足。子进程结束清理可能额外需要数秒。compact 输出逐行处理，队列最多 64 行，每行最多 16384 个字符，stderr 仅保留尾部；每轨只保存在线统计及最多 129 个检查点。内存不会随帧数线性增长。取消、读取失败和预算终止均回收 ffprobe；路径仍使用无 shell 的 argv 列表。

软补偿假设音频 PTS 是可信参考，只改善 PTS 与采样数的一致性。若音频与视频被各自重新标成内部一致的时间戳，真正的内容漂移可能完全没有出现在这些统计里；物理录制时钟问题和事件对齐仍需专门参考或人工播放确认。

新增单元测试覆盖正负线性漂移、固定偏移、突跳、时钟回退、B 帧重排、粗时间基、未知字段、逐音轨选择、部分扫描、内存上界、超时和取消。真实 FFmpeg 测试生成正负 250 ppm 的 240 秒录屏，检查估算值、补偿后时钟残差、视频端点/帧数/帧率、音频采样长度和 440 Hz 音调；另覆盖固定偏移、突跳和元数据帧率相同时的 VFR。
## V2 Input Compatibility Matrix 与真实素材回归

本阶段只扩充测试，不增加产品修复功能。`tests/media_factory/` 动态生成 8 个样本，每个视频约 4 秒，音频为 3/4/5 秒或无音轨；分辨率 256 × 128，单文件限制 5 MiB（当前样本均不足 1 MiB）。画面包含运动图案及固定 Y/U/V 色阶。每个样本分别运行通用和 Bilibili 预设，共 16 个矩阵用例。

覆盖目标 A–O：H.264/HEVC、Limited/Full、30/60/60000÷1001 FPS、基于实际帧时间戳的 VFR、音频短于/长于视频、无音轨、44.1/48 kHz AAC、中文文件名和空格路径。生成参数只是意图，类别覆盖必须由实际 ffprobe 字段和帧时间戳确认。当前 FFmpeg 把生成的 H.264 Full-range 解码为 `yuvj420p + pc`，该语义路径会测试，但不会冒充字面上的 B 类 `H264 / yuv420p / full`；若没有这种实际输入，B 类显示 **NOT TESTED**。换用其他 FFmpeg 构建或放入真实样本后，报告按实际观察更新。

一条命令依次运行全部单元测试、集成测试及可选真实素材测试，并合并到一个新的报告目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m tests.media_factory.run_regression
```

也可以分别运行（每次默认创建独立报告，避免旧结果冒充本轮结果）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -ra -m "not integration"
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -ra -m "integration and not private"
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -ra -m private
# 只运行新的合成矩阵：
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_compatibility_matrix.py
```

`conftest.py` 根据测试模块标记集成测试：真实 FFmpeg 集成模块、Qt GUI 集成、已有样本生成/回归模块和新矩阵模块属于 `integration`；真实文件测试还标记 `private`。其余为单元测试。普通 `pytest` 仍执行全部现有测试和新增测试。

报告写入忽略的 `tmp/compatibility-reports/<本次运行编号>/matrix.md` 和 `matrix.json`，控制台打印完整路径。报告包括三组测试计数、A–O 实际覆盖、每个样本/预设的 Analyze、Compatibility、Repair、Validate、Color、Duration、Source unchanged，以及输入画像、兼容性诊断、ExpectedOutputSpec 和 Post Repair Report 的文本/JSON 证据。各阶段从 **NOT TESTED** 开始；跳过、未运行、缺少编码器都不会算作 PASS。任一失败令测试和总运行退出码失败。缺少可选真实素材会跳过，测试退出码为零也不代表所有类别已经覆盖。

合成样本的颜色验证比较解码后的色阶数值，并含仅改标签、重复范围转换的反例；不能只凭输出 Limited 标签通过。长短音轨原有差异未恶化时，现有输出验证可给出 WARNING，该状态原样显示，不改写成 PASS。同一个 A–O 类别涉及多个样本时按最严重状态汇总，所以某个正常类别也可能因同时包含合法长短音轨样本而显示 WARNING；逐样本表列出具体原因。

同一样本只执行部分预设时，已观察类别的完整覆盖仍记为 NOT TESTED，未执行行不会伪造探测结果。Repair 仅在修复及文件发布成功后记为 PASS；验证失败时保留实际的时长检查和报告。颜色断言要求完整且有限的 Y/U/V 色阶值，真实素材抽样的旋转行为直接读取 RepairPlan。

### Real Sample Regression

将合法取得的真实录屏放进 `samples/private/`（可以有子目录）。该目录已加入 `.gitignore`，不提交真实媒体。自动发现 `.mp4/.mkv/.mov/.m4v/.avi/.webm/.ts/.mts/.m2ts/.nut`，不存在素材时输出明确的 **Real Sample Regression: NOT TESTED** 并跳过。

真实样本默认完整执行 `safe + general`，不是抽取短片假装测试整段。测试会读取 analyzer、核对兼容性诊断与实际字段、修复、重新 ffprobe、验证 ExpectedOutputSpec 和时长退化，并流式计算修复前后源文件 SHA-256。输出只写入项目 `tmp/` 下独立临时目录，成功和失败后均清理；报告保留。长视频可能需要较长转码时间和足够临时空间，不会复制整个源文件到仓库，也不会一次加载全部帧 JSON。

真实文件的像素检查抽取首个可解码视频帧，将独立按输入范围生成的 Limited 参考与输出统一缩小到 128 × 72 比较 Y/U/V 平均绝对误差（每平面不超过 3 个码值）。这是**抽样颜色检查**，不证明整段颜色准确或真实声音/画面事件同步；异常标签、HDR、损坏或当前能力不支持的输入会如实失败，不为“兼容”静默跳过。用合成文件验证真实回归机制的自测不会计入真实 Steam 素材覆盖。
