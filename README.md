# robot-systems-perf

面向具身机器人的中间件与系统软件性能测量、分析和优化工具。目录名与计划中的 GitHub 仓库名统一为 `robot-systems-perf`；范围覆盖通信、调度和系统资源，后续可扩展到业务链路，不绑定某一款 Jetson 或 ROS 版本。

本仓库用于先验证测量工具，再在 Jetson Orin 与 Thor 上建立独立性能基线。首版提供真实 ROS 2 跨进程通信 C01 和 Linux 周期任务 S01，记录原始数据、环境、逐轮指标及报告。总路线见 [性能分析规划](PERFORMANCE_PLAN.md)。

## 首版能力

| 场景 | 负载与测量 | 边界 |
| --- | --- | --- |
| C01 | 单生产者与单消费者、固定有效载荷和 QoS、计划周期发布；发布到回调、数据年龄、处理完成、CPU 时间、交付与截止期 | 同机跨进程；包含中间件、传输和派发，不是纯网络延迟 |
| S01 | 单线程按 CLOCK_MONOTONIC 绝对时间启动，可设置忙等持续时间与 CPU 干扰 | 开始偏差和周期误差不等于内核 runnable wait |

两类测试均保留计划时刻；落后时按原计划逐条追赶，不静默跳过。热路径事件保存于预留内存，结束后写 CSV。C01 回调的完整载荷检查属于被测工作量；接收端在有限排空等待后停止，仍未交付的任务计入缺失，原因不直接归为网络丢包。有业务截止期时，发送结束后的等待至少覆盖一个截止期长度，确保全部已发送任务的期限到达后才判断违约；实际等待记入 resolved.json。业务截止期以计划释放时间为锚；没有配置截止期时不判断业务达标。

S01 的 work_us 指按 CLOCK_MONOTONIC 计时的忙等墙钟持续时间；被抢占时，实际占用 CPU 的时间可能减少，须查看 cpu_time_ns。它不代表固定指令数或固定 CPU 时间预算；需要比较真实计算任务时应接入固定业务工作负载。

当前不提供 GPU 推理、内部队列、内核调度事件、实时优先级配置或跨设备单向延迟测量。它们在后续里程碑实现，不能从当前指标反推。

## Docker 快速运行

需要 Git、Docker 和 Bash；主机不需要安装 ROS 2。默认基于官方 `ros:humble-ros-base-jammy`，使用镜像内已有 C++ 工具构建，不安装额外 Python 依赖。ARM64 与 AMD64 由 Docker 引擎架构选择；脚本拒绝不匹配的镜像架构。

取得仓库后，在仓库目录运行：

```bash
./scripts/run-docker.sh configs/smoke.json results/smoke
```

Smoke 默认每个场景预热 1 秒、测量 5 秒、重复 2 次，仅用于检查构建、事件、退出及报告路径。输出目录必须尚不存在；再次运行使用新的目录名，避免覆盖证据。

```bash
./scripts/run-docker.sh configs/baseline.json results/baseline
```

Baseline 是探索参数：预热 30 秒、测量 120 秒、重复 5 次。运行前按业务确认周期、载荷、QoS、任务量和截止期；S01 的示例期限并不是产品要求。C01 默认没有业务截止期。

运行产物包括 `config.json`、`environment.json`、`resources.jsonl`、每场景逐轮 CSV 与日志、`summary.json`、`REPORT.md` 和 `run-status.json`。失败退出非零并保留故障目录；`run-status.json` 的 complete 只表示测量链路完成，不代表性能达标。

## Orin 与 Thor 实测

1. 在目标设备克隆源码，按 [平台模板](templates/platform-profile.template.json) 记录模组、BSP、功耗、散热和软件环境。宿主配置仍需单独确认，容器不能代替它。
2. 先跑 smoke，确认架构、实际中间件版本、交付、采集和进程退出；再运行适配后的 baseline。
3. Orin 与 Thor 分别使用新的结果目录，保存对应平台档案。不要求两台平台使用相同的 JetPack/BSP。
4. 固定实验中的软件、功耗模式、频率策略及散热；不要在脚本外悄悄修改默认配置。
5. 第二阶段以同台设备优化前后对照为主体，再比较平台与软件栈组合。

