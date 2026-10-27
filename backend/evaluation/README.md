# 企业 RAG 持续评估

标准问答集覆盖公司信息、商城政策、终端服务、综合问答、合规边界和不可回答问题。

## 指标

- 检索命中率：至少一个引用命中标准证据页或证据关键词的题目占比。
- 引用准确率：返回引用中命中标准证据页或证据关键词的引用占比。
- 回答忠实度：标准事实覆盖、关键数字无越界且存在相关引用的综合得分。
- 答案关键词覆盖率：辅助观察答案是否包含标准答案要点，不作为忠实度替代。

## 首次运行

从项目根目录执行，脚本会创建隔离账号和知识库、上传文档并输出 JSON 与 Markdown 报告：

```powershell
.\backend\.venv\Scripts\python.exe -m evaluation.evaluate `
  --document "C:\path\to\华为中国公开资料汇编_2026-06-13.pdf"
```

## 重复评估已有知识库

```powershell
.\backend\.venv\Scripts\python.exe -m evaluation.evaluate `
  --email "your-eval@example.com" `
  --password "your-password" `
  --knowledge-base-id 1 `
  --fail-under
```

默认连续执行 3 轮，并输出平均值、最低值、最高值和波动范围。可使用 `--rounds 1` 做快速诊断。

`--fail-under` 会在任一轮检索命中率低于 90%、引用准确率低于 80%、忠实度低于 80%，或最高平均延迟超过 15 秒时返回失败退出码，可用于 CI。
每次运行还会自动读取上一份 JSON 报告，记录三项核心指标的趋势变化。
趋势仅比较相同指标版本的报告，调整评分口径后会自动建立新基线。
