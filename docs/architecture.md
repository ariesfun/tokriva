# 架构草案

> 状态：草案，随实现演进调整。

## 设计原则

1. **可观测优先**：任何"免费"结论都必须带上采集时间、来源与置信度。
2. **数据与执行分离**：发现/监测产出的资源目录（catalog）是只读快照，调度层消费快照，不直接爬取。
3. **可复现评测**：评测固定模型版本、提示词、采样参数与评分口径，结果可重跑。
4. **成本显式化**：每条路由决策都能解释"为什么选它"，包括配额余量与预计成本。

## 模块边界

```
                 ┌──────────────┐
   public sources│  Discovery   │  产出：资源候选（provider, model, quota, limits）
                 └──────┬───────┘
                        │
                 ┌──────▼───────┐
                 │   Catalog    │  统一资源目录（带时间戳与来源）
                 └──┬────────┬──┘
                    │        │
          ┌─────────▼──┐  ┌──▼─────────┐
          │  Monitor   │  │    Eval    │
          │ 探活/限流   │  │ 基准/横评   │
          └─────────┬──┘  └──┬─────────┘
                    │        │
                 ┌──▼────────▼──┐
                 │    Router    │  按成本/延迟/质量/配额选路
                 └──────┬───────┘
                        │
                  调用方 / Agent
```

## 关键数据模型（初稿）

- **Resource**：`provider`、`model_id`、`quota`、`rate_limit`、`expires_at`、`source`、`observed_at`
- **HealthSample**：`resource_id`、`status`、`latency_ms`、`error`、`sampled_at`
- **EvalResult**：`model_id`、`benchmark`、`score`、`params_hash`、`run_at`
- **RouteDecision**：`request_id`、`candidates`、`chosen`、`reason`、`estimated_cost`

## 开放问题

- 免费额度的采集是否需要人工确认（部分平台条款限制自动化探测）
- 评测样本集的版权与可分发边界
- 多 provider 的 key 管理（本地优先 vs. 托管）
