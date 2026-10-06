# robot-systems-perf

开发板上的平台验证与受控ROS事件链见 [开发板接入指南](docs/DEVELOPMENT_BOARD.md)；业务事件不足时先验证通用接入，再接已知构建的算法回放和整机工况。

新增可复现的[Docker机器人参考场景](docs/DOCKER_REFERENCE.md)：导航、感知融合控制、平面机械臂三个跨进程闭环拓扑，共用现有资源和事件测量。运行 `./scripts/run-reference-docker.sh all results/reference-001`；基础算法与简化仿真结果不替代Nav2/MoveIt或生产业务验收。

面向具身机器人的中间件与系统软件性能工具。支持在 Orin/Thor 上采集通信、周期任务和资源基线，再按证据开展优化。

目标是 Orin/Thor 的通用基础适配：同一套工具通过环境识别和配置用于不同设备，具体算法部署通过 PID、名称、用户和 cgroup 等规则选择采集范围。平台基础功能不依赖某台机器的算法源码或固定服务名；真实业务链路、消息类型和期限按应用另外配置。基本适配不代表所有 BSP/ROS 版本、硬件指标和业务链路已经验证。

## 测量内容

| 模块 | 输出 |
| --- | --- |
| M1：新机器只读接入 | 机器档案、逐项接口能力、人工声明、light 建议配置与执行状态；不自动启动性能测试 |
| C01：ROS 2 同机跨进程通信 | 发送迟到、发布调用耗时、通信/数据年龄/响应时延、实际窗口收发速率与有效载荷吞吐、交付完整性 |
| S01：Linux 周期任务 | 启动偏差、周期误差、墙钟/CPU/响应时间、截止期违约及超期幅度 |
| 诊断 | 分位数、直方图、时间分段、尾事件各阶段关联、连续违约；C01 实际 RMW/QoS、映射库摘要与声明配置 |
| 资源 | 每核 CPU、进程/线程 CPU 与可用 schedstat、RSS、缺页、切换、cgroup 节流、温度/频率与可见功率传感器；可选 Jetson GPU/EMC 活动遥测 |
| 测试套件 | 参考点、大小载荷、高频、QoS、慢消费者、CPU 干扰及资源采集 ABBA 开销对照 |
| 业务进程采集 | 自动发现现有活跃进程、按名称/PID纳入对象、动态线程与进程重启跟踪；输出资源而非消息时延 |
| M2a：业务功能关系 | 现场清单按功能独立筛选进程，保存身份、歧义/冲突与共享资源引用；节点归属保持人工声明 |
| M3a：业务事件离线导入 | 同机输入输出按样本ID及身份关联，输出E2E分位数、吞吐、未完成/丢弃/无效结果、覆盖及选填deadline；现场事件需另行接入 |

保留原始 CSV、配置、环境、资源 JSONL 和逐轮报告；缺失能力标为不可用。内核等待是采样区间累计值，不能定位单次调度原因；内部队列、执行器就绪等待、GPU 推理干扰仍待实现，真实业务事件、节点归属与扰动需现场验证。

与官方及开源工具的有限对照入口见 [工具对照指南](docs/TOOL_COMPARISON.md)：pidstat 资源采样、cyclictest 周期唤醒，以及 ROS 2 tracing 的 Fast DDS/Cyclone DDS 事件采集。先预检查，再按固定轮次执行。

## 快速验证

需要 Git、Docker、Bash 和宿主 Python 3。默认镜像为 Ubuntu 22.04 / ROS 2 Humble；使用 Docker 引擎原生架构，不使用跨架构仿真。

```bash
git clone https://github.com/xiao-yang25/robot-systems-perf.git
cd robot-systems-perf
./scripts/run-docker.sh configs/suite-smoke.json results/suite-smoke suite
```

公共仓库支持免登录 HTTPS 克隆。短套件包含 12 个 case，通常数分钟；仅验证工具链路。每次必须使用新结果目录。原来的单次入口仍可用：

```bash
./scripts/run-docker.sh configs/smoke.json results/smoke
```

## Orin / Thor 测试

在目标设备运行，分别使用新的结果目录：

```bash
REQUIRE_JETSON=1 ENVIRONMENT_KIND=jetson-container \
  ./scripts/run-docker.sh configs/jetson-suite.json results/orin-baseline suite
```

