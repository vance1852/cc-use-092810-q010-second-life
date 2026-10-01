# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/battery_retirement/`：退役组件登记、测量台账、证据冻结评估、独立审批、版本与复核、梯次候选批次、带期限容量预留与唯一最终去向；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m battery_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m battery_assurance.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
PYTHONPATH=src python3 -m battery_retirement.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和退役处置/梯次利用流程，不访问外部网络。退役验收覆盖：晚到检测不覆盖已生效结论、独立审批后生效、复核派生新版本、容量预留过期与项目失败安全释放、每个组件最终只有一个去向。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m battery_retirement.api --database battery-retirement.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 退役评估与梯次利用

`battery_retirement` 解决退役处置中"晚到证据覆盖既有结论、组件在多个名单间反复移动"的问题：

- **证据冻结**：每次评估冻结资产配置、容量/内阻测量窗口、质量事件回溯起点和政策版本，连同采信记录与输入 SHA-256 写入不可变快照；窗口外（含晚到）证据只进台账，不改变结论。
- **解释性结论**：确定性策略引擎给出继续服役、降额使用、进入梯次利用、拆解回收或等待补证五种建议，附带逐条解释、证据缺口和剩余价值估算；相同输入永远得到相同输出，可通过 `GET /assessments/{id}/recompute` 复算任一历史结论并检测篡改。
- **独立审批与版本化**：结论提交后不生效，须由非准备人审批；批准新版本自动把旧生效版本置为 `superseded`，驳回留痕。新证据不能改写结论，只能发起复核申请（`/assessments/{id}/reviews`），受理后按新窗口派生新版本。
- **带来源候选批次**：只有生效结论为梯次利用的组件才能入批，批次明细保留来源评估编号；同一组件在任意时刻只能处于一个进行中批次（部分唯一索引强制）。
- **带期限、不重复占用的容量预留**：项目预留有到期时间，部分唯一索引保证同一组件容量不能被两个项目同时占用；过期由 `POST /holds/expire` 清扫，撤回与项目失败安全释放容量与组件占用，所有释放行保留历史。
- **唯一最终去向**：项目落地或人工处置确认后写入 `disposition_confirmations`（组件唯一），去向必须与生效结论一致；`GET /dispositions/register` 供委员会核对每个退役组件恰好一个有效去向，`GET /audit/chain` 提供哈希链审计。
