# ROS 环境预检与有限现场回归

0.7.0核心监控及故障回归已有三台设备通过反馈，真实图/组件查询已在Orin的Humble/Fast DDS验证。Thor本次只检查常规路径，未找到SDK不等于平台不支持。0.7.1补充显式SDK/Python/RMW选择、独立预检及有限回归入口。现场分项验证通过，但原样统一入口漏传温度跳过要求；Thor依赖错误被后续来源核对覆盖。0.7.2两项修补已有三机有限现场通过反馈；Orin原样入口首次图不完整，另一个新目录重试通过，首次失败原因仍未知。后续仅改受控fixture就绪与诊断，产品包和17模块保持0.7.2，不安装SDK、不切换系统默认环境。

## 先检查已有环境

预检不读取业务monitor目录，不做算法归属或性能验收。使用核心工具的已安装入口，选择现场SDK对应Python；SDK在私有路径时先加载其setup，再指定SDK目录用于核对，无须放到 `/opt/ros`。

```bash
robot-perf-ros --preflight --domain-id 0 --graph-wait 0 \
  --ros-python /usr/bin/python3 --sdk-prefix /path/to/ros \
  --rmw rmw_fastrtps_cpp --output results/ros-preflight-001
```

先在本次命令shell中按现场SDK方式加载已有环境，例如 `source /path/to/ros/setup.bash`。这一步由操作者执行，工具不加载或执行setup，不安装SDK、不猜私有目录；保留其启动日志。核心包不导入rclpy。

`--sdk-prefix`是绝对路径的已有目录，工具将实际rclpy模块路径和目录解析符号链接后核对包含关系；不匹配或模块路径缺失就失败。该选项用于核对已加载环境，不自动设置Python/库搜索路径；ROS overlay应指定rclpy实际所属前缀，而不是仅含业务包的overlay。未提供时沿用环境，仍记录实际来源。SDK目录可以在任意部署位置。

显式domain和RMW在适配器初始化前设置。`--rmw`省略时沿用环境和SDK默认，仍记录实际加载标识；明确选择后实际标识不符、依赖缺失或初始化失败均失败，不自动回退到其他实现。查询自有子进程直接启动指定Python；超时包含解释器启动/SDK导入，默认10秒、最多60秒；原SIGCHLD/独占回收前提仍适用。SDK环境的准备不是这次查询窗口的一部分。

预检临时创建工具自己的观察节点，读取图；指定 `--component-manager /robot/container` 时仅调用对应ListNodes。`graph-wait=0`用于短依赖预检，不证明图发现完整；空图可以通过依赖预检。指定组件管理器不可用时不通过。成功只证明这次选择的依赖与只读请求执行成功，不能证明所有SDK包、真实业务映射或tracing可用。

输出为权威 `ros-status.json`、请求 `ros-inputs.json`、`ros-preflight.json`、原始 `graph-query.json`及有界stdout/stderr。记录实际Python版本/路径、rclpy模块路径/版本、实际RMW及其环境声明；rclpy发行元数据缺失时版本为null，保留模块路径，不猜版本。解释器路径在导入rclpy前记录，缺依赖时也保留。适配器先失败时保留其首个原因，不用SDK/RMW核对覆盖；仅成功的图/空图才核对来源，首个来源不符也不被后续核对覆盖。失败非零，取消130；ready字段与权威终态共同判断，派生结果不能代替最终状态。已有结果目录拒绝覆盖。

现有M2b图入口也支持这两项环境参数，其他命令保持兼容：

```bash
robot-perf-ros --monitor-run results/business-001 --graph --domain-id 0 \
  --sdk-prefix /path/to/ros --ros-python /usr/bin/python3 \
  --rmw rmw_fastrtps_cpp --component-manager /robot/container \
  --output results/ros-evidence-001
```

图与已结束资源窗口分别记录，见 [M2b证据指南](ROS_BUSINESS_EVIDENCE.md)。

## 当前兼容证据

| 环境 | 核心/协议检查 | 真实ROS图和组件 | 限制 |
| --- | --- | --- | --- |
| macOS/Python3.9，无ROS | 宿主针对性验证 | 不适用 | 不作为Linux设备实测 |
| ARM64 Ubuntu22/Python3.10.12、Humble/rclpy3.3.22/Fast DDS | 断网Linux验证 | 开发容器真实部署通过 | 容器不是Orin/Thor性能基线 |
| Orin、现场Humble/Python3.10.12/rclpy3.3.21/Fast DDS，0.7.1/0.7.2 | 用户反馈337项全套及后续37项有限检查/安装通过 | 预检、真实图/组件通过反馈 | 0.7.2原样入口重试通过；首次空图失败保留，原因未知 |
| Thor、当前所选Python缺rclpy，0.7.1/0.7.2 | 用户反馈337项全套及后续37项有限检查/核心安装通过 | 未验证 | 0.7.2原样核心入口及缺依赖诊断通过；真实ROS未验证 |
| Cyclone/其他RMW、其他ROS/Python组合 | 已知/未知QoS受控契约检查 | 本增量未验证 | 受控值通过不认证真实RMW兼容 |

