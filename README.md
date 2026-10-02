# robot-systems-perf

面向具身机器人的中间件与系统软件性能工具：先建立可复现的指标与基线，再依据证据优化通信、调度和资源使用。

## 当前能力

| 场景 | 测量内容 |
| --- | --- |
| C01：ROS 2 同机跨进程通信 | 发布到回调、数据年龄、处理完成、CPU 时间、交付完整性与截止期 |
| S01：Linux 周期任务 | 启动偏差、周期误差、执行时间与截止期 |

支持配置载荷、频率、QoS、受控 CPU 干扰，保存原始事件、环境、逐轮统计和报告。当前仅支持同机测量；GPU、内核调度追踪和跨设备时钟同步尚未实现。

## 快速运行

需要 Git、Docker 和 Bash，宿主无需安装 ROS 2。默认使用 Ubuntu 22.04 / ROS 2 Humble 镜像；按 Docker 引擎选择 ARM64 或 AMD64，拒绝架构不匹配的镜像。

```bash
git clone git@github.com:xiao-yang25/robot-systems-perf.git
cd robot-systems-perf
./scripts/run-docker.sh configs/smoke.json results/smoke
```

私有仓库的克隆需要 GitHub 访问权限。Smoke 每个场景预热 1 秒、测量 5 秒、重复 2 次，用于检查工具链路。

确认运行正常后，复制并调整配置，再建立基线：

```bash
./scripts/run-docker.sh configs/baseline.json results/baseline
```

Baseline 示例预热 30 秒、测量 120 秒、重复 5 次。频率、负载、QoS 与截止期需按业务确定；示例参数不代表业务要求。每次使用新结果目录，已有目录不会被覆盖。

结果目录中的 `REPORT.md` 用于阅读，`summary.json` 用于分析；原始 CSV、日志、配置、环境和资源采样用于追溯。失败退出非零，检查日志和 `run-status.json`；`complete` 表示测量完成，不表示性能达标。结果与构建产物不上传源码仓库。

## Orin / Thor 实测

1. 在目标设备克隆仓库，填写 [平台档案](templates/platform-profile.template.json)，记录模组、BSP、内核、ROS/RMW、功耗模式、频率与散热。
2. 分别运行 smoke，再按业务配置运行 baseline；使用不同结果目录保存两台设备的证据。
3. 优化时先做同一设备的前后对照。无法隔离 BSP、内核或软件栈差异时，平台比较应注明这些差异。

需要使用宿主网络时显式设置：

```bash
ENVIRONMENT_KIND=jetson-container DOCKER_NETWORK=host \
  ./scripts/run-docker.sh configs/baseline.json results/jetson-baseline
```

Docker 固定用户态依赖，性能仍受宿主硬件、内核、调度与功耗影响。Docker Desktop 的 Linux VM 测试只验证工具链路，不能作为 Jetson 性能基线。当前不使用 GPU，无需 NVIDIA 容器运行时；脚本不自动配置 CPU 绑定、实时策略或功耗模式。

## 验证与文档

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/run-docker.sh
```

已构建的默认镜像可运行实际集成检查：

```bash
docker run --rm --init embodied-perf:local python3 tests/integration_checks.py
```

已完成 ARM64 Docker smoke、21 项单元测试和 3 项集成检查。Orin/Thor、AMD64 实机、原生部署与其他 ROS 发行版尚未验证。

- [性能规划](PERFORMANCE_PLAN.md)：两阶段路线、指标口径、扩展配置与原生运行。
- [验证记录](VALIDATION.md)：实际测试环境、结果与未验证范围。
- [平台档案模板](templates/platform-profile.template.json)：目标设备环境记录；不作为运行配置解析。
