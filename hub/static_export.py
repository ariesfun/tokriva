"""只读快照导出器：把控制台的只读接口烤成一份静态 JS，供纯静态 / GitHub Pages 部署。

动机
----
参考仓库 `mohabdelkarim/free-model-pulse` 是「零服务器」形态：GH Actions 定时探测
→ 提交静态产物 → Pages 托管。我们的控制台是「FastAPI + SQLite + 写操作」，
本模块只承担其中「产物静态化」那一半：把前端需要的只读接口整包导出，
静态页开只读模式后即可脱网运行（无服务端、无数据库、无写操作）。

用法
----
    python -m hub.static_export                     # → dist/（自包含静态站点）
    python -m hub.static_export --out dist          # 指定输出目录
    python -m hub.static_export --probes 500        # 只带最近 500 条探测
    python -m hub.static_export --excluded full     # 保留 excluded 诊断清单
    python -m hub.static_export --window 24,72      # 导出这些 overview 窗口

输出目录里是 `web/` 的副本 + 生成的 `snapshot.js`，index.html 已挂上快照脚本，
目录本身即可直接交给 GitHub Pages 托管（不需要服务端）。前端 app.js 检测到
`window.SNAPSHOT` 后进入只读模式：数据全部取自快照，写操作入口隐藏。

产物形状（前端适配层按 key 取用，与 /api 同名）
----
    window.SNAPSHOT = {
      generatedAt, source, readOnly: true, errors: [...],
      data: {
        health, endpoints, probes,
        rankings: { speed: {...}, agent: {...}, stable: {...} },
        reminders, catalog,
        overview: { "24": {...} },
        settings
      }
    }

约束
----
- **只读**：仅调用 `hub.api` 的 GET 语义函数，绝不写库、绝不触发探测。
- 直调函数而非 HTTP（已验证可行），因此无需先起 8766 服务。
- `excluded` 默认压缩为 `excludedSummary`（见 `_summarize_excluded`），
  因为实测它在三份榜单里合计占 207 KB / 全量 242 KB，而前端只用它的
  「接口 × 原因」聚合结果。`--excluded full` 可退回原样。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DEFAULT_OUT = ROOT / "dist"
SCENES = ("speed", "agent", "stable")
# 静态站点要一并带上的前端资源（快照脚本由本模块生成，不在此列）
SITE_ASSETS = ("index.html", "app.js", "styles.css", "data.js")


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def _collect(probes_limit: int, windows: list[int], excluded_mode: str,
             ) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """调用只读接口整包取数。单个接口失败不致命，记入 errors 后继续。"""
    import hub.api as api

    errors: list[dict[str, str]] = []
    data: dict[str, Any] = {}

    def take(key: str, fn: Callable[[], Any]) -> None:
        try:
            data[key] = fn()
        except Exception as exc:  # noqa: BLE001 — 导出器要尽力而为，不能因单点失败整包报废
            errors.append({"key": key, "error": f"{type(exc).__name__}: {exc}"})

    take("health", api.health)
    take("endpoints", api.endpoints)
    # probes 的 level/limit 是 FastAPI Query 默认值，直调时必须全部显式传参
    take("probes", lambda: api.probes(
        level=None, status=None, endpoint=None, q=None,
        limit=probes_limit, offset=0,
    ))
    take("reminders", api.reminders)
    take("catalog", api.catalog)
    take("settings", api.get_settings)
    take("schedule", api.schedule_status)
    # 下发预览：仅只读展示「将会写入网关的 YAML」，静态页不可 apply
    take("config", lambda: api.config_preview(scene="agent"))

    rankings: dict[str, Any] = {}
    for scene in SCENES:
        try:
            payload = api.rankings(scene=scene)
            if excluded_mode == "summary":
                payload = _summarize_excluded(payload)
            rankings[scene] = payload
        except Exception as exc:  # noqa: BLE001
            errors.append({"key": f"rankings:{scene}", "error": f"{type(exc).__name__}: {exc}"})
    data["rankings"] = rankings

    overview: dict[str, Any] = {}
    for hours in windows:
        try:
            overview[str(hours)] = api.overview(window_hours=hours)
        except Exception as exc:  # noqa: BLE001
            errors.append({"key": f"overview:{hours}", "error": f"{type(exc).__name__}: {exc}"})
    data["overview"] = overview

    return data, errors


def _summarize_excluded(payload: Any) -> Any:
    """把 `excluded`（逐模型的被过滤清单）压成按「接口 × 原因」聚合的摘要。

    前端 `rankings` 视图只用 `endpoint` 与 `reason` 做聚合展示
    （见 web/app.js 的 `byEp`），逐条明细属于诊断用途，静态页不必背负。
    原字段是 list[dict]；这里替换为等价的：
        excludedSummary = [{endpoint, total, reasons: [{reason, count}]}]
    """
    if not isinstance(payload, dict):
        return payload
    rows = payload.get("excluded")
    if not isinstance(rows, list):
        return payload

    agg: dict[str, dict[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ep = row.get("endpoint") or "—"
        reason = row.get("reason") or "数据不足"
        bucket = agg.setdefault(ep, {})
        bucket[reason] = bucket.get(reason, 0) + 1

    summary = [
        {
            "endpoint": ep,
            "total": sum(reasons.values()),
            "reasons": [
                {"reason": r, "count": n}
                for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])
            ],
        }
        for ep, reasons in sorted(agg.items(), key=lambda kv: -sum(kv[1].values()))
    ]

    out = {k: v for k, v in payload.items() if k != "excluded"}
    out["excludedCount"] = len(rows)
    out["excludedSummary"] = summary
    return out


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def build_snapshot(probes_limit: int = 2000, windows: list[int] | None = None,
                   excluded_mode: str = "summary") -> dict[str, Any]:
    data, errors = _collect(probes_limit, windows or [24], excluded_mode)
    return {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "source": "hub.db（只读快照）",
        "readOnly": True,
        "schema": 1,
        "errors": errors,
        "data": data,
    }


def build_site(snapshot: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """把前端资源 + 快照写成一个**自包含目录**，可直接交给 GitHub Pages 托管。

    刻意写到 `dist/` 而不是 `web/`：`web/` 是同源服务正在托管的活动资源，
    若把 snapshot.js 放进去，本机跑服务时也会命中只读模式，正常控制台就废了。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for name in SITE_ASSETS:
        src = WEB / name
        if src.exists():
            shutil.copy2(src, out_dir / name)
            copied.append(name)

    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    snap_path = out_dir / "snapshot.js"
    snap_path.write_text(
        "// 由 hub/static_export.py 生成（只读快照）。请勿手工编辑。\n"
        f"// 生成时间：{snapshot['generatedAt']}\n"
        "window.SNAPSHOT = " + body + ";\n",
        encoding="utf-8",
    )

    # 在副本的 index.html 里挂上快照脚本：必须排在 app.js 之前
    idx = out_dir / "index.html"
    if idx.exists():
        html = idx.read_text(encoding="utf-8")
        anchor = '<script src="app.js"></script>'
        if "snapshot.js" not in html and anchor in html:
            html = html.replace(
                anchor, '<script src="snapshot.js"></script>\n  ' + anchor, 1
            )
            idx.write_text(html, encoding="utf-8")
            copied.append("index.html（已注入 snapshot 脚本）")

    import gzip
    return {
        "out_dir": out_dir,
        "copied": copied,
        "raw_bytes": snap_path.stat().st_size,
        "gzip_bytes": len(gzip.compress(body.encode("utf-8"))),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m hub.static_export",
        description="把控制台只读接口烤成一个自包含静态站点（默认 dist/）。",
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"输出目录（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    ap.add_argument("--probes", type=int, default=2000,
                    help="随包携带的探测记录条数，最多 2000（默认 2000）")
    ap.add_argument("--window", default="24",
                    help="overview 统计窗口，小时，逗号分隔（默认 24）")
    ap.add_argument("--excluded", choices=("summary", "full"), default="summary",
                    help="excluded 明细：summary=压成聚合摘要（默认），full=原样保留")
    ap.add_argument("--quiet", action="store_true", help="只输出一行结果")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    windows = [int(w) for w in str(args.window).split(",") if w.strip()]
    probes_limit = max(1, min(2000, args.probes))

    snapshot = build_snapshot(
        probes_limit=probes_limit,
        windows=windows,
        excluded_mode=args.excluded,
    )
    site = build_site(snapshot, args.out)
    raw, gz = site["raw_bytes"], site["gzip_bytes"]

    data = snapshot["data"]
    ranks = data.get("rankings", {})
    probes = data.get("probes", {}) or {}
    if not args.quiet:
        print(f"已生成静态站点 {site['out_dir']}")
        print(f"  生成时间  {snapshot['generatedAt']}")
        print(f"  探测记录  {probes.get('total', '—')} 条（随包 {len(probes.get('items', []) or [])} 条）")
        print(f"  榜单      " + "  ".join(
            f"{s}:{len((ranks.get(s) or {}).get('items', []) or [])} 项" for s in SCENES
        ))
        print(f"  端点      {len((data.get('endpoints') or {}).get('items', []) or [])} 个")
        print(f"  文件      " + "、".join(site["copied"]))
        print(f"  体量      {raw:,} B（未压缩） / {gz:,} B（gzip）")
    else:
        print(f"{site['out_dir']}  {raw:,} B / gzip {gz:,} B")

    if snapshot["errors"]:
        print("\n⚠️ 以下接口导出失败（其余已正常写出）：", file=sys.stderr)
        for e in snapshot["errors"]:
            print(f"  - {e['key']}: {e['error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
