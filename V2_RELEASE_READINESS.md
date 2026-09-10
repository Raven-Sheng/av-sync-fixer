# V2 Release Readiness Report

审查日期：2026-09-10。范围：当前 V2 工作树、CLI、GUI、FFmpeg 执行与发布链路。未新增修复模式、HDR 色调映射或 GUI 功能，未升级依赖。

**发布建议：暂不建议面向“不同电脑、不同录屏设置”的广泛正式 Release V2。可以作为限定环境的候选测试版本。** 本次发现的三个 High 代码问题已修复，但跨电脑、长时间真实漂移和 FFmpeg 版本的证据不足。一次录屏转码成功不能替代这些验证。

本报告中的 PASS 表示对应检查实际通过；WARNING 保留未确认的事实；NOT TESTED 不算 PASS。“拒绝路径已验证”不表示输入已支持修复。

## 1. 审查结论与分级

| ID | 等级 | 发现 / 实际风险 | 当前状态 |
| --- | --- | --- | --- |
| C | Critical | 本次未确认源文件覆盖、并发覆盖或执行命令注入问题；不等于证明所有环境都不存在 Critical 风险 | 未发现已证实项 |
| H1 | High | HEVC 的 HDR 信息可以只在帧 SEI 中出现。2 秒、10-bit、带 mastering display / content light、缺 transfer 的输入，原实现会转成 8-bit 并以 WARNING 发布 | **已修复**：开头有限帧证据与原始 stream metadata 分开保存；颜色规划和输出验证使用同一 HDR 证据规则；不把疑似 HDR 猜成 SDR |
| H2 | High | 8 秒 MP4 前 6 秒 Limited、后 2 秒 Full；原实现输出 Overall PASS，后段黑白仍为 0/255，却标记为 Limited。只检查输出标签无法发现错误 | **已修复**：转码过程中、任何颜色/CFR 滤镜之前流式检查解码帧属性；与计划不符或中途改变范围、矩阵、transfer、像素格式、尺寸时终止并拒绝发布。检查记录缺失也失败 |
| H3 | High | 最终发布依赖硬链接，Windows 不支持硬链接的输出盘会在长时间转码后失败 | **已修复代码依赖**：Windows 使用拒绝覆盖目标的同盘 rename；其他系统保留排他硬链接。已测 NTFS、并发同名、发布权限失败及模拟无硬链接能力；真实 exFAT/FAT/网络盘仍 NOT TESTED |
| H4 | High · 发布验证缺口 | 当前只有一份约 123 秒的历史真实录屏，无法证明各厂商编码器、多轨设置、HDR 录制和 20 分钟以上渐进漂移场景可靠 | **未关闭，广泛发布阻塞项**；不能靠增加合成测试数量解决 |
| M1 | Medium | OutputSpec 检查是输出合规性检查，不是音画内容对齐测量。FPS 元数据、相近总时长、稳定的音频 PTS 都可能与实际声音/画面漂移同时存在 | 已在界面/报告中保留限制；真实长视频内容同步仍需独立事件基准 |
| M2 | Medium | 启动只检查工具可运行；PATH 中的 FFmpeg/ffprobe 可来自不同安装。旧版本或缺少 libx264/AAC/滤镜的构建会失败 | 已明确版本边界；仅本机 9.0.1 实测，未认证其他版本或定制构建 |
| M3 | Medium | ExpectedAudioSpec 不检查声道布局、每个声道内容、语言和默认轨标记；现有自动 PASS 的含义不能扩展到这些属性 | 本次新增混合采样率、mono + 5.1、默认第二轨和语言的独立集成断言；7.1、特殊布局和真实多设备录音仍未验证 |
| M4 | Medium | 大视频的深度分析有预算，超过预算只得到部分证据；普通 metadata JSON 与任意容器标签仍整体解析，没有总字节硬上限 | 深度扫描已有限时、记录上限和有界队列；没有多小时 4K/高轨数压力证据，也没有恶意超大标签资源耗尽认证 |
| M5 | Medium | GUI 不提供 deep-analysis 入口，也没有运行中取消按钮；关闭窗口会等待任务，强杀/断电可能遗留临时目录 | 当前限制；GUI 的快速分析不能被宣传成完整渐进漂移诊断 |
| M6 | Medium | Bilibili “Compatibility PASS” 是 RepairPlan 的编码/封装要求通过，并不证明平台上传、服务端转码或所有播放器显示正确；BT.2020 SDR 尤其需要平台验证 | 实际上传与平台转码 NOT TESTED |
| M7 | Medium · 本次修复回归 | 加入逐帧日志后，日志尾部曾挤掉原始损坏原因，8 项现有损坏测试失败 | **已修复**：帧检查日志单独消费，不占据错误日志尾部；保留原始解码错误，新增日志洪流回归 |
| L1 | Low | HDR/SDR 传递类型与 HDR side-data 判断曾分散重复，容易让诊断、规划和验证意见不一致 | 已集中到共享颜色规则 |
| L2 | Low | 未测量行覆盖率/分支覆盖率；当前环境没有 coverage 包 | 未把测试数量当覆盖率，也未安装额外产品依赖 |