默认同一容器内的两个进程通过 localhost discovery 通信。需要按生产网络与 IPC 环境对照时明确指定：

```bash
ENVIRONMENT_KIND=jetson-container DOCKER_NETWORK=host DOCKER_IPC=private \
  ./scripts/run-docker.sh configs/baseline.json results/jetson-baseline
```

可选择兼容的 ROS 软件栈，例如：

```bash
BASE_IMAGE=ros:jazzy-ros-base-noble IMAGE=embodied-perf:jazzy \
  ./scripts/run-docker.sh configs/smoke.json results/jazzy-smoke
```

Humble 为首版验证目标，其他发行版必须重新构建与实测；镜像标签是可变的，报告记录构建镜像 ID、基础镜像 ID、包版本和源码摘要。Docker 的 Ubuntu 用户态不改变宿主内核。首版不使用 GPU，因此不要求 NVIDIA 容器运行时；后续 GPU 场景需要按平台增加支持。

脚本不配置 CPU 配额、绑定或实时策略。若生产有这些约束，应通过明确的部署配置单独开展实验，并保存实际容器和宿主参数。资源采样记录可见的 cgroup 状态；未暴露的温度、功耗或 BSP 信息为不可用，不能填零。

## 原生 Linux 运行

在已经安装相应 ROS 2、C++ 编译器、CMake 和 Python 3.10 或更新版本的目标机器上：

```bash
source /opt/ros/humble/setup.bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
EP_ENVIRONMENT_KIND=jetson-native python3 -m perfkit.runner \
  --config configs/smoke.json --output results/native-smoke
```

不要在未核对支持组合时按此示例改装目标平台软件。原生构建和运行需要另外验证，与容器结论分开记录。

## 校验与故障检查

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/run-docker.sh
```

已构建镜像也可以执行测试：

```bash
docker run --rm embodied-perf:local python3 -m unittest discover -s tests -v
docker run --rm --init embodied-perf:local python3 tests/integration_checks.py
```

单元测试使用独立已知数据验证分位数、截止期分母、缺失与重复、无效 CSV 和进程清理。真实容器 smoke 另行验证编译与中间件链路，单元测试不能替代它。遇到失败先检查运行目录中的子进程日志和 `run-status.json`，不要删掉失败样本后重新统计。

每个场景的 `cpu_interference_workers` 可设置受控 CPU 干扰。C01 的 `callback_delay_us` 可用于验证慢消费者与积压分析；这是被明确注入的等待，不是系统根因。更改配置后保存为新文件，在新目录执行。

## GitHub 仓库与克隆

当前工程独立放在 `tools/robot-systems-perf/`，Git 历史随工程保存在该目录。其他工具可以放到 `tools/` 下的同级目录，各自维护仓库。

后续在 GitHub 创建名为 `robot-systems-perf` 的空仓库，选择需要的可见性，再在本地工程目录执行以下命令。将 `YOUR_ACCOUNT` 替换为自己的 GitHub 账号；以下命令是后续操作说明，当前未创建或推送云端仓库。

```bash
git remote add origin git@github.com:YOUR_ACCOUNT/robot-systems-perf.git
git push -u origin main
```

在另一台机器或 Orin/Thor 上：

```bash
git clone git@github.com:YOUR_ACCOUNT/robot-systems-perf.git
cd robot-systems-perf
./scripts/run-docker.sh configs/smoke.json results/smoke
```

## 离线迁移

没有远程仓库时可从已提交的本地 Git 仓库生成离线包：

```bash
git bundle create robot-systems-perf.bundle --all
```

把 bundle 传到另一台机器后：

```bash
git clone robot-systems-perf.bundle robot-systems-perf
cd robot-systems-perf
./scripts/run-docker.sh configs/smoke.json results/smoke
```

该包只包含 Git 已提交内容，不包含 Docker 镜像与实测结果。离线设备还需单独准备对应架构的镜像。

## 实现与验证状态

已实现 C01、S01、资源采样、逐轮统计与报告。ARM64 Docker smoke、21 项单元测试、3 项实际集成检查和独立代码审查已完成，详见 [验证记录](VALIDATION.md)。Orin/Thor、AMD64、GPU、内核事件、业务链路以及其他 ROS 发行版仍需对应实测支持。
