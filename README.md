# 音画同步修复器

本地 Python 视频工具，支持**音画同步风险诊断、多策略修复与 Bilibili 输出预设**，提供命令行和 PySide6 桌面界面。
输入一个文件，检查 ffmpeg 和 ffprobe 是否能够运行，然后显示文件名、文件大小、容器格式、总时长、视频和音频编码器、分辨率、avg_frame_rate、r_frame_rate、各轨道时长及 time_base、音频采样率。

## 环境与安装

- Python 3.11+。
- FFmpeg 和 ffprobe。优先复用 PATH 中的安装；未找到时使用项目内 `tools/ffmpeg/bin/` 下的同名可执行文件（Windows 为 `.exe`）。路径相对于项目位置，与运行时工作目录无关。
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

安装依赖后，在项目目录启动：

```powershell
python gui.py
# 或直接使用项目虚拟环境：
.\.venv\Scripts\python.exe gui.py
```

1. 点击“选择视频”，支持 MP4、MKV、MOV、WebM。选择后自动在后台分析。
2. 摘要显示文件名、分辨率、平均/标称 FPS、疑似 VFR、视频/音频时长、长度差和风险等级。缺失字段显示“未知”，缺失音轨明确提示。
3. 选择修复模式：**自动 → safe、CFR → cfr、时间戳 → timestamp、音频同步 → audio-sync**。没有音轨时音频同步模式不可执行。
4. 选择输出预设：**通用 → general（默认）、Bilibili → bilibili**。预设与修复模式可以组合；Bilibili 始终启用 CFR。
5. 点击“开始修复”，日志显示分析、风险提示、策略依据和转码状态；Bilibili 转码后还显示完整输出验证报告。完成后显示输出路径，可点击“打开文件所在目录”。输出规则与 CLI 相同。

分析、工具检查、FFmpeg 转码和输出复查都在 `QThread.run()` 中调用既有核心，使用 Qt 信号将日志、进度、结果和错误交回主线程；后台线程不访问控件。依据 [Qt QThread 文档](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html)。修复开始时清除旧诊断并重新分析输入，避免沿用选择文件后已经过时的元数据；重新分析失败时禁用修复按钮，重新选择有效文件后恢复。

进度解析集中在 `ffmpeg_utils.progress_seconds()`，支持传统 `time=HH:MM:SS.xx` 和机器输出 `out_time` / `out_time_us`。现有转码继续使用 `-progress pipe:1` 的机器输出；GUI 接收已处理秒数，以容器总时长（缺失时回退到已知轨道时长）估算百分比，不重复解析 FFmpeg 日志。CLI 和 GUI 共用 `progress_percent()`，兼容未知及异常数值，运行进度最多显示 99%；100% 仅在转码、复查和安全发布全部成功后显示。时长未知时 GUI 显示活动进度条及已处理秒数。

没有 FFmpeg/ffprobe 时分别显示“未检测到 FFmpeg”或“未检测到 ffprobe”和安装位置提示，可以准备好工具后重新选择文件。其他错误显示在状态栏与处理日志中，不向用户弹出 Python traceback。Windows 子进程统一使用 `CREATE_NO_WINDOW`，避免从 GUI 探测或转码时弹出额外控制台。

当前 GUI 每次处理一个文件；任务运行时禁用重新选择与重复修复，暂不提供取消或批处理。为避免销毁运行中的线程，处理过程中关闭窗口会被暂时阻止并提示等待，界面仍可响应；任务完成后可关闭。日志最多保留 1000 行。GUI 采用系统原生控件，没有自定义主题或动画。

## 输出解释与边界

- 文件大小来自本地文件状态，以字节和 MiB 显示，不依赖 ffprobe 是否返回 `size`。总时长使用容器 `duration`；缺失时显示“未知”。
- 编码器一栏展示 ffprobe 的 `codec_name`（如 h264、aac），表示编码格式，不能据此确定原始编码软件。音频采样率使用 `sample_rate`，以 Hz 显示；缺失或无效时显示“未知”。
- 每条音频、视频轨道单独列出，使用 ffprobe 原始轨道编号；封面图片不计为视频轨道。无音轨或无视频轨时明确显示“无”。
- 视频帧率分别显示 `avg_frame_rate` 与 `r_frame_rate`，包含 fps 小数及精确分数。字段缺失、`0/0` 或无效值显示“未知”。两字段不同并不足以确认 VFR。
- 轨道时长优先读取轨道 `duration`；缺失时尝试 `duration_ts × time_base` 并标注来源。仍不可用时显示“未知”，不使用容器时长代替轨道时长，也不将未知值当作 0。
- MKV/WebM 等文件可能仅提供容器时长，因此轨道时长显示“未知”是预期行为。当前没有逐帧扫描或从 metadata 标签猜测轨道时长。
- 容器格式可能显示多个别名，例如 `mov,mp4,m4a,3gp,3g2,mj2`，这是 ffprobe 返回的格式名称。
- 工具启动检查超时为 10 秒，媒体探测超时为 60 秒。损坏、无法读取或超时会输出错误，不向正常用户显示 Python traceback。
- CLI 不带 `--fix` 时只分析。诊断仅根据元数据提示潜在同步风险，不能确认实际音画不同步；源视频始终不被覆盖。

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
- 长度差可能源于尾部长度不同，起始偏移可能为固定偏移。当前没有逐帧时间戳扫描或声音与画面内容比对，因此不能仅凭这些线索确认累计漂移；低长度差风险也不保证实际播放同步。

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
- 每条音轨分别与所选视频比较；已知长度差达到 **0.2 秒**时，仅为该音轨开启 audio-sync。复用 analyzer 的阈值和比较函数；长度差只是启用检查的线索，并不是音频拉伸比例。
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

