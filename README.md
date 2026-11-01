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
GEN_TAR=/path/to/flightgen-image.tar bash scripts/run-test.sh run-001 200 120  # 题面示例参数
bash scripts/run-test.sh perf-001 0 60 20 4 steady                            # 尽力发送（rate=0）
bash scripts/crash-test.sh crash-001 2000 40 12 1                             # 写入中途 kill -9 后对账
bash scripts/full-suite.sh                                                    # §7 全部测量（会清空数据卷）
docker run --rm -i cosmos-observatory python -I - /srv < tests/regression.py  # 确认语义回归测试（11 例）
```

`run-test.sh` 会核对 receipt 的 `test_id` 是否属于本轮；对账发现完整性问题时以非零码退出。发送目标可以用 `TARGET_URL` 覆盖。

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

- `objects`：只放核心列（约 150 B/行）；索引 `(test, seq)`、`(test, type, seq)`
- `object_attrs`：属性 JSON（约 1.5 KB/行），整数主键 `test<<40 | sequence`。和核心列分开存放，这样搜索、列表、星图扫描的是窄表（理由和前后对比见 §7）
- `batches`：去重，同时充当摄入时间序列
- `types` / `tree_revisions` / `tree_changes`：当前类型树 + 完整变更史（含旧名、旧父节点）
- `type_counts` / `test_stats`：与数据**同事务**更新的计数，总览不扫表
- `fields`：每个类型的每个字段第一次出现的对象 / 批次 / 时间
- `rejects`：隔离区，原因 + 原文片段

每个发生器轮次（`test_id`）是一个独立的“世界”，网页右上角切换。

**界面**（`http://localhost:8000`，每 2 秒自动刷新）：

| 页 | 看什么 |
|---|---|
| 总览 | 已落盘对象、实时摄入速率曲线（按提交时间）、一级类别构成、写引擎状态（队列深度、组提交、确认延迟 p50/p99、HTTP 结果分布、落盘级别） |
| 万物目录 | 左边是带计数的类型树（可过滤；近期新出现的类型标“新”），右边是对象列表（按类型子树过滤、按名称或 ID 搜索、键集分页），点开看完整属性、类型路径、坐标和来源批次 |
| 星图 | 按参考系（类型 · ID · 单位）分别绘制对象位置，Canvas 画 2 万个点，颜色表示一级类别，点击光点看详情 |
| 演化 | 每个类型树修订的变更数、重命名/移动记录（新旧名称、新旧父节点）、新字段按首次出现时间倒序 |
| 数据可信 | 上传 receipt 一键对账（数据完整 / 演练完整两项结论）、质量计数、隔离区（被拒消息的原因和原文片段） |

---

## 5. 正确性与可靠性设计

| 风险 | 做法 |
|---|---|
| 确认了却没存下 | 请求 `await` 写线程 **COMMIT 之后**才返回；`synchronous=FULL` 下 COMMIT 即 WAL 已 fsync |
| 重试 / 故意重复 | `(test, batch_id)`、`(test, revision)` 为主键，并保存消息**规范化内容摘要**（键排序后的 JSON 做 BLAKE2b）。标识和摘要都相同才算重复，回 200、不再写入、计数；标识相同但内容不同 → 409，记入隔离区 |
| 跨批次对象冲突 | 对象主键 `(test, sequence)`。新批次里出现已存对象：内容完全相同 → 只保留一份（计入 `id_conflicts`）；内容不同 → 整条消息回滚并回 409。计数、坐标系、字段发现只基于实际存下的对象 |
| 坏消息 | 请求线程做结构校验（类型、必填、`id = test_id-sequence`、批内序号不重复、头部与正文一致）；写线程再校验 `type_id` 是否存在于当前类型树、树操作是否合法（父节点存在、不成环、不重复创建）。任一不过 → 整条拒收（无法解析 400 / 内容非法 422），写入隔离区 |
| 乱序的树修订 | 每个节点记录最后改动它的修订号；晚到的旧修订不会覆盖新状态（变更照样记录，标为 `applied=0`）。对账检查已确认范围内的修订有没有缺口 |
| 部分写入 | 组事务里每条消息有自己的 `SAVEPOINT`：一条被拒只回滚它自己。组提交出错则整体回滚并逐条重试，只有失败的那条回 500（发生器会重试） |
| 磁盘写满 | 每秒检查数据库 + WAL 的大小和卷剩余空间；到达水位后新消息回 507（不确认），不会出现先确认、后写失败 |
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
- **Python 而不是 Go/Rust。** 开发速度和可解释性优先；单进程受 GIL 限制，实测 CPU 只用到约 1–1.4 核（见 §8）。
- **不自研 WAL/日志存储。** 自研要处理截断写、校验、恢复、去重、索引重建和容量控制；SQLite 已经提供这些，而且能被逐批对账验证。