QoS集成不再要求所有RMW都报告UNKNOWN/depth0。对两个发布端、两个订阅端检查实际字段：UNKNOWN时depth=null且原因明确；已知策略须与测试组件的KEEP_LAST一致，depth等于reported_depth且为配置深度3、无缺失原因。可靠性仍须符合组件RELIABLE配置。实际端点结果保存到安装验证记录，不将报告0当作真实队列深度。

## 复用现场回归入口

回归脚本属于源码仓库，使用与当前源码Python模块逐字节匹配的wheel；从METADATA读取版本，不手工替换断言。已有离线pip/venv及Linux pidfd必需，不下载、不自动打包或编译SDK。

只做核心有限回归：

```bash
scripts/run-device-regression.sh \
  --wheel /path/to/robot_systems_perf-0.7.2-py3-none-any.whl \
  --output results/device-regression-001 --skip-temperature
```

它顺序复用M1安装和M2a受控集成，包含共享/身份/范围、防覆盖及故障/取消检查。显式 `--skip-temperature` 传递到所有真实采集，包括M1首次接入、取消及可选ROS fixture资源采集；M2a的light配置原已跳过温度，现在也显式传递。测试子进程安装温度发现/读取哨兵，保存逐子进程的temperature-guard记录并要求正常收尾且accesses为空；哨兵触发使回归失败，不作为成功绕过。未提供此选项时M1仍使用原来的默认温度探测。每阶段独立目录；`device-regression-status.json`保留版本、wheel摘要、阶段状态、结束和失败原因。没有请求ROS时标not_requested，不称全部ROS验证通过。

需要SDK预检可追加 `--preflight --sdk-prefix ... --ros-python ... --rmw ... --domain-id ...`。先由操作者加载匹配SDK，shell包装器仅exec所选Python，使fixture loader使用相同解释器；包装器不执行setup或改变SDK搜索路径。未定位SDK时保持具体缺项，不悄悄安装或回退。

需要真实受控组件集成时，先在已有匹配SDK中编译本仓库fixture（见 [构建命令](ROS_BUSINESS_EVIDENCE.md#验证与停止条件)），再明确追加：

```bash
scripts/run-device-regression.sh \
  --wheel /path/to/robot_systems_perf-0.7.2-py3-none-any.whl \
  --output results/device-ros-regression-001 --skip-temperature --preflight --ros-fixture \
  --sdk-prefix /path/to/ros --ros-python /usr/bin/python3 \
  --rmw rmw_fastrtps_cpp \
  --component-prefix results/m2b-component-install \
  --container-binary /path/to/ros/lib/rclcpp_components/component_container
```

受控fixture用独立domain77和随机namespace，只加载到测试自己创建的容器，不对生产组件做LoadNode/unload。默认预检domain0与fixture隔离domain77是两项不同观察。fixture正常/重复名称两个阶段分别使用最多3次、20秒总查询预算；单次graph-wait仍为2秒，query-timeout最多10秒且受剩余预算限制，预算不足不启动下一次查询。每次CLI有额外最多2秒退出等待，达到就绪期限后返回的完整结果也不通过；进程回收沿用已有有界清理，20秒不代表包括所有关闭/落盘的整阶段墙钟上限。attempt的evaluated_monotonic记录完整性判定时间，必须早于期限；finished_monotonic记录后续终态保存点，不用其代替判定时间。真实业务仅用生产只读入口，不用fixture假称算法验证。追踪导入仍为synthetic契约检查，未新增真实CTF证明。

## 下一次设备验证与停止条件

0.7.2温度传递及原始依赖错误两项旧P2已有三机有限验证，可以收尾；不用再跑核心安装、37项或三机全套/C01/S01/GPU/DDS/ABBA。Orin首次图不完整不能因同版本重试通过而被删除或称为根因修复。

测试后续修补只更新仓库的fixture就绪与诊断，复用模块匹配的0.7.2 wheel。具备已有SDK的一台设备，只运行一次 [受控ROS集成](ROS_BUSINESS_EVIDENCE.md#验证与停止条件)，追加 `--skip-temperature`。检查graph-readiness和duplicate-readiness：最终ready、selected_output对应一个单独完整快照；每次不完整快照与日志保留，recovered_incomplete和尝试数可见。不能合并不同快照补齐节点/端点；SDK/查询失败、任何已观察端点的QoS错误（即使端点数量不完整）、意外重复名和端点归属不符立即失败，不重试整个套件或自动重启fixture。持续不完整到次数/期限上限则失败，保留预期/实际节点、端点和查询窗口。查询退出失败仍有预期/实际退出码及日志路径。

开发受控空图复现只能证明处理机制，不能认定现场空图根因。若单次现场仍失败，保留已有目录和上述记录用于分析；不进行无限重试。该有限检查满足后收尾测试增量；没有SDK的Thor无需为此重测缺依赖路径。

下一项独立工作是在一台已定位兼容SDK的Thor上做真实预检/只读graph，缺依赖记录具体错误，不自动安装或反复搜索全盘。真实CTF身份桥接、算法声明确认和业务输入输出统计另做有限增量；没有事件和预算时业务验收保持not_evaluated。
