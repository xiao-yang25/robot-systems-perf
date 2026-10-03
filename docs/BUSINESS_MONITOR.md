# 现有 ROS 2 业务进程自动采集

在 Orin/Thor 宿主启动采集器，观察已经运行的 ROS 2 节点和组件容器。仅依赖 Python 标准库，不需要重新构建业务、安装 ROS 追踪包或让采集器启动业务。测试基准 C01/S01 与此模式分别运行；它们的消息时延不能作为已有业务链路的时延。

## 直接运行

推荐安装一次后从任意目录启动，Python 3.9+。在克隆的仓库目录创建隔离环境并安装：

```bash
python3 -m venv "$HOME/.venvs/robot-systems-perf"
source "$HOME/.venvs/robot-systems-perf/bin/activate"
python3 -m pip install .
```

后续激活此环境即可从任意有输出写权限的目录使用命令：

```bash
robot-perf-monitor --require-jetson --include-name '^component_container.*$' \
  --seconds 60 --output results/orin-business-001
```

也可直接调用 `$HOME/.venvs/robot-systems-perf/bin/robot-perf-monitor`，无需激活环境。相对输出路径和外部 `--config` 路径按启动目录解析；安装包不需要源码目录或配置文件存在。`environment.json` 记录安装版本与实际模块摘要，安装版没有源码Git时不猜测工作目录的版本。更新时在仓库拉取最新代码，使用该环境的 pip 重新安装。

Ubuntu若未提供 venv/pip，先准备相应 Python 工具。安装构建使用 setuptools/wheel，运行没有额外 Python 库依赖。没有网络的设备可在准备机器生成纯Python wheel，再用设备已有的 pip 安装：

```bash
python3 -m pip wheel --no-deps . --wheel-dir dist
python3 -m pip install --no-index /path/to/robot_systems_perf-0.3.0-py3-none-any.whl
```

上述是准备机器与设备分别执行的命令，wheel文件路径需替换。wheel只提供业务资源采集的命令与Python模块；C01/S01需要C++构建，仍使用源码仓库的 Docker/native 入口。

不安装时，原源码入口仍在仓库目录运行：

```bash
python3 -m perfkit.monitor --config configs/business-monitor.json \
  --output results/orin-business-001
```

Thor 使用独立结果目录。默认观察 60 秒，每 0.5 秒采集资源、每 1 秒发现进程，最多同时采集 64 个目标。配置校验本地 Linux ARM64 Jetson，失败保存环境与 failed 状态；测试环境可不加载此配置，使用 CLI 默认参数。

默认仅纳入当前用户可读的非内核进程，排除采集器自身。首次扫描只建立 CPU 计数器基准，后续观察单核口径 CPU 利用率达到 1% 的候选。`component_container*` 的进程名或可执行文件名匹配时，即使当前空闲也额外纳入。

## 同时采集多个进程

自动发现会同时纳入多个进程，并不是一次只测一个。默认上限64，可用 `--max-targets` 调整到1..256；超过上限的候选数明确写入发现记录。每个目标下的可见线程都会动态采集。

只采集两个已知进程（PID替换为现场值），关闭活动发现：

```bash
robot-perf-monitor --require-jetson --active-cpu-percent 0 \
  --pid 12345 --pid 23456 --output results/two-processes-001
```

多个名称规则也可重复指定，任意一个匹配均可纳入：

```bash
robot-perf-monitor --require-jetson --active-cpu-percent 0 \
  --include-name '^perception_node$' --include-name '^planner_node$' \
  --include-name '^component_container.*$' --output results/multi-node-001
```

CPU 活动不能判断“这是算法进程”；报告保留进程名、可执行文件 basename、PID、UID、启动时间、cgroup 路径和筛选依据。后台服务也可能成为候选，需根据这些信息确认业务范围。Python 节点可能仅显示 python3，CPU 很低的节点或等待 GPU 的工作进程可能漏掉，应使用名称或 PID 显式纳入。

## 缩小或补充范围

名称规则是正则表达式，匹配 `/proc` 的 comm 或 exe basename，不读取命令行参数。comm 可能截断，可执行文件 basename 可补充，但 Python 脚本名无法据此获知。

仅采集指定名称，不使用活动发现：

```bash
python3 -m perfkit.monitor --require-jetson --active-cpu-percent 0 \
  --include-name '^(component_container.*|perception_node|planner_node)$' \
  --seconds 120 --output results/selected-business-001
```

显式添加当前 PID，其他候选仍由活动发现：

```bash
python3 -m perfkit.monitor --require-jetson --pid 12345 \
  --exclude-name '^(rviz2|ros2)$' --output results/business-extra-001
```