H2 不通过“自动采用另一套颜色策略”处理变化输入：固定 RepairPlan 无法证明安全时明确失败。没有使用 `ignore_err`、丢弃异常帧、放宽色差/时长容差或删除失败断言来获得成功。

## 2. 实际验证环境

| 项目 | 实际值 |
| --- | --- |
| OS | Windows 11，build 26200，x64 |
| Python | 项目 `.venv`，3.13.5 |
| GUI | PySide6 6.11.2，真实 QThread/QtTest 集成测试 |
| FFmpeg / ffprobe | 项目内 `9.0.1-essentials_build-www.gyan.dev`，同一安装 |
| 编码实现 | 软件 libx264 输出、AAC 输出；合成 HEVC 使用 libx265 |
| 输出盘 | D: NTFS |
| 代码状态 | 审查当前工作树，保留此前 V2 修改，未自动提交 Git |

保持原帧间隔的命令使用 `-enc_time_base filter`。官方 n6.0 文档只列数值形式，n6.1 才列 `filter`，因此当前命令语法要求至少 6.1；**这不是“6.1+ 全部实测通过”**。[FFmpeg 6.0 文档](https://github.com/FFmpeg/FFmpeg/blob/n6.0/doc/ffmpeg.texi)、[FFmpeg 6.1 文档](https://github.com/FFmpeg/FFmpeg/blob/n6.1/doc/ffmpeg.texi)。

Windows `os.rename` 在目标已存在时抛出 FileExistsError；不能把 POSIX 的会覆盖行为用于此处。本次只在 Windows 选择 rename。[Python 官方语义](https://docs.python.org/3/library/os.html#os.rename)。

## 3. Input Compatibility Matrix

以下 A–O 指的是实际生成并探测到的输入属性，不按生成意图推定覆盖。矩阵内 8 个短素材各经过 general / bilibili，两套预设共 16 条链路。

| 类别 | 实际输入 | Analyze | Repair | Validate | 证据边界 |
| --- | --- | --- | --- | --- | --- |
| A | H264 / yuv420p / Limited / CFR | PASS | PASS | PASS | 短合成素材 |
| B | H264 / **字面 yuv420p / Full** | NOT TESTED | NOT TESTED | NOT TESTED | 本机将该类全范围 H264 stream 报为 yuvj420p；不能拿语义相近样本替代这个组合 |
| C | HEVC / yuv420p / Limited | PASS | PASS | PASS | 包含 60000/1001 FPS |
| D | HEVC / yuvj420p / Full | PASS | PASS | PASS | 合成数值色阶 + 一份真实录屏 |
| E | 30 FPS | PASS | PASS | PASS | 短合成素材 |
| F | 60 FPS | PASS | PASS | PASS | 短合成素材；真实录屏约 59.992 FPS，单独记录 |
| G | 59.94 FPS（60000/1001） | PASS | PASS | PASS | general 保留分数帧率，bilibili 按现有策略取 60 CFR |
| H | VFR | PASS | PASS | PASS | 合成帧间隔独立验证；真实输入只做抽样时序诊断 |
| I | audio shorter than video | PASS | PASS | WARNING | 原有约 1 秒差保留，未恶化，不把它猜成时钟漂移 |
| J | audio longer than video | PASS | PASS | WARNING | 同上；未用 shortest 裁剪 |
| K | 无音轨 | PASS | PASS | PASS | 不凭空生成音轨 |
| L | AAC 44.1 kHz | PASS | PASS | PASS | general 保留策略，bilibili 48 kHz |
| M | AAC 48 kHz | PASS | PASS | PASS | 合成及真实样本 |
| N | 中文文件名 | PASS | PASS | PASS | 实际 Windows 文件系统/子进程路径 |
| O | 空格路径 | PASS | PASS | PASS | 还包含部分 &、emoji、前导短横线测试 |

**15 类中 14 类有覆盖；B 仍 NOT TESTED。** 16 条合成修复均完成，12 条验证 PASS、4 条 WARNING，不能把 WARNING 写成全 PASS。

### 额外输入与边界

| 输入/操作 | 结果 | 应如何解读 |
| --- | --- | --- |
| H264 / HEVC，10-bit YUV420，BT.709 SDR，Full / Limited | PASS | 真实编码后精确核对源 10-bit 数值，再核对两预设输出 Y/U/V 色阶；输出依然降为 8-bit，有精度损失 |
| H264 / HEVC，10-bit YUV420，BT.2020 SDR，Full / Limited | PASS（本机数值/标签链路） | 保留 BT.2020，不自动转成 BT.709；不证明平台显示兼容、真实场景无色带 |
| PQ / HLG；transfer 缺失或冲突但带 HDR SEI | 拒绝路径 PASS | 不支持色调映射；没有“成功修复 HDR”的证据 |
| 中途改变 Full/Limited、矩阵、HDR transfer、尺寸 | 拒绝路径 PASS | 8 秒实际变化码流，初始探测未覆盖后段，运行时终止且无最终文件 |
| 两音轨：44.1 kHz mono + 48 kHz 5.1，第二轨 default，eng/zho | PASS | 两预设核对轨数、采样率、声道布局、默认标志/语言，并解码全部输出轨；未测每声道空间定位 |
| 192 kHz、10 kHz、46.05 kHz 音频 | PASS | 现有测试独立对照本机 AAC 的最近支持采样率策略，不能推及任意编码器构建 |
| 负起点、固定相对 offset、多音轨不同起点、旋转、奇数尺寸 | PASS（现有对应测试） | general 偶数补边；bilibili 奇数尺寸明确拒绝；不等于支持任意翻转/仿射显示矩阵 |
| 截断 MP4、已有输出、并发目标、Ctrl+C/回调异常 | PASS（对应错误/保护路径） | 失败不发布，不覆盖源或已有文件；不包含真实断电恢复 |

以上新增素材仍为短合成素材，不提交视频到 Git。

## 4. Real Sample Regression

`samples/private/` 为空，自动 private 阶段 **1 skipped / NOT TESTED**。

此外，从已有 [V2 基线记录](V2_BASELINE.md) 定位到同一份历史真实录屏，单独运行两种预设完整回归。没有把它复制到 Git，也没有移动源文件。其来源沿用历史记录，容器标签本身不能认证 Steam 或识别录制硬件。

| 输入属性 | 实际读取 |
| --- | --- |
| 文件 | 历史 `test.mp4`；502,018,281 bytes |
| SHA-256 | `[私有样本哈希保留于本地证据；校验一致]`；修复/深度分析前后均一致 |
| 视频 | HEVC Main / 2560×1440 / yuvj420p / Full / BT.709 / 8-bit |
| 平均 FPS | 约 59.9923305；nominal 60 |
| 音频 | AAC LC / 48 kHz / stereo |
| 输入时长 | video 123.382438 s；audio 123.477333 s；差 0.094895 s |

| 预设 | Analyze | Compatibility | Repair | Output spec | Color sample | Duration | Source unchanged |
| --- | --- | --- | --- | --- | --- | --- | --- |
| general / safe（快速分析） | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| bilibili / safe（快速分析） | PASS | PASS | PASS | PASS | PASS | PASS | PASS |

| 输出 | Video | Audio | Color | Timing | Compatibility | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| general | PASS | PASS | PASS | PASS | PASS | PASS |
| bilibili | PASS | PASS | PASS | PASS | PASS | PASS |

两者输出均为 H264 / yuv420p / Limited / BT.709、AAC 48 kHz、2560×1440。general 视频约 123.388 s，轨道差约 **0.089 s**；bilibili 视频约 123.417 s，60 FPS 元数据，轨道差约 **0.061 s**。没有从输入约 0.095 s 的差恶化。

真实颜色验证为第一帧独立缩小参考图比较：general 的 Y/U/V MAE 约 **0.192 / 0.298 / 0.282**，bilibili 约 **0.186 / 0.299 / 0.282**，均低于既定 3 码值上限。转码期间检查全部经过滤镜的输入帧属性；这不能代替全片像素质量评估或人工观看。

报告与逐项证据：真实样本回归（本地证据，不提交 Git：`tmp/release-audit/real-baseline-regression/matrix.md`）。临时转码输出已由测试机制清理，只保留小型文本/JSON 证据。

同一文件另外完成只读 deep analysis：全文件 13,101 packets、5,788 音频帧，视频 5 个窗口中实际计入 1,193 帧；没有把视频抽样称为全片逐帧分析。得到 **VFR_SUSPECTED、TIMESTAMP_ANOMALY 候选**：视频未观测到 DTS 回退/突跳，音频累计 89 次原始时间戳缺失计数；音频 PTS 与采样时钟拟合斜率近零，**没有足够证据声称存在 progressive drift 或物理音频时钟故障**。deep 模式的 safe 计划会选择 60 CFR，保留 Full→Limited，并关闭 async / genpts。上述两次修复使用的是快速分析，不冒充 deep 计划执行结果。深度证据与决策（本地证据，不提交 Git：`tmp/release-audit/real-baseline-regression/deep-analysis.txt`）。

**本次没有人工测量真实录屏开头、中段、末尾的事件音画误差，实际内容同步效果仍未验证。**

## 5. 时序、参数和性能审查

- `RepairPlan → builder → ExpectedOutputSpec → validation` 责任分离保留；GUI 显示模型和回调结果。没有把颜色与漂移业务规则挪入 GUI。
- safe 不根据音视频总时长差自动打开 async；只有完整、稳定、微小的音频 PTS/采样时钟证据才启用受限软补偿。现有 ±250 ppm、240 秒合成音频测试核对音调、长度和视频时序，但不代表真实声音事件对齐。
- 软补偿上限 500 ppm（0.05%），不以视频总时长强行拉伸音频。超过上限、缺时间戳、突跳、不完整证据均不自动补偿。
- 显式旧 `audio-sync` 在没有 deep 证据时仍采用 gaps/overlaps 的补缺口/裁重叠策略；不是通用渐进漂移修复，较大缺口可能可闻。没有默认全局启用。
- `genpts` 补缺失 PTS、负起点处理不等于修复中途所有异常；已知相对起点保留，不以全部轨道归零掩盖 offset。
- 普通颜色探测为固定 32 packet 开头预算；深度分析默认总时限 120 秒、1,000,000 records，32 条选中轨道上限，队列 64 行，单行上限 16,384 字符，拟合只留有限检查点。预算到达明确 partial，不自动授权补偿。
- 新输入帧检查与原转码同一次解码，禁用 showinfo 像素 checksum；Python 只留基线/计数。逐帧记录从错误日志缓存中分离，保留解码器真实错误。[FFmpeg showinfo 说明](https://ffmpeg.org/ffmpeg-filters.html#showinfo)。
- FFmpeg 命令一直以 argv 启动；不使用 shell 拼接媒体路径，拒绝 Windows 批处理包装器。修复先暂存，检查后排他发布。
- GUI 工作在线程内执行，以信号更新主线程；运行期间禁用会改变计划的控件。新增检查异常会经现有失败信号返回。日志主视图有行数上限。
- 未执行数小时 4K/8K、低内存机器、网络盘断连、真实磁盘满/断电压力测试。123 秒真实样本和 240 秒低分辨率合成测试不能替代此类性能验证；本次并发运行部分回归，耗时不作为性能基准。

## 6. 尚未验证 / 不支持

**未验证**：不同 NVIDIA/AMD/Intel Steam 实机素材集合；真实 20–60 分钟渐进漂移；4K/8K 长文件；真实 HDR 色彩正确性；7.1/特殊声道布局；不同 FFmpeg 6.1/7/8/其他 9.x 发行构建；Windows 10/ARM64；真实 FAT/exFAT/SMB/UNC/超长路径；强杀、断电、网络中断恢复；平台实际上传转码；全片声音/画面事件对齐；全片感知画质。

**当前明确不支持安全转换**：PQ/HLG、HDR/Dolby Vision 附加证据冲突的输入；未知/冲突颜色范围；未支持的颜色体系/RGB 转换；中途改变颜色属性/尺寸/像素格式；bilibili 预设下无法保持原尺寸的奇数边长；修复损坏码流以尽量抢救内容；手动校准固定内容 offset；任意幅度时钟漂移补偿。

只有第一条非封面视频轨输出；其他视频轨、字幕、封面、章节不会完整保留。音轨按既有策略全部映射并重新编码 AAC。10-bit 输出降至 8-bit，不是无损归档工具，也不把 BT.2020 自动映射到 BT.709。

## 7. 测试与交付状态

审查前完整基线：**835 unit PASS + 154 integration PASS + 1 private skipped**。

最终完整回归：**864 unit PASS + 176 integration PASS = 1,040 passed；1 private skipped；0 failed**。三阶段 runner 退出码 0。unit 6.47 秒，integration 148.92 秒，private 0.24 秒；这仅为本次测试耗时，不代表产品性能基准。`compileall` 和 `git diff --check` 也通过。执行入口：

```powershell
.venv/Scripts/python.exe -X utf8 -m tests.media_factory.run_regression
```

最终报告目录：Compatibility Matrix（本地证据，不提交 Git：`tmp/compatibility-reports/20260910T024526Z-7b18a0e5/matrix.md`）。另有独立真实样本 **2/2 PASS**，不可与空 private 目录的 skip 混为一谈。中间一次回归曾因错误详情被帧日志挤掉而失败；修复后保留原断言重跑，没有掩盖该失败历史。

新增测试集中在 `test_release_audit_integration.py`、`test_frame_validation.py`，并补充颜色/输出验证/发布测试。FFmpeg 快照只增加输入监视滤镜与必要日志级别，其余修复参数保持原有策略。既有 mock 适配新增诊断回调参数。未测覆盖率百分比。

修改涉及核心颜色证据（analyzer/models/color/compatibility/media_profile）、流式执行与发布（frame_validation/ffmpeg_utils/fixer）、RepairPlan 决策说明、输出 HDR 检查、相关单元/集成测试及快照、README。本次没有修改 GUI 架构或增加 GUI 控件。

## 8. 发布判定

**Release V2（广泛正式发布）：NO-GO。** 明确阻塞证据：

1. 真实回归只有一份短录屏，尚无跨机器/录屏设置的代表性集合。
2. 核心目标“开头同步、后来越来越偏”缺少真实长录屏与开头/中段/末尾独立事件测量；当前不能承诺实际音画内容已同步。
3. 发行环境尚未形成经过验证的工具版本/构建和 Windows/文件系统支持范围，当前只有本机组合。

已修复问题不再作为已知未解决的代码阻塞项。若作为受限测试候选，应明确限定目前实际验证的 Windows / NTFS / FFmpeg 9.0.1 / 稳定 SDR 输入范围，并把 HDR、特殊输入和内容同步保证排除在支持承诺之外。本次不执行发布。
