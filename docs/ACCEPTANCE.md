# 验收配置与尾部诊断

第一阶段先确认采集、输入、交付、资源覆盖与成本证据；第二阶段针对异常一次改变一个因素并复测。各维度独立记录，没有总的“性能通过”。真实算法的期限和容许影响由应用确定，模板保持未配置。

## 待填模板

| 模板 | 用途 | 尚需填写 |
| --- | --- | --- |
| [acceptance-suite.template.json](../configs/acceptance-suite.template.json) | C01 小/大包、S01 参考与三组连续 ABBA | 每个场景期限、容许违约率、采集预算与 ABBA 增幅 |
| [business-acceptance.template.json](../configs/business-acceptance.template.json) | 已运行业务的完整资源采集 | UID/名称/PID/cgroup 范围、总 CPU 与周期占比预算 |
| [tail-diagnostic.json](../configs/tail-diagnostic.json) | 大包 30 Hz、五轮独立尾部观察 | 可重复的环境、RMW、运行顺序与负载条件 |

复制模板到本地结果准备目录再编辑；现场范围和结果不提交源码仓库。套件模板展开为 15 个 case，名义运行 1428 秒，另加构建、发现、分析及过载延长。先通过短检查再运行正式窗口。

| 配置路径 | 单位/含义 | 默认 |
| --- | --- | --- |
| `cases[].scenarios[].deadline_us` | 从计划释放到完成的期限，微秒 | null |
| `cases[].scenarios[].acceptance_limits.max_deadline_miss_fraction` | 允许违约比例，0..1，0 表示观测范围内不允许违约 | null |
| `cases[].scenarios[].max_data_age_us` | C01 从生成到回调开始的数据年龄阈值，微秒 | null |
| `cases[].scenarios[].acceptance_limits.max_data_age_expired_fraction` | 有效交付中允许超龄的比例，0..1 | null |
| `defaults.resource_options.max_observer_cpu_percent_one_core` | 采集进程加自有遥测子进程的共同有效区间 CPU，单核=100% | null |
| `defaults.resource_options.max_cycle_fraction` | 最大完整采样管线墙钟耗时 / 实际采样节拍 | null |
| `sampler_comparisons[].max_p99_increase_percent` | 每组 ABBA 的逐轮响应 P99 中位数增幅上限，百分数 | null |

业务模板的后两项成本预算直接填写 `resource_options`。它没有真实 topic/frame/回调事件与业务 deadline 的配置能力，`business_chain` 保持 `not_evaluated`；不能拿资源 CPU 代替业务链时延。

`quality_limits` 的 1000 样本、释放迟到超过一个周期占比 1% 是可修改的输入质量起点，模板仍显式列出。它们与业务期限及开销预算是不同要求。旧 S01 配置的 1 ms 仅保留参考统计；未同时配置容许违约比例时不提供验收通过。

## 分项状态

每轮 `summary.json` 的 `results[].acceptance` 与 `REPORT.md` 展示：

| 维度 | 状态解释 |
| --- | --- |
| execution | complete 仅表示运行和统计完成；失败记录在 run-status.json，退出非零 |
| input | valid / invalid / unavailable / not_configured，独立检查实际输入数量及释放迟到 |
| delivery | complete / incomplete / unavailable；C01 缺失、重复、无效载荷和意外编号分别保留 |
| resources | observed / partial / unavailable，表示采样覆盖和有效计数区间，非所有可选传感器认证 |
| deadline、data_age | not_configured / not_evaluated / exceeded / within_observed_scope |
| collector_budget | not_configured / not_evaluated / exceeded / within_observed_scope，保留具体观测值及缺口 |

期限或年龄阈值与容许比例都需填写才评估；输入无效或交付不完整时不会给期限预算通过。仍保留原始统计，不删除输入超限轮次。资源和业务性能分别解释；资源缺失不自动将完整的消息输入判为无效。

