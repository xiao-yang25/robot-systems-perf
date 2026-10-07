# M3a：一条单机业务链的事件接入

0.8.0新增 `robot-perf-business`，离线读取M2a资源窗口、业务链声明、输入输出事件和原始来源，关联样本并输出E2E分位数、吞吐、结果分类及选填deadline观察。仅用Python标准库，不依赖ROS SDK；不改已有采样器、不控制外部进程。当前阶段是通用接入接口，不代表已确认现场功能、接通真实CTF或完成业务验收。

开发板缺真实业务事件或构建绑定时，先按 [受控ROS链指南](DEVELOPMENT_BOARD.md) 验证真实ROS传递、ID关联和通用导入，再进入已知构建算法回放；不靠反复扫描未知部署或重复合成统计推进业务验收。

0.8.1修补mkdir成功后的取消留证空隙：在try内部延期中断，依次创建新目录、标记所有权、保存初始状态，随后处理挂起中断并落盘interrupted。mkdir失败不获得目录所有权，也不向已有结果写终态；首条状态写失败保留原错误并尝试failed终态。源工作清单引用的现有process实体，PID/starttime必须是1..2^63−1整数，拒绝bool、浮点、字符串及负数/越界，并核对引用中的身份；可选registration_id存在时也须为匹配的正整数。缺失资源仍保持unresolved，不改旧入口兼容行为。

## 总体路径与范围

先选一个真实功能路径，现场确认部署版本、输入/输出节点和样本ID。用原monitor入口观察相应进程，同时通过已有日志/事件导出，必要时增加最小埋点。导出器生成下述格式；导入核对同机时钟和资源身份，输出每个事件和每个样本的判定，再做稳定输入下的无采集/采集对照。

本次选择离线导入，避免在高频采集路径增加解析或ROS查询开销；已有ROS图与node-init导入继续使用M2b入口，CTF解码和初始化早于资源窗口的生命周期桥接留待独立增量。graph查询耗时不是消息时延。进程身份一致仅证明导出声明与历史资源记录相符，不认证算法、节点所有权或业务部署；功能继续candidate/人工声明。CPU/RSS只引用原资源记录，不能均分给节点。

## 接入前待确认

现场可直接使用 [只读业务接入核验prompt与匿名回传表](BUSINESS_INTAKE_REVIEW.md)，先确认源码—构建—部署绑定、请求生命周期和已有事件字段，再决定测量或最小埋点方案；该核验不修改生产、不启用追踪，也不重复已完成的框架回归。

| 项目 | 要确认的内容 | 未确认时 |
| --- | --- | --- |
| 业务边界 | input表示什么；output怎样判有效；明确一输入一终态的单路径 | 不以任意日志间隔代替E2E |
| 样本ID | 输入/输出同ID；窗口内不复用；重启/重试需含epoch或attempt | 重复ID整样本无效，不取第一条 |
| 部署关系 | 工作清单的function_id、绝对节点名、workload_id、workload_version与部署版本 | 保留人工声明，缺版本身份不关联 |
| 时钟与身份 | 事件发生时的boot_id、PID namespace、PID/starttime、linux_monotonic与domain | 不回填当前身份，不用墙钟强转单调时钟 |
| 完整性 | 独立输入清单或生产端计数、导出器丢事件计数与明确窗口 | expected_inputs/dropped_events保持null，质量partial |
| 要求 | 应用deadline、observer CPU/完整周期/超周期/覆盖预算、业务扰动上限 | 不预设达标线，不判通过 |

用户当前尚未确认样本ID和时间戳；[业务链模板](../configs/business-chain.template.json)及[事件模板](../configs/business-events.template.json)用于现场待填。事件模板的null不是合法身份，未填完导入会拒绝；不要用示例值替代实证。配置和原始日志只放现场结果目录，不提交公开仓库。

## 输入格式

