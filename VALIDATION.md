# 验证记录

## 独立安装与任意目录入口验证

新增纯Python安装包和 `robot-perf-monitor` 命令。通过PEP517构建后端离线生成 `robot_systems_perf-0.3.0-py3-none-any.whl`；安装运行没有额外Python库依赖，也不包含C++二进制或仓库配置文件。

在ARM64 Linux ROS容器的隔离venv中离线安装正式wheel，删除启动环境的源码PYTHONPATH，从独立工作目录调用实际console入口：帮助与两秒采集均退出0，相对结果目录在该工作目录创建。同时显式纳入两个PID，采集到两个进程和忙碌样例的动态线程；业务样例仍存活。报告的安装版本为0.3.0，所有模块SHA与wheel内容一致，安装位置没有Git时记录null。

9项monitor相关单元测试通过，包含新安装来源口径与此前中断/故障边界。测试安装时复用本机已有pip模块作为离线bootstrap，仅供隔离验证，不进入产品包。原始证据在忽略的 `results/install-verification-pep517`，stdout/stderr在 `results/install-pep517-final.log`。

此项验证的是Linux安装入口和多进程功能；Orin/Thor现场与采集开销的验证范围没有扩大。C01/S01仍需源码仓库的C++/Docker/native入口。

## 现有业务进程自动采集验证

新增独立 monitor 入口，用于宿主 ROS 2 节点/组件容器的进程和线程资源观察。活动筛选只是候选发现；没有自动识别算法语义或生成业务消息时延。目标 Orin/Thor 仍未连接，验证环境继续为原生 ARM64 Docker Desktop Linux VM。

- 82 项单元测试在宿主 Python 与 Linux ROS 容器通过，包含发现范围、可读性、UID、PID复用、累计 CPU 算术、动态注册身份固定及既有统计兼容。
- 真实外部进程检查通过：活动候选、名称纳入空闲对象、运行中新进程、死亡和新PID重启、多线程名称/TID采集；测试进程命令行中的标记未出现在任何报告文件。
- SIGTERM 使采集器退出130，保存 interrupted 原始证据与报告，被观察的业务样例保持存活。摘要生成、报告写入、最后状态写入期间的真实 SIGINT 回归验证终态一致。
- 原有6项 ROS 集成检查继续通过；采样失败、业务发现失败与既有目录拒绝覆盖不被包装成成功。
- 独立复审通过：两处取消终态边界已修正，受审源码摘要与Linux原始运行环境记录一致；复审核对实际进程/线程快照、筛选事件、覆盖与隐私检查结果。
- 从提交 fe93345 创建干净克隆、重新构建后，C01/S01各两轮 smoke 回归通过，每轮500个测量事件，C01无测量交付缺失，资源报告有效。原始证据保存在 `results/business-monitor-baseline-regression`。

业务集成证据保留在忽略的 `results/business-monitor-verification`，原始stdout/stderr为 `results/business-monitor-final.log`；Linux单元与ROS集成最后输出分别保留在 `results/linux-unit-monitor-final-tail.txt`、`results/ros-integration-monitor-final-tail.txt`。开发验证时镜像源码版本标签为 development，实际运行源文件 SHA256 记录在各 environment.json 中，用于关联受审代码；原始证据不提交到仓库。

尚未验证真实ROS组件容器的线程命名与现场可读权限、跨用户/容器现场、Jetson传感器、低CPU或GPU等待业务的发现完整性及业务采集开销。此模式没有节点/回调级归因；使用者必须检查筛选依据、namespace、覆盖和遗漏，详见 [业务采集指南](docs/BUSINESS_MONITOR.md)。

## Jetson 测试入口与扩展指标验证

本轮仍使用 Apple Silicon 上 Docker Desktop 的原生 ARM64 Linux VM，Ubuntu 22.04 / ROS 2 Humble / rmw_fastrtps_cpp。没有连接 Orin 或 Thor；以下结果验证构建、运行、统计与故障处理，不是目标设备性能基线。

| 检查 | 实际结果 |
| --- | --- |
| 单元测试 | 56 项在宿主及 ROS 容器通过；覆盖已知数据统计、窗口边界、计数器重置、进程身份、平台检测、开销对照与清理中断 |
| ROS 集成检查 | 6 项通过：期限成熟后统计、拒绝覆盖、运行中断清理、minimal 模式、采样失败、套件中断 |
| 12 case 短套件 | 参考、大载荷、高频、best-effort、慢消费者、CPU 干扰、周期任务及 ABBA 采样对照均完成；慢消费者的缺失交付保留并报告 |
| 最新单次 smoke | C01 与 S01 各两轮完成，每轮 500 个测量事件；新增资源与诊断输出可读取 |
| 原生入口脚本 | 在 ARM64 Linux ROS 容器内完成 CMake 构建与四轮 smoke；仅验证入口，不代表 Jetson 原生宿主实测 |
| 干净克隆复现 | 从提交 27ff06d 创建独立干净克隆，重新构建镜像并完成最终 12 case 短套件；逐 case 状态、报告和环境记录检查通过 |
| 独立复审 | 清理过程中 SIGTERM 不遗留负载进程；无有效交付的对照保留 null，不给预算通过；最终复审通过 |
| 静态检查 | 两个入口脚本语法、Python 源码语法与差异空白检查通过 |

