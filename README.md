# 多 Agent 协作 Workflow —— 需求 → 设计 → 代码 → 审查

一个可运行的最小 Workflow 引擎：5 个 Agent 接力，把一句话需求变成可运行的 Python 工程，
审查不通过会自动带意见返工，全程消息可回放、状态可恢复。

```
需求分析 ──► 架构设计 ──┬──► 编码实现 ──┐
                       └──► 测试生成 ──┤
                                       ▼
                                    代码审查 ──FAIL──► 回到编码（最多 2 轮）
                                       │
                                      PASS
                                       ▼
                                    交付汇总
```

- **Agent 协作**：Agent 之间不直接调用，全部通过消息总线通信（`messages.jsonl` 可回放）
- **状态共享**：`state.json` 记录节点状态、产物索引、审查结论，是断点续跑的依据
- **流程控制**：顺序、并行（编码 ∥ 测试）、条件分支（PASS/FAIL）、循环（返工）
- **可扩展**：流程用 `NodeSpec` + `Edge` 声明，加 Agent 不改引擎
- **零依赖**：HTTP 层优先 `requests`，没有就自动降级标准库 `urllib`

详细架构与设计决策见 **[Design.md](./Design.md)**。

## 快速开始

在项目根目录直接启动：

```bash
python run.py
```

终端里先问你要不要用真实 API：

```
  请选择运行方式：
    [1] 调用 DeepSeek API    —— 真实生成，需要 API Key
    [2] 离线演示模式        —— 不联网，使用内置样例

  请输入 1 或 2，直接回车默认 1：
```

- 选 **1**：粘贴 API Key（不回显，只存进程内存），再粘贴需求
- 选 **2**：用内置样例跑通全流程，**会完整演示"审查不过 → 返工 → 通过"**，不联网
- 已配置 Key 时显示 `已检测到 Key sk-xxx...abcd`，直接回车沿用

需求输入结束后，另起一行输入 `:end` 回车开始执行。

跳过询问的用法：

```bash
python run.py -r "做一个命令行待办清单工具"
python run.py --mock -r "随便写点什么"     # 强制离线
python run.py -f 需求.txt                  # 从文件读
python run.py --show-graph                 # 只看流程图
```

### 不同入口的行为

| 启动方式 | 是否询问模式 | API Key 来源 |
| --- | --- | --- |
| `python run.py`（终端） | 询问 | 询问时输入，或回车沿用已配置 |
| `python run.py -r "..."`（终端） | 询问 | 同上 |
| 管道 / 重定向输入 | 不询问 | 环境变量或 `.env`，都没有则自动离线 |
| `--mock` | 不询问 | 不使用 |
| `--live` | 不询问 | 环境变量或 `.env`，缺失直接报错 |

API Key 只保存在进程内存，不写入任何文件；终端与日志里一律脱敏为 `sk-abc...wxyz`。

依赖说明：装了 `requests` 就用它，没装自动用标准库 `urllib`，行为一致。
因此**不装任何依赖也能直接运行**。

```bash
pip install -r requirements.txt   # 可选
```

## Workflow 长什么样

```bash
python run.py --show-graph
```

```
[requirement]        需求分析 Agent
    --规格说明书-->  design
[design]             架构设计 Agent
    --设计与接口-->  group:generate
[code] (并行组)       编码实现 Agent
    --代码 + 测试--> review
[test] (并行组)       测试生成 Agent
    --代码 + 测试--> review
[review]             代码审查 Agent
    --PASS-->              finalize
    --FAIL(轮次用尽)-->    finalize
    --FAIL-->              code    ← 返工循环
[finalize]           交付汇总
```

| 节点 | Agent 职责 | 产物 |
| --- | --- | --- |
| requirement | 把模糊需求拆成可验收的规格说明 | `01_requirement.md` |
| design | 输出架构、数据结构、接口签名、文件清单 | `02_design.md` |
| code | 按设计生成完整代码 | `03_code/**` |
| test | 与编码并行，只依据设计写黑盒测试 | `03_code/tests/**` |
| review | LLM 审查 + 本地静态检查，给 PASS/FAIL | `04_review_rN.md` |
| finalize | 汇总交付说明与遗留问题 | `05_delivery.md` |

四类控制结构：

| 结构 | 体现 |
| --- | --- |
| 顺序 | `requirement → design → code` |
| 并行 | `code` 与 `test` 同属 `generate` 组，线程池并发 |
| 条件分支 | `review` 三条出边按顺序匹配条件函数 |
| 循环 | `review --FAIL--> code`，下游节点失效重跑，最多 2 轮 |

## 一次运行的产物

