# M2b：ROS 图与对象关系证据

0.7.0 增加独立入口 `robot-perf-ros`。先结束一次 M2a workload 采集，再明确启用一次图查询或导入节点初始化元数据。原 monitor 目录只读，新证据写入新目录；图查询不放进高频资源采样循环。节点名称、图中可见和导入身份一致性分别记录。

本增量使用官方 rclpy 图接口和 composition_interfaces/ListNodes 只读服务，不启动 ROS CLI daemon、不订阅业务数据、不设置参数、不加载/卸载生产组件。核心安装包只依赖 Python 标准库；ROS 查询在操作者选定的、已有 rclpy 的独立 Python 解释器中运行。查询会临时创建自身的 ROS 节点，退出后销毁并回收查询进程。

0.7.0图功能已收到Orin现场通过反馈；0.7.1可先做独立SDK/Python/RMW预检，见 [环境与有限回归指南](ROS_ENVIRONMENT.md)。真实tracing与业务归属仍未验证。

## 在设备上执行一次

保留现场清单和节点名字在本地。先按 [业务声明指南](BUSINESS_MAPPING.md) 采集一套业务；需要后续追踪一致性判断时，在清单填写正确的 `ros_domain_id`。0.7.0 monitor 在 environment.json 增加 boot_id、PID namespace 与 linux_monotonic 时间域，缺项保留 null 和原因。旧目录仍可做图查询；缺身份上下文时不提升追踪关联。

在与业务一致的 ROS SDK/RMW 环境中，使用对应 Python：

```bash
robot-perf-ros --monitor-run results/business-001 \
  --graph --domain-id 0 --ros-python /usr/bin/python3 \
  --graph-wait 2 --query-timeout 10 \
  --output results/ros-evidence-001
```

`/usr/bin/python3` 是示例，需要选现场 SDK 实际可导入 rclpy 的解释器，并保留 SDK 必需的环境路径；安装核心工具的 venv 不必装 rclpy。`--domain-id` 必须显式提供，不与已记录的清单 domain 矛盾。工具不自动安装或切换 SDK。缺 rclpy、组件接口、服务超时和查询失败都留证，明确启用的图查询失败退出非零。

已有组件管理器可追加一个或多个绝对名称：

```bash
robot-perf-ros --monitor-run results/business-001 \
  --graph --domain-id 0 --ros-python /usr/bin/python3 \
  --component-manager /robot/container \
  --output results/ros-evidence-components-001
```

只查询显式管理器的 ListNodes；不扫描服务后向未知管理器发请求。管理器报告组件名和 unique_id，不提供可信本地 PID。没有该服务保留不可用，不把它当成没有组件。参数非法在读取业务证据、创建输出或启动查询前拒绝；已有目录拒绝覆盖，输出不得位于源monitor目录内，含解析后的符号链接路径。

## 怎样解释结果

| 输出 | 用途 |
| --- | --- |
| ros-status.json | 权威执行状态、原始错误、结束时间；complete仅表示请求执行完成 |
| ros-inputs.json | 原 M2a 文件摘要、工具来源、查询选项与可选导入摘要 |
| graph-query.json | 指定domain的节点、topic/type、publisher/subscription端点GID/QoS、组件结果和查询时间 |
| ros-graph-query/stdout.bin、stderr.bin | 自有适配器的原始有界输出，保留失败证据 |
| trace-metadata.json | 提供时逐字节保留的规范化元数据输入 |
| ros-relations.json、ROS_REPORT.md | 各功能的声明、图可见性、追踪身份一致性及共享资源引用 |

自有查询要求默认SIGCHLD：忽略或自定义处理器会在启动查询前拒绝，避免子进程自动回收丢失失败退出码；不修改全局处理器。嵌入API的调用者还须禁用原生SA_NOCLDWAIT及其他子进程reaper，Python守卫不能检测这些原生状态。最终状态写入出现一次性错误时，入口保留原异常并独立尝试保存failed/interrupted状态；介质持续不可写时不能保证落盘，派生报告不得代替权威状态。

图可包含远端节点，没有本地 PID 所有权。图快照与原 monitor 窗口分别记录，通常发生在资源窗口之后，不声称同期。图为空标 empty/未观察到，不证明没有业务；重复节点名标 ambiguous。节点和端点读取不是原子操作，短窗口也不证明发现完整。

QoS来自实际RMW端点图接口，不能还原完整应用配置。保留策略枚举及 `reported_depth`；history为 UNKNOWN 时 `depth=null`，带 `depth_reason`，不将报告的0解释为配置深度。端点GID也不是本地PID。

资源仍引用原 monitor-summary.json 的 registered_entities。同一个组件容器被多个节点引用时，不复制CPU/RSS、不求和、不均分。quality只描述关系证据；business_acceptance保持not_evaluated。图查询在资源窗口外执行，原monitor开销不包含这次查询进程；本增量没有给图查询CPU或业务扰动预算通过。

## 可选：导入节点初始化元数据

本增量仅支持版本1的规范化 `ros2:rcl_node_init` 元数据输入，不直接解码任意CTF、Babeltrace文本或callback事件，不启动tracing。真实SDK缺失或初始化事件没有采到时，可完成图路径并保留进程归属未知；不要为补事件重启生产业务。

```bash
robot-perf-ros --monitor-run results/business-001 \
  --trace-metadata trace-nodes.local.json \
  --output results/ros-trace-evidence-001
```