---

## 7. 实测结果

测试机：Windows 10 + WSL2 Ubuntu 24.04 中的 Docker Engine 29（x86_64）。**发生器是 arm64 镜像，在本机经 QEMU 模拟运行**，比 Mac 原生慢，所以以下吞吐是保守下限。服务限制 `cpus=4, mem=4g`，数据在命名卷上。

最终一套测量（`bash scripts/full-suite.sh`，最终代码，清空数据卷后依次运行，2026-11-01 10:22–10:27）：

| 轮次 | 参数 | 吞吐 / 延迟 | 数据结果 | 对账 |
|---|---|---|---|---|
| `final-example` | 题面示例：200 对象/s，bursty，batch 20，4 workers，120 s | 186 对象/s（目标速率）；发生器侧平均 6.7 ms、最大 46 ms；服务侧确认 p50 3.2 ms / p99 25 ms | 1,116 批、22,320 对象全部确认并落盘；18 条故障全拒；28 个重复批吸收；112 个树修订 | 数据完整 ✓ 演练完整 ✓ |
| `final-max-b20` | rate 0（尽力），batch 20，4 workers，60 s | **4,330 对象/s**；发生器侧平均 13.2 ms、最大 70 ms；p50 9.6 / p99 33.5 ms | 12,990 批、259,800 对象；240 故障全拒；266 重复吸收 | ✓ ✓ |
| `final-max-b100` | rate 0，batch 100，16 workers，60 s | **8,601 对象/s**；发生器侧平均 70.6 ms、最大 493 ms；p50 54 / p99 150 ms | 确认 516,100 对象；另有 9 批已落盘但停止时确认未送达（允许）；83 故障全拒；104 重复吸收 | ✓ ✓ |
| `final-crash` | 2,000 对象/s，第 12 s 对服务进程 `kill -9` | 服务约 2 s 后由重启策略拉起 | 发生器确认的 514 批全部在库，0 丢失、0 多余；发生器在宕机期间耗尽树修订重试，以 `tree_error` 结束本轮（见 §8.3） | 数据完整 ✓ 演练未完整（预期） |
| `final-bench` | 原生回放压测（`tools/bench.py`，不经发生器），16 进程，batch 100，30 s | **15,674 对象/s**，p50 93 ms / p99 210 ms | 4,718 批全部确认；1 批被 422 拒收 —— 是压测工具本身的占位符 bug 把一个对象 ID 改坏了（已修），服务的校验正确拦下了它 | — |

补充测量：

- **落盘级别对比**（同为 batch 100 / 16 workers / 60 s，测于上一版提交 `ef13f78`）：`FULL` 8,682 对象/s，`NORMAL` 11,860 对象/s（+37%），两者对账都完全一致。
- **资源**：整套测试中服务内存的最高采样值为 433 MiB（上限 4 GiB），CPU 最高采样值约 105%（单进程 GIL，见 §8.1）。发生器 CPU 约 45–50%，没有到它的 1 vCPU 上限。
- **存储密度**：套件结束时库里约 128 万个对象，数据库 + WAL 共 2.04 GB，约 1.6 KB/对象（含索引）。按 17 GiB 水位算，约可容纳 1,100 万个对象。
- **回归测试**：`tests/regression.py` 11/11 通过（相同重试幂等、键顺序不影响去重、同批次号不同内容 409、跨批次对象冲突整批回滚且不污染计数/字段、相同对象跨批只存一份、未知类型 422、旧修订不覆盖新状态、修订冲突 409、成环 422、同组中坏消息不影响邻居、重启后内存状态重建）。
- **全新 clone 复现**：`scripts/fresh-clone-check.sh` 从 GitHub 克隆 `2302a6f` 到空目录，`docker compose up --build`；回归测试 11/11 通过，一轮发生器（300 对象/s、30 s）对账数据完整 ✓ 演练完整 ✓。
- **arm64**：`scripts/arm64-check.sh` 在 QEMU 下构建 linux/arm64 镜像，确认 `aarch64`、sqlite 3.46.1，以及 orjson、uvloop、httptools、fastapi 都能导入。
- **探索阶段**：最早的 `e2e-001`、`perf-001..003`、`crash-001` 的记录也保留在 `results/` 中。它们出自更早的代码版本，仅供参考。

