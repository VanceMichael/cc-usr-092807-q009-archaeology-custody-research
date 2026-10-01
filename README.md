# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
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

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

## 遗物与样本谱系（中乌联合考古）

平台内置考古遗物/样本谱系能力（`src/civicflow/` 下的 `archaeology.py`、`samples.py`、`custody.py`、`research.py`、`monitoring.py`、`provenance.py`），保留从田野到发表的完整证据链：

- **田野谱系**：遗址 → 探方 → 墓葬/灰坑（遗迹）→ 层位 → 出土事件 → 遗物，逐级核对归属；田野照片与田野记录在出土事件上固化，保管地点与修复单独留痕。
- **样本守恒**：母样/子样以不可变 `sample_lineage_events` 流水记录分装、取样、消耗、退库、销毁和保管转移；分装总量不得超过余量，父子数量必须相等，`verify` 做全库守恒巡检。
- **跨国交接**：交接单固化申报编号、重量与海关凭证；重复扫描只确认原交接；包装编号、样本编号或重量冲突立即写入隔离单并冻结样本与交接单；同一样本不得同时存在于两份在途交接单。
- **研究治理**：人骨等敏感资料按项目+用途授权；取样申请必须命中授权，申请人不能批准自己的申请；消耗不得超过批准计划；年代/文化判断只能以**研究版本追加**并引用具体样本、检测结果与出土事件，田野记录永不被覆盖。
- **持久化监控**：逾期返还与保存条件巡检使用落库的 `scheduled_jobs`，服务重启后不丢失；巡检按周期自动续排，异常写入 `monitoring_findings`。
- **结论反查**：`ProvenanceService.trace_version/trace_claim` 可从一句结论反查到墓葬、样本谱系、检测实验与审批人、跨国交接责任链和发表许可。

运行奥佐德墓地牙齿样本端到端演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-arch.sqlite3 --now 2026-09-28T12:00:00+08:00 demo-arch
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-arch.sqlite3 verify
```
