#!/usr/bin/env python3
"""岗位发现 agent —— 用 Claude 驱动 ats/ 的确定性工具。

    python3 agent/find_jobs.py "找加拿大远程的中级 Applied AI Engineer 岗位"
    python3 agent/find_jobs.py "..." --effort medium --max-rounds 20

与直接跑 scan.py 的区别不在抓取本身 —— 抓取那部分一模一样，都是 ats/ 里的函数。
区别在**谁来决定下一步抓什么**：

    scan.py     公司表是写死的，跑完就完了
    这个 agent  看着上一轮的命中率和命中类型，自己决定要不要扩表、扩哪些、什么时候停

2026-08-26 那次人工完成的 20 轮循环，就是这个 agent 要自动化的东西。
四个判断点（该扩哪些公司 / 对输出起疑 / 口径本身错了 / 什么时候停）写在下面的
SYSTEM 里 —— 那是这个文件真正的主体，Python 部分只是外壳。
"""

import argparse
import datetime
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools_def import ALL_TOOLS, STATE

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-opus-5"
PRICE = {"in": 5.00, "out": 25.00}  # USD / 1M tokens, claude-opus-5


SYSTEM = f"""你是岗位发现 agent，为一位在加拿大找远程 AI 工程岗的候选人工作。今天是 {datetime.date.today()}。

## 你的产出

一份 markdown 岗位清单，每条包含：公司、岗位名、真实发布日期、地点与远程口径、
ATS、官方投递链接、级别信号（年限要求 / JD 里的资深标识）、薪资（如果 JD 写了）。
用 save_report 落盘。

## 推荐流程

1. `list_known_companies` 看基线覆盖
2. `scan_boards` 扫已知公司（三家 ATS 分别扫，slugs 留空即扫全部）
3. `filter_jobs` 粗筛
4. **`enrich_jobs` 对入选岗位取真实发布日 —— 这步不能省**
5. `read_jd` 对最终候选读正文，确认级别门槛和加拿大是否真的可投
6. `probe_slugs` 扩表 → `add_companies` 固化 → 回到 2
7. `save_report`

## 四条判断准则

**一、`updated_at` 是陷阱。**
Greenhouse 列表接口只给 `updated_at`（雇主上次改动这条记录的时间），不是发布日。
改个错别字就会刷新它。实测 28 条候选里 15 条年龄被低估 3 倍以上，最极端的一条
列表显示 24.9 天、真实 716.7 天。**任何岗位进最终清单前必须 enrich_jobs**，
按 `first_published` 判断新鲜度，不是按列表时间。HTTP 4xx 的直接剔除。

**二、对自己的输出起疑。**
命中率 0%、结果为空、所有看板都失败 —— 先怀疑是故障，不是事实。
用一个已知一定在线的 slug（greenhouse:gitlab / ashby:cohere）复验再下结论。
2026-08-26 那次第一轮因为 SSL 证书问题返回 live=0，看起来完全正常，
差点得出"加拿大没有 AI 岗"的结论。

**三、扩表要有依据。**
slug 只能猜。看已命中的是什么类型的公司（AI infra / devtools / 加拿大本土 SaaS /
语音 AI …），沿着同类往外扩，一批 40–80 个，用 `probe_slugs` 试。
命中率降到 20% 以下说明这个方向挖完了，换方向或收手。
**只把 probe 验证过在线的写进 companies.txt。**

**四、判断什么时候停。**
不要无限扩表。连续两批扩表没有带来新的合格岗位，就收手出报告。
宁可给 8 条扎实的，不要给 30 条没核验过的。

## 真实性边界

报告里的每条信息都必须来自工具返回的数据。不要脑补公司背景、薪资、团队情况。
JD 里没写的就写"JD 未说明"。链接必须是工具返回的 url 原样，不要自己拼。
"""


