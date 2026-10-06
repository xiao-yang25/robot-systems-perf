# 开发板上的受控ROS业务链

开发板可先验证平台、中间件和通用事件接入，再接已知构建的算法回放，最后在整机验证传感器到执行器及实际工况。当前已有机器信息、S01/C01、资源、ROS图和离线事件能力；不因缺整机重复全套，也不继续无限扫描未知部署源码。

## 本次有限目标

复用0.8.1安装包，不改18个生产模块。新增源仓库测试入口 [integration_ros_event_chain.py](../tests/integration_ros_event_chain.py) 和 [ROS链源码](../tests/fixtures/ros_event_chain.py)。三个测试自有进程在随机namespace内运行：source发布input，processor订阅并产生output/drop，sink订阅output并核对ID及有效性。显式指定domain、SDK、Python和RMW；入口不安装SDK、不执行setup、不操作现有业务进程。

控制器预先保存30个唯一epoch:attempt:id，attempt固定为0；这是独立于事件写出流的**计划输入清单**。运行必须核对计划、真实input、terminal以及sink输出ID完全匹配；缺项、重复、身份或有效性不一致直接失败，不能把计划数当作已经接纳的实际输入数。受控规则为20有效output、5明确drop、5无效output，sink应收到25条消息。

节点在ROS初始化后自行记录实际domain、RMW、解释器、rclpy来源、boot、PID namespace和PID/starttime；控制器与安全绑定的自有进程及monitor上下文核对。直接执行Python源码的摘要绑定该实例，并作为workload/deployment版本；这不证明任何生产部署的版本或节点归属。

## 环境与执行

选择一台已有兼容ROS SDK的Linux机器；当前本地验证为原生ARM64、Humble/Python3.10、Fast DDS。Thor缺SDK时先准确定位现有SDK，依赖缺失应失败留证，不用JSON导入冒充ROS验证。SDK依赖沿用 [ROS环境指南](ROS_ENVIRONMENT.md)，离线pip/wheel准备见 [机器接入指南](MACHINE_INTAKE.md)。

在源码仓库执行，先按现场惯例加载选定SDK环境。参数必须对应实际环境，输出使用新目录：

```bash
python3 tests/integration_ros_event_chain.py \
  --wheel /path/to/robot_systems_perf-0.8.1-py3-none-any.whl \
  --ros-python /usr/bin/python3 \
  --sdk-prefix /opt/ros/humble \
  --rmw rmw_fastrtps_cpp \
  --domain-id 78 \
  --output /path/to/new-controlled-ros-chain
```

domain 78仅是示例，应选现场允许的测试domain；随机namespace隔离测试topic。入口先离线安装到临时虚拟环境，从无关目录调用安装入口并比对wheel版本及全部模块摘要，再执行SDK预检。资源观察固定5秒、light、100ms资源/200ms发现，显式跳过温度并用读取/发现哨兵验证。数据阶段30个输入以约50ms间隔发送；就绪与交付有10秒上限，节点总寿命上限30秒，没有失败后自动重跑。

相关检查：

```bash
python3 -m unittest tests.test_ros_event_chain tests.test_business_events \
  tests.test_ros_evidence tests.test_workload tests.test_acceptance -v
```

## 证据与指标边界

| 文件 | 用途 |
| --- | --- |
| test-status.json | 测试complete/failed/interrupted、首错、结束时间及收尾错误；取消退出130，运行失败退出1，参数用法错误退出2 |
| install.log、installed-modules.log、preflight/ | 安装、模块摘要核对、真实SDK预检、原始依赖错误与来源；安装超时或中断也保留已输出诊断 |
| fixture-provenance.json、各角色identity.json | 实际执行源码摘要、SDK/RMW/domain、各进程自行记录的身份与上下文 |
| input-inventory.json | 控制器预先计划的唯一输入，必须与实际input逐ID核对 |
| 各角色raw.jsonl、recording.json、done.json | 实际事件/接收记录、成功记录数量、完成标志；写出失败不伪报零损失成功 |
| monitor/、temperature-guard.json | 原始资源窗口及温度跳过证据 |
| events-raw.jsonl、events.local.json、chain.local.json | 保留逐进程原始字节，确定性拼接及导入声明 |
| import/、verification.json | 复用现有business导入；核对分类、分位数、整窗吞吐及源monitor不变 |
| lifecycle.json、各阶段log和command.json | 安全回收自有进程及执行证据；不信号外部业务进程 |

E2E从source的input事件（publish之前）到processor的终态output事件（publish调用返回之后）或drop事件；包含受控输入写出、DDS/executor等待和回调处理。sink接收仅作为独立输出传递核对，**不作为本次E2E终点**；publish返回不等于下游成功。20个有效完成样本才进入分位数，吞吐分母为完整资源窗口，不是发送时段。

这条链使用真实ROS传递和Linux单调时间，但业务数据仍为synthetic=true。处理规则是测试规则；记录器同步写文件并flush，其开销包含在观察中，不能作为生产埋点最佳实践、低开销承诺或算法性能。无效输出使质量partial；deadline、预算未配置，业务验收not_evaluated。30个输入不能用来证明长期尾延迟或硬件排名。

## 收尾与下一步

一台Linux的相关单测和一次安装ROS链成功，核对取消回收、防覆盖及缺依赖失败后，收尾该增量。不要求三机重复，不重跑全量、旧ROS组件、C01/S01/GPU/DDS/ABBA。失败保留新目录、具体阶段和日志，先分析原因，不运行直到通过。

随后选一份构建与源码明确的实际算法，在隔离实例用固定输入回放。确认真实input/terminal边界、ID传递及输入完整性，再适配 [业务事件格式](BUSINESS_EVENTS.md)。回放结果明确标注来源，不替代真实传感器到执行器的整机验证。旧部署无法绑定时可建立新的受控构建基线，不能据此声称匹配旧二进制。
