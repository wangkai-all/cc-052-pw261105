# 本地文件发布台 · Batch Desk

一个用于本地发布数据文件的发布台：文件先进入**暂存目录**，逐份校验后**原子移动**到**正式目录**，并用 SQLite 记录发布进度。服务进程在任意阶段被异常终止后，重启时能根据磁盘实际状态和数据库记录判断走到哪一步，继续未完成的发布；缺失或损坏的文件会被判定失败并保留诊断记录。

## 核心保证

- **正式可见即可下载**：下载接口只允许 `phase='published'` 的批次；`staging/` 与 `published/` 从不作为静态目录挂载，无法通过 URL 枚举或读取未发布文件。每个下载请求都按清单校验大小与 SHA-256。
- **三个阶段显式记录**：
  - `preparing` 准备中：上传写入暂存目录，逐条登记文件大小与散列；
  - `file_ready` 文件就绪：所有声明文件齐全且校验通过，写入阶段快照；
  - `published` 正式可见：文件已全部移入正式目录并再次校验，最终事务已提交。
- **分步提交**：文件移动是同盘 `os.rename`（原子），每成功移动一个文件就更新该行状态；数据库提交在移动全部完成、再次校验之后进行。因此崩溃可发生在「文件写完」「移动完成」「提交前后」等任意位置。
- **每阶段保存文件列表与散列**：`batch_snapshots` 为 `staged / file_ready / moved / published` 各存一份清单与内容摘要；`batch_files` 记录每个文件的行状态（`staged` / `moved`）。
- **恢复幂等**：快照按 `(批次, 尝试, 阶段)` 唯一，诊断按 `(批次, 尝试, 代码, 路径, 详情)` 唯一；重复恢复不会重复登记，也不会重新打开已完成批次。
- **同编号换内容报冲突**：批次编号一旦登记即绑定文件集合与内容摘要；相同编号、不同文件集合或不同字节都会返回 `content_conflict`。每个文件首次被接收的散列会写入 `batch_content_witness`，因此即使首次尝试在封存前就失败、从未写入内容摘要，换字节重试同样会被拒绝。
- **取消与发布串行判断**：二者都要获取同一把每批次文件锁，再各自用 `BEGIN IMMEDIATE` 事务复核阶段。已正式可见的批次取消会返回 `already_published`，且不会删除任何已发布文件；取消只清理本批次 `batch_files` 中记录的自身文件。

## 目录结构

```
backend/
  app/
    config.py       # 路径配置；校验暂存/正式/数据库同盘
    db.py           # SQLite schema（WAL + synchronous=FULL）
    locks.py        # 每批次 flock 锁
    storage.py      # 路径规范化、散列、原子移动
    manifests.py    # 规范化清单与内容摘要
    service.py      # 发布状态机（核心）
    recovery.py     # 文件物理位置诊断
    crashpoints.py  # 可配置的进程猝死点（测试/演示用）
    main.py         # FastAPI 路由与下载门禁
  run.py            # 启动服务
  tests/            # 单元、崩溃恢复、HTTP、真实进程重启测试
frontend/
  src/              # React 页面（阶段时间线、上传、发布、取消、恢复横幅）
scripts/
  crash_demo.sh     # 在真实服务进程上演示暂停点与重启恢复
```

## 运行

需要 Python 3.11+ 与 Node 20+。

```bash
# 后端
cd backend
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python run.py            # http://127.0.0.1:8000

# 前端（开发时热更新，代理到 8000）
cd frontend
npm ci
npm run dev                        # http://127.0.0.1:5173

# 生产：构建后由 FastAPI 直接托管
npm run build                      # 产物在 frontend/dist，FastAPI 自动挂载 /
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BATCH_DESK_DATA_DIR` | `data/` | 数据根目录 |
| `BATCH_DESK_STAGING_DIR` | `<data>/staging` | 暂存目录 |
| `BATCH_DESK_PUBLISHED_DIR` | `<data>/published` | 正式目录 |
| `BATCH_DESK_DB_PATH` | `<data>/batch_desk.sqlite3` | SQLite 文件 |
| `BATCH_DESK_DIAGNOSTICS_DIR` | `<data>/diagnostics` | 诊断记录（按日期归档 JSON） |
| `BATCH_DESK_TMP_DIR` | `<data>/.tmp` | 上传中间文件；提交前不会出现在暂存目录 |
| `BATCH_DESK_LOCK_DIR` | `<data>/.locks` | 每批次文件锁 |
| `BATCH_DESK_CRASH_POINTS` | 空 | 逗号分隔的崩溃点列表，见下 |
| `BATCH_DESK_MAX_FILES` | `2000` | 单批次文件数上限 |
| `BATCH_DESK_MAX_FILE_BYTES` | 10 GB | 单文件上限 |
| `BATCH_DESK_MAX_BATCH_BYTES` | 20 GB | 单批次总量上限 |
| `BATCH_DESK_VERIFY_DOWNLOADS` | `1` | 下载前是否重新校验散列 |

