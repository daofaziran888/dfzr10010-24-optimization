# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 | 任务编号、目标表名、文件范围 |
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务 | 任务编号、任务类型、调度表达式 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 数据迁移的分段校验

数据迁移的大表分段重跑以 `app/services/migration_validation.py` 为唯一口径：

- 源目标一致性校验结果按「迁移任务 + 批次 + 分段明细」做成版本，重复提交不会重复落库；
  同一份结果同时供迁移详情、迁移核对清单和表统计信息消费。
- 异常段不会清空其它已校验段，已校验段始终形成明确边界；迁移完成状态不能越过任何异常段，
  有异常段时「归档任务」会被拒绝。
- 两种异常段处理口径：`停在异常段之前`（边界取完整前缀）与 `隔离异常段续跑`
  （边界推进到独立已校验段，失败段仍留在异常段列表里）。
- 每次动作都从 `下一次续跑位置` 继续，不会从头扫描已校验段。
- 上线前检查 `GET /api/migrate/{id}/preflight` 校验源库/目标环境依赖、回退快照以及
  本地配置与线上结构是否漂移。

示例任务 `MIGR-0004`（payments_2026Q3，32 段 × 25 万行，校验 payment_id 唯一数与
amount 汇总，目标分区 event_date=2026-07-01..2026-09-30）带有第 9、10 段异常，
可直接在「数据迁移管理 → 核对详情」里查看边界与续跑位置。

主要接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/migrate/{id}/validations` | 校验版本列表（含当前生效版本） |
| `POST /api/migrate/{id}/validations` | 提交校验结果，同指纹只保留一个版本 |
| `POST /api/migrate/{id}/validation-strategy` | 切换异常段处理口径 |
| `GET /api/migrate/{id}/checklist` | 迁移核对清单（已迁移范围 / 异常段 / 续跑位置） |
| `GET /api/migrate/{id}/preflight` | 上线前依赖与快照检查 |
| `GET /api/stats/{id}/validation` | 统计报告读取同一份校验结果（可指定旧版本核对旧批次） |
| `POST /api/stats/{id}/collect` | 按批次记录行数与异常段，结论回写核对清单 |

领域：数据工程
子方向：数仓与数据湖