使用 `aresample=async=1:min_hard_comp=0.1:max_soft_comp=0`，常量集中在 `app/ffmpeg_utils.py`：

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

转码后使用 ffprobe 重新分析临时输出，由 `app/presets.py` 统一验证并生成中文报告；CLI 和 GUI 展示同一份报告：

- 容器及 `major_brand`（避免把共享格式别名的 MOV 误认成 MP4）。
- 视频编码、像素格式、分辨率、平均/标称 FPS 和 CFR 目标。
- 视频时长，以及每条音轨的编码、采样率、时长和与视频的长度差。
- 不符合项、残留同步风险及验证结论；缺失字段显示“未知”。

若容器、视频/音频编码、yuv420p、目标 FPS、48 kHz、原分辨率或音轨数量不符合要求，**报告失败且不发布最终文件**，清理临时输出。CFR 由已有 fps 滤镜生成，复查平均/标称 FPS 均匹配目标；生产流程不额外逐帧扫描，元数据验证不能证明声音与画面内容同步。

轨道长度差仍达到 **0.2 秒**、起始偏移仍大于 **0.05 秒**，或时长/起点未知等，会显示明确的同步风险提示。编码要求通过时仍保留输出；不会使用 `-shortest`、按时长比例拉伸或自动补齐尾部来掩盖差异。报告会区分“编码达标”与“同步风险未消除”，需要播放确认。阈值复用 analyzer 常量。

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

转码先写到 `output/.avsync-.../` 临时目录，ffprobe 检查通过后用同文件系统硬链接原子发布最终文件，防止并发任务覆盖同名输出。临时目录在成功、失败和 Ctrl+C 取消后清理，失败不会留下冒充成功的最终文件。输出盘须支持硬链接（本项目已在 Windows NTFS 上验证；FAT/exFAT 等不支持时会报错）。大型视频仍需要足够空间存储重新编码后的文件。

进度使用 FFmpeg 的 `-progress pipe:1`；已知总时长时显示估计百分比，未知时显示已处理秒数。100% 仅在转码、输出复查和发布全部完成后显示。stderr 由独立读取线程持续排空，失败时显示末尾关键内容；Ctrl+C 会停止子进程并清理临时输出。

全部修复模式启用 `-xerror`：遇到解码或损坏包错误就失败并清理临时输出，避免 FFmpeg 容错跳过内容后仍返回成功。已用“保留 MP4 头部、截断媒体数据”的样本验证。该选项不改变正常修复策略，也不表示能检测所有内容缺失；严格失败意味着本工具不用于尽可能抢救损坏视频。参数依据 [FFmpeg 文档](https://ffmpeg.org/ffmpeg.html#Advanced-options)。

强制结束进程或断电不会保证执行 Python 清理代码，可能残留临时目录；强杀 GUI 时子进程也可能尚未结束。确认没有相关任务运行后再手动处理残留文件，程序不自动删除来源不明的临时目录。

修复后时长差可能仍存在，元数据复查通过不代表已确认实际音画同步。HDR 色调映射和其他高级色彩管理不在当前范围内。

### v1 已知兼容问题

部分全范围录屏（例如 ffprobe 显示 `yuvj420p`、`color_range=pc` 的 HEVC 视频）使用 Bilibili 预设时，转码输出仍可能被识别为 `yuvj420p`，随后因预设要求 `yuv420p` 而验证失败，不发布最终文件。当前只设置了输出像素格式，尚未完善颜色范围转换；这一真实素材场景尚未修复，也不在已有 394 项通过测试的覆盖范围内。源文件不会被修改。

## 开发与测试

完整审查的分级发现、逐项检查和使用边界见 [CODE_REVIEW.md](CODE_REVIEW.md)。

```powershell
.\.venv\Scripts\python.exe -m pip install --cache-dir tmp/pip-cache -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

单元测试无需安装 FFmpeg；覆盖多轨道、音频长短差、字段缺失、无效帧率、时长换算、FPS 容差、风险阈值边界、负起始时间、缺少音轨、中文和空格路径、缺失工具、执行失败、超时及 CLI 错误展示。集成测试在工具可用时生成短 MP4、MKV、无音轨视频、WAV 及音视频长度不同的样本，验证真实元数据和风险报告、中文/空格/emoji 路径、从其他目录运行、损坏文件报错，以及源文件哈希保持不变；缺少工具时跳过集成测试。pytest 临时文件放在项目的 `tmp/pytest`，该目录仅供测试使用。

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

工具先在临时子目录生成并用 ffprobe 探测全部样本，再发布正式文件；生成/探测失败会清理临时数据，已存在的同名样本会被拒绝覆盖。重复手动生成时，请先自行移走旧样本，或指定新目录。发布使用同文件系统硬链接，与产品修复一样需要文件系统支持。

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
app/fixer.py            策略/FPS 选择、只读计划生成、执行、复查与安全发布
app/presets.py          输出预设验证与 CLI/GUI 共用的验证报告
app/cli.py              命令行参数与中文报告
tests/                  单元测试与真实 FFmpeg 集成测试
tests/generate_samples.py 五类 6 秒样本的独立生成工具
tests/media_helpers.py  新旧集成测试共用的媒体生成与逐帧读取
tests/generated_samples/ 本地合成样本与短期测试子目录（不纳入版本控制）
tools/ffmpeg/           本地 FFmpeg（不纳入版本控制）
tmp/                    本地缓存和测试临时文件
```

尚未实现声音与画面内容比对或自动估算时钟速率差。