业务链包含固定format_version=1、chain_id、workload_id、deployment_version，以及input/output的function_id和绝对node名、可选deadline_ns（正整数纳秒或null）。两个端点必须在源monitor工作清单声明中，允许同一功能、共享进程或不同进程。deployment_version必须等于monitor的workload_version；不同版本/窗口无法关联。链中没有自动验收上限，deadline用于观察。

事件导出为一个JSON对象（16MiB上限、最多100000事件），kind为robot_business_events，source.adapter为application_events_v1。tool_version记录导出器版本；raw_sha256必须与 `--raw-source` 的实际原始文件字节相符；synthetic显式区分测试fixture与现场导出。保留原始文件只能证明字节摘要，不证明转换正确或来源真实性，转换由导出器负责。原始文件也限16MiB，FIFO、目录、设备文件不读取。

context包含boot_id、pid_namespace、clock=linux_monotonic和ros_domain_id（0..232）。只有与源monitor一致时才关联；不支持跨机、墙钟/ROS模拟时间转换。window包含start_ns、end_ns、expected_inputs及dropped_events：时间段非空且事件都在段内，段应被monitor窗口覆盖。后两项可为null；不要仅数已读事件后就宣称独立输入完整或丢事件为0。

每个事件必须有以下全部字段：

```json
{
  "sample_id": "fixture-epoch-1-frame-42",
  "type": "input",
  "monotonic_ns": 123456789,
  "pid": 123,
  "starttime_ticks": 456,
  "function_id": "input_function",
  "node": "/fixture/input_node",
  "valid": true,
  "reason": null
}
```

type为input、output或drop。input/drop的valid必须true；output可为false表示无效交付。drop和无效output必须有非空reason。drop是明确终态丢弃，不是根据缺输出推断丢包；由链的output端点记录。一输入多输出、输出加drop、重复input/output/drop都不支持，整样本归为invalid_sample。乱序文件允许，但终态时间不能早于输入。

导出器应在事件发生时取得身份，而非事后根据PID查询；starttime_ticks是Linux `/proc/<pid>/stat` 的第22字段，不是纳秒。namespace属于记录的PID解释空间；单调时间必须实际可比，不能仅写同名clock。已退出对象身份、初始化事件和跨namespace映射不在本次自动桥接范围。进程/线程采集源当前身份与资源历史引用窗口按已有[M2a规则](BUSINESS_MAPPING.md)核对。

## 执行与输出

先按[业务采集指南](BUSINESS_MONITOR.md)运行明确范围的monitor，配置workload_version及domain，再导出同一窗口内业务事件。导出JSON和原始源文件不能相互循环计算摘要。导入在任意工作目录使用安装入口：

```bash
robot-perf-business \
  --monitor-run /path/to/monitor-run \
  --chain /path/to/chain.local.json \
  --events /path/to/events.local.json \
  --raw-source /path/to/original-events.jsonl \
  --output /path/to/new-business-result
```

不覆盖已有结果，输出不能在源monitor目录内部；源monitor必须是complete/interrupted、有结束时间且包含工作清单的M2a窗口。严格JSON校验拒绝重复字段、未知字段、非有限数值（包括指数溢出）、无法编码为UTF-8的文本及非法身份；事件整数不超过2^63−1。源monitor上下文必须是对象或null，严格预检与原读取的四文件摘要不一致时拒绝。输入错误在新结果目录创建前拒绝。运行中失败非零留证，SIGTERM/KeyboardInterrupt返回130。以business-status.json及cleanup_errors判断终态，报告存在不代表成功；永久不可写时终态不能保证落盘，原错误仍传播。

| 文件 | 内容 |
| --- | --- |
| business-status.json | 执行终态、原始错误、结束时间和次要收尾错误 |
| business-inputs.json | 工具来源、源monitor四文件摘要及三项输入摘要 |
| chain.json / events.json / raw-source.bin | 输入字节原样副本（可能包含现场敏感信息） |
| business-event-associations.jsonl | 每个事件的原始字段、身份关联、资源引用或缺失原因 |
| business-samples.jsonl | 每个样本结果、事件索引、有效E2E及deadline是否可评估 |
| business-summary.json | 分布、吞吐、计数、比例、质量、deadline及限制 |

