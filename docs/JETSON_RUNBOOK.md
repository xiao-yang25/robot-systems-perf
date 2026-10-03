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

## 故障定位

先读 suite-status.json，定位失败 case，再读 run-status.json 及子进程日志。失败退出非零并保留现场；重复目录被拒绝。发送者超时、采样失败、接收记录溢出或未正常退出都不会包装为成功报告。SIGTERM/中断会清理当前发布者、订阅者及 CPU 干扰进程。

重新执行使用新的结果名；若构建阶段失败，可能已经保留同级 host.json。后续需要旧镜像/离线源码时单独准备，不把 Docker 镜像或设备结果塞进源码仓库。