采样成本的 `cycle_fraction_max` 保留旧口径，包含资源日志 flush，但不含成本日志自身写入。新增 `full_cycle_fraction_max` / `full_over_period_cycles` 使用写完成本日志后的完成时间；每轮完成记录放入下一轮或终止标记，避免递归计时。周期预算使用新口径；旧日志或中断造成完成证据不足时无法评估。该管线计时不含调度记账与终止标记写入，发现墙钟成本另列；总 CPU 包含整个进程，预算也要求有效观测和自有子进程证据。

## 多组 ABBA

`repeat_blocks` 默认 1，可设置 1..16，展开后套件最多 128 个 case。每组顺序必须是 minimal/basic/basic/minimal，工作负载、轮次和要求相同；额外组自动生成名字并紧接原组执行。

保留每个 run、每组增幅、范围、中位数及最坏有效组。所有组的计划 case/场景/轮次完整、输入有效、交付完整，且每组都在预设预算内，才给 `within_budget`。某一组超限不能被总体中位数掩盖；未知、零参考值、缺轮次或预算 null 都不能判通过。逐轮 P99 的中位数不是合并事件的 P99，也不是置信区间；ABBA 只比较资源采样增量，两种模式均保留时间戳和运行证据。

## C01 尾部证据

新采集的 C01 在正式循环外由自身记录启动与结束 maps，以及启动时查询的实际 RMW 标识和 endpoint QoS。结束 QoS/RMW 使用启动缓存并明确标注；订阅者结束快照可能已发生 ROS context shutdown，不冒充关闭前状态。

每轮保存 `runtime-evidence.json` 和原始 `*-runtime-{start,end}.{json,maps}`。子进程退出后在同一文件系统命名空间离线计算映射库当前文件的 SHA256，检查设备/inode 与哈希前后文件身份；已删除、被替换、不可读的库保留 null 和原因。这些哈希不是已加载内存的哈希，也不能排除总结前原地修改。DDS 环境只记录白名单声明；显式本地配置文件可哈希，内联 XML 只保留摘要，不访问网络 URI。映射库、环境和配置不能证明实际启用了某种传输，`transport_verified` 始终为 false。

`metrics.diagnostics.response_tail_decomposition` 关联 top10 响应尾事件编号，以下四段相加等于该事件响应时间：

1. 释放迟到：计划释放 → 实际生成。
2. 生成到发布：生成 → publish 开始。
3. publish 到回调：publish 开始 → callback 开始。
4. 回调墙钟：callback 开始 → 完成。

另列 publish 调用和 callback CPU 耗时，它们与上述段重叠，不能再相加。`received_in_drain` 区分计划窗口结束后的回调。第三段同时包含中间件与执行器等待，没有内核/DDS 逐事件 trace 时不能归因为分片、重传、网络或执行器中的某一项。

## 下一轮设备执行

短检查后，分别运行待填套件与大包诊断，使用新目录：

```bash
python3 -m perfkit.suite --config configs/acceptance-suite.template.json --output results/planned --dry-run
REQUIRE_JETSON=1 ENVIRONMENT_KIND=jetson-native \
  ./scripts/run-native.sh configs/acceptance-suite.template.json results/device-acceptance suite
RMW_IMPLEMENTATION=rmw_fastrtps_cpp REQUIRE_JETSON=1 \
  ./scripts/run-native.sh configs/tail-diagnostic.json results/device-fast-tail
RMW_IMPLEMENTATION=rmw_cyclonedds_cpp REQUIRE_JETSON=1 \
  ./scripts/run-native.sh configs/tail-diagnostic.json results/device-cyclone-tail
```

需要已经安装对应 RMW。比较前固定软件包/库摘要、CPU/功耗/散热、输入负载与 QoS，按不同顺序再执行一批；先看释放迟到，再看尾事件各段。异常轮次单列，不自动删掉。S01 参考可同时用于检查主机调度波动。Thor 正式 Ubuntu24/BSP38 的 ROS 依赖方案仍需设备验证。

现场完整日志、maps、库路径、业务选择规则均留在受控环境；交接仅需脱敏版本代号、分项状态、逐轮数值、覆盖及缺口类别。真实业务仍按 [业务采集指南](BUSINESS_MONITOR.md) 做相同覆盖的无采集/有采集对照，实际 topic/frame/回调链与 tracing 接入留待应用适配。