输入契约如下；示意时间、PID、namespace和digest必须由真实导出证据替换，不能拿模板冒充现场数据：

```json
{
  "format_version": 1,
  "kind": "ros2_node_init_metadata",
  "source": {
    "adapter": "ros2_tracing_normalized_v1",
    "tool_version": "your-exporter-version",
    "raw_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "context": {
    "boot_id": "00000000-0000-0000-0000-000000000001",
    "pid_namespace": "pid:[1]",
    "clock": "linux_monotonic",
    "ros_domain_id": 0
  },
  "events": [
    {
      "event": "ros2:rcl_node_init",
      "monotonic_ns": 150,
      "pid": 123,
      "starttime_ticks": 50,
      "node_handle": 1,
      "node_name": "detector",
      "namespace": "/robot"
    }
  ]
}
```

官方 [Humble tracepoint定义](https://github.com/ros2/ros2_tracing/blob/humble/tracetools/include/tracetools/tracetools.h)包含节点handle、名称和namespace；boot_id、PID namespace、starttime及时间转换不是该事件自带的完整身份保证。导出侧须有同时采集的身份/时钟依据，不能用导入时当前PID回填过去身份，不能把CTF epoch时间直接当monotonic。本仓库尚未提供原生CTF导出适配器或验证真实追踪SDK；没有这些依据就保持声明/未知。

导入只做一致性校验：boot/namespace/时间域/domain匹配原M2a记录，事件PID/starttime在该功能的历史资源引用范围内，事件时间落在该引用的首次与末次观察边界内，且有匹配的进程资源记录。初始化早于引用窗口、PID重用、范围外对象、旧目录缺身份上下文及不匹配均保留原因；多个引用保留歧义，不取第一个。该边界是历史点观察，不证明中间每一刻关系持续有效。

成功状态为 `imported_trace_identity_consistent`，来源明确为外部规范化导出，真实性未认证，不写为已验证算法/持续节点归属。原始digest和导出器版本是可追溯声明，不是工具已核验原始CTF的证明。未知format/事件/字段、重复事件/JSON键、非有限值、FIFO/设备文件和超大文件拒绝；单文件16MiB、事件最多100000。

## 验证与停止条件

开发集成使用已有Humble SDK、真实rclcpp组件容器和本仓库两个测试组件，另加独立节点及重复名称节点；全部部署由测试新建并只回收自己的进程。规范化追踪数据是显式synthetic fixture，仅证明安装导入边界，不代表真实tracetools运行。

如需复现该受控集成，先在已有ROS构建环境编译测试组件，再运行安装测试：

```bash
cmake -S tests/fixtures/ros_components -B results/m2b-component-build \
  -DCMAKE_INSTALL_PREFIX="$PWD/results/m2b-component-install"
cmake --build results/m2b-component-build -j2
cmake --install results/m2b-component-build
python3 tests/integration_ros_evidence.py \
  --wheel /path/to/robot_systems_perf-0.7.2-py3-none-any.whl \
  --component-prefix results/m2b-component-install \
  --container-binary /opt/ros/humble/lib/rclcpp_components/component_container \
  --ros-python /usr/bin/python3 --skip-temperature --output results/m2b-integration-001
```

集成需要Linux pidfd、已有匹配SDK/编译依赖和预备离线pip，不自动下载；LoadNode仅用于该测试随机namespace下新建的组件容器，生产入口不调用LoadNode。测试核对模块摘要、共享引用、图/资源窗口区分、QoS未知项、重复名、无ROS失败、防覆盖、真实SIGTERM130及自有查询进程回收。失败目录保留。

受控fixture的正常图和重复名称图分别保存graph-readiness.json及duplicate-readiness.json，最多3次、20秒查询预算。每次使用新结果目录（graph、graph-002等），stdout、graph-query、CLI终态不重写；就绪记录含预期/实际节点、组件、端点、QoS和两个查询窗口。仅完整的同一次快照被selected_output引用；不完整成功观察可以继续，查询/依赖错误及任何已观察端点的错误QoS等立即失败，不因端点缺失而跳过QoS校验。evaluated_monotonic记录期限内完整性判定，终态落盘时间另列。单次graph-wait保持2秒，query-timeout不超过10秒/剩余预算，额外退出与对象回收时间单列在 [环境指南](ROS_ENVIRONMENT.md)。此为测试就绪机制，不改变生产图查询或消息时延语义，也不证明现场瞬时空图根因。

现场只需一套代表部署做一次只读图关系复核：优先同时包含独立节点和共享组件，核对声明名、实际图/组件结果、重复/缺失原因及资源引用，确认源目录不变。没有rclpy或ListNodes时记录具体缺口，停止重复尝试，不因此要求三机全套/C01/S01/DDS/GPU或性能ABBA重跑。该增量功能复核通过后收尾；原生追踪适配/身份实证另开有限增量，真实输入输出时延、吞吐、deadline和稳定输入无采集对照属于M3。

接口依据：[rclpy Node图API](https://github.com/ros2/rclpy/blob/humble/rclpy/rclpy/node.py)、[组件管理器ListNodes实现](https://github.com/ros2/rclcpp/blob/humble/rclcpp_components/src/component_manager.cpp)。开发核对日期2026-10-06，开发环境为Humble/Fast DDS；Orin的Humble/Fast DDS已有有限现场通过反馈；Thor真实ROS及其他发行版尚未验证，不据此认证所有现场SDK。