所有轮次的 receipt、对账结果、资源采样都在 `results/` 目录。

---

## 8. 已知限制

1. **CPU 上限来自单个 Python 进程。** 剖析（`tools/profile_store.py`，每批 100 个对象）：解析约 1.05 ms、校验约 0.16 ms、写入约 2.1 ms、提交约 2.0 ms（FULL）。HTTP 处理、内容摘要和写线程共用一把 GIL，所以服务实测在每秒 1.4 万个对象左右封顶，4 vCPU 没有用满。下一步：把解析和校验放到多进程，只保留一个写者。
2. **单写者、单文件。** 写入不能横向扩展，也没有副本或高可用。
3. **崩溃恢复期间发生器可能放弃整轮。** `kill -9` 后约 2 秒恢复服务。在这段时间里，发生器的 2 次快速重试可能用完；而树修订是后续批次的前提，所以发生器会以 `tree_error` 结束本轮（发生器的设计）。已确认的数据不受影响，对账为 `integrity_ok=true`、`exercise_complete=false`。
4. **校验规则来自实测流量。** 没见过的故障形态可能通过结构校验（比如语义上不合理但类型正确的数值）。对账里的“故障误收”会把它暴露出来。
5. **按名称搜索是 `LIKE` 扫描**（有返回条数上限），数据到千万级后，冷门关键词会变慢。没有用 FTS5，因为它会拖慢写入。
6. **星图一次只画一个参考系**，只扫描最新 30 万个对象、最多画 2 万个点。数据里没有局部坐标系之间的变换关系，所以不同参考系不合并。
7. **磁盘预算靠应用自己守。** 数据库加 WAL 超过 17 GiB，或卷剩余不足 1 GiB 时，新消息回 507（不确认，所以不会谎报保存），阈值可用 `MAX_DB_GIB`、`MIN_FREE_GIB` 调整。Docker Desktop 的命名卷本身没有配额；容器日志限制在 3×10 MB。
8. **性能没有在 Mac 原生 arm64 上测过。** arm64 镜像已经在 QEMU 下构建并验证依赖可以导入（`scripts/arm64-check.sh`），但吞吐数字全部来自 x86 主机 + 模拟运行的发生器。
9. **部分监控指标只在内存里**（写线程的组提交次数、确认延迟分位数），重启后清零；业务计数在库里，与数据同事务提交。
10. **没有鉴权。** 端口发布在 `0.0.0.0:8000`（容器内的发生器经 `host.docker.internal` 访问需要它），同一网络内的任何人都能访问。

---

## 9. 目录

```
app/main.py         HTTP 接入、读 API、对账
app/store.py        SQLite 模式、单写线程组提交、去重、类型树与字段登记
app/validate.py     结构校验规则
app/static/         网页（index.html / app.js / style.css / vendor/echarts）
scripts/            gen.sh 发生器控制 · run-test.sh 一轮测试+对账 · crash-test.sh 崩溃测试
                    full-suite.sh §7 全套测量 · perf-sync-compare.sh 落盘级别对比 · smoke.sh 接口冒烟
                    arm64-check.sh arm64 构建验证 · fresh-clone-check.sh 从 GitHub 全新 clone 复现
tests/regression.py 确认语义回归测试（无需 pytest，在服务镜像里运行）
tools/              probe_receiver.py 探索阶段探针（只用于看格式，不可当接收端） · bench.py 原生回放压测
                    profile_store.py 摄入 CPU 剖析
results/            实测 receipt / 对账 / 资源采样
```
