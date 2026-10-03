# 系统与 ROS 2 性能工具参考

检索日期：2026-10-03。范围：Orin/Thor 上的宿主 ROS 2 节点、组件容器、中间件通信和 Linux 调度。本文根据官方文档及项目原始仓库整理；外部工具尚未在本项目的 Orin/Thor 实机上验证。

现有基础采集、通信基准和周期任务测试与成熟工具存在重叠。建议保留本项目的自动发现、环境记录、测试编排、指标口径和报告，逐步复用外部追踪与诊断工具。以下接入顺序是建议，不能视为已实现功能。

## 官方与上游工具

| 工具与来源 | 主要能力 | 对本项目的用途与边界 |
| --- | --- | --- |
| NVIDIA [tegrastats](https://docs.nvidia.com/jetson/archives/r38.2/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html) | CPU、RAM/SWAP、GPU 活跃率、EMC 使用率、频率、温度及功率等 | 第一阶段优先补充 Jetson 整机指标。字段取决于平台和版本；整机 GPU/EMC 指标不能直接归属某个业务进程 |
| NVIDIA [Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html) | CPU 采样、线程切换、CUDA API、GPU kernel、数据传输和同步时间线 | 第二阶段定位 CPU/GPU 协同、等待和同步问题。具体采集能力取决于平台、驱动、版本和权限 |
| ROS 2 [ros2_tracing](https://github.com/ros2/ros2_tracing) | 基于 LTTng 的 ROS 核心事件和回调追踪；提供 CLI 和 launch 集成 | 补充组件容器内部回调观察。ROS 用户态事件与内核调度事件需要分别配置 |
| NVIDIA [ros2_benchmark](https://github.com/NVIDIA-ISAAC-ROS/ros2_benchmark) | ROS 业务图吞吐、延迟、抖动与计算资源利用；支持 rosbag 和实时输入 | 对真实算法链路做可重复基准，需要配置输入、输出监测和测试图；不能自动识别任意已有业务 |
| Linux [cyclictest / rt-tests](https://wiki.linuxfoundation.org/realtime/documentation/howto/tools/cyclictest/start) | 测量周期线程预期唤醒与实际唤醒的时间差；rt-tests 还包含其他实时性测试 | 与 S01 的唤醒迟到交叉验证。周期、调度策略、优先级、CPU 绑定、负载和电源条件须对齐；不能要求不同口径的指标相等 |
| Linux [RTLA](https://docs.kernel.org/tools/rtla/rtla.html) | timerlat 测量 IRQ/线程定时器延迟，osnoise 分析操作系统干扰 | 第二阶段分析系统实时性异常；需要目标内核的 tracer 支持。它不是任意业务线程的完整回调分析工具 |

Jetson 应选择与目标 JetPack 匹配的 Nsight Systems Embedded Platforms Edition；不能仅凭 ARM64 架构选择通用工作站安装包。参考 [Nsight 安装说明](https://docs.nvidia.com/nsight-systems/InstallationGuide/)。

## GitHub 与其他开源项目

官方工具也可能托管在 GitHub，本节补充其他相关项目。

| 项目 | 主要能力 | 与现有功能的关系 |
| --- | --- | --- |
| [jetson-stats / jtop](https://github.com/rbonghi/jetson_stats) | Jetson 硬件、JetPack 识别，CPU/GPU/内存等监控，Python 接口 | 与硬件采样重叠，可参考平台适配。也具有风扇、功耗及频率控制能力，监测时不要调用控制接口 |
| TIER IV [CARET](https://github.com/tier4/caret) | 回调时延、通信时延，以及跨节点/回调路径时延 | 补足 ROS 链路分析。主仓库是 meta-repository，完整采集/分析需要配套仓库、专用 tracepoints 和运行环境适配 |
| [ros2/ros2-performance](https://github.com/ros2/ros2-performance) | 根据拓扑构造系统，统计延迟、可靠性、CPU 和内存；含 composition benchmark | 与 C01 合成基准重叠。README 说明主要面向单进程，多进程场景有指标限制，测试节点默认不执行真实算法计算 |
| [ros-realtime/reference-system](https://github.com/ros-realtime/reference-system) | 固定节点、消息、处理时间与发布频率，比较 executor 和其他配置 | 用作第二阶段可控参考业务；项目定义的参考系统要求节点运行于同一进程 |
| [iovisor/bcc](https://github.com/iovisor/bcc) | runqlat：调度排队分布；offcputime：off-CPU 时间及栈；profile：CPU 热点 | 补充当前 schedstat 区间累计值的解释能力。off-CPU 时间包含不同等待原因，不能直接等同于锁等待或执行器等待；需检查 BPF、内核及权限支持 |
| [sysstat](https://github.com/sysstat/sysstat) | pidstat：进程/线程 CPU、内存、I/O、切换；mpstat：每核 CPU；iostat：存储；sar：系统历史 | 可交叉验证现有资源统计，也可补充存储和网络观测 |
| Apex.AI [performance_test](https://gitlab.com/ApexAI/performance_test) | ROS 2/DDS 等 pub/sub 中间件通信性能基准；performance_report 提供批量实验与绘图 | 与 C01 重叠。旧 [GitHub 仓库](https://github.com/ApexAI/performance_test)已标记迁移，后续以 GitLab 项目和实际版本为准 |

## Orin / Thor 与软件版本

| 项目 | 已检索到的支持信息 | 实测前仍须核验 |
| --- | --- | --- |
| tegrastats | 文档包含 Orin 与 Thor。GPU GPC 数量、功率轨名称等有差异；文档说明 Orin 的 NVDLA 指标不适用于 Thor | 使用设备实际输出建立字段映射；不可用字段记录 null 与原因 |
| Nsight Systems | 官方提供 Tegra/Jetson 版本；用户指南明确列出 Thor 的部分 CPU metrics 支持 | JetPack、Nsight、CUDA/驱动组合；具体事件可用性；丢失事件及采集开销 |
| jtop | 当前 README 列出 Orin、Thor；发布记录含 Thor 专项适配 | 安装版本与 JetPack 匹配；实际指标完整性。见 [发布记录](https://github.com/rbonghi/jetson_stats/releases) |
| ros2_tracing | Linux/LTTng；仓库要求使用与 ROS 发行版匹配的分支；README 说明从 Iron 起 LTTng 成为 ROS 依赖 | Humble 等具体安装是否启用 tracepoints，使用 `ros2 run tracetools status` 核验；内核 tracer 需另行准备 |
| ros2_benchmark | 当前 README 支持表列出 Humble、ARM64/x86_64，并有 Orin 测试说明 | 不能推定 Thor、Jazzy 或其他组合已验证；还需核验输入时间戳、QoS 和图配置 |
| CARET | 当前安装文档列出 Humble/Ubuntu 22.04 和 Jazzy/Ubuntu 24.04 | 按 [安装说明](https://tier4.github.io/caret_doc/main/installation/installation/)核验 ROS 库、LTTng、Python 和 ARM64 构建；只安装分析包不等于可采集 |
| RTLA/BCC | 依赖 Linux 内核 tracer/BPF 等能力 | 目标 BSP 的内核配置、tracefs、头文件、权限和事件支持；安装命令成功不代表采集可用 |

### 运行中的业务与自动发现

本项目 monitor 可以观察已运行的进程及动态线程，不要求业务重启；自动发现只识别候选进程。组件容器内多个 ROS 节点共享进程资源，不能据此拆分算法耗时。

ros2_tracing 通常需要在业务初始化前配置追踪，以保留节点、发布订阅及回调元数据。快照或双会话可以按需保存运行事件，但仍须保留初始化信息。CARET 同样需要按其采集说明准备库和启动环境。见 [ros2_tracing 追踪说明](https://github.com/ros2/ros2_tracing#tracing)与 [CARET recording](https://tier4.github.io/caret_doc/main/tutorials/recording/)。

### Docker 与实机

Docker 可验证构建、解析、数据格式和工具链路。性能基线应在目标 Orin/Thor 上采集，并记录宿主内核、JetPack、功耗/散热、ROS/RMW、容器限制及测试负载。

Docker 共享宿主内核。监测宿主业务需要正确的 PID 可见范围和硬件接口；Nsight 的 CPU profiling 还受 `perf_event_open`、seccomp 与权限约束。jtop 的容器使用说明要求宿主服务与 socket 映射。不要把 Docker Desktop 的虚拟机结果当作 Jetson 基线，也不要把容器启动成功视为内核/GPU 采集已可用。参考 [Nsight 用户指南](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)和 [jtop Docker 说明](https://github.com/rbonghi/jetson_stats#docker)。

## 两阶段接入建议

### 第一阶段：建立指标和交叉验证

1. 保留现有 C01、S01、业务自动发现、环境档案、原始数据和报告。
2. 优先接入 tegrastats，补充 GPU 活跃率、EMC 使用率等；保留原始输出、采样时间与平台字段差异。
3. 用 pidstat/mpstat、cyclictest 对照验证资源和唤醒统计，明确窗口、CPU 百分比口径、线程身份与调度条件。
4. 对真实业务选择 rosbag 或实时输入，试用 ros2_benchmark 建立可重复链路基准。
5. 所有外部采集器先单独测开销，再与业务组合；记录缺失字段、丢失事件和覆盖范围。

### 第二阶段：按异常选择诊断工具

| 异常或问题 | 优先工具 | 希望得到的证据 |
| --- | --- | --- |
| CPU 高或函数热点 | perf / BCC profile / Nsight CPU sampling | 函数/调用栈的采样占比与时间范围 |
| 线程就绪后迟迟不运行 | BCC runqlat；必要时内核调度追踪 | 排队延迟分布、相关线程与调度事件 |
| 周期线程唤醒迟到 | cyclictest + RTLA timerlat/osnoise | IRQ、线程延迟与干扰来源 |
| 组件容器内部回调变慢 | ros2_tracing / CARET | 回调执行、通信及业务路径时延 |
| GPU 活跃率低或 CPU/GPU 等待 | Nsight Systems | CUDA API、kernel、传输与同步时间线 |
| executor/调度配置取舍 | reference-system / composition benchmark | 固定拓扑与负载下的对比结果 |

优化前后保持业务输入、设备条件和指标口径可比，使用多轮原始数据评估尾延迟、违约、吞吐和资源变化。接入外部工具不能自动替代本项目对输入质量、采样覆盖和采集开销的判断。

## 本地下载与后续复用

下载资料保存在仓库根目录的 `reference-downloads/2026-10-03/`，由 Git 和 Docker 构建上下文忽略。它是本机参考缓存，另一台机器 clone 本项目不会带上这些文件；如需离线使用可单独复制整个目录。

本次下载结果：10 个源码归档（8 个 GitHub 项目、Apex.AI GitLab 项目、rt-tests 2.11），6 份 HTML 文档和 1 份官方 checksum 文件，合计约 19 MiB。源码归档可打开读取，另提取了 README 便于直接查阅。所有成功下载文件均复核了本地 SHA-256；rt-tests 还与本次取得的官方 checksum 文件匹配，未做 GPG 签名验证。

cyclictest 官方文档页面直连与本机代理均返回 HTTP 403，未取得 HTML 快照；在线入口仍保留，cyclictest 源码已包含在 rt-tests 归档。Apex.AI 初次直连返回 403，使用本机代理后按 commit 下载成功。详细结果保存在本机下载清单，不随源码公开。

- 开源项目保存源码压缩包，不自动安装、构建、执行或递归下载子模块。
- GitHub 快照按下载时的默认分支 commit 固定；滚动分支仅供参考，部署时须选择与目标 ROS 发行版匹配的版本。
- `manifest.json` 记录来源、实际 commit/版本、文件大小、SHA-256、归档条目和下载状态；`INDEX.md` 提供可点击的本地入口。失败记录不会冒充下载成功。
- 官方文档保存 HTML 页面；部分页面依赖在线样式、脚本、图片或链接，不是完整离线站点。源码包中已有的 README 可直接离线阅读。
- CARET 主包是 meta-repository，后续完整使用须读取 `.repos` 下载相应发行版的配套仓库。参考系统、性能测试等项目也可能需要子模块或数据集。
- Nsight 安装包、JetPack 内 tegrastats 可执行文件以及 Linux 内核中的 RTLA，不统一下载通用二进制；应在确定目标设备版本后从官方渠道获取。本次保留文档，rt-tests 另保存官方发行源码。
- 保留第三方 LICENSE/COPYING。尤其 jtop 当前仓库标注 AGPL-3.0，直接整合或分发前应核对具体文件及许可证；参考下载不改变本项目运行依赖。

下载仅证明文件已取得并通过相应格式/哈希记录检查，不证明构建、功能、真实性签名或 Orin/Thor 兼容性。下一项验收应是在目标设备选定版本后，实际采集一个已知负载并检查指标和开销。
