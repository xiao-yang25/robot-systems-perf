# Orin / Thor 测试指南

## 运行前

在目标 Linux ARM64 Jetson 上执行。需要可用 Docker 与镜像下载条件；不要求刷机或安装宿主 ROS。当前 CPU/ROS 测试没有 CUDA 依赖，默认 Ubuntu 22.04 / ROS 2 Humble 用户态；它与设备 BSP 是分别记录的层。若使用其他 ROS 版本，通过 BASE_IMAGE 指定兼容 ARM64 镜像，再重新构建与 smoke 验证，不把未测版本标为通过。

记录具体模组、BSP、内核、生产部署方式、功耗模式、频率策略、风扇/散热与环境条件。入口会生成与结果目录同级的 `*-host.json`；容器每个运行 environment.json 同时保存该宿主快照和容器环境。缺失的现场信息使用 [平台模板](../templates/platform-profile.template.json) 补充并保存在 results 下。模板只是记录文件，不作为运行配置解析。

宿主档案包含当前内核启动参数等现场信息；共享结果时按实际内容处理。源码提交不包含 results。当前脚本仅执行 nvpmodel -q 查询，不配置 nvpmodel、jetson_clocks、实时优先级或 CPU 隔离。

## 先检查，再建立基线

```bash
REQUIRE_JETSON=1 ./scripts/run-docker.sh configs/suite-smoke.json results/device-smoke suite
```

短套件成功后，复制正式探索配置，先确认参数，再执行：

```bash
cp configs/jetson-suite.json configs/device-suite.json
python3 -m perfkit.suite --config configs/device-suite.json --output results/planned --dry-run
REQUIRE_JETSON=1 ENVIRONMENT_KIND=jetson-container \
  ./scripts/run-docker.sh configs/device-suite.json results/device-baseline suite
```

`device-suite.json` 是你的实验配置；不要在已经产生的结果目录内继续运行或覆盖配置。Orin 与 Thor 使用各自档案和结果目录。默认 12 个 case，8 个参考/压力条件各 3 轮，4 个采集对照条件各 1 轮；名义时长 2044 秒，不包括构建、发现、分析和过载时的延长。

## 必须确认的参数

- defaults 的预热、测量时长、轮次与资源采样间隔。默认预热不保证设备热稳定；按现场状态延长。高频测试每个 case 的预热加测量最多 1,000,000 条记录，需要长时观察时分批运行并保留所有批次。
- 各 case 的频率、有效载荷、QoS 深度/可靠性、CPU 干扰 worker 数、回调等待与周期工作。干扰实际 CPU 使用量可从资源汇总核对，不能仅凭 worker 数判断压力。
- deadline_us 与 max_data_age_us（仅 C01）。截止期锚为计划释放时刻；未配置时不判断业务达标。套件 S01 的 1000 us 是探索示例，C01 默认 null。
- quality_limits.min_samples 与 max_release_late_fraction。默认 1000 样本及“迟到超过一个周期的比例不超过 1%”是可修改的测量质量起点，不是业务时限。压力 case 超出时保留结果并报告，不能静默删除。
- sampler_comparisons 的 max_p99_increase_percent。默认 null，填入事先约定的采集开销预算后才给预算评价。只对比资源采样增量，双方仍保留 C++ 时间戳记录。

资源采集对照四个 case 必须保持同一负载和要求，只改变 sampling_mode，执行顺序为 minimal/basic/basic/minimal。程序保留各轮 P99，并计算这些逐轮值的中位数差异；不把它称为合并样本 P99，也不将一次差异归为确定采集开销。

## 网络与资源约束

默认同容器的两进程通过 localhost discovery 通信，Docker network 为 bridge、IPC 为 private。这不是跨设备网络实验。按生产部署设置明确约束，例如：

```bash
REQUIRE_JETSON=1 ENVIRONMENT_KIND=jetson-container DOCKER_NETWORK=host \
  DOCKER_CPUSET=0,1 DOCKER_CPUS=2 \
  ./scripts/run-docker.sh configs/device-suite.json results/device-constrained suite
```