PID 是现场值，不能照抄示例。PID 固定指定不会跨新 PID 重启保持业务角色；需要重启后自动发现时用名称或活动规则。

UID、排除名称和 cgroup 条件是硬过滤，适用于活动、名称、PID及保留对象。`--include-name` 是额外纳入条件，默认不禁止其他活跃候选；需要名称白名单时同时设置 `--active-cpu-percent 0`。

业务由其他用户运行时，按现场权限选择运行用户，或明确扫描所有用户：

```bash
sudo "$HOME/.venvs/robot-systems-perf/bin/robot-perf-monitor" --require-jetson \
  --include-name '^component_container.*$' --all-users \
  --output results/all-users-business-001
```

`--all-users` 不提升权限；sudo 只是跨用户读取可能需要的现场执行方式，采集器不自动提权。sudo 产生的结果可能属于 root。进程名称、cgroup 路径和设备信息是现场资料，结果留在 results，不进入源码仓库。

## 容器与组件容器

ROS 2 的 component container 是宿主上的一个进程，与 Docker 容器不同。采集其 PID 下的全部可见线程，并保留线程名/TID；多个 ROS 节点共享进程时，资源不能自动归到单个节点或回调。名称规则只能提供线索。

宿主启动采集器也能看到本机 Docker 业务进程；按可见 UID 和 cgroup 路径限制范围，例如：

```bash
sudo "$HOME/.venvs/robot-systems-perf/bin/robot-perf-monitor" --require-jetson --all-users \
  --cgroup-pattern '(docker|containerd|kubepods)' \
  --output results/container-business-001
```

这匹配的是宿主可见 cgroup 路径，不是容器名称；可用实际容器 ID 或路径替换。cgroup 条件只限制范围，不独立触发空闲进程纳入。无需 Docker socket 或 privileged 容器。若在普通容器内启动采集器，只能观察该 PID namespace 可见对象，不能声称采集了宿主业务。建议在目标宿主运行。

发现支持 cgroup v1/v2 路径；CPU 节流和 cgroup 内存统计仍只支持 v2，v1 会标不可用。

## 输出与生命周期

- `MONITOR_REPORT.md`：按 CPU 排序的进程资源摘要与采集质量。
- `monitor-summary.json`：进程/线程 CPU、RSS 采样峰值、缺页、上下文切换、累计 schedstat 等资源汇总，以及系统/传感器可用性与覆盖范围。
- `discovery.jsonl`：每次发现的筛选统计、目标和登记/移除事件；记录上限导致的遗漏。
- `resources.jsonl`：原始系统、目标进程/线程与 cgroup 快照。
- `monitor-config.json`、`environment.json`、`monitor-status.json`：实际配置、宿主/源码/namespace与运行状态。

目标只在身份可验证、可读且仍满足过滤范围时保留，空闲不会单独导致摘除。死亡、不可验证或移出范围时移除；新 PID 或复用 PID 按新的启动时间独立登记，计数器不跨身份做差分。未成功登记的身份竞态记入发现记录。线程随每次资源采样重新枚举；瞬时进程/线程可能漏掉。

资源汇总窗口由采集器的 CLOCK_MONOTONIC 定义，只使用完整落在窗口内的快照，不外推边界。资源峰值是采样峰值，进程 CPU 以单核=100%计，多线程进程可能超过100%。进程 CPU 包含全部线程，而进程 schedstat/上下文切换是主线程；线程行分别展示，不要重复求和 RSS 或进程与线程 CPU。

SIGINT/SIGTERM 在采集期间结束观察、关闭采样器并保存 interrupted 报告，退出码130；启动阶段被中断可能只有原始证据与状态。采集错误退出非零并保留 failed 状态；已有结果目录拒绝覆盖。采集器不向业务发信号，不改调度、功耗或频率，不读取业务命令行参数、环境变量或进程内存；此模式也不记录内核启动命令行。

没有发现目标时会明确报告需复核，不能称为业务采集成功。目标上限默认64、可调1..256；长时测试应控制采样间隔和时长，检查 JSONL 体积与采样覆盖。当前没有标定业务采集开销，需对比同负载的无采集运行。

## 诊断边界

此模式直接采集现有业务的资源与调度累计线索。它不产生业务消息端到端时延、单次回调耗时、节点级执行期限或因果链，也没有 GPU 使用率/推理时间。已有资源指标与可用性说明见 [设备测试指南](JETSON_RUNBOOK.md)。要从组件容器拆分到节点与回调，需要接入 ROS 追踪或业务时间戳。