在 Thor 上将结果目录改为 `results/thor-baseline`。入口只读采集宿主档案，校验本地 Linux ARM64 Jetson；不自动改变功耗、频率、CPU 绑定或调度策略。默认套件预热 10 秒、测量 60 秒、重复 3 次，名义时长约 34 分钟，加上构建、发现和分析时间。热稳定及稀有尾延迟需要按业务延长。

运行前复制并调整套件参数，补充模组、功耗和散热条件；C01 默认无业务截止期，S01 的 1 ms 是探索示例。发送质量与采集预算同样需要确认。详见 [设备测试指南](docs/JETSON_RUNBOOK.md)。

已安装兼容 ROS 2、C++、CMake 和 Python 3.10+ 的目标 Linux 设备也可原生运行：

```bash
REQUIRE_JETSON=1 ENVIRONMENT_KIND=jetson-native \
  ./scripts/run-native.sh configs/jetson-suite.json results/orin-native suite
```

只验证周期任务时，Linux 上无需 ROS，入口仅构建 `periodic_bench`：

```bash
./scripts/run-native.sh configs/s01-only.json results/s01-native
```

需要 CMake、C++17 编译器和 Python 3.10+；该配置的 1 ms 截止期是参考条件，不能直接作为业务达标线。

## 采集现有业务

已运行的宿主 ROS 2 节点/组件容器可直接采集，无需重启业务或构建 C++。在仓库目录安装一次（Python 3.9+）：

```bash
python3 -m venv "$HOME/.venvs/robot-systems-perf"
source "$HOME/.venvs/robot-systems-perf/bin/activate"
python3 -m pip install .
```

新机器先做只读接入，核对环境与缺口；不需要 ROS 或 C++ 构建：

```bash
robot-perf-intake --view native-host --require-jetson --machine-id robot-demo \
  --output results/machine-intake-001
```

安装后可在任意目录执行。机器代号与平台视图是操作者声明；工具文件存在不代表采样已验证。人工补充模板、五个输出文件与设备复核方法见 [机器接入指南](docs/MACHINE_INTAKE.md)。

0.5.1 可用 `--skip-temperature` 显式跳过接入温度探测并记录 skipped；仅不要求 thermal 仍会读取温度。该选项与 `--require-capability thermal` 冲突时拒绝执行。

之后在该环境激活的任意目录运行，无需配置文件：

```bash
robot-perf-monitor --profile light --require-jetson --include-name '^component_conta' \
  --output results/orin-business-001
```

默认发现当前用户 CPU 活跃候选，示例名称规则额外纳入组件容器；不开profile时最多64个进程且动态采集线程，可用 `--max-targets` 调到1..256。上述light示例最多256个进程、关闭线程。多个PID或名称可重复指定相应参数。算法语义、组件内部各节点/回调耗时需要另外关联。源码的 `python3 -m perfkit.monitor` 入口仍保留；多进程、离线安装和其他用户范围见 [业务采集指南](docs/BUSINESS_MONITOR.md)。

0.4 支持系统、进程、线程独立采样周期与线程名称/TID筛选，记录采集进程CPU、阶段成本和各层覆盖。通用起点见 `configs/jetson-business-basic.json`；可选 `--jetson-telemetry`，缺失接口保留null与原因，EMC活动百分比不代表精确带宽。设计取舍和两阶段推进方式见 [架构设计](docs/ARCHITECTURE.md)。

内建 `--profile light` 每秒采集系统/进程、关闭线程；`--profile full` 每0.5秒包含全部可见线程。两者最多256个目标、跳过温度、不预设开销预算；配置文件和命令行可覆盖。降低频率或关闭线程改变覆盖，不能记作同范围优化。已有明确服务 cgroup 时，可用 `--cgroup-pattern` 配合 `--cgroup-prefilter` 减少范围外的详情读取，默认关闭。

读取业务结果目录的 `MONITOR_REPORT.md`、`monitor-summary.json` 和 `discovery.jsonl`；无目标或覆盖不足会提示复核。

0.6.0 可通过 `--workload workload.local.json` 使用业务清单代替活动发现触发规则，多个功能共享进程时引用同一资源记录。模板、范围约束与有限设备复核见 [业务功能关系指南](docs/BUSINESS_MAPPING.md)；ROS图另用M2b，回调及真实事件需明确接入。

