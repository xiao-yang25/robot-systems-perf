# M2a：业务清单与进程关系

M2a 在现有 monitor 上增加业务声明和进程关系输出：每个功能独立选进程，多个功能引用一份资源表。它不查询 ROS 图、不启动 tracing、不读取业务消息，不重启或发送信号给业务。Python 运行仍只依赖标准库；独立节点进程与组件容器都可使用。

本地受控进程与安装验证支持该入口的行为，尚未进行新增设备业务验证。声明的 ROS 节点、domain、业务版本和功能语义不因此变成运行时事实。

## 首次使用

在已激活的虚拟环境中安装当前源码，将 [业务清单模板](../templates/workload.template.json) 复制到设备本地，替换示例进程规则与节点声明，然后运行：

```bash
python3 -m pip install .
cp templates/workload.template.json workload.local.json
robot-perf-monitor --workload workload.local.json --profile light --seconds 10 \
  --require-jetson --output results/business-map-001
```

模板复制在源码目录执行；安装后命令支持任意目录，参数路径按启动目录解析。无 pip 的源码模块路径沿用 [机器接入说明](MACHINE_INTAKE.md#无-pip-或离线设备)，将模块替换为 `perfkit.monitor`。普通 Linux 验证可省略 `--require-jetson`。

首轮 10 秒用于检查声明和范围；若进程尚未启动、采样周期较长或覆盖不足，不能得到有效资源区间。持续时长、采样周期与预算由现场选择，不将短检查作为业务性能验收。

## 清单格式与选择语义

顶层 `format_version=1`，业务代号 `workload_id`，选填 `workload_version`、`ros_domain_id`，1..64 个 `functions`。每个功能有唯一 `id`、`process_selector`、选填节点标签 `ros_nodes` 和 `expected_processes`（默认 1，可设 1..256）。`relation_source` 仅支持 `operator_declared`，不能配置成追踪已验证。

选择器支持 `include_names`、`pids`、`uids`、`exclude_names`、`cgroup_patterns`。名称与显式 PID 是并集触发；该功能的 UID、排除项、cgroup 是硬约束，再与采集器的全局硬范围取交集。各功能分别匹配，避免将 A 的名字与 B 的 cgroup 混成扩大范围的全局规则。

必须提供名称或 PID；仅用 cgroup 时，明确填写 `include_names: [".*"]` 并配置 cgroup 硬范围。名称匹配 Linux comm 或可执行文件 basename，不是 ROS 节点名；comm 通常只有 15 个可见字符。配置多个 Python 进程时，优先结合 PID 或服务 cgroup。

全局 `--all-users`、排除名称、cgroup 与 `--max-targets` 继续有效。清单模式只用功能触发规则，不做活动阈值发现：全局 names/PIDs/activity CLI 选项被拒绝，配置中的非空 include_names/pids 或非默认 active_cpu_percent 也拒绝。monitor 配置中的默认 active_cpu_percent=1 在该模式不参与选择，实际模式写入 workload-profile 和 discovery 记录。

每个进程只注册一次。总量上限在功能匹配之后生效：保存完整匹配数量，候选详情限于实际选择范围；发生截断不把第一个候选冒充唯一归属。角色 `uids=null` 表示不另加 UID 限制，全局默认仍是当前用户。

`ros_nodes` 只接受本格式支持的绝对节点标签：ASCII 字母或下划线开头、后续可含数字，以 `/` 分层。一个功能内重复标签拒绝；多个功能重复声明同一节点标签，完整匹配身份并集大于一时标记声明冲突。这项检查在总量截断前执行，不依赖资源注册成功。多个功能共享同一进程身份不会因此冲突；单个功能声明多进程与一组节点时，尚未建立组内逐节点归属，不推断其内部冲突。以上均不判定运行时 ROS 重名。domain 仅是 uint32 声明，不配置或验证 ROS 网络。

## 输出与状态

原有 resources.jsonl、discovery.jsonl、monitor-summary、状态与报告保留，启用清单时新增：

| 文件/字段 | 内容 |
| --- | --- |
| `workload-profile.json` | 校验后的清单、内容摘要、全局范围与实际选择语义；可见 PID namespace |
| `business-relations.jsonl` | 每次扫描的读取窗口、功能匹配数、候选 PID/starttime、资源注册引用、过期引用及缺口；窗口关闭记录 |
| `monitor-summary.json.workload` | 最后扫描状态、历史状态/质量计数、历史资源引用及是否实际形成资源记录 |
| `BUSINESS_MAP_REPORT.md` | 业务功能关系和现有资源引用，显示人工声明与范围限制 |

| 状态 | 含义 |
| --- | --- |
| `candidate` | 当前观察到的匹配数符合声明；不证明算法语义或节点 PID 归属 |
| `unresolved` | 本轮没有匹配；不可解释成没有业务 |
| `incomplete` | 观察到的匹配少于声明数量 |
| `ambiguous` | 匹配超过声明数量，不任意挑选一个确认 |
| `conflict` | 多个功能重复声明同一节点标签且完整匹配身份并集大于一，需修正或核对声明 |

匹配数量还带 `scope_complete`：proc 项消失/不可读或可选来源缺失时保守标为不完整。候选超过总上限、资源注册竞态、未形成有效资源样本分别记录。成功注册的短命对象可能没有资源记录，保留引用和 `no valid process resource sample` 原因，不伪造 CPU/RSS。

扫描只证明对应读取窗口中的观察，不证明两次扫描之间连续有效。进程退出、PID 重用、重新注册或离开功能规则后，新扫描会标记旧引用过期；历史引用保留供复核。引用含资源 registration ID，因此同一 PID/starttime 离开范围后重新注册也不会混成一个窗口。namespace 缺失时保留 null，身份只能在本轮记录内解释，不能跨机器/重启直接拼接。

## 怎样查看功能资源

先看关系状态，再用资源引用查 monitor-summary 的 `resources.registered_entities`。两个节点或功能共享组件进程时会指向同一记录；这里不复制 CPU/RSS、不按节点数均分，也不将多个功能的同一进程重复求和。

资源指标覆盖整段资源注册窗口，关系引用仅表明该功能在哪些扫描中关联过它；本版不按功能关系子窗口重新统计 CPU。回调、线程归属、ROS 图验证、消息输入输出时延与业务 deadline 属于后续 M2b/M3。采集 CPU/周期预算沿用 monitor，未填写时保持未配置；关系候选齐全不等于业务验收通过。

每次扫描的关系构建、编码和写入计入发现总成本，暂并入 `selection_registration` 阶段；duration_scope 明确扩展口径。此功能增加工作量，本轮未宣称降低采集开销。历史歧义、冲突、范围缺口、截断和注册竞态不会被最后一轮恢复正常掩盖，质量保留 review_required。

## 设备有限复核

先挑一个组件容器和一个独立节点进程，填写本地通用代号、进程范围及人工节点声明，运行一次短窗口。核对匹配数量、PID/starttime、共享引用及范围缺口即可；业务进程不需要为了测试而重启。自然发生重启时可以核对旧引用过期；受控重启测试已经在本地完成，生产操作需按现场流程另行安排。

现场清单和原始结果留设备，公开仓库只放通用模板。反馈工具版本/模块摘要、脱敏匹配状态与异常类型，不需要上传节点名、服务路径或私有部署版本。已有全量性能套件不作为本次关系复核的前置条件。