启动时会校验暂存目录、正式目录、诊断目录、上传临时目录与数据库是否位于**同一文件系统**；不满足则拒绝启动，因为跨盘 `rename` 不是原子的。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查，返回启动时恢复结果 |
| `GET` | `/api/batches` | 批次列表 |
| `POST` | `/api/batches` | `{batch_id, files:[{rel_path,size}]}` 登记；同集合重复登记幂等返回 200 |
| `GET` | `/api/batches/{batch_id}` | 批次详情（阶段、清单、快照、诊断） |
| `POST` | `/api/batches/{batch_id}/files` | multipart：`rel_path` + `file` |
| `POST` | `/api/batches/{batch_id}/seal` | 封存，进入 `file_ready` |
| `POST` | `/api/batches/{batch_id}/publish` | 发布；未封存时记录发布请求并返回 409 `publish_pending` |
| `POST` | `/api/batches/{batch_id}/cancel` | 取消并清理自有文件；已发布返回 409 `already_published` |
| `POST` | `/api/batches/{batch_id}/retry` | 失败后开启新尝试（保留旧尝试审计记录） |
| `POST` | `/api/recover` | 立即执行一次恢复核对 |
| `GET` | `/api/recoveries` | 最近恢复记录 |
| `GET` | `/api/downloads/{batch_id}` | 仅已发布批次；返回文件清单与下载地址 |
| `GET` | `/api/downloads/{batch_id}/files/{path}` | 流式下载单个文件，响应头带 `X-Content-SHA256` |

错误统一返回 `{"error": "<code>", "message": "...", "status": n}`。

## 暂停点与恢复演示

`BATCH_DESK_CRASH_POINTS` 支持以下值，命中时进程直接 `os._exit`（模拟被杀，不执行任何清理）：

| 崩溃点 | 位置 |
| --- | --- |
| `after_files_written` | 所有声明文件写完后 |
| `after_seal` | `file_ready` 快照提交后 |
| `after_move` | 所有文件移入正式目录后、提交前 |
| `before_commit` | 最终事务提交前一刻 |
| `after_commit` | 最终事务提交后 |

```bash
./scripts/crash_demo.sh before_commit
```

脚本会：启动带崩溃点的服务 → 登记/上传/封存/发布（进程在指定点猝死）→ 用同一数据目录重启服务（启动自动恢复）→ 打印恢复摘要并验证下载内容。可在 `after_move`、`after_seal` 等点重复。

## 恢复语义

1. 服务启动时执行 `recover()`，并记录一条 `recovery_runs`。
2. `preparing`：若暂存目录里出现已声明但尚未登记的文件（上传完成原子改名后、写入 SQLite 前进程被杀），恢复会核对声明大小与该编号的历史散列后补登记（`adopted_unrecorded_upload` 诊断）；无法核对的文件记为诊断并使批次失败，而不是静默忽略。其余情况：若无人请求发布，保持原样等上传；若已请求发布且声明文件未齐，保持等待；若文件齐备则重新封存并继续。
3. `file_ready`：按磁盘证据判断——文件只可能在暂存或正式一侧：
   - 全部已在正式目录：只补提交（`before_commit` / `after_move` 崩溃）；
   - 部分已移动：逐份校验后继续移动，再提交；
   - 尚未移动且无人请求发布：保持 `file_ready`，等待显式发布。
4. 缺失或散列不符：写入 `batch_diagnostics`（同时落盘 JSON），批次标记失败并给出原因；下载被阻止。用户可「重新提供数据」（`retry` 开启新尝试）。
5. 已标记失败的批次再次恢复时保持失败，不会自动复活；`failed_at` 保证重复恢复不覆盖首次原因。

取消优先规则：`published` 永不因取消而被删；取消只在未发布阶段生效。发布路径中，`publish_state` 在第一次移动前改为 `moving`，恢复据此区分「封存后暂停」与「移动中断」。

## 测试

```bash
cd backend && .venv/bin/python -m pytest        # 46 项：生命周期、冲突、崩溃恢复、HTTP、真实 uvicorn 重启
cd frontend && npm test                         # 5 项：页面阶段、下载门禁、错误展示
```

崩溃测试通过子进程在指定点 `os._exit(75)` 后，再启动新进程恢复，并断言：阶段推进正确、快照不重复、损坏文件产生诊断且下载被阻止。真实进程测试会启动 uvicorn、在发布请求中杀死服务、重启并核对下载内容与响应头散列。