0.8.0新增 `robot-perf-business`，从已有日志/事件导出离线关联一条单机业务链。样本ID、输入输出边界、身份及部署版本尚未确认时，先填写 [通用接入模板与指南](docs/BUSINESS_EVENTS.md)。它不自动解码CTF、不认证业务归属，也不把缺输出推断为网络丢包；deadline和预算不预设达标线。

0.6.1 提前拒绝 metadata 读取前的参数冲突，纯 PID 清单只读取 PID 并集内的进程详情，并补齐异常窗口结束记录、保留原始错误。现场开销收益和业务扰动需按指南的有限对照分别验证。

0.6.2 修补最终状态落盘中断叠加摘要/报告写入失败，独立保存 interrupted 状态和错误记录。0.6.1 的纯PID四轮对照已有现场反馈，业务扰动仍未评估；0.6.2两个受控故障已有三机通过反馈并收尾，见 [最终状态复核](docs/BUSINESS_MAPPING.md#062-最终状态有限复核)。

## ROS 对象关系证据（M2b）

0.7.0 增加独立只读入口，明确查询指定domain的ROS图与组件，关联已结束的M2a记录：

```bash
robot-perf-ros --monitor-run results/business-001 --graph --domain-id 0 \
  --ros-python /usr/bin/python3 --output results/ros-evidence-001
```

核心不增加ROS依赖，图查询使用已有SDK对应的rclpy解释器。图可见不等于本地PID归属；可选导入规范化节点初始化元数据，仅标外部证据身份一致性。原生CTF/真实追踪SDK、消息链路时延和deadline尚未完成。执行方式、未知QoS、证据层次与一次设备复核见 [M2b指南](docs/ROS_BUSINESS_EVIDENCE.md)。

0.7.0核心回归已有三机通过反馈，Orin的Humble/Fast DDS图查询通过；Thor真实ROS仍需定位已有SDK。0.7.1新增独立环境预检及显式SDK/RMW选择；0.7.2修正统一回归温度跳过传递和依赖失败诊断：

```bash
robot-perf-ros --preflight --domain-id 0 --graph-wait 0 \
  --ros-python /usr/bin/python3 --sdk-prefix /path/to/ros \
  --rmw rmw_fastrtps_cpp --output results/ros-preflight-001
```

有限现场回归入口、QoS兼容边界和实际验证矩阵见 [ROS环境指南](docs/ROS_ENVIRONMENT.md)。真实tracing与业务路径仍待后续。

## 结果与检查

阅读套件目录下的 `SUITE_REPORT.md`，再查看每个 case 的 `REPORT.md` 和 `summary.json`。`suite-status.json` / `run-status.json` 的 complete 只表示采集成功；业务达标、输入质量和采集开销分别判断。失败退出非零并保留证据；结果不进入 Git。

每轮新增 `acceptance` 分项状态；期限与容许违约率、总 CPU / 完整采样周期预算以及多组 ABBA 增幅未配置时不判通过。待填模板、三组 ABBA 和五轮大包尾部诊断见 [验收与诊断指南](docs/ACCEPTANCE.md)。库映射与配置声明不等于实际传输验证；本工具尚未提供 DDS/内核逐事件根因归因。

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/run-docker.sh scripts/run-native.sh
docker run --rm --init robot-systems-perf:local python3 tests/integration_checks.py
docker run --rm --init robot-systems-perf:local python3 tests/integration_monitor.py
docker run --rm --init robot-systems-perf:local env PYTHONPATH=/app python3 tests/integration_resource_profiles.py
```

Docker Desktop 的 Linux VM 结果不能作为 Jetson 基线。Orin/Thor 实机性能、AMD64 和其他 ROS 软件栈需要现场验证，具体执行证据见 [验证记录](VALIDATION.md)。

- [两阶段实施路线](docs/SYSTEMS_ROADMAP.md)：新机器接入、业务节点观测与优化方法资产。
- [机器接入指南](docs/MACHINE_INTAKE.md)：M1 只读入口、人工补充及能力解释。
- [性能规划](PERFORMANCE_PLAN.md)：两阶段路线与指标口径。
- [设备测试指南](docs/JETSON_RUNBOOK.md)：配置、资源限制、结果判断和诊断边界。
- [平台补充模板](templates/platform-profile.template.json)：人工记录未自动采集的设备与业务条件。
- [外部性能工具参考](docs/TOOL_LANDSCAPE.md)：官方与开源工具、Orin/Thor 兼容边界、两阶段接入建议和本地下载说明。
