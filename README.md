# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/battery_retirement/`：退役评估版本冻结、可解释退役结论、独立审批、复核与梯次利用候选批次、容量预留；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
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
PYTHONPATH=src python3 -m battery_retirement.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、退役评估与梯次利用和组件质量流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m battery_retirement.api --database battery-retirement.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 退役评估与梯次利用

`battery_retirement` 针对「晚到检测结果覆盖已完成处置结论、组件在回收/翻新/梯次名单间反复移动」的问题，提供版本化、可解释、可复算的处置管理：

- **依据冻结**：每个评估版本（`assessments`）冻结当时的资产配置版本、政策版本、测量窗口 `[start, cutoff]`，并将窗口内接纳的容量保持率、内阻增幅记录与维修/安全事件连同输入摘要 `input_sha256` 一并固化。检测与事件一经入库即不可变；入库晚于截止点的记录进入 `late_evidence`，只被隔离、不被删除，也不会改动既有结论。
- **解释性结论**：纯函数引擎 `engine.evaluate` 依据容量、内阻和未闭环质量事件的分级，给出 `continue_service`（继续服役）、`derating`（降额使用）、`cascade`（进入梯次利用）、`recycle`（拆解回收）或 `pending_evidence`（等待补证），并返回每条信号的取值、分级区间、证据缺口和剩余价值。
- **独立审批生效**：评估版本经 `open → submitted → approved/rejected`，提交人不能审批自己的版本；未批准不形成有效去向，`pending_evidence` 不允许批准生效。新版本生效时旧版本自动置为 `superseded`，部分唯一索引保证每个组件至多一个 `effective=1` 去向。
- **新证据只触发新版本或复核**：晚到证据通过 `reviews` 申请，复核通过后开放一个仍需独立审批的后继版本；容量仍被梯次项目占用时，不允许把去向改为其他类型。
- **来源批次与容量预留**：只有生效去向为 `cascade` 的组件才能进入候选批次，批次逐项记录来源评估，封存时计算内容摘要。容量预留有 `holds_until` 期限，部分唯一索引确保同一组件的容量不被两个项目重复占用；到期、撤回、项目失败会安全释放容量并保留完整历史，项目成功结项则转为终态 `consumed`。
- **委员会 API**：`POST /assessments/{id}/recompute` 用冻结输入复算并核对摘要与结论，`GET .../evidence_gaps`、`GET .../residual_value`、`GET /components/{id}/disposition` 与 `GET /reconciliation` 用于查看缺口、剩余价值并确认每个退役组件最终只有一个有效去向。