```
runs/20261024-005048-133a/
├── SUMMARY.md           ← 一页纸摘要，先看这个
├── state.json           ← 共享状态：节点状态/产物索引/审查结论
├── messages.jsonl       ← 全部 Agent 消息，可回放
├── meta.json            ← 机器可读元数据
├── requirement_input.txt
├── 01_requirement.md
├── 02_design.md
├── 03_code/             ← 代码 + tests/
├── 04_review_r1.md      ← 第 1 轮：FAIL
├── 04_review_r2.md      ← 第 2 轮：PASS
├── 05_delivery.md
├── llm/                 ← 每次请求与响应原文
└── run.log
```

看一眼消息时间线：

```bash
python -c "
import json
for line in open('runs/20261024-005048-133a/messages.jsonl', encoding='utf-8'):
    m = json.loads(line)
    print(f\"{m['ts'][11:19]}  {m['from']:>11} -> {m['to']:<9} [{m['kind']}] {m['summary']}\")
"
```

```
00:50:48       engine -> *         [status] workflow mini-se started
00:50:48  requirement -> *         [artifact] 需求分析 Agent 完成 -> 01_requirement.md
00:50:48       design -> *         [artifact] 架构设计 Agent 完成 -> 02_design.md
00:50:48         test -> *         [artifact] 测试生成 Agent 完成 -> 03_code/（1 个文件）
00:50:48         code -> *         [artifact] 编码实现 Agent 完成 -> 03_code/（4 个文件）
00:50:48       review -> code      [feedback] 审查未通过，回传 2 条问题
00:50:48       engine -> code      [control ] 返工重置 code, finalize, review
00:50:50         code -> *         [artifact] 编码实现 Agent 完成 -> 03_code/（4 个文件）
00:50:50       review -> *         [artifact] 代码审查 Agent 完成 -> 04_review_r2.md
00:50:50     finalize -> *         [artifact] 交付说明 -> 05_delivery.md
```

`[feedback]` 那行是反方向的（review → code），也是循环能收敛的关键。

## 多次运行如何隔离

| 机制 | 实现 |
| --- | --- |
| 唯一目录 | `run_id = 年月日-时分秒-4位随机hex`，如 `20261024-005048-133a` |
| 原子创建 | `os.makedirs(exist_ok=False)`，不存在"先查再建"的竞态 |
| 拒绝覆盖 | `--run-id` 指定的目录已存在，立即报错退出（码 2），不写一个字节 |

```bash
python run.py --list
```

```
RUN ID                   状态       审查    耗时 文件  时间             需求
20261024-005245-084b     completed  PASS   2.77s    6  2026-10-24 00:52 串行模式测试
```

## 断点续跑

运行失败或被 Ctrl+C 中断后，目录不会丢弃：

```bash
python run.py --resume 20261024-005048-133a
```

它会从 `state.json` 恢复模式与进度，跳过已完成节点，重放已有消息，只补跑剩下的部分。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `-r, --requirement TEXT` | 直接传入需求 |
| `-f, --requirement-file PATH` | 从文件读需求（UTF-8） |
| 不给来源 | 交互式粘贴，单独一行 `:end` 结束 |
| `--mock` | 离线演示，跳过模式询问 |
| `--live` | 强制真实调用，缺 Key 直接报错 |
| `--resume RUN_ID` | 从历史运行断点续跑 |
| `--no-parallel` | 关闭并行，编码与测试改串行（便于调试） |
| `--show-graph` | 打印流程图与 Mermaid 源码后退出 |
| `--model` / `--base-url` | 模型与接口地址 |
| `--max-tokens` / `--timeout` / `--retries` | 输出上限 / 超时 / 重试次数 |
| `--runs-dir` / `--run-id` | 产物根目录 / 自定义运行 ID |
| `-q, --quiet` | 精简终端输出，日志仍写文件 |
| `--list` | 列出历史运行 |

退出码：`0` 成功且通过审查、`1` 运行失败、`2` 参数或环境问题、`3` 流程跑完但审查未通过、`130` 用户中断。

## 环境变量

复制 `.env.example` 为 `.env` 后按需修改。环境变量优先级高于 `.env`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | 必填（`--mock` 或交互选 2 除外） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 自动补 `/v1` |
| `DEEPSEEK_MODEL` | `deepseek-chat` | `deepseek-reasoner` 也可用 |
| `DEEPSEEK_MAX_TOKENS` | `8192` | 太小会导致代码被截断 |
| `DEEPSEEK_TIMEOUT` / `DEEPSEEK_MAX_RETRIES` | `180` / `3` | 单次超时 / 重试次数 |
| `WORKFLOW_RUNS_DIR` | `runs` | 产物根目录 |
| `WORKFLOW_MOCK` | `0` | 设 `1` 强制离线 |
| `WORKFLOW_PARALLEL` | `1` | 设 `0` 关闭并行 |

## 代码落盘协议

编码与测试 Agent 的输出靠固定标记切分，程序据此写文件：

```
<<<FILE: app/main.py>>>
文件完整内容
<<<END>>>
```

