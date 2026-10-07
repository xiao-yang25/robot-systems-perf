# 受控 ROS 追踪接入与一次有限验收

本入口用于接通真实 ROS 用户态 tracing，不是业务 E2E 测试或性能优化对照。复用 ROS SDK 的 ros2_tracing/tracetools 追踪点、LTTng 采集和 Babeltrace 解码；直接调用 LTTng 控制会话，不调用交互式 `ros2 trace`。Python 编排只用标准库，不写 CTF 解码器。

当前是**源码仓库脚本** `scripts/collect_ros_trace.py`，不包含在 0.8.1 wheel 中。核心 18 个 perfkit 模块不变。必须保留脚本所在仓库的 scripts、tests 和 perfkit 目录；可从任意目录用脚本绝对路径执行。结果目录父目录须已存在，结果目录必须不存在。

## 先决条件与边界

- Linux 原生环境，已有兼容 ROS SDK、tracing 编译支持、一个明确的 RMW、LTTng 2.x、Babeltrace 1.x/2.x CLI 和**已经运行**的同权限 session daemon。本工具不安装依赖、不执行 SDK setup、不启动或停止 daemon，不改实时权限、频率、亲和性或生产节点。
- SDK 环境由操作者事先加载，明确 `--ros-python`、`--rmw` 和 `--domain-id`；可用 `--sdk-prefix` 限制实际加载的 tracetools 库来源。该检查不认证完整 overlay，也不证明源码—构建绑定。pid_ns/procname 上下文不支持时明确失败，不降级为仅 PID 过滤。Babeltrace 2 的实际版本兼容性仍需现场验证。
- `build/ros_bench` 已在选定 SDK 下构建；可用 `--ros-bench` 指向已有构建。文件摘要、实际 RMW 和加载的 tracetools 路径/摘要分别记录和核对。
- `--view host/container` 是环境声明，不提供硬件隔离。Docker 结果不能作为 Orin/Thor 宿主调度或性能证据。
- 无温度发现或读取路径，request.json 明确记录 skipped。此脚本不自动启动资源 monitor；事件与资源窗口的生命周期桥接仍需后续适配。

只运行两个自有 C++ 测试进程，测试 publisher/subscriber 使用随机唯一话题、64 字节负载、100Hz、可靠 QoS。发布窗口限定 2～10 秒，默认 5 秒；发现与收尾额外占时，不把总作业时间称为固定 5 秒采集窗口。无性能达标阈值，也不修改业务源码。

自有进程先停在 exec 前的等待门。绑定 pidfd、PID/starttime 后，按这两个 vpid、实际 pid_ns inode 和本轮随机 procname 设置 `ros2:*` 过滤，保存 vpid/vtid/pid_ns/procname 上下文；先启动会话，再放行 ROS 初始化。正常退出不提前回收 PID，先停止/销毁会话，再回收测试对象。测试 ELF 复制到结果目录的随机 15 字节名称，摘要须与预检一致；此副本的随机线程名称是额外过滤保护，不是身份认证。这里只要求已知 C01 主线程的初始化/发布/回调，不保证采入改名的后台线程；相对 `$ORIGIN` 依赖若不支持复制位置，会明确失败。取消只清理本次随机名称的会话与自有对象，不使用全局 stop/destroy 或按业务进程名发送信号。若 stop/destroy 失败，仍有界回收测试进程，会话可能残留；保留 session_may_remain 与 cleanup_errors，活动状态无法确认时为 null。随机名称+namespace+PID guard 限制残留规则范围，但不保证会话已经停止；操作者须按记录的 session_name 检查并清理，只处理该会话，清理前不要重新执行保留的随机名称副本。

## 执行

以下从仓库根目录运行。选用现场已有 SDK 和 Python，domain 31 仅为示例，不是业务 domain 结论。先在明确的 Orin Humble 环境做一次预检：

```bash
mkdir -p results
python3 scripts/collect_ros_trace.py \
  --preflight --view host --domain-id 31 \
  --ros-python /usr/bin/python3 --rmw rmw_fastrtps_cpp \
  --sdk-prefix /opt/ros/humble \
  --output results/trace-preflight-001
```

预检只检查工具、库编译状态、RMW和已有 daemon；不创建会话或启动 ROS 测试节点。缺依赖退出非零并保留每个原始 probe 的 stdout/stderr，不能删掉编译支持要求继续运行。若现场使用 overlay，应指定真实 tracetools 所在 prefix，而非直接套用 `/opt/ros/humble`。

ready=true 后，在另一个新目录执行**一次**：