CPU 编号需按该设备实际拓扑选择；配额与绑定含义不同。限制作用于整个容器，含 ROS 后台线程和采样器。脚本不更改宿主设置、不授予 privileged 权限。原生入口可在已经配置好的生产调度环境下执行，与容器实验分别建基线。

```bash
REQUIRE_JETSON=1 ENVIRONMENT_KIND=jetson-native \
  ./scripts/run-native.sh configs/device-suite.json results/device-native suite
```

只测试S01时无需安装或加载ROS，入口自动设置 `EP_BUILD_ROS=OFF`，只构建周期任务：

```bash
REQUIRE_JETSON=1 ENVIRONMENT_KIND=jetson-native \
  ./scripts/run-native.sh configs/s01-only.json results/device-s01-native
```

需要Linux、CMake、C++17编译器和Python3.10+。自行构建时可用 `cmake -S . -B build -DEP_BUILD_ROS=OFF`。直接Python启动runner/suite的新运行也记录启动时当前进程可见的平台档案，`host_profile_source` 区分外部档案与本地视图；`required_binaries` 和 `binary_sha256` 只记录实际使用的程序。旧结果不会补写。Ubuntu24/BSP38正式ROS依赖与镜像digest仍需按现场准备和验证，S01独立构建不能证明这套ROS环境已适配。

Linux集成回收检查需要CPython提供 `os.pidfd_open` / `signal.pidfd_send_signal` 且内核/权限允许调用，SIGCHLD必须为默认处理器且无并发reaper；忽略或自定义处理器在创建负载前明确拒绝。不支持时明确失败，不用裸PID回退。业务monitor入口不依赖这套测试辅助器。兼容回归可分别用模块方式或从任意目录调用测试脚本；集成检查仍需要其对应的源码、配置与已构建程序。

