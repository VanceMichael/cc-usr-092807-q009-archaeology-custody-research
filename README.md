# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

平台内置**遗物与样本谱系**模块（`civicflow.lineage`），面向跨国联合考古队：遗址、探方、墓葬与灰坑、层位、出土事件、遗物、母样与子样、保管地点、修复、跨国交接、研究申请、检测结果与发表许可全链路保留原始关联，论文审核人可以从一句结论反查到墓葬、样本、实验过程、跨国责任和发表许可。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `src/civicflow/lineage/`：遗物与样本谱系（田野记录链、样本账本、跨国交接、研究版本、反查与持久定时任务）。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

运行遗物与样本谱系演示（奥佐德墓地牙齿样本：登记、分装、跨国交接、测年、发表与反查）：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-lineage.sqlite3 --now 2026-10-01T08:00:00+08:00 lineage-demo
```

执行到期的谱系定时任务（逾期返还检查、保存条件检查；任务持久化在 SQLite 中，服务重启后不会丢失）：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-lineage.sqlite3 run-lineage-jobs
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

## 谱系模块要点

- **田野记录不可覆盖**：遗址到遗物的记录链登记后不可修改，只能通过 `correct` 追加更正；年代或文化判断作为引用具体证据的研究版本（`versions`）追加，可 supersede 旧版本，绝不写回田野记录。
- **数量守恒**：分装、预留、消耗、返还都写入 `sample_movements` 流水并在同一事务内核对父子关系与可用数量；研究申请批准即预留，同一母样不会被两家机构重复消耗。
- **跨国交接**：交接单固化包装编号与清单重量摘要；重复扫描只确认原交接，编号或重量冲突立即隔离交接单并冻结所涉样本；返还逐项核对，逾期由持久定时任务标记并通知。
- **敏感资料授权**：人骨等敏感遗物按项目、用途和资料类别授权（`grants`），未授权者看到的敏感字段被裁剪；申请人不得批准自己的取样。
- **发表许可**：研究版本公开发表必须持有覆盖该版本的许可（`permits`）。
