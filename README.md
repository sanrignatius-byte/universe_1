# 宇宙万物观测站 · Cosmos Observatory

一个单机系统：持续接收发生器推送的海量、陌生、不断演化的对象，**先可靠落盘再确认**，去重、拒收坏数据，并通过网页把这个世界组织成可以浏览、搜索、对账的目录、星图和演化时间线。

- 后端：Python 3.11 · FastAPI/uvicorn(uvloop) · orjson · SQLite（WAL，`synchronous=FULL`，单写线程组提交）
- 前端：原生 JS + ECharts（已随仓库内置，离线可用），由后端直接提供
- 部署：一个容器 + 一个命名数据卷；`docker compose up` 即可

---

## 1. 快速开始（验收环境：Mac Docker Desktop，linux/arm64）

```sh
git clone https://github.com/sanrignatius-byte/universe_1.git && cd universe_1
docker compose up -d --build          # 服务监听 0.0.0.0:8000，数据在命名卷 cosmos-data
open http://localhost:8000            # 网页界面
```

按出题方《发生器操作说明》启动发生器，`target_url` 填 **`http://host.docker.internal:8000/ingest`**（compose 已把 8000 端口发布到宿主机）。

一轮结束后对账（两种方式任选）：

```sh
curl -fsS http://127.0.0.1:9001/receipt -o run-001-receipt.json
curl -sS -X POST http://127.0.0.1:8000/api/reconcile --data-binary @run-001-receipt.json
# 或：网页「数据可信」页上传 receipt 文件
```

自动化脚本（需要时会 `docker load` 并启动发生器；跑一轮、取 receipt、对账、采样资源占用，结果写入 `results/`）：

```sh
GEN_TAR=/path/to/flightgen-image.tar scripts/run-test.sh run-001 200 120       # 题面示例参数
scripts/run-test.sh perf-001 0 60 20 4 steady                                 # 尽力发送（rate=0）
scripts/crash-test.sh crash-001 2000 60 15 2                                  # 写入中途 kill -9 两次后对账
```

> 在 Linux Docker Engine 上，脚本会给发生器加 `--add-host=host.docker.internal:host-gateway`；Docker Desktop 自带该域名。
> `SQLITE_SYNC=NORMAL docker compose up -d` 可切换为更快、但不防断电的落盘级别（见 §6）。

资源：compose 为服务设置 `cpus: 4`、`mem_limit: 4g`，与题目预算一致；数据只写命名卷。

---

## 2. 我对题目的理解（第一性原理）

题面最硬的一句是接入约定：**“返回 2xx 表示已可靠保存整条消息”**。展开成系统必须守住的不变量：

1. **确认 ⇒ 已持久化**：只有事务提交（WAL fsync）之后才回 2xx。
2. **至少一次投递 ⇒ 幂等存储**：发生器会超时重试，还会故意重发（2%），同一消息多次到达只能存一次 —— 效果上“恰好一次”。
3. **整条消息原子**：要么整条入库，要么整条拒收；坏消息不能“存一半”，也不能被谎报成功。
4. **因果顺序靠确认保证**：“部分发送依赖前序确认” —— 对象批次引用的类型树修订，一定在该修订被确认后才发出。只要坚持第 1 条，顺序自然成立。

性能和界面都建立在这四条之上。发生器最后给出的 receipt 是出题方的对账单，所以系统内置了**逐批对账**，用来证明“数据可信”。

---

## 3. 数据契约（通过探针实测得出，题面未提供）

先用一个只记录原文、一律回 200 的探针（`tools/probe_receiver.py`）跑了一轮，再解码 receipt，得到：

| 项 | 实测结论 |
|---|---|
| 传输 | `POST /ingest`，JSON；头部 `X-Flightgen-Kind / Test-Id / Batch-Id / Tree-Revision` 与正文一致 |
| 对象批次 `kind=objects` | `{test_id, batch_id, tree_revision, objects:[...]}`；对象 `{id, sequence, type_id, name, timestamp_ms, position, attributes}`，`id = test_id-sequence` |
| `position` | `null`（非空间对象，如文档、服务）或 `{frame_kind: galactic(ly) / stellar_system(km) / local_surface(m), frame_id, unit, x, y, z}` |
| `attributes` | 每个类型的字段集合不断演化（新类型、新字段如 `spectral_feature_11`），含嵌套对象/数组，单对象可达约 8 KB |
| 类型树 `kind=types` | 每 10 批一个修订 `{revision, changes:[{op, node:{type_id, parent_id, name}}]}`；op 有 `create / rename / move`；最多 4096 个类型、深度 5；根为“万物” |
| 重复 | 同一 `batch_id` 的完全相同正文再发一次（`duplicate_*_bitmap`） |
| 故障 | **单独发送**，`batch_id = max_batches + 原批次号`；内容如缺 `type_id`（不完整）、`attributes` 为数组、`id` 与 `sequence` 不符（非法）等。receipt 有 `fault_rejected_bitmap` —— 期望接收端**拒收** |
| receipt | `sent_bitmap / ack_bitmap`（lsb0 位图，按 batch_id 编号）+ 注入位图 + 统计；发生器超时 3 s、重试 2 次 |

---

## 4. 架构