本轮原始证据保留在忽略的 results 目录中，包括 `v2-suite-smoke`、`v2-smoke-reviewed`、`v2-native-entry` 和最终的 `v2-clean-clone-suite`。最终短套件包含全部退出清理修复：慢消费者有效交付 178/300，缺失 122 条被保留；其余 C01 case 均无测量交付缺失，两个 S01 case 各保留 3000 个测量样本。basic 模式每个三秒窗口内有 15 个完整资源快照；minimal 模式明确关闭资源采集。采样 ABBA 只比较资源采样增量，默认预算未配置，短时差异不构成稳定开销结论。

Orin/Thor 实机、Jetson 原生宿主、AMD64、其他 ROS/BSP 组合、长期热稳定与业务时限仍需现场验证。内部队列与执行器就绪等待、逐事件内核调度归因、GPU 推理干扰及真实机器人链路尚未实现。已观察最大值不是 WCET。

## 首版 Docker 验证

本记录说明首版测量链路的实际验证范围。执行环境是 Apple Silicon 上 Docker Desktop 的原生 ARM64 Linux VM，Ubuntu 22.04 用户态、ROS 2 Humble、rmw_fastrtps_cpp；没有使用 x86 架构仿真。它证明工具能构建和运行，不构成 Orin 或 Thor 的平台性能基线。

### 已完成验证

| 检查 | 实际结果 |
| --- | --- |
| C++ Release 构建 | periodic_bench 与 ros_bench 构建成功 |
| Docker smoke C01 | 两轮，每轮 500 个测量消息均有效交付；另有 100 个预热消息；未发现测量消息缺失、重复或无效载荷 |
| Docker smoke S01 | 两轮，每轮 500 个测量任务；另有 100 个预热任务；保留开始偏差、周期误差和期限统计 |
| 单元测试 | 21 项在宿主与 ROS 容器 Python 环境通过；覆盖已知分位数、全部发送任务期限分母、缺失、重复、无效事件及失败记录 |
| 截止期集成检查 | 请求排空 10 ms、任务期限 1 s、慢回调场景，运行至所有已发送任务的期限到达后再统计；实际通过 |
| 既有目录集成检查 | 重复使用结果目录退出非零，原运行状态不被改写；实际通过 |
| 信号集成检查 | C01 与 CPU 干扰运行中向主进程发送 SIGTERM；运行标记失败，发布者、订阅者和干扰进程均被回收；实际通过 |
| 独立审查 | 三项阻断问题已修复并复审；从四轮原始 CSV 重算统计，与保存汇总一致；源码摘要与受审文件一致 |
| Shell 检查 | Docker 入口脚本语法通过 |

基础 ARM64 镜像为 `arm64v8/ros:humble-ros-base-jammy`，本轮解析的 digest 为 `sha256:de1c7c0cd857992571b629e7fddd984e2abb2fedfbc09066628651c7cf9e64eb`。核心运行包包括 rclcpp 16.0.21 和 rmw_fastrtps_cpp 6.2.10；具体发行包版本、镜像 ID 和源码摘要写入每次运行的 environment.json。

### 故障与修正

首次运行因随机 UUID 的首字符可能是数字，触发 ROS 主题 token 命名错误。修正为字母开头的 `run_` 前缀后真实链路通过。故障目录保留 failed 状态，没有当作成功报告。

独立审查提出的截止期观察过早、初始环境采集异常漏记状态、单线程时间区间可重叠三项问题均已修复。另补充新机器首次获取基础镜像时的版本记录、容器线程配置记录与子进程退出检查。

### 证据与复现

本机原始证据存于忽略的 results 目录，包括失败运行、docker-smoke-reviewed 和 verification 集成输出，不进入源码提交。源码的 Git 版本与运行时文件 SHA256 用于关联；每轮报告仍应保留环境和配置。另一台设备通过 README 中的 Docker 命令生成自己的证据目录。

Git bundle 已生成并通过校验；从包克隆至干净目录后，21 项单元测试以及完整的四轮 Docker smoke 再次通过。该运行记录了已提交的源码版本，原始证据保存在 clone-smoke 目录。原始实测结果和 Docker 镜像不包含在源码包内；克隆后的同一源码需在当地环境重新构建和执行。

### 首版验证边界

首版未覆盖 Orin 与 Thor 实机、原生宿主部署、AMD64 实机、ROS 2 其他发行版、业务端到端链路、GPU、内核追踪和基础采集增量开销对照。后续新增验证见上文。Smoke 的短时样本与示例期限不能证明业务达标、长期尾延迟或硬实时保证。
