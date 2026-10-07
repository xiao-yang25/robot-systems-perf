# ROS 拓扑、真实路径与测量接入

目标是同一套工具适配不同机器人部署。机器档案、资源、通信结构和业务事件分别采集，再按时间与身份关联。真实场景用于确定业务语义、负载、部署约束和验收要求；Docker 参考场景用于可复现回归，两者互补。

## 三种图及其证据

| 图 | 来源 | 可以说明 | 不能证明 |
| --- | --- | --- | --- |
| 预期拓扑（静态） | 匹配部署版本的 launch、参数、remap、组件配置、源码 | 计划启动哪些节点和接口，候选内部路径 | 当前实际启动、生效分支、真实消息交付；仅源码包名不能绑定部署二进制 |
| 发现拓扑（运行时） | ROS graph、端点 GID/QoS、显式 ListNodes | 查询窗口内可见的节点名称、话题和端点声明 | 本地 PID、完整发现、QoS 匹配后真实交付、某个输出由哪个输入产生 |
| 样本处理路径（运行时） | ROS/内核 trace、关联 ID、应用阶段与终态事件 | 有证据的输入、回调、任务、输出/丢弃之间的关联及耗时 | 未采事件的历史身份、真实机械执行成功、缺失链路的因果关系 |

“静态”不等于离线保存：运行时图可以保存成静态文件，但它依然是运行观察。源码分析也不等于自动得到业务契约：条件分支、插件、参数和部署版本可能改变实际路径。

```text
配置/源码 ─────────────→ 预期接口和路径（声明）
                              │ 对照，保留偏差
运行时发现 ───────────→ 节点名 → topic → 节点名（端点声明）
                              │ 加身份、事件和样本关联
ROS/内核追踪 + 业务事件 → input → callback → task → output/drop（实证）
```

ROS pub/sub 是数据流，不是函数调用关系；服务是请求响应，action 还涉及 goal、反馈、结果和取消。节点 A 发布、节点 B 订阅同一话题，只能画出端点关系。重复节点名、远端节点和共享组件容器均不能按名字自动绑定 PID。

例如 B 同时订阅相机和 IMU，定时器每 50ms 发布一次状态。图能显示两个订阅和一个发布，但不能证明某次状态来自最新相机帧，也不能证明一次相机输入对应一次状态。融合、批处理、合并、多输出、重试和状态流需要各自的关联契约，不能用最近时间戳或第一条输出替代。