每事件仅当boot/namespace/clock/domain/版本/窗口一致、端点声明一致且PID/starttime在该功能的资源引用观察包络内唯一匹配时，标imported_endpoint_identity_consistent。包络不代表连续存活，节点仍为导出声明；无记录、范围外、PID重用、多个注册重叠均保留unresolved或歧义，不选第一条。

## 指标口径

| 指标 | 分母/边界 |
| --- | --- |
| E2E P50/P95/P99/max | 唯一、身份一致且有效完成样本的input→output纳秒；nearest-rank；空样本null |
| 吞吐 | 有效完成数 / 整个声明事件窗口秒数；不是只取完成样本首尾 |
| 各结果比例 | 窗口内已观察输入的唯一ID数；孤立终态单列orphan_sample_ids，不加入比例 |
| 未完成 | 有输入、无终态，有限窗口内观察；不推断网络丢包 |
| 显式丢弃/无效交付/无效样本 | 分别为drop、invalid/reversed output、重复或冲突/孤立事件 |
| 质量 | expected_inputs与唯一输入数、导出丢事件、身份及样本有效性独立检查；reported_complete只是导出报告完整 |
| deadline | 固定成熟输入队列：input+deadline≤窗口end；边界附近完成与未完成均不进入分母 |

deadline按该成熟队列统计没有在期限内有效输出的样本；等于deadline算按时。身份未知/重复输入不算已测违约，记录unassessed_inputs。未到期输入、未知输入量、导出丢事件或无效/未关联数据不能据此判业务通过；deadline可能保留部分观测计数，但质量不完整时status=not_evaluated。未配置deadline为not_configured。业务验收始终not_evaluated；导入没有无采集基线，collector_budget为not_evaluated，源monitor开销预算需另看原报告。

稳定输入下做固定次数的无采集/采集对照，记录输入、时延、吞吐、超时与覆盖，再按[验收模板](ACCEPTANCE.md)填写现场预算；事件导出自身的开销也应计入。该步骤独立于导入功能验证。

## 有限验证与停止条件

无需重复三机全量、C01/S01/GPU/DDS或此前fixture回归。0.8.0仅新增离线模块和入口，已有生产采样/ROS模块应与0.7.2匹配。

首次在一台Linux设备做安装集成（不需ROS SDK）：

```bash
python3 tests/integration_business_events.py \
  --wheel /path/to/robot_systems_perf-0.8.1-py3-none-any.whl \
  --output /path/to/new-m3a-integration
```

此脚本使用测试自有进程的真实单调时间，synthetic=true，不代表真实算法验证；采集固定5秒并显式跳过温度，覆盖共享引用、原始记录复算、防覆盖、错误摘要、实际取消和自有对象回收。缺离线pip时按[离线安装指南](MACHINE_INTAKE.md)准备，不下载或安装SDK。相关单测为tests.test_business_events、tests.test_ros_evidence、tests.test_workload、tests.test_acceptance。

0.8.0安装与离线导入已有三机有限通过反馈，可以收尾。0.8.1仅需相关单测及一台Linux的上述安装集成，额外核对mkdir后SIGTERM130/interrupted、首次状态故障failed、错误源身份在创建前拒绝及旧目录逐字节不变；无需三机全套或再次比较夹具性能。

安装功能通过后收尾该增量；待现场确认一条路径再做一个固定业务窗口，核对独立输入清单、原始事件、身份/窗口、未完成分母及deadline。失败保留目录与具体缺口，不以增加次数直到通过为验收。Thor真实SDK与CTF是其他有限工作，不能以本次JSON导入替代。
