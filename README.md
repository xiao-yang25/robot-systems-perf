# robot-systems-perf

面向具身机器人的中间件与系统软件性能工具。支持在 Orin/Thor 上采集通信、周期任务和资源基线，再按证据开展优化。

## 测量内容

| 模块 | 输出 |
| --- | --- |
| C01：ROS 2 同机跨进程通信 | 发送迟到、发布调用耗时、通信/数据年龄/响应时延、实际窗口收发速率与有效载荷吞吐、交付完整性 |
| S01：Linux 周期任务 | 启动偏差、周期误差、墙钟/CPU/响应时间、截止期违约及超期幅度 |
| 诊断 | 分位数、直方图、时间分段、尾部消息编号、连续违约；可选数据年龄阈值 |
| 资源 | 每核 CPU、进程/线程 CPU 与可用 schedstat、RSS、缺页、切换、cgroup 节流、温度/频率与可见功率传感器 |
| 测试套件 | 参考点、大小载荷、高频、QoS、慢消费者、CPU 干扰及资源采集 ABBA 开销对照 |
| 业务进程采集 | 自动发现现有活跃进程、按名称/PID纳入对象、动态线程与进程重启跟踪；输出资源而非消息时延 |

保留原始 CSV、配置、环境、资源 JSONL 和逐轮报告；缺失能力标为不可用。内核等待是采样区间累计值，不能定位单次调度原因；内部队列、执行器就绪等待、GPU 推理干扰和真实机器人业务链路尚未实现。

## 快速验证

需要 Git、Docker、Bash 和宿主 Python 3。默认镜像为 Ubuntu 22.04 / ROS 2 Humble；使用 Docker 引擎原生架构，不使用跨架构仿真。

```bash
git clone git@github.com:xiao-yang25/robot-systems-perf.git
cd robot-systems-perf
./scripts/run-docker.sh configs/suite-smoke.json results/suite-smoke suite
```

私有仓库需要 GitHub 访问权限。短套件包含 12 个 case，通常数分钟；仅验证工具链路。每次必须使用新结果目录。原来的单次入口仍可用：

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

## 采集现有业务

已运行的宿主 ROS 2 节点/组件容器可直接采集，无需重启业务或构建 C++。在仓库目录安装一次（Python 3.9+）：

```bash
python3 -m venv "$HOME/.venvs/robot-systems-perf"
source "$HOME/.venvs/robot-systems-perf/bin/activate"
python3 -m pip install .
```

之后在该环境激活的任意目录运行，无需配置文件：

```bash
robot-perf-monitor --require-jetson --include-name '^component_container.*$' \
  --output results/orin-business-001
```

默认发现当前用户 CPU 活跃候选，示例名称规则额外纳入组件容器；默认同时最多64个进程，每个进程动态采集线程，可用 `--max-targets` 调到1..256。多个PID或名称可重复指定相应参数。算法语义、组件内部各节点/回调耗时需要另外关联。源码的 `python3 -m perfkit.monitor` 入口仍保留；多进程、离线安装和其他用户范围见 [业务采集指南](docs/BUSINESS_MONITOR.md)。

读取业务结果目录的 `MONITOR_REPORT.md`、`monitor-summary.json` 和 `discovery.jsonl`；无目标或覆盖不足会提示复核。

## 结果与检查

阅读套件目录下的 `SUITE_REPORT.md`，再查看每个 case 的 `REPORT.md` 和 `summary.json`。`suite-status.json` / `run-status.json` 的 complete 只表示采集成功；业务达标、输入质量和采集开销分别判断。失败退出非零并保留证据；结果不进入 Git。

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/run-docker.sh scripts/run-native.sh
docker run --rm --init robot-systems-perf:local python3 tests/integration_checks.py
docker run --rm --init robot-systems-perf:local python3 tests/integration_monitor.py
```

Docker Desktop 的 Linux VM 结果不能作为 Jetson 基线。Orin/Thor 实机性能、AMD64 和其他 ROS 软件栈需要现场验证，具体执行证据见 [验证记录](VALIDATION.md)。

- [性能规划](PERFORMANCE_PLAN.md)：两阶段路线与指标口径。
- [设备测试指南](docs/JETSON_RUNBOOK.md)：配置、资源限制、结果判断和诊断边界。
- [平台补充模板](templates/platform-profile.template.json)：人工记录未自动采集的设备与业务条件。
