# 自然史标本与实验协作服务

本项目是一套可离线运行的 Python 后台，用于自然史馆、学校实验室和野外调查团队协同管理昆虫、植物及其他生物标本。系统把保藏与转运、分类实验复核、生物安全处置三个业务子域保存在 SQLite 中，提供角色权限、幂等请求、事务状态、版本化记录和可追溯审计。

## 目录

- `src/collection_logistics/`：馆藏环境指标、库房与转运路线、保藏资源、调拨任务和调整情景；
- `src/taxonomy_lab/`：采集设备、实验协议、观察记录导入、异常排除、分析租约和鉴定决定；
- `src/biosafety_ops/`：库区记录、有害生物监测、风险告警、处置工单和资源分配；
- `fixtures/`：离线验收使用的实验协议与结构化观察记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
```

验收会建立临时 SQLite 数据库，登记馆藏环境指标、保藏库房、转运路线和材料批次，完成实验观察导入、异常复核、生物安全告警与资源分配，并输出 JSON 结果。命令不访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m collection_logistics.api --database collection.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database taxonomy.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database biosafety.sqlite3 --host 127.0.0.1 --port 8082
```

三个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可以继续查询与复核。

## 转运响应时长语义

- 转运路线的响应时长统一以**分钟**登记：字段 `response_minutes`，必须是 1..1440（24 小时）的整数；零、负数、浮点、布尔和超出范围的值在写入前一律拒绝。
- 兼容旧记录的登记接口接受 `response_time` + `response_time_unit`（`minute` 或 `hour`），入库时归一化为分钟。`response_time_unit` 为 `unknown`（或无法识别单位）时，该路线标记为 `duration_ambiguous`，**禁止提交调度申请和计算预计到达时间**，需馆员调用 `POST /road_corridors/{id}/clarify_duration` 明确分钟数后才恢复可用；系统不会在分钟与小时之间猜测。
- 预计到达时间从带时区的出发时刻在 UTC 时间线上加 `response_minutes` 分钟得到，并同时给出接收设施时区的本地时刻，因此跨日与夏令时切换（春令时拨表等）不会造成一小时的偏差。
- `GET /road_corridors/{id}` 与 `GET /deployments/{id}` 使用同一语义展示分钟数、本地预计到达时刻和 `overdue` 超时判断；接收端可用 `POST /deployments/{id}/arrival` 登记实际到达时刻。
- 打开旧版 SQLite 数据库时自动迁移：既有分钟值保持分钟语义；超出合理范围的旧值原值保留在 `legacy_response_time` 并标记 `ambiguous`；历史部署的预计到达时间仅在单位明确时按分钟回填。

