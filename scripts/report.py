#!/usr/bin/env python3
"""Render the allowlisted experiment metadata as standalone HTML; no external assets."""
import argparse
import collections
import html
import json
import pathlib


def render(data):
    rows = data["observations"]
    groups = collections.Counter((r["phase"], r["status"]) for r in rows)
    table = "".join("<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(html.escape(str(phase)), status, count)
                    for (phase, status), count in groups.items())
    passed = all(r["passed"] for r in rows)
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>ContextFence 本地实验报告</title><style>
body{font:16px/1.6 system-ui,sans-serif;margin:0;background:#101923;color:#dfe8f0}main{max-width:960px;margin:40px auto;padding:24px}
h1{font-size:38px;margin-bottom:4px}p{color:#b7c6d3}.cards{display:flex;gap:18px;flex-wrap:wrap}.card{background:#1c2a38;padding:18px 28px;border-radius:12px;min-width:160px}.number{font-size:36px;color:#62d8bb}
table{width:100%%;border-collapse:collapse;margin-top:28px}th,td{text-align:left;padding:10px;border-bottom:1px solid #324454}th{color:#62d8bb}code{color:#f4c379}footer{margin-top:24px;color:#b7c6d3}</style>
<main><h1>ContextFence</h1><p>上下文失效控制 · 双实例 HTTP 实验</p><p>记录时间：%s</p>
<div class="cards"><div class="card">实际 HTTP 样本<div class="number">%d</div></div><div class="card">撤权确认后拒绝<div class="number">%d / 50</div></div><div class="card">断言结果<div class="number">%s</div></div></div>
<p>两实例分别连续读取两次后，由 B 确认撤权，再向 A/B 并发发起 50 次取用。正文缓存的实际命中计数另由集成测试验证。</p>
<table><thead><tr><th>实验阶段</th><th>HTTP 状态</th><th>样本数</th></tr></thead><tbody>%s</tbody></table>
<footer>此报告仅含合成实验的状态与计数，无正文或凭据。延迟仅为本机观测，不代表生产吞吐。先获锁的重叠请求可能完成；已发送数据无法撤回。数据库断连、锁顺序和进程终止的证据见自动化测试报告。</footer></main></html>""" % (
        html.escape(data["recorded_at"]), len(rows), sum(r["phase"] == "after-revoke" and r["status"] == 403 for r in rows),
        "PASS" if passed else "FAIL", table)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="artifacts/local/demo.json")
    parser.add_argument("--output", default="artifacts/local/report.html")
    args = parser.parse_args()
    data = json.loads(pathlib.Path(args.input).read_text(encoding="utf-8"))
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(data), encoding="utf-8")
    print("Report: " + str(output))


if __name__ == "__main__":
    main()