本轮升级后先进行短兼容检查，再复测相同业务、相同线程覆盖和周期的旧/新采集器，优先比较发现成本、线程成本、总CPU及超周期。随后单独比较 [轻量/完整profile](BUSINESS_MONITOR.md#分层采样与可选-jetson-遥测04)；降低覆盖的结果另列。需要cgroup早筛时确认实际服务路径、同一候选范围和动态移入/移出，不能直接把开发fixture读取削减比例当作设备收益。

## 如何读取结果

每轮看发送质量、实际收发、响应/交付与期限、资源和异常五部分。C01 的真实窗口速率使用统一半开计划窗口，窗口后的交付另列；观察发布速率与计划记账速率也分别保留。chain_latency 从实际产生开始，response_time 从计划释放开始，因此要同时查看 release_lateness。

诊断保留直方图、一秒时间桶、top10 尾事件、连续违约及已完成超期幅度。未交付计入配置期限的违约分母，但不能把未知原因的缺失称为网络丢包。数据年龄只评价有效交付，缺失交付另列。

资源汇总仅使用整个采样区间落在测量窗口内的快照；计数器仅对连续有效样本做差分。不外推窗口边界，不把采样峰值当精确峰值。覆盖时长、拒绝的重置区间、可用性与 scope 都写入报告；不足两个快照时累计指标为不可用。短测试、任务快速退出或权限限制可能使部分进程指标不可用。

/proc/stat 与 meminfo 是可见系统视图；进程/线程和 cgroup 指标独立展示。schedstat 的运行队列等待是采样区间累计值，只能作为线索，不能解释某条消息的具体等待；计数为零且内核统计状态未知时也不能断言没有等待。[Linux 调度统计](https://docs.kernel.org/scheduler/sched-stats.html)

温度、CPU 频率、具名 GPU/EMC devfreq 频率和 hwmon 功率只在当前 sysfs 可见时记录。功率字段单位为 microwatt，保留 rail 名称，不能将可能重叠的电源 rail 相加。[Linux hwmon 单位规范](https://docs.kernel.org/hwmon/sysfs-interface.html)

0.4可通过资源配置 `jetson_telemetry: true`（monitor也支持 `--jetson-telemetry`）采集该BSP的tegrastats GPU活动、频率及EMC活动百分比，默认关闭；工具缺失与字段缺失保留null和原因。精确EMC带宽仍未采集；接收时钟不代表设备生成时间，不能将未关联日志直接拼成单条消息的因果链。[NVIDIA tegrastats](https://docs.nvidia.com/jetson/archives/r38.2.1/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html)

## 基线与优化边界

complete 仅表示工具成功完成。业务达标需有真实期限与交付要求；正式基线还需确认输入质量、热稳定、样本量与测量开销。内部队列/执行器等待、单次内核调度事件、GPU 推理干扰和机器人实际输入输出链路留待第二阶段按发现接入。现有 C01/S01 可直接采集平台参考点，但不能独自完成这些归因。

先在同台设备、固定配置下比较改动前后，再比较 Orin/Thor。无法隔离内核、BSP、用户态与散热差异时，结论是平台与软件栈组合差异。容器不替代宿主内核；Mac Docker Desktop 是 Linux VM，只证明工具链路。

## 采集后提供的材料

敏感现场默认采用本地验证、脱敏汇总交接：完整结果及现场代码留在设备受控环境中，无需上传原始日志或diff。下面的完整目录清单用于现场保留和本地排查；仅在允许分享时提供副本。通常只需反馈设备类别、版本代号、采集配置、各批采集器父进程/含自有子进程CPU、发现与采集阶段耗时、实际采样间隔、超期次数、目标/线程数量、缺口数量及预算状态。进程名、PID、命令行、业务路径、IP和错误全文可以不提供；质量提示按通用类别描述，不粘贴可能带路径的原始错误。

本轮通用优化不依赖现场敏感补丁。现场合并时保留既有修复，核对冲突并运行测试；尤其核对平台探测降级和自有测试进程清理。未经现场合并和验证，不能把Docker通过当作现场版本通过。

### 同覆盖复测

先在更新前保存现场 `perfkit/resources.py` 到仓库忽略的结果目录，再保留现场修复并合并新版。以下样例只启动并观察自己的一份49线程测试进程，默认跳过温度；需在仓库根目录、Linux设备上运行。输出目录必须是新的。

```bash
# 更新前保存，保留现场实际代码，不上传
mkdir -p results/collector-baseline
cp perfkit/resources.py results/collector-baseline/resources.py
# 合并优化后，运行三批交替顺序的同覆盖对照
python3 -m unittest discover -s tests -v
python3 -m tests.integration_collector_optimization \
  --baseline results/collector-baseline/resources.py \
  --output results/collector-comparison
```

基线模块需支持现有resource-options接口；比较结果记录双方源码摘要。该样例验证采集开销和覆盖，不依赖ROS业务或标定设备性能。随后固定真实业务输入、UID/cgroup/名称/PID规则及采样周期，分别做至少三批前后对照，检查相同目标覆盖、有效计数区间和读取缺口；进程/线程数量本来会变化时，报告实际变化，不能只比较CPU百分比。开启遥测时同时检查总CPU及自有子进程可用性；总成本缺失不算预算通过。

Orin、Thor 分别提供一份完整结果目录，并附业务说明。首次可先采集30–60秒的稳态业务，用于确认目标选择、接口权限和指标覆盖；这不是正式性能基线或业务达标验收。采集方式见 [业务采集指南](BUSINESS_MONITOR.md)。确认采集有效后，再安排较长的重复测试和相同业务输入下的无采集/有采集对照。

### 完整结果目录

业务 monitor 模式保留以下工具实际生成的文件；启动中断或失败时也保留已有文件和终端错误信息，缺失文件注明原因即可。

| 文件 | 分析用途 |
| --- | --- |
| `environment.json` | 设备、内核、可读接口、源码版本及可见范围 |
| `monitor-config.json`、`monitor-status.json` | 实际采集参数、运行状态与错误 |
| `discovery.jsonl` | 目标选择依据、登记/移除事件及遗漏 |
| `resources.jsonl`、`resources-costs.jsonl` | 原始资源观测、采样窗口与阶段成本 |
| `monitor-summary.json`、`MONITOR_REPORT.md` | 指标汇总、覆盖、缺失原因和质量提示 |
| `resources-tegrastats.jsonl`、`resources-tegrastats.jsonl.stderr.log` | 可选遥测的原始接收记录与错误日志，仅在启用且生成时提供 |

若运行 C01/S01 或套件，提供整个运行/套件目录，包含所有 case、CSV、配置、环境、状态、报告及已有日志。使用 Docker/native 脚本时，一并提供结果目录同级生成的 `*-host.json`；不要把不同设备或不同运行的环境文件混用。

### 业务与运行条件

| 补充信息 | 建议内容 |
| --- | --- |
| 设备 | Orin/Thor 具体型号、内存、BSP；环境文件已记录的内容无需重复填写 |
| 目标业务 | 期望观察的进程名称/PID、大致职责、关键线程；标明预期对象是否出现在发现记录中 |
| 软件与部署 | 实际业务的 ROS 2 版本、RMW 实现、宿主或容器部署；容器内外的 PID 对应关系（若适用） |
| 业务负载 | 相机/传感器数量、分辨率与FPS、模型、任务频率或输入数据集，按实际业务填写 |
| 运行条件 | 功耗模式、是否锁频、散热/环境温度、CPU绑定、调度策略或容器配额（若已设置） |
| 采集时段 | 启动、稳态或异常发生时；期间是否切换任务或负载，异常大致时间 |
| 问题与目标 | CPU高、周期抖动、掉帧等现象，以及期望频率、时限或允许的违约要求；未知项注明未确定 |

monitor 的平台预检不自动读取实际业务的 ROS/RMW 配置，这两项需人工确认。Python 节点或组件容器内的节点关系也需补充说明；进程资源指标不能直接归因到某个 ROS 回调。

可将下面内容保存为本次结果目录中的 `FIELD_CONTEXT.md`，按实际情况填写，无需提供算法源码：

```text
设备：Orin / Thor，具体型号、内存与BSP
业务：感知 / 规划 / 控制等，目标进程及职责、关键线程
软件：实际ROS 2版本、RMW、宿主或容器部署
负载：输入数量、频率、分辨率、模型或数据集
运行条件：功耗模式、锁频、散热、CPU绑定、调度策略/容器配额
采集时段：启动 / 稳态 / 异常；期间的负载变化与异常大致时间
问题与目标：现象、期望频率/时限、允许的违约要求
采集完整性：目标是否选中；缺失指标、失败或中断及终端错误
```

### 打包与后续分析

先结束采集并补充业务说明，再打包完整目录。例如：

```bash
tar -czf orin-trial-001.tar.gz -C results orin-trial-001
tar -czf thor-trial-001.tar.gz -C results thor-trial-001
```

使用自己的实际目录名；每台设备、每次采集分别保留。脚本生成的同级 `*-host.json` 可单独提供，或作为额外路径加入打包命令。现场结果与填写的业务说明用于单独分享，不提交到公共源码仓库。

收到材料后，先核对平台能力、目标范围、采样覆盖和开销，再分析CPU、内存、调度累计值与可用遥测。确认指标有效且实验条件可比较后，进入瓶颈定位、优化及复测。

## 故障定位

先读 suite-status.json，定位失败 case，再读 run-status.json 及子进程日志。失败退出非零并保留现场；重复目录被拒绝。发送者超时、采样失败、接收记录溢出或未正常退出都不会包装为成功报告。SIGTERM/中断会清理当前发布者、订阅者及 CPU 干扰进程。

重新执行使用新的结果名；若构建阶段失败，可能已经保留同级 host.json。后续需要旧镜像/离线源码时单独准备，不把 Docker 镜像或设备结果塞进源码仓库。
