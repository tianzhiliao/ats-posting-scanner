#!/usr/bin/env python3
"""阶段 2 —— 逐条取详情、真实发布日、JD 信号、存活性。

扫描阶段拿到的是列表接口的摘要，判断不了「这岗是不是真的新」「是不是真远程」
「要几年经验」。这一步对每个候选打详情接口，把 JD 正文拉下来跑正则，产出可判断的信号。

    python3 ats/enrich.py
    python3 ats/enrich.py --in out/cand.json --workers 8

三件这一步才能做的事：

1. **真实发布日。** Greenhouse 列表只有 updated_at（雇主上次动过），详情才有
   first_published。2026-08-26 那次就是靠这个把「刷新过的老岗」从新岗里剔出去的。
2. **存活性。** 打一次 URL 看 HTTP 码。看板缓存有延迟，列表里还在、点开已 404 的
   情况真实存在。写进投递清单前必须回查。
3. **正文信号。** 远程口径（fully remote / hybrid / 每周几天到岗）、年限要求、
   级别词、薪资数字、加拿大提及 —— 全在正文里，列表接口一个都没有。
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ats as A

HERE = os.path.dirname(os.path.abspath(__file__))

# JD 正文信号。分开写而不是塞进一个大正则，是因为每一类要单独看结果。
SIG = {
    # 远程口径：hybrid / onsite / "3 days a week in office" 是最常见的隐藏门槛
    "remote_kw": re.compile(
        r"(fully remote|remote-first|remote first|work from anywhere|hybrid"
        r"|in[- ]office|on[- ]site|onsite|days? (?:a|per) week in|relocat)", re.I),
    # 年限：连同后面 40 个字符一起抓，才看得出是 "3 years of Python" 还是 "3 years preferred"
    "yoe": re.compile(r"(\d\+?\s*(?:-|to|–)?\s*\d*\s*years?[^.,;]{0,40})", re.I),
    "level": re.compile(
        r"\b(entry[- ]level|new grad|junior|mid[- ]level|intermediate|early career"
        r"|l[3-5]\b|ic[1-4]\b)\b", re.I),
    "canada": re.compile(
        r"(canada|canadian|toronto|vancouver|montr[eé]al|ottawa|calgary|kitchener"
        r"|waterloo|british columbia|ontario|alberta|nova scotia)", re.I),
    # 薪资：CAD / C$ / $ 后跟 5-6 位数。币种要靠上下文判断，见 --pay-context
    "pay": re.compile(r"(?:CAD|C\$|\$)\s?\d{2,3},\d{3}", re.I),
}


def signals(text):
    low = text.lower()
    out = {k: sorted(set(m if isinstance(m, str) else m[0] for m in r.findall(low)))
           for k, r in SIG.items()}
    out["yoe"] = out["yoe"][:6]
    out["canada"] = out["canada"][:8]
    out["pay"] = out["pay"][:8]
    out["ft"] = bool(re.search(r"full[- ]time", low))
    return out


def job_id(j):
    if j.get("jid"):
        return str(j["jid"])
    return (j.get("url") or "").rstrip("/").split("/")[-1].split("?")[0]


def enrich_one(j, retries=2):
    j = dict(j)
    text = ""
    for attempt in range(retries + 1):
        try:
            if j["ats"] == "greenhouse":
                d = A.greenhouse_job(j["company"], job_id(j))
                j["first_published"] = d.get("first_published")
                j["pub_age"] = A.days_ago(d.get("first_published"))
                j["updated_age"] = A.days_ago(d.get("updated_at"))
                j["offices"] = [o.get("name") for o in (d.get("offices") or [])]
                j["meta"] = {m["name"]: m.get("value")
                             for m in (d.get("metadata") or []) if m.get("value")}
                text = A.strip_html(d.get("content"))

            elif j["ats"] == "ashby":
                # Ashby 没有单岗详情接口，重拉看板再按 id/url 对回去。
                board = A.ashby_board(j["company"])
                jid = job_id(j)
                hit = [x for x in board.get("jobs", [])
                       if str(x.get("id")) == jid
                       or (x.get("jobUrl") or x.get("applyUrl")) == j.get("url")]
                if not hit:
                    j["pub_age"] = "GONE"      # 看板还在，这条岗位没了
                    break
                d = hit[0]
                j["first_published"] = d.get("publishedAt")
                j["pub_age"] = A.days_ago(d.get("publishedAt"))
                j["comp"] = (d.get("compensation") or {}).get("summaryComponents")
                j["emp"] = d.get("employmentType")
                j["isRemote"] = d.get("isRemote")
                text = A.strip_html(d.get("descriptionHtml"))

            elif j["ats"] == "lever":
                d = A.lever_posting(j["company"], job_id(j))
                j["first_published"] = d.get("createdAt")
                j["pub_age"] = A.days_ago(d.get("createdAt"))
                j["emp"] = (d.get("categories") or {}).get("commitment")
                text = d.get("descriptionPlain") or A.strip_html(d.get("description"))
                for sec in d.get("lists") or []:
                    text += " " + sec.get("text", "") + " " + A.strip_html(sec.get("content"))
            break
        except Exception as e:
            if attempt == retries:
                j["error"] = f"{type(e).__name__}: {e}"[:160]
            else:
                time.sleep(1.5 * (attempt + 1))   # 详情接口会限流，退避重试

    j["http"] = A.http_status(j["url"]) if j.get("url") else None
    j["sig"] = signals(text)
    j["excerpt"] = text[:1400]
    j["text_len"] = len(text)
    return j


def main():
    ap = argparse.ArgumentParser(description="给候选岗位补详情、真实发布日、JD 信号")
    ap.add_argument("--in", dest="src", default=os.path.join(HERE, "out", "cand.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "out", "enriched.json"))
    ap.add_argument("--workers", type=int, default=8,
                    help="详情接口比列表接口更容易限流，默认 8；被限流就调到 1")
    ap.add_argument("--delay", type=float, default=0, help="每条之间 sleep 秒数（--workers 1 时用）")
    ap.add_argument("--full-text", action="store_true", help="把 JD 全文一并写入 json")
    args = ap.parse_args()

    cand = json.load(open(args.src))
    print(f"富化 {len(cand)} 条 …", file=sys.stderr)

    if args.workers <= 1:
        out = []
        for j in cand:
            out.append(enrich_one(j))
            if args.delay:
                time.sleep(args.delay)
    else:
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            out = list(ex.map(enrich_one, cand))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w"), indent=1, ensure_ascii=False)

    def key(x):
        v = x.get("pub_age")
        return v if isinstance(v, (int, float)) else 9999

    errs = [j for j in out if j.get("error")]
    gone = [j for j in out if j.get("pub_age") == "GONE" or str(j.get("http", "")).startswith("4")]
    print(f"\n成功 {len(out)-len(errs)}  失败 {len(errs)}  已下架 {len(gone)}")
    print("=" * 110)
    for j in sorted(out, key=key):
        if j.get("error"):
            print(f"!! {j['company']:20} {j['title'][:50]:50} {j['error']}")
            continue
        s = j["sig"]
        print(f"\n=== {j['company'].upper()} | {j['title']}")
        print(f"    真实发布={j.get('pub_age')}d  列表 updated={j.get('updated_age', j.get('age'))}d"
              f"  http={j['http']}  {j['loc'][:56]}")
        print(f"    full-time={s['ft']}  级别词={s['level']}  年限={s['yoe'][:3]}")
        print(f"    远程口径={s['remote_kw']}")
        if s["pay"]:
            print(f"    薪资={s['pay'][:6]}")
    print(f"\n  → {args.out}")


if __name__ == "__main__":
    main()
