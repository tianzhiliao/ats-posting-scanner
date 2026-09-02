#!/usr/bin/env python3
"""阶段 1 —— 批量扫看板。

拿 companies.txt 里的候选 slug 去三家 ATS 逐个探测，命中的看板拉全部在挂岗位，
按标题正则粗筛，写出 raw.json；再按「加拿大 / 新鲜度 / 非资深」筛一遍，写出 cand.json。

    python3 ats/scan.py --probe              # 只看哪些 slug 活着
    python3 ats/scan.py                      # 完整扫描 + 筛选
    python3 ats/scan.py --ats ashby --max-age 30

⚠️ 这一步用的时间字段是「列表接口给什么用什么」，Greenhouse 那边是 updated_at，
   不可信。真实发布日在阶段 2（enrich.py）才拿得到。这里的 --max-age 只是粗筛，
   放宽一点（默认 45 天）比放严好，免得把新岗漏掉。
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ats as A

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description="扫描 Ashby / Lever / Greenhouse 公司看板")
    ap.add_argument("--companies", default=os.path.join(HERE, "companies.txt"))
    ap.add_argument("--ats", default="", help="只扫某几家，逗号分隔：ashby,lever,greenhouse")
    ap.add_argument("--slug", default="", help="只扫指定 slug（配合 --ats），逗号分隔")
    ap.add_argument("--probe", action="store_true", help="只探测看板存活，不筛岗位")
    ap.add_argument("--all-titles", action="store_true", help="不用标题正则，全部收下")
    ap.add_argument("--workers", type=int, default=40, help="并发数，默认 40（再高会 429）")
    ap.add_argument("--max-age", type=float, default=45, help="粗筛：距今天数上限")
    ap.add_argument("--keep-senior", action="store_true", help="不排除 senior/staff/lead")
    ap.add_argument("--anywhere", action="store_true", help="不限加拿大/北美")
    ap.add_argument("--now", default="", help="参照日期 YYYY-MM-DD，用于复现历史结果")
    ap.add_argument("--out", default=os.path.join(HERE, "out", "raw.json"))
    ap.add_argument("--cand", default=os.path.join(HERE, "out", "cand.json"))
    ap.add_argument("--live-out", default=os.path.join(HERE, "out", "live_boards.txt"))
    args = ap.parse_args()

    now = None
    if args.now:
        y, m, d = (int(x) for x in args.now.split("-"))
        now = datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc)

    if args.slug:
        atses = [a.strip() for a in (args.ats or "ashby,lever,greenhouse").split(",")]
        targets = [(a, s.strip()) for a in atses for s in args.slug.split(",")]
    else:
        targets = A.load_companies(args.companies)
        if args.ats:
            keep = {a.strip() for a in args.ats.split(",")}
            targets = [t for t in targets if t[0] in keep]

    title_re = None if (args.probe or args.all_titles) else A.TITLE_RE
    print(f"探测 {len(targets)} 个看板，并发 {args.workers} …", file=sys.stderr)

    done = [0]

    def tick(ats, slug, status, info, nrows):
        done[0] += 1
        if done[0] % 50 == 0:
            print(f"  … {done[0]}/{len(targets)}", file=sys.stderr)

    results, boardlog = A.scan_boards(
        targets, title_re=title_re, workers=args.workers, now=now, on_board=tick
    )

    live = sorted((b[0], b[1], b[3]) for b in boardlog if b[2] == "OK")
    print(f"\n看板探测={len(boardlog)}  在线={len(live)}  命中率={len(live)/max(1,len(boardlog)):.0%}")
    by_ats = {}
    for a, s, n in live:
        by_ats.setdefault(a, []).append((s, n))
    for a in sorted(by_ats):
        tot = sum(n for _, n in by_ats[a] if isinstance(n, int))
        print(f"  {a:11} 在线看板 {len(by_ats[a]):4}  在挂岗位合计 {tot}")

    os.makedirs(os.path.dirname(args.live_out), exist_ok=True)
    with open(args.live_out, "w") as f:
        for a, s, n in live:
            f.write(f"{a}\t{s}\t{n}\n")
    print(f"  → 在线看板清单 {args.live_out}")

    if args.probe:
        return

    print(f"\n标题命中 = {len(results)} 条")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(results, open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"  → {args.out}")

    # ---- 粗筛 ----
    cand = results
    if not args.anywhere:
        cand = [j for j in cand if j["ca"] or j["na"]]
        print(f"  + 加拿大或泛北美地点: {len(cand)}")
    cand = [j for j in cand if j["age"] is not None and j["age"] <= args.max_age]
    print(f"  + 距今 <= {args.max_age:g} 天: {len(cand)}")
    if not args.keep_senior:
        cand = [j for j in cand if not j["senior"]]
        print(f"  + 排除 senior/staff/lead: {len(cand)}")

    print("=" * 108)
    for j in sorted(cand, key=lambda x: x["age"]):
        print(f"[{j['age']:>6}d] {j['ats']:10} {j['company']:22} | {j['title'][:56]:56} | {j['loc'][:60]}")
    json.dump(cand, open(args.cand, "w"), indent=1, ensure_ascii=False)
    print(f"\n  → {args.cand}（下一步：python3 ats/enrich.py）")


if __name__ == "__main__":
    main()
