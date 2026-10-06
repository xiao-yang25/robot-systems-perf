# Docker机器人参考场景

用一套可克隆的环境，在无私有算法、无整机的条件下建立已知拓扑的基础测量。三个场景都执行实际基础算法，并通过ROS 2跨进程通信和仿真状态反馈闭环；不是只发送字符串或用sleep替代计算。

这是简化的CPU参考负载，不是完整生产机器人软件栈。导航没有Nav2/SLAM、机械臂没有MoveIt/抓取或碰撞场景、感知没有神经网络推理。所有结果为`synthetic=true`；完成仅表示参考场景执行和证据校验成功，业务验收保持`not_evaluated`。

## 三个通信拓扑

| 场景 | 独立进程/节点 | 实际计算与反馈 |
| --- | --- | --- |
| navigation | simulator → localizer → planner → controller → simulator | 已知栅格、绝对位置观测与里程计的标量Kalman融合、四邻域A*绕墙路径、限速比例控制、二维全向质点积分 |
| perception | simulator → perception → fusion → planner → controller → simulator | 带背景的模拟点云、深度阈值前景分割/质心、目标状态平滑、A*、限速控制与质点积分 |
| arm | simulator → perception → ik → trajectory → controller → simulator | 模拟点云目标、平面二连杆解析逆解、三次关节路径短视距插值、限速关节控制、关节积分和正运动学反馈 |

一帧只有一个在途样本，返回控制结果后才能发送下一帧；传感器在计划释放时间生成输入，超时或节点退出则失败，不用增加在途数掩盖过载。默认20Hz、10秒、200个独立预先登记的样本；若链路积压，记录释放迟到，不声称满足20Hz实时性。仿真每次指令按固定`1/hz`步长积分，不是高保真物理或真实时间动力学。

每次有独立namespace；闭环每一跳一个可靠订阅。传输使用`std_msgs/String`的JSON信封，便于携带sample_id与同机单调时间。它的载荷/编码成本不代表PointCloud2、Image或Nav2 action的成本；需要这些原生类型时建立独立场景，不能复用本场景数值作为结论。图查询保存双向端点和实际RMW QoS，未知history/depth继续保存未知值。

## 一次运行全部场景

需要Docker、Git、Bash。容器包含Ubuntu 22.04/ROS 2 Humble、Fast DDS、参考节点、现有Python采集器及C01/S01二进制；不需要GPU、显示器、pip或私有SDK。默认入口使用Docker引擎原生ARM64或AMD64，禁止用另一架构仿真报告平台性能。

```bash
git clone https://github.com/xiao-yang25/robot-systems-perf.git
cd robot-systems-perf
./scripts/run-reference-docker.sh all results/reference-001
```

首次可能拉取官方基础镜像、编译两个已有C++工具；运行约一分钟，发现/预检和启动另计。输出目录必须不存在。单场景和有限负载调整：

```bash
./scripts/run-reference-docker.sh navigation results/navigation-001
./scripts/run-reference-docker.sh perception results/perception-001 --seconds 10 --hz 20 --points 2048
./scripts/run-reference-docker.sh arm results/arm-001 --seconds 10 --domain-id 80
```

参数范围：seconds 2..30、hz 1..100、points 8..8192、depth 1..1024、monitor-interval 0.1..2秒、domain 0..232。points仅影响两个点云场景；seed默认42。较短窗口只验证链路；闭环目标误差必须下降，但不以短窗口是否最终到达目标判性能达标。默认监控light、资源0.5秒/发现1秒、温度跳过，并使用读取/发现哨兵核查零访问。期限和开销预算没有预设通过线。

镜像可独立导出到离线机器：

```bash
docker save robot-systems-perf:reference -o robot-reference.tar
# 在相同CPU架构的另一台机器：
docker load -i robot-reference.tar
mkdir -p results
docker run --rm --init --stop-timeout 15 --user "$(id -u):$(id -g)" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,size=256m --network bridge \
  --mount "type=bind,src=$(pwd)/results,dst=/results" \
  -e EP_ENVIRONMENT_KIND=docker-offline-reference \
  robot-systems-perf:reference python3 -m scenarios.robot_reference.run \
  --scenario all --output /results/offline-001
```