def main():
    ap = argparse.ArgumentParser(description="岗位发现 agent")
    ap.add_argument("request", nargs="?",
                    default="找加拿大可远程的全职 Applied AI / LLM 工程岗位，"
                            "初级到中级，排除 senior/staff/lead，近 30 天内发布的。")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-rounds", type=int, default=30, help="工具调用轮数上限")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--no-fallbacks", action="store_true", help="不启用 refusal 服务端回退")
    ap.add_argument("--dry-run", action="store_true", help="只打印系统提示词和工具清单，不调 API")
    args = ap.parse_args()

    if args.dry_run:
        print(SYSTEM)
        print("=" * 70)
        for t in ALL_TOOLS:
            print(f"  {t.name}")
        return

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("未找到 ANTHROPIC_API_KEY。先 export ANTHROPIC_API_KEY=... 再跑。\n"
                 "（想先看 agent 会怎么工作而不花钱：加 --dry-run）")

    client = anthropic.Anthropic()
    kw = dict(
        model=MODEL,
        max_tokens=args.max_tokens,
        system=SYSTEM,
        tools=ALL_TOOLS,
        messages=[{"role": "user", "content": args.request}],
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": args.effort},
        max_iterations=args.max_rounds,
    )
    if not args.no_fallbacks:
        # 服务端 refusal 回退：安全分类器拒答时同一次调用内换模型重跑。
        # tool_runner 没有一等的 fallbacks 参数，走 extra_body。
        kw["betas"] = ["server-side-fallback-2026-07-01"]
        kw["extra_body"] = {"fallbacks": "default"}

    print(f"模型 {MODEL} · effort={args.effort} · 轮数上限 {args.max_rounds}")
    print(f"需求：{args.request}\n" + "=" * 70)

    trace, usage = [], {"in": 0, "out": 0, "cache_read": 0}

    def run(runner_kw):
        runner = client.beta.messages.tool_runner(**runner_kw)
        n = 0
        for message in runner:
            n += 1
            u = message.usage
            usage["in"] += u.input_tokens
            usage["out"] += u.output_tokens
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0

            print(f"\n──── 第 {n} 轮 ──── stop={message.stop_reason} "
                  f"(in {u.input_tokens} / out {u.output_tokens})")
            trace.append(f"\n## 第 {n} 轮 · stop={message.stop_reason}\n")

            for b in message.content:
                if b.type == "thinking" and getattr(b, "thinking", ""):
                    print(f"  💭 {b.thinking[:400]}")
                    trace.append(f"**思考：** {b.thinking}\n")
                elif b.type == "text" and b.text.strip():
                    print(f"  {b.text[:600]}")
                    trace.append(f"{b.text}\n")
                elif b.type == "tool_use":
                    arg = {k: (str(v)[:90] + "…" if len(str(v)) > 90 else v)
                           for k, v in (b.input or {}).items()}
                    print(f"  🔧 {b.name}({arg})")
                    trace.append(f"**工具：** `{b.name}` `{arg}`\n")
            if message.stop_reason == "refusal":
                print(f"  ⛔ 被拒答：{message.stop_details}")
                break
        return n

    try:
        rounds = run(kw)
    except anthropic.BadRequestError as e:
        if not args.no_fallbacks and "fallback" in str(e).lower():
            print("⚠️ 服务端回退不可用，去掉后重试")
            kw.pop("betas", None)
            kw.pop("extra_body", None)
            rounds = run(kw)
        else:
            raise
    except anthropic.RateLimitError as e:
        sys.exit(f"限流：{e}")
    except anthropic.APIConnectionError as e:
        sys.exit(f"连不上 API：{e}")

    cost = usage["in"] / 1e6 * PRICE["in"] + usage["out"] / 1e6 * PRICE["out"]
    summary = (f"\n{'='*70}\n共 {rounds} 轮 · "
               f"input {usage['in']:,}（缓存命中 {usage['cache_read']:,}）· "
               f"output {usage['out']:,} · 约 ${cost:.2f}\n"
               f"扫到岗位 {len(STATE['jobs'])} 条，已核验 {len(STATE['enriched'])} 条")
    print(summary)

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    p = os.path.join(HERE, "out", "agent_trace.md")
    with open(p, "w") as f:
        f.write(f"# Agent 运行记录 {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n"
                f"需求：{args.request}\n\n模型：{MODEL} · effort={args.effort}\n")
        f.write("".join(trace))
        f.write(summary)
    print(f"完整记录 → {p}")


if __name__ == "__main__":
    main()
