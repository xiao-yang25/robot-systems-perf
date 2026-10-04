# 官方与开源工具的有限对照

本轮只验证资源采样、周期唤醒、ROS 事件采集三项。默认每个条件 60 秒、重复 3 次，交替顺序；不自动加测或改变业务部署。缺工具、权限、目标退出、命令失败、超时均保留证据并退出非零。`status.json` 的 `execution_completed` 仅表示执行完成；未配置业务 SLA/预算，不输出达标结论。工具定位与官方资料见 [工具调研](TOOL_LANDSCAPE.md)。

## 准备

在 Orin / Thor 宿主 Linux 上执行。需要 Python 3.10+、Linux pidfd 支持及源码 checkout；脚本不随 Python wheel 安装。入口根据自身路径定位仓库，可以从任意目录运行。每台设备单独执行，记录功耗模式、散热、BSP、ROS/RMW 版本和其他负载，使用新的结果目录。

```bash
git pull --ff-only
sudo apt-get update
sudo apt-get install sysstat rt-tests
```

依赖由操作者安装；入口不安装软件、调频、设置实时策略或重启业务。容器只验证执行链路，其内核、调度和设备可见性不能代表 Jetson 宿主性能。

预检查要求可识别且无错误的版本/帮助输出。退出 127、超时、空输出和加载失败均标记不可用；仅 cyclictest / Babeltrace 的 `--help` 返回 1 且输出通过完整帮助校验时可接受。原始命令、返回码与日志保留，文件存在不代表工具可运行。

## 1. 无采集 / 本工具 / pidstat

```bash
python3 scripts/compare_tools.py --mode resource --preflight --output results/resource-preflight-001
python3 scripts/compare_tools.py --mode resource --output results/resource-compare-001
```

默认启动并最终回收一个 49 线程测试负载，名义测量 9 分钟。业务范围需自己提供 PID，可重复指定；不识别算法语义，不启动、停止或改变外部进程：

```bash
python3 scripts/compare_tools.py --mode resource --pid 1234 --pid 5678 --output results/business-resource-001
```

本工具使用 `full` profile，系统/进程/线程/发现均为 1 秒，跳过温度和 Jetson 外部遥测；pidstat 使用 `-t -u -r -w`、1 秒周期。同一组 PID 的身份和 CPU 计数器在窗口前后、轮次之间核验；业务重启后需换新目录重新测试。

两者覆盖不同，本工具额外读取 cgroup、schedstat、亲和性等，CPU 差异不能称为同范围优化收益。先核验共同字段：进程/线程 CPU、RSS、缺页和切换。保留 pidstat 原文和本工具 JSONL；RSS 不跨线程求和，末端 RSS、均值和峰值分别比较。

`runs.json` 记录目标 `/proc/stat` 端点 CPU、末端 RSS、线程数量、本工具的覆盖/采集成本/observer 指标。`command.cpu_percent_one_core` 是已回收命令的总 CPU，包含启动、结束和 Python 执行门；目标端点窗口也包含这些时间。它不等同于稳定窗口的 observer CPU，不混合计算收益。无采集条件没有 collector CPU 数值；真实业务时延需另供同一窗口业务指标。

## 2. S01 零工作量 / cyclictest

```bash
cmake -S . -B build -DEP_BUILD_ROS=OFF
cmake --build build -j2
python3 scripts/compare_tools.py --mode wakeup --preflight --output results/wakeup-preflight-001
python3 scripts/compare_tools.py --mode wakeup --output results/wakeup-compare-001
```

名义测量 6 分钟。双方为 1 kHz、单工作线程、CLOCK_MONOTONIC、SCHED_OTHER/优先级 0，继承当前亲和性；可加 `--cpu 2` 将双方的测试进程绑定到允许的 CPU。cyclictest 使用 `--default-system` 保留电源管理，不请求内存锁定。权限不足或版本不支持参数会失败，不自动提升权限。

预检查核对当前命令需要的全部选项，含 `--default-system`。部分发行版 rt-tests 2.2（显示 cyclictest 2.20）不提供该选项，此时应退出非零，并在 `preflight.json` 记录缺失原因；不能删除选项后沿用本对照的电源管理声明。若需继续，由操作者选择兼容二进制，通过 PATH 指定，再在新目录运行。帮助检查通过也不保证实际采样权限足够。

比较 S01 的 `start_ns - scheduled_ns` 与 cyclictest 的唤醒延迟。S01 为零工作量、无预热、固定样本数；cyclictest 按持续时间结束，数量可能不同。原先 50 us 工作量的 S01 响应时间不可混入对照。

S01 输出纳秒分布；cyclictest 先验证采样资格，再写入成功记录。当前支持 rt-tests 2.2/2.5 形态的单线程完整稠密直方图：100000 个连续桶、正数 Total、完整统计与 overflow 尾部；桶计数之和必须等于 Total，overflow 尾部列出的事件数加 `N others` 必须等于 Histogram Overflows。总样本数为 Total 加 Histogram Overflows；quiet + histogram 模式通常不输出线程 C 计数，若存在则必须与总样本数一致。空输出、错误/帮助、零样本、截断、计数不一致与未知/稀疏格式均失败并停止后续轮次。格式支持不代表该工具版本支持全部命令选项。

