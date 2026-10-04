# 示例问题集：证据候选复核

状态：候选证据已机器辅助盘点，**不是冻结答案集**。完成正式评测前，需由用户/导师逐题确认必要答案要点、等价证据页和“不可回答”判定。

输入：`示例论文文档与问题集/问题集.md`，SHA-256 `33219B012852E89C6EAFE9360619BDEB32E93FD240A1BF0103DCF0C100930300`。本记录不改写原始问题。页码为 PDF 物理页序，从 1 开始；页码仅是检索候选，最终应以 PDF 阅读器核对引用。

## 逐题候选

| 题号 | 初步可答性 | 候选证据页 | 复核说明 |
|---|---|---|---|
| Q1 Agent Harness 定义 | 部分可答；先确认“涉及该概念”的范围 | `01‑Cost‑Effective‑Agent‑Harnesses.pdf` pp.1–4, 15–16；可对照 `02‑ModelScope‑Agent.pdf` pp.2–4、`03‑Tongyi‑DeepResearch‑Report.pdf` pp.5–8、`04‑中文‑大语言模型智能体出行日志.pdf` pp.4–7 | Cost-Effective 论文明确围绕 agent harness / architecture 展开。其余论文描述 Agent 系统架构，但全文关键词初筛未发现把 “Agent Harness” 作为同一术语定义；不可把系统简介冒充作者对 Harness 的正式定义。建议答案区分原文定义、系统架构描述和综合归纳。 |
| Q2 ModelScope-Agent 与 Tongyi DeepResearch 的记忆、工具设计 | 可答 | `02‑ModelScope‑Agent.pdf` pp.2–4；`03‑Tongyi‑DeepResearch‑Report.pdf` pp.5–8, 12 | ModelScope 描述可配置工具库/API 检索，并以 memory module 管理 system message、用户历史、示例、工具消息及本地知识；Tongyi 描述研究工具、上下文管理及面向开放世界实体的记忆机制。引用应分别核对模块职责，不把“有记忆”简化成相同设计。 |
| Q3 pass@k 公式与含义 | 可答 | `01‑Cost‑Effective‑Agent‑Harnesses.pdf` p.6 | 该页说明任务正确性判定及 pass@1/pass@2 的语义，并给出问题所指估计量。复核公式上下标、组合项和该论文采用的候选去重/排序口径后再冻结答案。 |
| Q4 科研智能体评估 Benchmark | 可答但需区分任务领域 | `03‑Tongyi‑DeepResearch‑Report.pdf` pp.1, 11–12；`02‑ModelScope‑Agent.pdf` pp.5–7, 9；对照 `01‑Cost‑Effective‑Agent‑Harnesses.pdf` p.6、`04‑中文‑大语言模型智能体出行日志.pdf` pp.10–12 | Tongyi 列出深度研究/信息检索基准；ModelScope 列出 MSAgent-Bench 与若干工具使用数据集。ARC‑AGI‑1 是通用抽象推理评测，出行论文用 DeepMove 和轨迹指标评估移动模拟，不应统称为“科研智能体 Benchmark”。答案需标注基准用途及它是否直接评估科研/深度研究能力。 |
| Q5 MobiA 架构模块 | 可答 | `04‑中文‑大语言模型智能体出行日志.pdf` pp.5–7 | 第 5 页流程图可辨认三个主阶段：出行特征与知识库构建、动机推理生成具体日程、出行日程空间映射；右侧还有自洽性评估与反思/更新回路。回答需结合图示和正文，不只列图中子模块。 |
| Q6 各系统依赖的底层模型 | 可答，ModelScope 应写为可配置模型族 | `01‑Cost‑Effective‑Agent‑Harnesses.pdf` pp.6, 16；`02‑ModelScope‑Agent.pdf` pp.2, 5–8；`03‑Tongyi‑DeepResearch‑Report.pdf` pp.2, 11–12；`04‑中文‑大语言模型智能体出行日志.pdf` p.10 | Cost-Effective 主实验用 DeepSeek V3.2，另在 99 题子集测 Qwen3‑235B‑Instruct；Tongyi 基于 Qwen3‑30B‑A3B‑Base；MobiA 的实验描述 DeepSeek R1 与 V3 分工；ModelScope 是支持多种控制模型的框架，不应强行填成单一底模。 |
| Q7 长任务/长轨迹问题及应对 | 部分可答；不得声称四篇都有同类方法 | `01‑Cost‑Effective‑Agent‑Harnesses.pdf` pp.8, 15；`03‑Tongyi‑DeepResearch‑Report.pdf` pp.6, 8, 12, 16；范围对照 `04‑中文‑大语言模型智能体出行日志.pdf` p.16 | Cost-Effective 使用探索结果压缩及受限的迭代/重探索；Tongyi 描述长上下文、轨迹摘要和并行研究，并承认 128K 上下文仍不足以处理最复杂长程任务。MobiA 第 16 页谈的是单日建模及未覆盖的跨周期依赖，不是通用 Agent 长轨迹方案。ModelScope 的记忆/上下文组装不能单凭此推断为长任务恢复机制。 |
| Q8 Agent 工作台类系统的研究局限 | 可归纳，但题干外推范围大 | `01‑Cost‑Effective‑Agent‑Harnesses.pdf` pp.19–21；`03‑Tongyi‑DeepResearch‑Report.pdf` p.16；`04‑中文‑大语言模型智能体出行日志.pdf` p.16；ModelScope 结论 `02‑ModelScope‑Agent.pdf` p.6 | 各论文的任务类型不同：ARC 推理 Harness、Agent 框架、深度研究模型、出行模拟系统，并非四种同类“工作台”。可以逐篇归纳作者承认的局限，但不能从四篇样本概括成整个领域已证实的共识；ModelScope 第 6 页结论未明确列出局限，应写“本文未明确列出”，不要代作者补写。 |

## 当前样本边界

- 4 篇共 78 页，抽取文字层初筛覆盖 78/78 页；它们不能替代问题集要求的扫描件 OCR 夹具。
- 已渲染并目视复核候选图表页：Cost-Effective p.6 的 pass@k、ModelScope-Agent p.10 的图表、中文 MobiA p.5 的架构流程图。ModelScope 总体架构主图另在 p.3。
- 页码和上表可用于建立下一轮 evidence groups；在导师/用户确认前，禁止作为黄金答案、Recall 标注或模型成绩依据。
