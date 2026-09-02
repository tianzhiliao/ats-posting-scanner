#!/usr/bin/env python3
"""阶段 3 —— 抓申请表单的字段定义。

投递前要知道这家公司到底问哪些问题：几个必填、有没有论述题、下拉框有哪些选项、
工作许可怎么问。手动点开表单一个个抄很慢而且会漏，这些定义在接口里是现成的。

    python3 ats/forms.py https://jobs.ashbyhq.com/cohere/3fe03041-...
    python3 ats/forms.py https://job-boards.greenhouse.io/gitlab/jobs/8698314002 \
        --json applications/03-gitlab-.../inputs/form-fields.json

--json 落盘的是 ATS 的原始数组，跟 applications/*/inputs/form-fields.json 现有格式一致。

三家的可得性差很多：

  Greenhouse  ✅ 官方支持。详情接口加 ?questions=true 就返回完整 questions[]。
  Ashby       ✅ 要走它前端自己用的 GraphQL 端点 non-user-graphql（公开，无需鉴权）。
                 posting-api 那个官方只读接口不含表单，只有 JD。
  Lever       ⚠️ 公开 API 不暴露表单定义。只能退回去解析申请页 HTML 的 input name。
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ats as A

ASHBY_GQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"

# 只要 applicationForm。整个 jobPosting 对象很大，多要字段反而更容易被 schema 变更打断。
#
# 字段名只能靠试 —— Ashby 关掉了 GraphQL introspection，报错信息里的建议也被隐藏了
# （"[Suggestion hidden]"）。实测：descriptionHtml ✅ / descriptionPlain ❌ / description ❌。
# 猜错任何一个字段名，整个查询会以 GRAPHQL_VALIDATION_FAILED 整体失败，不是部分降级。
ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    applicationForm { fieldEntries { field isRequired descriptionHtml } }
  }
}"""


def parse_url(url):
    """从投递链接里拆出 (ats, org, job_id)。"""
    u = url.strip()
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", u)
    if m:
        return "ashby", m.group(1), m.group(2)
    m = re.search(r"(?:job-boards|boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)", u)
    if m:
        return "greenhouse", m.group(1), m.group(2)
    m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", u)
    if m:
        return "lever", m.group(1), m.group(2)
    raise SystemExit(f"认不出这个 URL 属于哪家 ATS：{url}")


def ashby_form(org, posting_id):
    body = json.dumps({
        "operationName": "ApiJobPosting",
        "variables": {"organizationHostedJobsPageName": org, "jobPostingId": posting_id},
        "query": ASHBY_QUERY,
    })
    d = A.fetch_json(ASHBY_GQL, data=body, headers={"Content-Type": "application/json"})
    if d.get("errors"):
        raise SystemExit("Ashby GraphQL 报错：" + json.dumps(d["errors"])[:300])
    return (((d.get("data") or {}).get("jobPosting") or {}).get("applicationForm") or {}).get("fieldEntries") or []


def greenhouse_form(slug, job_id):
    return A.greenhouse_job(slug, job_id, questions=True).get("questions") or []


def lever_form(org, job_id):
    """公开 API 没有表单定义，退回去扒申请页的 input/textarea name。"""
    html = A.fetch(f"https://jobs.lever.co/{org}/{job_id}/apply")
    names = re.findall(r'name="([^"]+)"[^>]*(?:\s(required))?', html)
    seen, out = set(), []
    for n, req in names:
        if n in seen or n.startswith("_"):
            continue
        seen.add(n)
        out.append({"label": n, "required": bool(req), "fields": [{"name": n, "type": "html-scraped"}]})
    return out


def show_ashby(entries):
    print(f"Ashby 表单字段数：{len(entries)}")
    print("=" * 95)
    for e in entries:
        f = e.get("field") or {}
        req = "必填" if e.get("isRequired") else "选填"
        title = f.get("title") or f.get("humanReadablePath") or f.get("path") or ""
        print(f"[{req}] {title[:62]:62} | {f.get('type')}")
        opts = (f.get("metadata") or {}).get("options") or f.get("selectableValues")
        if opts:
            print(f"        选项: {str(opts)[:170]}")
        if e.get("descriptionHtml"):
            print(f"        说明: {A.strip_html(e['descriptionHtml'])[:150]}")


def show_greenhouse(questions):
    print(f"Greenhouse 表单问题数：{len(questions)}")
    print("=" * 95)
    for q in questions:
        req = "必填" if q.get("required") else "选填"
        fs = q.get("fields") or [{}]
        print(f"[{req}] {(q.get('label') or '')[:62]:62} | {fs[0].get('type')}")
        vals = fs[0].get("values") or []
        if vals:
            print(f"        选项: {str([v.get('label') for v in vals])[:170]}")
        if q.get("description"):
            print(f"        说明: {A.strip_html(q['description'])[:150]}")


def main():
    ap = argparse.ArgumentParser(description="抓 ATS 申请表单的字段定义")
    ap.add_argument("url", help="投递链接（Ashby / Greenhouse / Lever）")
    ap.add_argument("--json", dest="dump", default="", help="把原始数组写到这个路径")
    args = ap.parse_args()

    which, org, jid = parse_url(args.url)
    print(f"ATS={which}  org={org}  id={jid}\n", file=sys.stderr)

    if which == "ashby":
        data = ashby_form(org, jid)
        show_ashby(data)
    elif which == "greenhouse":
        data = greenhouse_form(org, jid)
        show_greenhouse(data)
    else:
        data = lever_form(org, jid)
        print("⚠️ Lever 公开 API 不给表单定义，以下是从申请页 HTML 扒的 input name，可能不全：")
        for q in data:
            print(f"  {q['label']}")

    n_req = sum(1 for x in data if x.get("isRequired") or x.get("required"))
    print(f"\n合计 {len(data)} 个字段，其中必填 {n_req}")

    if args.dump:
        os.makedirs(os.path.dirname(os.path.abspath(args.dump)), exist_ok=True)
        json.dump(data, open(args.dump, "w"), indent=1, ensure_ascii=False)
        print(f"  → {args.dump}")


if __name__ == "__main__":
    main()