`metrics.json` 的 `sampling_evidence` 记录桶内样本、overflow、总样本及计数来源。微秒直方图及原始日志保留；自动分位数仍为 null，采样资格通过不证明时延达标或两工具完全同义。手工核验后再比较，不能把微秒桶当作纳秒精度。

## 3. Fast DDS / Cyclone DDS，trace off / on

需要兼容 ROS SDK、两种 RMW、LTTng 用户态工具、Babeltrace 1 和已启用的 ROS tracepoints。按 [ROS 官方构建说明](https://docs.ros.org/en/humble/How-To-Guides/Building-ROS-2-with-Tracing-Instrumentation.html) 在隔离 SDK 准备 tracing，不直接替换生产 SDK。Humble 中仅安装 LTTng 不保证已有 ROS 二进制开启 tracing。

```bash
sudo apt-get install lttng-tools babeltrace
source /path/to/compatible/ros/setup.bash
ros2 run tracetools status
cmake -S . -B build -DEP_BUILD_ROS=ON
cmake --build build -j2
lttng-sessiond --daemonize
python3 scripts/compare_tools.py --mode trace --preflight --output results/trace-preflight-001
python3 scripts/compare_tools.py --mode trace --output results/trace-compare-001
```

替换 SDK 示例路径。session daemon 由操作者准备；入口使用 `--no-sessiond`，不自动启动 daemon，也不停止其他会话。预检查拒绝已有活动会话造成的 off/on 干扰，已停止会话保留。安装与实际 SDK 匹配的 Fast DDS/Cyclone DDS；入口不自动修复跨 BSP/ROS 依赖。

固定 C01：1 MiB、30 Hz、reliable、depth 64，预热 5 秒、测量 60 秒、drain 3 秒、`minimal` 采样模式。每个 RMW 的 trace off/on 和 RMW 顺序交替；3 轮共 12 个窗口，测量 12 分钟，另加预热、drain、发现和分析。

节点启动前启用随机名称的自有 LTTng 会话，按 `procname == ros_bench` 过滤 `ros2:*`。原始 trace 可能包含同名测试程序；离线事件计数按本轮发布/订阅 PID 核验，缺少任一 PID 的事件时失败。正常结束或中断只清理入口创建的会话与测试进程。

CTF、Babeltrace 解码、会话状态原文和 C01 CSV/报告保留。`trace_lost_events` 仍为 null，需结合状态和解码警告复核；事件存在不证明无丢失。C01 报告区分释放延迟、publish、publish-to-callback、callback wall/CPU，记录输入质量和实际 RMW。输入超限轮次单列，不能作为性能达标或采样预算证据。

本入口验证 ROS 事件可采集，未建立消息 ID 到 DDS 内部/内核事件的完整因果链，不自动归因尾延迟。CARET、内核 RTLA、Nsight 暂不加入本轮。

## 结束与回传

先加 `--seconds 5 --repetitions 1` 做短验证，再跑默认有限轮次。范围为 2–120 秒、1–3 轮；短验证可能达不到质量门槛，不能用作性能证据。每条命令用新目录；Ctrl+C/SIGTERM 回收自有对象，不向指定的业务 PID 发信号。

回传代码版本、环境摘要、`preflight.json`、`status.json`、`runs.json` 和问题窗口的原始日志/CSV/CTF。结果包含本机路径、PID、进程名称、映射库及可能的 topic 名称；保留在设备或内部渠道，脱敏后提供摘要，勿提交公开仓库。完成固定轮次即停止，只有工具故障或明确证据缺口才安排后续工作。

本次修复只需在设备做以下短复核，不要求重跑整套性能测试：

```bash
python3 scripts/compare_tools.py --mode wakeup --preflight --output results/wakeup-fix-preflight-001
python3 scripts/compare_tools.py --mode wakeup --seconds 5 --repetitions 1 --output results/wakeup-fix-short-001
```

预检查失败时先处理已记录的工具兼容问题；第二条仅在预检查通过后执行。有效运行应有正样本 `sampling_evidence`；无效运行应为 `failed`、退出非零，不生成 cyclictest 的成功 `comparison.json`。保留失败目录，复跑用新目录。

此前 b6c3654 的 ARM64 容器记录将 rt-tests 2.2 的错误/帮助输出误计为 cyclictest 执行完成，因此撤回该项短实跑通过的结论，旧状态不能证明实际采样。资源与 ROS trace 记录不因此补写或改动。此次验证使用原生 ARM64 Docker、Ubuntu 22.04；真实设备仍需上述短复核。容器内权限不足的采样与缺少选项的工具应明确失败，不能作为设备性能证据。

本次 235 项单元/入口回归通过，覆盖退出 0 的帮助、空输出、零样本、截断、计数不一致、有效直方图与 overflow，以及预检查 127、超时和空输出。官方 rt-tests 2.5 的 3 秒短实跑通过采样资格，3000 个桶内样本、0 overflow；临时容器提供 `SYS_NICE` 权限，仅验证入口。发行版 rt-tests 2.2 缺少 `--default-system` 时在预检查被拒绝，权限不足的 2.5 实跑也退出非零。自动分位数保持 null。