官方 [ROS topic 教程](https://github.com/ros2/ros2_documentation/blob/humble/source/Tutorials/Beginner-CLI-Tools/Understanding-ROS2-Topics/Understanding-ROS2-Topics.rst)演示运行图；[CARET 路径配置](https://tier4.github.io/caret_doc/main/configuration/intra_node_data_path/)也要求复杂链路明确节点内部的数据关系。

## 已实现：保存图快照的离线导出

现有 `robot-perf-ros --graph` 已通过 rclpy 采集节点、topic/type、publisher/subscription 端点及 QoS；显式组件管理器使用 ListNodes。查询创建自己的临时节点，会产生发现流量，不能当作零扰动观察。详见 [ROS 证据](ROS_BUSINESS_EVIDENCE.md)。

新增源码工具 `scripts/export_ros_topology.py`，不要求 ROS SDK，也不连接业务。将已有 **graph-query.json** 导出为 JSON 或 Graphviz DOT；不接受 preflight 文件或任意 ROS CLI 文本。

```bash
python3 scripts/export_ros_topology.py \
  --graph-query results/ros-evidence-001/graph-query.json \
  --output results/topology-001.json

python3 scripts/export_ros_topology.py \
  --graph-query results/ros-evidence-001/graph-query.json \
  --format dot --output results/topology-001.dot
```

输出父目录须已存在，文件必须不存在；可从其他目录用脚本的绝对路径调用。图形渲染按需使用已有 Graphviz，工具不安装它：

```bash
dot -Tsvg results/topology-001.dot -o results/topology-001.svg
```

导出语义：

- 节点是**名称分组**，不是可信节点身份。重复名称保留计数和 ambiguous；只有端点中出现的名称保留 endpoint_only，不补造节点观察。
- 每条边对应一个已报告的发布/订阅端点，保留完整 GID、QoS 和 topic/type；不将发布端和订阅端自动配对成已交付消息，也不判定完整 QoS 兼容性。
- 同一个名称分组仍可拥有多个端点；不选择一个 PID，不把组件 CPU/RSS 分摊到节点。
- 保存源文件字节 SHA256、domain、查询窗口、运行来源和组件原始观察。未知 depth 保留 null；缺失端点读取保留 unavailable，不填零。
- JSON 保留完整结构；DOT 的可视标签是概览，完整结构（包含全部端点 QoS、组件状态/原因和来源）保留在文件首行的单行 JSON 注释中。组件观察不画成本地进程归属关系。
- failed/empty/unavailable 图可作为诊断导出，保留原状态和原因；导出退出 0 仅表示派生文件写出，不代表图查询或业务验收成功。
- 源目录只读；运行终态仍以源 `ros-status.json` 为准，本导出不读取或认证该终态。写出失败/取消返回非零，可能留下部分派生文件，应在新文件名重做，不覆盖源证据。

这次仅支持单个 topic 快照。服务/action、周期图采集、快照差异、静态配置解析、真实 CTF 关联均未实现。图中名称可能敏感，派生文件与原始证据留在本地 `results/`，发布前由操作者决定匿名化范围。

## 动态运行怎样处理

动态部署存在节点创建/销毁、组件加载/卸载、重启、PID 重用和短暂发现缺失。后续图序列应按固定的低频周期与总窗口保存每次原始观察；每次图保留自己的时间窗口与状态，不能拼接为某一时刻的“完整图”。

两次观察的差异首先是“本次出现/未观察到”，不能直接称为进程出生/死亡。需要初始化/销毁事件与 boot、PID namespace、PID/starttime 的生命周期证据才能升级结论。endpoint GID 也不是资源引用或主机 PID。跨 boot 或时钟不确定的记录不合并成连续生命周期。

当前资源 monitor 已跟踪进程身份更替；节点生命周期桥接仍是缺口。初始化早于资源首窗时，现有规范化元数据入口可能 unresolved，不能回填当前身份冒充历史身份。没有授权不为补初始化事件重启业务。

## 是否必须修改业务源码

不必从全量插桩开始。按需要分三层接入：

| 层 | 复用工具/证据 | 业务源码 | 限制 |
| --- | --- | --- | --- |
| 基础观察 | 本工具资源采集、rclpy graph；已有 rqt_graph 可查看 | 通常不改 | 得不到逐请求 E2E 或内部队列因果 |
| 框架追踪 | ros2_tracing + tracetools_analysis；按需 CARET、内核 trace、Nsight | 启用已有 tracepoints 时通常无需手写埋点；可能需匹配编译库/启动环境 | 具体事件取决于发行版、rclcpp/rclpy、RMW、编译开关和权限；不保证覆盖业务异步任务或 GPU 完成 |
| 业务关联 | 已有日志/请求 ID/结果契约；不足时最小业务事件 | 缺可靠事件时才修改 | 明确接纳、样本关联、任务传递、真实终态及丢事件计数 |

[ros2_tracing](https://github.com/ros2/ros2_tracing/tree/humble)提供框架事件；[tracetools_analysis](https://github.com/ros-tracing/tracetools_analysis)提供分析；[CARET](https://tier4.github.io/caret_doc/main/design/software_architecture/)使用已有事件和部分函数 hook，也需要用户补充复杂路径关系。hook 不等于任意二进制都兼容，不能未经验证改生产运行环境。

[ros2_benchmark](https://github.com/NVIDIA-ISAAC-ROS/ros2_benchmark)可通过输入播放和输出监测对图进行基准，不要求给每个算法函数写埋点；仍需要明确输入/输出关联、负载和有效结果定义。内部诊断和业务验收边界应分别报告。

如果请求进入队列、跨线程、重试后才产生结果，建议只在缺口处记录：

```text
输入责任边界 ── sample_id / epoch / attempt ──→ 异步任务 ──→ 真实终态
    input              ID 随任务传递               output / drop
```

事件发生时记录 Linux 单调时间和身份上下文；源码须先与实际构建/部署绑定。不采业务载荷。输入回调收到数据、成功接纳和上游发出数据是不同边界，应选择并记录。publish 返回也不同于下游执行成功。

业务事件导出需有界缓冲、明确失败/缓冲满/未排空计数；热路径避免同步写盘。导出自身有开销，纳入稳定输入下的无采集/采集对照。当前参考场景的同步 JSONL 写入只服务受控验证，不是生产推荐埋点实现。

M3a 目前按选定样本的 input 与至多一个真实终态统计；融合或多输出须先选择合适的业务契约，不能强行伪造一对一。周期控制还可能需要数据年龄、反应时间和更新率，这些不能由同名 topic 图直接推导。

## 一套工具的复用边界

扩展当前仓库作为测量编排和证据关联层：

```text
机器档案 + 资源窗口 + 图快照 + ROS/内核/GPU追踪 + 业务事件
                              ↓
                 来源/身份/时钟/窗口/完整性校验
                              ↓
                拓扑、阶段耗时、E2E、覆盖与预算
```

复用成熟 tracer、CTF 解码器和 profiler，不自行重写它们。自研范围是适配器、统一证据、身份桥接、业务关联模板和有限验收。`header.stamp`/ROS 仿真时间不自动等于 Linux 单调时间；跨机需要时钟映射及误差，不能直接相减。SDK/trace 不可用时基础资源观察仍可使用，缺失能力保留原因。

## 有限推进顺序与停止条件

1. **本次增量**：公开资料离线包 + 已保存图的 JSON/DOT 导出；复用现有 Docker 三图和已知缺失/重复名夹具验证。不会要求三机重跑全部套件。
2. **真实框架证据**：一套明确 SDK 的受控部署，先验证真实 ros2_tracing/CTF、初始化、回调与进程身份；不以合成元数据宣布实机 tracing 通过。
3. **一条代表业务链**：优先已有可关联事件；缺口处加最小事件。静态声明与动态观察对照，独立输入清单、终态和导出损失必须可核对。
4. **性能验收**：固定输入、预热、有限重复；分别测闭环响应和独立速率输入下的容量/排队，配置 deadline 和采集预算。控制闭环保留业务语义，不因测试强行改成开放输入。

完成既定窗口即停止；缺 SDK、身份或业务终态时保留 unresolved/not_evaluated。真正业务测试决定场景是否有代表性；基础工具完善不必等待完整机器人，但不能据 Docker 或开发板短测认证机器人生产性能。

相关：[参考资料](REFERENCE_GUIDE.md)、[Docker 参考场景](DOCKER_REFERENCE.md)、[业务事件契约](BUSINESS_EVENTS.md)、[业务映射](BUSINESS_MAPPING.md)、[验收预算](ACCEPTANCE.md)。
