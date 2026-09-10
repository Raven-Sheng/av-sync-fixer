# 更新日志

## V2 开发阶段里程碑 — 2026-09-10

V2 将输入诊断、修复决策、FFmpeg 命令和输出验证连接为明确的数据流程，并增强了桌面界面的媒体诊断能力。本次提交保存完整 V2 阶段成果，**不代表已通过广泛正式发布验收**。发布判断及未验证场景见 [V2 Release Readiness Report](V2_RELEASE_READINESS.md)。

### 媒体分析与兼容性

- 扩展视频、音频、容器、颜色、位深、旋转、默认音轨与多轨信息；区分字段缺失、未知、无效和有效值，保留原始探测证据。
- 增加独立兼容性报告，识别 H264/HEVC、Full/Limited、常见像素格式、BT.709/BT.2020、潜在 HDR 和音频布局风险。合法 HEVC、yuvj420p、Full Range 输入不再被当成编码失败。
- 开头采用固定 packet 预算获取有限帧颜色证据，识别只出现在帧 SEI 中的 HDR 信息；未观察到 HDR 不等于证明全片是 SDR。

### 修复策略与颜色处理

- 引入 `RepairPlan`：业务层决定视频、音频、时间戳及容器策略，FFmpeg builder 统一生成参数。迁移 safe、cfr、timestamp、audio-sync 和 bilibili 预设组合。
- dry-run 输出具体决策与原因；不再仅根据轨道时长差同时打开 CFR、genpts、async 和颜色转换。
- 对已知 Full Range YUV 执行数值范围转换，输出 H264 / yuv420p / Limited，并保留已知 SDR 色彩体系；已为 Limited 时避免重复压缩。
- 不猜测未知或冲突的颜色范围；PQ/HLG、HDR 附加证据冲突和未支持的颜色转换明确停止。没有新增 HDR 色调映射。
- 转码时在颜色/CFR 滤镜之前流式检查输入帧属性；中途改变颜色、像素格式或尺寸时停止发布，防止“标签合格但像素错误”的输出。

### 同步分析

- 新增 CLI `--deep-analysis`，结合 packet/frame timestamps、起点、time base、帧率与累计音频采样时钟分析。
- 报告 STATIC_OFFSET、PROGRESSIVE_DRIFT、VFR_SUSPECTED、TIMESTAMP_ANOMALY 或 UNKNOWN 候选，不把长度差当作确定的漂移证据。
- 深度分析使用流式读取、视频窗口抽样、时间/记录预算和有界拟合数据，避免全量加载数百万帧 JSON；部分分析不会授权自动软补偿。
- 只有完整、稳定且微小的音频时钟证据才允许受限软补偿，上限 500 ppm；不以视频总长度强行拉伸音频。保留已知相对起点。

### 输出验证与文件安全

- 从 RepairPlan 生成 `ExpectedOutputSpec`，逐项比较实际输出与计划，输出 PASS / WARNING / FAIL 和 Post Repair Report。
- 检查文件存在/非空、ffprobe 可读、视频和音轨、编码、帧率、像素格式、颜色、采样率、尺寸、时长、起点及 MP4 faststart。
- 音视频时长差明显恶化仍判 FAIL，即使编码和颜色标签都符合计划；缺失证据不改写为 PASS。
- 先写临时文件，复查通过后排他发布；保护源文件和已有输出，处理并发目标创建、执行失败和中断清理。
- Windows 最终发布不再依赖硬链接；保留中文、空格、Unicode 路径的 argv 调用及批处理包装器防护。
- 帧诊断与错误日志缓存分离，避免大量帧日志掩盖原始解码错误。

### 桌面界面

- 选片后显示媒体兼容性摘要、推荐处理和“为什么需要修复？”说明。
- 技术字段和完整计划放入折叠详情；修复后显示 Before/After，以及同步、颜色、编码、音频、平台兼容五项结果。
- Steam 来源提示以元数据证据为依据，不根据文件名、HEVC 或 Full Range 猜测。
- GUI 继续消费核心模型，通过后台线程和信号更新界面；失败和缺失证据不会显示为成功。

### 测试与回归

- 增加动态短媒体工厂、A–O Input Compatibility Matrix、独立像素数值检查、RepairPlan 单元测试和命令快照。
- 增加可选 `samples/private/` 真实素材回归；目录不提交 Git，没有素材时明确 NOT TESTED。
- 覆盖 10-bit SDR、Full/Limited、BT.2020、多采样率、多音轨/5.1、HDR 拒绝、运行中颜色变化、损坏输入和发布竞态。
- 生产审查完整回归：**864 单元测试 + 176 集成测试通过，1 个 private 阶段跳过，0 失败**。
- 另对一份约 502 MB、123 秒、1440p HEVC Full Range 历史真实录屏验证 general / bilibili：**2/2 通过**，源文件哈希未改变，时长差未恶化。该结果不能替代真实音画内容对齐测量。

### 已知限制与发布状态

- A–O 矩阵覆盖 14/15 类；字面 `H264 / yuv420p / Full` 组合仍 NOT TESTED，本机相应全范围 stream 表示为 yuvj420p。
- 当前实际测试环境为 Windows 11 / NTFS / FFmpeg 9.0.1。保持帧率命令语法至少需要 FFmpeg 6.1，但没有认证所有 6.1+ 版本或定制构建。
- 尚缺跨电脑、真实长时间渐进漂移、特殊音轨/文件系统、平台上传转码等证据。
- 不支持 HDR 色调映射、未知/冲突颜色的猜测转换、中途变化输入的自适应修复或任意幅度时钟漂移补偿。10-bit 输出仍降为 8-bit。
- GUI 目前没有 deep-analysis 入口、运行中取消或批处理；同步 PASS 表示符合输出计划，不保证实际声音与画面事件同步。

## V1 初始导入

- 导入 CLI 媒体诊断、多策略修复、Bilibili 预设、PySide6 GUI 和测试套件。
- Git 基线：`d639dfa`（Import av-sync-fixer v1 CLI, GUI, and test suite）。