防护：路径必须相对（拒绝绝对路径、盘符、`..`、保留设备名），单文件 512KB、总数 80，
同名只保留第一份，解析不出的文件跳过并记 warning，一个都没解析出来则判该节点失败。
`__init__.py` 允许为空，其它文件为空视为生成失败。

### 文件多时不会一次吐完

单次调用的输出上限是 8K token，十几个文件的工程一次生成必然在尾部被截断
（表现为最后几个文件内容不完整、`setup.cfg`/`README.md` 直接缺失）。

所以 `CodeAgent` 会先解析设计文档的交付清单，超过 4 个文件就自动分批：

```
【本次为第 2/3 批】只生成下列 4 个文件，其它文件由其它批次负责，不要重复输出：
- todo/cli.py
- todo/service.py
...
```

每批都带完整设计文档，产出的文件块合并后落到同一目录。批次数和文件数会写进
`meta.json` 的节点记录，终端上显示为 `编码实现 Agent（3 批）`。

### 落盘即自检

代码写盘后立刻 `ast.parse` 一遍，有语法错误就带着错误行号重试一次该节点，
不让"根本编译不过的文件"流到审查环节。重试次数记在 `state.json` 的 `self_retried` 字段。

## 项目结构

```
.
├── run.py                  CLI：参数、交互引导、resume、结果汇总
├── agents_workflow/
│   ├── workflow.py         流程声明：NodeSpec / Edge / 条件函数 / 校验 / 出图
│   ├── pipeline.py         执行引擎：批次调度、并行、路由、循环、恢复
│   ├── message.py          消息总线（Message + MessageBus）
│   ├── state.py            共享状态（WorkflowState + 状态机）
│   ├── agents.py           5 个 Agent 实现 + 审查结论解析
│   ├── prompts.py          各 Agent 的提示词
│   ├── hooks.py            节点上下文准备、交付说明生成
│   ├── tools.py            静态检查、代码上下文拼装
│   ├── llm.py              DeepSeek 客户端（双 HTTP 后端 + 重试）
│   ├── artifacts.py        运行目录隔离、路径安全、代码块解析
│   ├── config.py           配置加载
│   ├── console.py          终端着色
│   └── mock.py             离线演示数据
├── Design.md               架构与设计决策
├── requirements.txt        仅 requests（可选）
└── runs/                   运行产物，每次一个目录
```

## 扩展：加一个 Agent

```python
# 1) prompts.py 加提示词
# 2) agents.py 继承 BaseAgent
class DocAgent(BaseAgent):
    stage, title, output_name = "doc", "文档生成 Agent", "06_doc.md"
    def build_messages(self, design_doc, code_files):
        return prompts.build_doc_messages(design_doc, code_files)

# 3) workflow.py 注册节点并接线
nodes["doc"] = NodeSpec(name="doc", agent="doc",
                        inputs=("design_doc", "code_files"),
                        output_key="doc", output_path="06_doc.md")
edges["review"] = [Edge(dst="doc", when=review_passed), ...]
edges["doc"] = [Edge(dst="finalize")]

# 4) pipeline.py 的 self.agents 注册表加一行
```

消息、状态、日志、token 统计、并行与恢复能力全部自动继承，引擎代码零改动。
`python run.py --show-graph` 会立即校验新图是否连通、并行组出口是否一致。

## 常见问题

**Q：日志出现"输出因 max_tokens 上限被截断"？**
A：说明某个节点一次要产出的内容超过 8K token。代码节点已自动分批；
如果单批仍被截断，说明设计文档要求的单个文件太大，可调小
`CodeAgent.files_per_batch`（默认 4）或让设计文档拆分模块。

**Q：审查两轮都 FAIL，最后还标"完成"？**
A：这是设计如此——流程跑完但质量门禁没过，退出码为 `3`，交付说明
`05_delivery.md` 里列着遗留问题，产物仅供人工复核。想提高通过率可以：
调大 `--max-tokens`、减少设计文档要求的文件数、或把 `MAX_REVIEW_ROUNDS` 调大。

**Q：返工了但还是同一个问题？**
A：先看 `04_review_rN.md` 的"问题清单"落到哪个文件：
实现问题会重跑 `code`，测试文件问题会连带重跑 `test`，两者都会带上上一轮
feedback。如果问题是"需求本身没写清楚"，那要回到 `01_requirement.md` 人工确认。

**Q：生成的代码跑不起来？**
A：`cd runs/<id>/03_code` 后看 `README.md`；`python -m unittest discover -s tests` 能跑测试。
静态检查只保证语法可编译，运行期问题以审查报告和实际执行为准。

## 文档

| 文件 | 内容 |
| --- | --- |
| `README.md` | 使用说明、流程图、命令行参考（本文件） |
| `Design.md` | 分层架构、消息模型、状态机、调度算法、设计决策、扩展方式 |
| `runs/<id>/SUMMARY.md` | 单次运行的节点表与消息时间线 |
