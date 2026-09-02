# 岗位发现 Agent（阶段 B）

> 中文技术笔记。英文总览见仓库根目录的 [README.md](../README.md)。
> 本文原是一个私有求职工作区里的内部文档，抽取成独立仓库时保留了原貌，个别对外部文件的引用已移除。

用 Claude 驱动 [`../ats/`](../ats/) 的确定性工具。**抓取代码一行没变** —— 变的是
谁来决定下一步抓什么。

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 agent/find_jobs.py "找加拿大远程的中级 Applied AI Engineer 岗位"

python3 agent/find_jobs.py --dry-run    # 只看系统提示词和工具清单，不花钱
```

---

## 为什么需要 agent，而不是再写一个脚本

2026-08-26 那次抓取实际是 **20 轮人工循环**，每轮的输入都取决于上一轮的输出。
脚本能做的只有其中的抓取动作，做不了这四个判断：

| 判断 | 脚本为什么做不了 |
|---|---|
| **该扩哪些公司** | slug 只能猜。扩哪批取决于上一轮命中的是什么类型的公司 |
| **对自己的输出起疑** | `live=0` 是「没岗位」还是「网络挂了」？脚本只会照实打印 0 |
| **口径本身错了** | 发现 `updated_at` 不可信 → 改用 `first_published`。这是改逻辑不是调参数 |
| **什么时候停** | 第三波扩表收益递减到什么程度算够 |

这四条写在 [`find_jobs.py`](find_jobs.py) 的 `SYSTEM` 里。**那才是这个文件的主体**，
Python 部分只是外壳。

---

## 工具面：返回值的形状比功能重要

[`tools_def.py`](tools_def.py) 把 ats 层包成 8 个工具。设计难点不在功能，在**返回多少**。

一次全量扫描是 947 条标题命中。原样塞进上下文是几十万 token，跑三轮就填满 1M 窗口，
而且 agent 会淹死在细节里做不出判断。所以：

> **全量数据落盘，上下文里只走统计量 + 摘要 + 短 id。**

每条岗位分配一个 `gr-gitlab-3` 这样的短 id，agent 靠 id 引用，想看细节再按 id 取。

| 工具 | 返回 | 设计意图 |
|---|---|---|
| `list_known_companies` | slug 清单 | 先知道基线，才谈得上往哪扩 |
| `probe_slugs` | 命中率 + 命中/未命中 slug | 廉价探针，扩表专用，不拉岗位内容 |
| `scan_boards` | 统计 + **前 50 条**一行摘要 | 命中 947 条也只给 50 条，其余在盘上 |
| `filter_jobs` | 符合条件的 id 清单 | 筛选在本地做，不消耗模型 token |
| `enrich_jobs` | 真实发布日 / 列表日 / **低估倍数** / http | 直接把陷阱标出来，不指望模型自己算 |
| `read_jd` | 3000 字符一段，带 offset | 按需分页，不一次灌全文 |
| `add_companies` | 写回 companies.txt | **拒绝写入未经 probe 验证的 slug** |
| `save_report` | 落盘路径 | 交付物 |

两个刻意的约束：

- `enrich_jobs` 直接在返回里算好 `⚠️低估29倍` 和 `☠️已下架`。判断陷阱这件事不该
  依赖模型每次都记得除一下 —— 能在工具里算的，就不要留给模型算。
- `add_companies` 会**拒绝**写入没经过 `probe_slugs` 的 slug。agent 可能想当然地把
  猜测固化进表里，这个门是关掉那条路的。

---

## 运行参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--effort` | `high` | `low`/`medium`/`high`/`xhigh`/`max`。agentic 任务建议 ≥ high |
| `--max-rounds` | 30 | 工具调用轮数上限，防跑飞 |
| `--dry-run` | — | 只打印系统提示词和工具清单，不调 API |
| `--no-fallbacks` | — | 关掉服务端 refusal 回退 |

模型固定 `claude-opus-5`，adaptive thinking + summarized display —— 每轮的思考过程
会打到终端并写进 `out/agent_trace.md`，那是看清楚 agentic loop 的地方。

跑完会打印 token 用量与估算成本（$5 / $25 per MTok）。

---

## 已验证 / 未验证

诚实标注，免得当成全部跑通过：

**已验证（脱离 API 直接调用工具函数）**
- 8 个工具全部注册成功，schema 由函数签名 + docstring 自动生成
- `probe_slugs` / `scan_boards` / `filter_jobs` 实测返回正确
- `--dry-run` 正常
- 底层 ats 层全量跑通（1106 看板 / 294 在线 / 28 候选）

**未验证（需要 ANTHROPIC_API_KEY）**
- 完整的 agent 循环
- refusal 服务端回退那条路径（`betas` + `extra_body`，失败时会自动去掉重试）
- token 用量与成本估算的实际数值

设了 key 之后建议先跑一次小范围的，例如
`python3 agent/find_jobs.py "只扫 greenhouse 上的 gitlab 和 dialpad，出一份清单" --max-rounds 8`
确认循环行为正常，再放开跑全量。
