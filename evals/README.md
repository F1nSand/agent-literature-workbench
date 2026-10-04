# 检索评测输入

`datasets/sample-retrieval-qrels.json` 把当前机器辅助候选页整理为可供工具读取的结构化文件。它仍是 `candidate` 标注，不是黄金答案。

`datasets/supplemental-candidate-set.json` 保存 16 条新增候选：8 条可回答、4 条当前语料证据不足、4 条专题范围/提示注入安全场景。每条都标记 `review.status=pending`；PDF 哈希、物理页、原文摘录、类别配额和建议留出集由 `validate_candidate_set.py` 校验。建议留出分配目前仅是草案，未冻结，也不可用于正式成绩。

从候选集中检查来源并导出新增 8 条可回答题的评测器输入（仍是 candidate）：

```powershell
python evals/validate_candidate_set.py --emit-retrieval-qrels evals/datasets/supplemental-retrieval-candidate-qrels.json
```

校验会重算 4 篇来源 PDF 的 SHA-256，并确认每段摘录确实出现在标注的物理页。它不代表人工确认。新增问题与原始 Q1–Q8 保持分开；只有用户/导师复核答案要点、必要证据组、缺证判定和留出集后，才能合并冻结并运行正式对照实验。提示注入样例是合成测试输入，不属于论文语料或证据。

`retrieval_eval.py` 是离线评分入口，只读取 qrels 与已保存的检索排名，不连接数据库、不访问模型服务。运行记录需按 `R1`（向量）、`R2`（向量+关键词/RRF）和 `R3`（增加重排）分别保存各问题排名、延迟和已核实请求成本：

```powershell
python evals/retrieval_eval.py --qrels evals/datasets/sample-retrieval-qrels.json --run path/to/retrieval-run.json --output path/to/report.json
```

每条排名使用 `{"document": "PDF文件名", "page_number": 6}`；运行 JSON 的 `strategies` 下可包含 `R1`、`R2`、`R3`。指标包括 Evidence Recall@8、Hit@8、MRR@8、平均延迟和请求成本。宏平均的分母是所有带有效必要证据的 qrels 问题；没有排名记录的问题按零分计入，并显示在 `questions_missing_rankings` 中。延迟或成本只要有一道可评分题缺数据，整体值就为 `null`，不推断为零。

运行文件的最小结构如下；没有实际结果的策略留空即可，工具会标记为 `no_rankings`，不会生成虚构分数：

```json
{
  "run_id": "描述运行配置的稳定标识",
  "corpus_manifest_sha256": "与 qrels 相同的语料清单 SHA-256",
  "strategies": {
    "R1": {
      "rankings": {"Q3": [{"document": "01‑Cost‑Effective‑Agent‑Harnesses.pdf", "page_number": 6}]},
      "latency_ms_by_question": {"Q3": 12},
      "request_cost_cny_by_question": {"Q3": 0.0}
    },
    "R2": {"rankings": {}},
    "R3": {"rankings": {}}
  }
}
```

样本 qrels 仍处于待人工确认状态，因此工具把结果标为 `candidate-exploration`，并明确 `official_claim_allowed: false`。正式结果还必须满足语料清单哈希一致、R1/R2/R3 均覆盖全部可评分问题，以及运行记录显式标记 `evaluation_scope: confirmed-evaluation` 与 `official_claim_allowed: true`。覆盖不全会标为 `incomplete-run`；缺少正式运行标记会标为 `nonofficial-run`。这些门槛需要用户或导师确认答案要点、必要证据页、冻结集和实验配置。当前生成器只产生 candidate-exploration 运行记录，正式运行模式尚未启用。

`generate_retrieval_run.py` 从当前数据库中按样本清单固定四篇文献、活动版本和索引代，生成真实的 R1/R2/R3 页面排名。默认仅做无计费预检；预检会核对清单哈希、文献哈希/页数、Embedding 缓存和令牌上限。`--execute` 才会发起调用，且执行器在代码内限制最多 1 次批量 Embedding 请求、8 次 Reranker 请求、40 个候选/题、1,350,000 个重排输入估算 token 和 ¥0.01 可核实估算成本；不重试。结果明确标为候选探索，不会写入研究者前端。

项目 Compose 提供仅按需启动的评测工具容器（不属于默认前端服务）：

```powershell
docker compose run --rm retrieval-eval python /evals/generate_retrieval_run.py --qrels /evals/datasets/sample-retrieval-qrels.json --manifest /evals/datasets/sample-corpus-manifest.json --output /runs/sample-retrieval-candidate-run.json
docker compose run --rm retrieval-eval python /evals/generate_retrieval_run.py --qrels /evals/datasets/sample-retrieval-qrels.json --manifest /evals/datasets/sample-corpus-manifest.json --output /runs/sample-retrieval-candidate-run.json --execute
python evals/retrieval_eval.py --qrels evals/datasets/sample-retrieval-qrels.json --run experiments/runs/sample-retrieval-candidate-run.json --output experiments/reports/sample-retrieval-candidate-report.json
```
