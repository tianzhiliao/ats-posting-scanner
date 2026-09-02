#!/usr/bin/env python3
"""Agent 的工具面 —— 把 ats/ 的确定性函数包成 Claude 可调用的工具。

设计要点全在**返回值的形状**上，不在功能上：

一次全量扫描是 947 条标题命中。原样塞进上下文是几十万 token，跑三轮就把 1M 窗口填满，
而且 agent 会淹死在细节里做不出判断。所以每个工具都返回**统计量 + 摘要 + 短 id**，
全量数据落盘，agent 想看细节再按 id 取。

这是 agent 工具设计里最关键的一条：**工具的职责是给 agent 可判断的输入，不是倾倒数据。**
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ats"))
import ats as A
from anthropic import beta_tool

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
ATS_DIR = os.path.join(HERE, "..", "ats")

# 全量数据放这儿，上下文里只走 id。
STATE = {"jobs": {}, "enriched": {}, "probed": {}}


def _jid(rec, n):
    return f"{rec['ats'][:2]}-{rec['company']}-{n}"


def _save():
    os.makedirs(OUT, exist_ok=True)
    json.dump(STATE["jobs"], open(os.path.join(OUT, "agent_jobs.json"), "w"),
              indent=1, ensure_ascii=False)
    json.dump(STATE["enriched"], open(os.path.join(OUT, "agent_enriched.json"), "w"),
              indent=1, ensure_ascii=False)


# ------------------------------------------------------------------ 工具


@beta_tool
def list_known_companies(ats: str = "all") -> str:
    """列出 companies.txt 里已收录的公司 slug 和数量。

    开工前先看这个，知道基线覆盖了哪些公司，才谈得上"该往哪扩"。

    Args:
        ats: ashby / lever / greenhouse / all。默认 all。
    """
    targets = A.load_companies(os.path.join(ATS_DIR, "companies.txt"))
    by = {}
    for a, s in targets:
        by.setdefault(a, []).append(s)
    out = []
    for a in sorted(by):
        if ats not in ("all", a):
            continue
        out.append(f"[{a}] {len(by[a])} 个：" + " ".join(sorted(by[a])))
    return "\n\n".join(out) if out else "（无）"


@beta_tool
def probe_slugs(ats: str, slugs: str) -> str:
    """探测一批公司 slug 在指定 ATS 上有没有公开看板。

    这是扩表用的廉价探针：只看看板在不在、有多少岗位，不拉岗位内容。
    slug 猜错返回 404 是常态，命中率 50% 左右就算正常。

    ⚠️ 如果**全部**返回失败（命中率 0%），先怀疑是网络/环境故障，不要当成
    "这批公司都没有看板"。用一个已知一定活着的 slug（如 greenhouse 的 gitlab、
    ashby 的 cohere）复验一次再下结论。

    Args:
        ats: ashby / lever / greenhouse。
        slugs: 空格或逗号分隔的 slug，一次最多 80 个。
    """
    lst = [s.strip() for s in slugs.replace(",", " ").split() if s.strip()][:80]
    if ats not in A.SCANNERS:
        return f"错误：ats 必须是 ashby / lever / greenhouse，收到 {ats!r}"
    _, log = A.scan_boards([(ats, s) for s in lst], title_re=A.TITLE_RE, workers=30)
    live = sorted((b[1], b[3]) for b in log if b[2] == "OK")
    dead = sorted(b[1] for b in log if b[2] != "OK")
    for s, n in live:
        STATE["probed"][f"{ats}:{s}"] = n
    return (f"探测 {len(lst)} 个，命中 {len(live)}（{len(live)/max(1,len(lst)):.0%}）\n"
            f"命中（slug=岗位总数）：{', '.join(f'{s}={n}' for s, n in live) or '无'}\n"
            f"未命中：{' '.join(dead) or '无'}")


@beta_tool
def scan_boards(ats: str, slugs: str, all_titles: bool = False) -> str:
    """扫看板并按 AI 工程岗标题正则筛选，结果存入内部状态并分配短 id。

    返回统计 + 命中岗位的一行摘要。摘要里的时间是**列表接口的时间**，
    Greenhouse 那边是 updated_at，**不可信**（见 enrich_jobs）。

    Args:
        ats: ashby / lever / greenhouse。
        slugs: 空格或逗号分隔的 slug，一次最多 80 个。留空则扫该 ATS 全部已知公司。
        all_titles: True 则不用标题正则，收下全部岗位（慎用，量很大）。
    """
    if ats not in A.SCANNERS:
        return f"错误：ats 必须是 ashby / lever / greenhouse，收到 {ats!r}"
    if slugs.strip():
        lst = [s.strip() for s in slugs.replace(",", " ").split() if s.strip()][:80]
    else:
        lst = [s for a, s in A.load_companies(os.path.join(ATS_DIR, "companies.txt")) if a == ats]

    rows, log = A.scan_boards([(ats, s) for s in lst],
                              title_re=None if all_titles else A.TITLE_RE, workers=30)
    live = [b for b in log if b[2] == "OK"]
    total_open = sum(b[3] for b in live if isinstance(b[3], int))

    n = len(STATE["jobs"])
    added = []
    for r in rows:
        n += 1
        i = _jid(r, n)
        STATE["jobs"][i] = r
        added.append((i, r))
    _save()

    head = (f"扫 {len(lst)} 个看板 → 在线 {len(live)}（{len(live)/max(1,len(lst)):.0%}）"
            f"，在挂岗位合计 {total_open}，标题命中 {len(rows)} 条\n")
    if not rows:
        return head + "（无命中）"

    ca = sum(1 for _, r in added if r["ca"] or r["na"])
    jr = sum(1 for _, r in added if not r["senior"])
    head += f"其中 加拿大/泛北美地点 {ca} 条，非资深标题 {jr} 条\n\n"

    shown = sorted(added, key=lambda x: (x[1]["age"] is None, x[1]["age"]))[:50]
    lines = [f"{i}  [{r['age']}d] {r['company']:<16} {r['title'][:50]:<50} | {r['loc'][:44]}"
             for i, r in shown]
    tail = f"\n（只列前 50 条，共 {len(rows)} 条，全部已存入状态）" if len(rows) > 50 else ""
    return head + "\n".join(lines) + tail


@beta_tool
def filter_jobs(max_age_days: float = 45, canada_only: bool = True,
                exclude_senior: bool = True) -> str:
    """对已扫描的全部岗位做筛选，返回符合条件的 id 清单。

    注意这里用的还是列表接口的时间，只能当粗筛。真正的年龄判断要 enrich_jobs。

    Args:
        max_age_days: 距今天数上限（按列表时间）。
        canada_only: 只保留加拿大或泛北美地点。
        exclude_senior: 排除标题含 senior/staff/lead/principal 等资深标识的。
    """
    out = []
    for i, r in STATE["jobs"].items():
        if canada_only and not (r["ca"] or r["na"]):
            continue
        if r["age"] is None or r["age"] > max_age_days:
            continue
        if exclude_senior and r["senior"]:
            continue
        out.append((i, r))
    out.sort(key=lambda x: x[1]["age"])
    if not out:
        return f"0 条符合（库里共 {len(STATE['jobs'])} 条）"
    lines = [f"{i}  [{r['age']}d] {r['ats']:<10} {r['company']:<16} {r['title'][:48]:<48} | {r['loc'][:40]}"
             for i, r in out]
    return f"{len(out)} 条符合（库里共 {len(STATE['jobs'])} 条）\n\n" + "\n".join(lines)


@beta_tool
def enrich_jobs(job_ids: str) -> str:
    """取真实发布日、JD 信号、存活性。**出清单前必须对入选岗位跑这一步。**

    为什么必须：Greenhouse 列表接口只给 updated_at（雇主上次改动），挂了两年的岗位
    可以显示成"25 天前"。实测 28 条候选里 15 条年龄被低估 3 倍以上，最极端的
    差了 29 倍。真实发布日只在详情接口的 first_published 里。

    返回每条的：真实发布天数 / 列表天数 / HTTP 码 / 远程口径 / 年限 / 薪资。
    HTTP 4xx 表示岗位已下架，必须剔除。

    Args:
        job_ids: 空格或逗号分隔的短 id（来自 scan_boards / filter_jobs），一次最多 40 个。
    """
    sys.path.insert(0, ATS_DIR)
    import enrich as E

    ids = [s.strip() for s in job_ids.replace(",", " ").split() if s.strip()][:40]
    miss = [i for i in ids if i not in STATE["jobs"]]
    ids = [i for i in ids if i in STATE["jobs"]]
    if not ids:
        return f"没有可用 id。未知 id：{miss}"

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda i: (i, E.enrich_one(STATE["jobs"][i])), ids))
    for i, j in res:
        STATE["enriched"][i] = j
    _save()

    def k(x):
        v = x[1].get("pub_age")
        return v if isinstance(v, (int, float)) else 9999

    lines = []
    for i, j in sorted(res, key=k):
        if j.get("error"):
            lines.append(f"{i}  ❌ {j['error']}")
            continue
        s, p = j["sig"], j.get("pub_age")
        u = j.get("updated_age") or j.get("age")
        gap = f" ⚠️低估{p/u:.0f}倍" if isinstance(p, (int, float)) and u and p / u >= 3 else ""
        dead = " ☠️已下架" if str(j.get("http", "")).startswith("4") else ""
        lines.append(
            f"{i}  {j['company']} | {j['title'][:44]}\n"
            f"     真实发布={p}d 列表={u}d http={j['http']}{gap}{dead}\n"
            f"     远程={s['remote_kw']} 年限={s['yoe'][:2]} 级别={s['level']} 薪资={s['pay'][:4]}")
    warn = f"\n\n（未知 id 已忽略：{miss}）" if miss else ""
    return "\n".join(lines) + warn


@beta_tool
def read_jd(job_id: str, offset: int = 0) -> str:
    """读某条岗位的 JD 正文片段。判断级别门槛、技术栈、加拿大是否真的可投时用。

    Args:
        job_id: 短 id。必须先 enrich_jobs 过。
        offset: 从第几个字符开始读，每次返回 3000 字符。
    """
    j = STATE["enriched"].get(job_id)
    if not j:
        return f"{job_id} 还没 enrich 过，先调 enrich_jobs。"
    txt = j.get("excerpt", "")
    seg = txt[offset:offset + 3000]
    more = f"\n\n（还有 {len(txt)-offset-len(seg)} 字符，用 offset={offset+3000} 继续）" if len(txt) > offset + 3000 else ""
    return f"{j['company']} | {j['title']}\nURL: {j['url']}\n\n{seg}{more}"


@beta_tool
def add_companies(ats: str, slugs: str) -> str:
    """把新发现的、确认有看板的公司 slug 追加进 companies.txt，供以后复用。

    只加已经 probe_slugs 验证过在线的，不要把猜测写进去。

    Args:
        ats: ashby / lever / greenhouse。
        slugs: 空格或逗号分隔的 slug。
    """
    path = os.path.join(ATS_DIR, "companies.txt")
    known = {s for a, s in A.load_companies(path) if a == ats}
    lst = [s.strip() for s in slugs.replace(",", " ").split() if s.strip()]
    new = [s for s in lst if s not in known]
    unverified = [s for s in new if f"{ats}:{s}" not in STATE["probed"]]
    if unverified:
        return f"拒绝写入：这些 slug 还没经 probe_slugs 验证在线 —— {unverified}"
    if not new:
        return "没有新 slug（都已在表内）"
    txt = open(path).read()
    marker = f"[{ats}]"
    i = txt.index(marker)
    j = txt.index("\n", i) + 1
    txt = txt[:j] + "  " + "  ".join(new) + f"   # agent 追加\n" + txt[j:]
    open(path, "w").write(txt)
    return f"已追加 {len(new)} 个到 [{ats}]：{' '.join(new)}"


@beta_tool
def save_report(filename: str, markdown: str) -> str:
    """把最终岗位清单写成 markdown 报告落盘。这是任务的交付物。

    Args:
        filename: 文件名，如 加拿大远程-AI岗位-2026-08-28.md。写到项目根目录。
        markdown: 报告全文。
    """
    root = os.path.join(HERE, "..", "..")
    safe = os.path.basename(filename)
    if not safe.endswith(".md"):
        safe += ".md"
    p = os.path.abspath(os.path.join(root, safe))
    open(p, "w").write(markdown)
    return f"已写入 {p}（{len(markdown)} 字符）"


ALL_TOOLS = [list_known_companies, probe_slugs, scan_boards, filter_jobs,
             enrich_jobs, read_jd, add_companies, save_report]