```bash
python3 scripts/collect_ros_trace.py \
  --view host --domain-id 31 --seconds 5 \
  --ros-python /usr/bin/python3 --rmw rmw_fastrtps_cpp \
  --sdk-prefix /opt/ros/humble \
  --output results/trace-capture-001
```

失败即保留目录、原因和原始记录；不自动重试、扩大扫描、重建 SDK 或重复三机全量套件。SDK 未具备条件时，先返回缺口；由操作者单独准备隔离兼容环境，再继续正向测试。

## 证据和判断

| 输出 | 含义 |
| --- | --- |
| trace-status.json | 终态、退出原因、primary_error首因、interruption_error、cleanup_errors、会话/事件状态；优先于派生文件 |
| preflight.json、probe-*/stdout.bin、stderr.bin | 依赖检查及原始错误；ready 不代表已采集事件 |
| request.json、host.json、sdk-probe.json | 请求环境、boot/namespace、主机单调窗口、解释器及实际库来源 |
| identities.json、*-command.json、*-start.json/maps | exec 前 PID/starttime 与测试节点实际运行来源；不是业务身份 |
| control-*/command.json、result.json、stdout.bin、stderr.bin | 所有会话操作及停止后的原始诊断 |
| ctf/、ctf-manifest.json | 原始 metadata/事件流及逐文件字节数/SHA256；不重写 CTF |
| decode/stdout.bin、stderr.bin | Babeltrace 原始解码结果和诊断，合计上限 4MiB |
| publisher/subscriber.csv、*.log | 自有 C01 节点的原始记录；此入口不生成通信 benchmark 汇总 |

三项状态分别记录：tracing_compiled、session_active、events_observed。正常结束会话已经停止，故 session_active=false；这不表示从未启动。

`status=events_observed` 只表示：自有节点成功退出，停止/销毁/回收正常，CTF metadata 和非空事件流存在，解码成功，两 PID 均出现 rcl_node_init，publisher 出现 rclcpp_publish，subscriber 出现 callback_start/end，实际 PID/RMW/库来源核对通过。不保证全事件无损、消息全部交付或业务通过。解码逐事件核对 PID、namespace 和随机名称。synthetic=true 表示测试工作负载；在真实采集环境，CTF 事件来自实际框架执行。

原始 CTF 上限 64MiB，执行中周期检查，超过即失败并收尾；这不是内核级磁盘配额，检测间隙可能超限。失败和取消也尽量保存 CTF 清单，原始目录不删除。控制命令各限 10 秒，解码限 20 秒；超时或输出超过限额失败。清理错误独立保存，不把原始错误改成成功。磁盘完全不可写时无法承诺终态落盘，退出非零并在 stderr 保留写入错误及已存在证据。

以下字段仍未验收：CTF 与 Linux 单调时钟映射、CTF packet 的 discarded/lost 计数、节点生命周期与资源引用桥接、回调耗时解析和业务关联。`trace_lost_events=null` 保留原因，绝不填零；`callback_latency=null`、`business_acceptance=not_evaluated`。CTF 原始 metadata 用于后续时钟解释，不能直接将显示的 CTF 时间和 monitor 单调时间相减。

## 交给现场的测试描述

固定仓库提交和脚本摘要，在 Orin 已明确的 Humble SDK 上执行上述预检。记录内核、CPU架构、SDK/Python/RMW、LTTng/Babeltrace版本、ros_bench 与实际 tracetools 摘要，以及已有 daemon 条件。缺依赖留证停止，不改生产配置或安装全局 SDK。

预检成功后，只跑一个 5 秒自有节点发布窗口；核对原始 CTF 非空、逐文件摘要、两 PID 的初始化/发布/回调事件及实际库来源，核对最终状态、cleanup_errors 和进程回收。保留所有失败记录；没有业务样本关联时不计算业务 E2E。温度访问应为零，可沿用现场只读哨兵验证。

首次验证范围不包含三机全量、C01/S01性能套件、GPU、DDS尾延迟或ABBA。得到真实样本后再实现 CTF 导出适配、时钟与身份桥接；此后才进入真实业务关联。

参考：[ros2_tracing Humble](https://github.com/ros2/ros2_tracing/tree/humble)、[LTTng 事件过滤](https://github.com/lttng/lttng-tools/blob/stable-2.13/doc/man/lttng-enable-event.1.txt)、[Babeltrace](https://github.com/efficios/babeltrace)。与[ROS拓扑](ROS_TOPOLOGY_AND_TRACING.md)、[ROS证据](ROS_BUSINESS_EVIDENCE.md)配套使用。