```
 generator ──POST /ingest──▶ ┌──────────────────── observatory（1 个容器）────────────────────┐
  (1 vCPU)                   │ uvicorn/uvloop 事件循环                                          │
                             │  ① orjson 解析 + 结构校验 ── 不合格 → 记入隔离区 → 400/422        │
                             │  ② 放入有界队列（满则请求排队等待 ≤2 s，再满回 503 —— 背压）      │
                             │                        │                                         │
                             │                        ▼                                         │
                             │  单写线程：把队列里攒下的消息合成一个事务（组提交）                │
                             │   · 批次/修订去重（主键）   · 类型引用与树操作校验                  │
                             │   · 写对象、批次、计数、字段登记、树变更   · COMMIT（WAL fsync）     │
                             │                        │                                         │
                             │  ③ 提交成功后才唤醒等待中的请求 → 200 {"status":"stored|duplicate"} │
                             │                                                                  │
                             │  读 API（线程池 + 只读连接；WAL 下读写互不阻塞）◀── 网页 / 对账    │
                             └────────────────────── SQLite @ 命名卷 /data ────────────────────┘
```

**存储模型**（`app/store.py`）：

- `objects`：核心列 + `attrs` JSON；索引 `(test, seq)`、`(test, type, seq)`
- `batches`：去重，同时充当摄入时间序列
- `types` / `tree_revisions` / `tree_changes`：当前类型树 + 完整变更史（含旧名、旧父节点）
- `type_counts` / `test_stats`：与数据**同事务**更新的计数，总览不扫表
- `fields`：每个类型的每个字段第一次出现的对象 / 批次 / 时间
- `rejects`：隔离区，原因 + 原文片段

每个发生器轮次（`test_id`）是一个独立的“世界”，网页右上角切换。

---

## 5. 正确性与可靠性设计

| 风险 | 做法 |
|---|---|
| 确认了却没存下 | 请求 `await` 写线程 **COMMIT 之后**才返回；`synchronous=FULL` 下 COMMIT 即 WAL 已 fsync |
| 重试 / 故意重复 | `(test, batch_id)`、`(test, revision)` 为主键，事务内先查后写；重复回 200，不再写入，计数 |
| 坏消息 | 请求线程做结构校验（类型、必填、`id = test_id-sequence`、批内序号不重复、头部与正文一致）；写线程再校验 `type_id` 是否存在于当前类型树、树操作是否合法（父节点存在、不成环、不重复创建）。任一不过 → 整条拒收（无法解析 400 / 内容非法 422），写入隔离区 |
| 部分写入 | 每条消息在同一事务内完成；组提交出错则回滚并逐条重试，只有失败的那条回 500（发生器会重试） |
| 进程崩溃 | WAL 保证已提交事务完整；内存状态（类型树、字段登记）启动时从库重建；`restart: unless-stopped` |
| 优雅停止 | uvicorn 先停止接收并等待在途请求；`lifespan` 关闭时排空写队列再关库 |
| 过载 / 内存 | 有界队列 + 等待式背压；页缓存 256 MiB、读连接 16 MiB/线程；对象正文不常驻内存 |
| 证明数据可信 | `POST /api/reconcile`：按位图逐批核对“确认但缺失 / 存了但从未发送 / 故障误收 / 对象行数 / 树修订”，网页可直接上传 receipt |

---

## 6. 关键取舍

- **SQLite 单文件 + 单写者，而不是 Kafka / ClickHouse / Postgres。** 在 4 vCPU / 4 GiB / 120 分钟的约束下，组件越少越可靠；组提交下单线程即可每秒写入上万行，瓶颈先出现在 fsync 延迟和发生器并发上。代价：写入无法横向扩展。
- **先提交后确认，用组提交摊薄 fsync。** 不做“先回 200 再异步写”——吞吐更高，但违反 2xx 的含义。
- **默认 `synchronous=FULL`。** `NORMAL` 在 WAL 模式下同样扛得住进程/容器崩溃，延迟更低，但宿主机断电可能丢最后几次提交；需要时用 `SQLITE_SYNC=NORMAL` 显式开启（对比数据见 §7）。
- **固定核心列 + JSON 属性。** 世界在演化，不为每个新字段改表；另用 `fields` 表记录“何时出现了什么新字段”，把“变化”本身变成可浏览的数据。
- **严格校验、整条拒收。** 规则全部来自对正常流量的实测；对账显示正常批次 0 误杀，故障消息 100% 拒收。
- **Python 而不是 Go/Rust。** 开发速度和可解释性优先；单进程受 GIL 限制，CPU 上限约 1.5 核（见 §8）。

---

## 7. 实测结果

测试机：Windows 10 + WSL2 Ubuntu 24.04 中的 Docker Engine 29（x86_64）。**发生器是 arm64 镜像，在本机经 QEMU 模拟运行**，比 Mac 原生慢，所以以下吞吐是保守下限。服务限制 `cpus=4, mem=4g`，数据在命名卷上。

RESULTS_PLACEHOLDER

所有轮次的 receipt、对账结果、资源采样都在 `results/` 目录。

---

## 8. 已知限制

LIMITS_PLACEHOLDER

---

## 9. 目录

```
app/main.py         HTTP 接入、读 API、对账
app/store.py        SQLite 模式、单写线程组提交、去重、类型树与字段登记
app/validate.py     结构校验规则
app/static/         网页（index.html / app.js / style.css / vendor/echarts）
scripts/            gen.sh 发生器控制 · run-test.sh 一轮测试+对账 · crash-test.sh 崩溃测试
                    perf-sync-compare.sh 落盘级别对比 · smoke.sh 接口冒烟
tools/              探索阶段的探针接收器
results/            实测 receipt / 对账 / 资源采样
```