镜像运行无需网络下载；桥接网络仅用于容器内ROS通信。不映射宿主PID、设备或生产ROS网络，不更改SDK、频率、实时权限。需要离线复现时另保留镜像ID与tar摘要；只有相同源码和依赖不保证调度噪声相同。`BASE_IMAGE`可以设为已审核的镜像digest，`REFERENCE_IMAGE`控制构建标签；默认标签会随上游更新，结果保留实际基础镜像、运行镜像ID和依赖清单。离线示例没有宿主引擎自动校验，操作者必须核对镜像架构。

## 测什么、如何读结果

先读`reference-status.json`，`complete`还应有`lifecycle.json`确认自有对象已回收；报告不是终态依据。失败/取消退出非零，SIGTERM返回130，保留原始证据且不覆盖旧结果。自有子进程通过pidfd清理，外部/生产进程不纳入生命周期管理。

| 输出 | 用途与边界 |
| --- | --- |
| REFERENCE_REPORT.md | 三场景输入/完成数量、E2E分位数与采集器CPU |
| environment.json / dependencies.txt | 架构、内核、源码摘要、实际镜像与软件包版本；源码checkout入口，不声称安装wheel验证 |
| 每场景config.json / input-inventory.json | 固定配置和独立预先计划的输入清单，不能从成功输出反推输入数 |
| graph/graph-query.json | 单次完整双向端点/QoS快照及原始查询字节，不是持续拓扑覆盖 |
| simulator-events.jsonl / 各节点stages.jsonl | 原始输入/终态与逐帧边界；实际PID/starttime、boot、PID namespace、domain、Linux单调时钟 |
| summary.json | 原始事件独立复算的E2E P50/P95/P99/max、各跳数据年龄、算法wall/线程CPU、callback至发布返回的wall/CPU、publish调用、释放迟到和目标误差 |
| monitor/ | 现有采集器CPU、RSS、缺页/切换、cgroup与采集成本/覆盖；node_resource_refs关联同一资源，不均分或重复计算 |
| import/ | 现有M3a严格导入、完整窗口吞吐、结果分类与身份/覆盖核查；输入/终态数不符即失败 |

E2E起点为模拟传感输入事件、ROS发布之前；终点为控制指令返回simulator、运动学状态更新完成。它包含节点计算、编码、通信与调度，不含传感器生成/真实曝光，也不证明物理执行成功。每跳数据年龄从编码/发布之前到下一节点callback进入，不能叫纯DDS或网络延迟。算法CPU只包围算法计算；callback测量在节点阶段日志之前结束。同步测试日志与温度哨兵会影响整个负载/采集器开销，尚无稳定输入下的无采集对照。

分位数只包含唯一关联的有效完成样本，用nearest-rank；吞吐分母为整个资源监控窗口（默认14秒，包含观测首尾余量），不是10秒输入区间。原始计划、事件、所有阶段和严格导入均要求完整一致；任何缺项保留失败，而不是删掉未完成样本后报告成功。默认目标误差下降只验证闭环反馈；不设置机器人任务deadline、精度或业务达标阈值。

## 用于Orin/Thor与后续适配

在Orin/Thor的原生ARM64 Docker上运行相同入口，保留同一镜像digest、配置和原始文件，就能做这组参考软件负载的设备基线。Docker Desktop结果属于Linux VM，仅验证可用性；不能外推为Orin/Thor性能。当前实际验证矩阵见[验证记录](../VALIDATION.md)。

后续按独立场景接入[Nav2](https://api.nav2.org/nav2-humble/html/index.html)和[MoveIt 2](https://moveit.picknik.ai/humble/index.html)，固定地图、机器人模型、插件、SDK版本及action边界；不要用本场景标签冒充官方算法。真实相机/激光雷达、深度学习、Gazebo物理、机械臂控制器、真实CTF和其他中间件另行扩展。跨机时钟、GPU推理与生产业务验收仍遵循原项目未完成状态。

业务接入可替换节点计算或拓扑，但要同时更改接口契约、来源摘要、输入清单、真正终态定义和预期端点；不能只按同名节点自动宣称业务身份已确认。优先用这组已知拓扑验证采集管线，然后再接整机业务。
