from __future__ import annotations

from pathlib import Path
from runner.scorer import ScenarioResult


def generate(run_id: str, results: list[ScenarioResult], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run_id}.html"
    path.write_text(_render(run_id, results), encoding="utf-8")
    return path


def _render(run_id: str, results: list[ScenarioResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    avg_score = sum(r.score for r in results) / total if total else 0.0
    run_at = results[0].run_at.strftime("%Y-%m-%d %H:%M") if results else ""

    rows = ""
    for r in results:
        verdict_class = "pass" if r.passed else ("error" if r.verdict == "error" else "fail")
        verdict_icon = "✅" if r.passed else ("💥" if r.verdict == "error" else "❌")
        score_pct = int(r.score * 100)
        bar_color = "#22c55e" if r.score >= 0.7 else ("#f59e0b" if r.score >= 0.4 else "#ef4444")
        summary = _esc(r.judge_summary)
        error = f'<div class="error-msg">{_esc(r.error)}</div>' if r.error else ""
        pr_link = (
            f' <a class="pr-link" href="{_esc(r.pr_url)}" target="_blank">↗ PR</a>'
            if r.pr_url else ""
        )
        rows += f"""
        <tr class="{verdict_class}">
          <td><strong>{_esc(r.scenario_id)}</strong>{pr_link}<br><span class="sub">{_esc(r.scenario_name)}</span></td>
          <td class="center">{verdict_icon} {r.verdict}</td>
          <td class="center">
            <div class="score-bar-wrap">
              <div class="score-bar" style="width:{score_pct}%;background:{bar_color}"></div>
            </div>
            <span class="score-num">{r.score:.2f}</span>
          </td>
          <td class="center">{r.required_found}/{r.required_total}</td>
          <td class="center">{r.false_positives}</td>
          <td class="center">{r.total_comments}</td>
          <td class="center">{r.duration_seconds:.1f}s</td>
          <td class="summary">{summary}{error}</td>
        </tr>"""

    overall_color = "#22c55e" if passed == total else ("#f59e0b" if passed > 0 else "#ef4444")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Benchmark Report — {_esc(run_id)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f8fafc; color: #1e293b; padding: 2rem; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
  .meta {{ color: #64748b; font-size: 0.875rem; margin-bottom: 2rem; }}
  .summary-cards {{ display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }}
  .card {{ background: white; border-radius: 0.75rem; padding: 1.25rem 1.75rem;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); min-width: 130px; }}
  .card .label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase;
                  letter-spacing: .05em; margin-bottom: 0.25rem; }}
  .card .value {{ font-size: 2rem; font-weight: 700; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border-radius: 0.75rem; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th {{ background: #f1f5f9; font-size: 0.75rem; text-transform: uppercase;
        letter-spacing: .05em; color: #64748b; padding: 0.75rem 1rem; text-align: left; }}
  td {{ padding: 0.875rem 1rem; border-top: 1px solid #f1f5f9; vertical-align: top;
        font-size: 0.875rem; }}
  tr.pass {{ background: #f0fdf4; }}
  tr.fail {{ background: #fff7f7; }}
  tr.error {{ background: #fff3cd; }}
  .center {{ text-align: center; vertical-align: middle; }}
  .sub {{ color: #94a3b8; font-size: 0.75rem; font-weight: 400; }}
  .score-bar-wrap {{ background: #e2e8f0; border-radius: 4px; height: 6px;
                     width: 80px; margin: 0 auto 4px; }}
  .score-bar {{ height: 6px; border-radius: 4px; }}
  .score-num {{ font-weight: 600; }}
  .summary {{ max-width: 380px; line-height: 1.5; }}
  .error-msg {{ color: #dc2626; font-size: 0.8rem; margin-top: 0.4rem; }}
  .pr-link {{ font-size: 0.75rem; font-weight: 400; color: #6366f1; text-decoration: none; margin-left: 0.4rem; }}
  .pr-link:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Benchmark Report</h1>
<div class="meta">Run: {_esc(run_id)} &nbsp;·&nbsp; {run_at}</div>

<div class="summary-cards">
  <div class="card">
    <div class="label">Passed</div>
    <div class="value" style="color:{overall_color}">{passed}/{total}</div>
  </div>
  <div class="card">
    <div class="label">Avg Score</div>
    <div class="value">{avg_score:.2f}</div>
  </div>
</div>

<table>
  <thead>
    <tr>
      <th>Scenario</th>
      <th class="center">Verdict</th>
      <th class="center">Score</th>
      <th class="center">Required</th>
      <th class="center">FP</th>
      <th class="center">Comments</th>
      <th class="center">Duration</th>
      <th>Judge summary</th>
    </tr>
  </thead>
  <tbody>{rows}
  </tbody>
</table>
</body>
</html>"""


def _esc(s: str | None) -> str:
    if not s:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))
