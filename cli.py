# -*- coding: utf-8 -*-
"""标准查新 CLI —— 供 DeepSeek++ Shell MCP 等外部工具 headless 调用。

用法（项目根目录）:
  .venv313\\Scripts\\python.exe cli.py status --json
  .venv313\\Scripts\\python.exe cli.py search "GB/T 1.1-2020" --json
  .venv313\\Scripts\\python.exe cli.py download "GB/T 1.1-2020" --json
  .venv313\\Scripts\\python.exe cli.py download "GB/T 1.1-2020" --out "D:\\电子规范" --json

进度信息写入 stderr；stdout 仅输出 JSON（--json）或人类可读文本。
退出码：0 成功，1 失败，2 需浏览器手动操作（NEED_BROWSER）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app_env  # noqa: F401  DNS 兜底 + 日志

from config import Config
from spiders.spider import SpiderEngine


def _load_version():
    try:
        with open(os.path.join(ROOT, "version.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("version", "?")
    except Exception:
        return "?"


def _python_launcher():
    """返回 shell_exec 应使用的 Python 可执行文件（venv 失效时回退当前解释器）。"""
    for venv_name in ('.venv313', '.venv'):
        venv_py = os.path.join(ROOT, venv_name, 'Scripts', 'python.exe')
        if not os.path.isfile(venv_py):
            continue
        try:
            import subprocess
            subprocess.run(
                [venv_py, '-c', 'import sys; sys.exit(0)'],
                check=True,
                capture_output=True,
                timeout=10,
            )
            return venv_py
        except Exception:
            continue
    return sys.executable


def _serialize_standard(std):
    if std is None:
        return None
    data = {
        "std_no": std.std_no,
        "std_name": std.std_name,
        "status": std.status,
        "publish_date": std.publish_date,
        "implement_date": std.implement_date,
        "download_url": std.download_url or "",
        "detail_url": getattr(std, "detail_url", "") or "",
        "source_site": std.source_site,
        "download_status": getattr(std, "download_status", ""),
        "is_local": bool(getattr(std, "is_local", False)),
        "local_file_path": getattr(std, "local_file_path", None) or "",
        "download_sources": list(getattr(std, "download_sources", None) or []),
        "is_adopted": bool(getattr(std, "is_adopted", False)),
    }
    replacement = getattr(std, "replacement", None)
    if replacement is not None:
        data["replacement"] = _serialize_standard(replacement)
    return data


def _emit_json(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _progress_handler(message):
    sys.stderr.write("%s\n" % message)
    sys.stderr.flush()


def _pick_best_result(results, keyword):
    if not results:
        return None
    key_norm = (keyword or "").replace(" ", "").upper()
    for item in results:
        if item.std_no.replace(" ", "").upper() == key_norm:
            return item
    return results[0]


def cmd_status(args):
    payload = {
        "ok": True,
        "version": _load_version(),
        "root": ROOT,
        "python": _python_launcher(),
        "download_dir": Config.effective_download_dir(),
        "lan_share_dir": (Config.LAN_SHARE_DIR or "").strip(),
        "download_sources": [s.get("name") for s in Config.DOWNLOAD_SOURCES],
    }
    if args.json:
        _emit_json(payload)
    else:
        sys.stdout.write(
            "标准查新 CLI v%s\n根目录: %s\nPython: %s\n下载目录: %s\n"
            % (payload["version"], payload["root"], payload["python"], payload["download_dir"])
        )
    return 0


def cmd_search(args):
    engine = SpiderEngine()
    engine.progress_updated.connect(_progress_handler)
    try:
        results = engine.search_standard(
            args.keyword,
            check_download=not args.no_download_check,
        )
    except Exception as e:
        payload = {
            "ok": False,
            "error": "%s: %s" % (type(e).__name__, e),
            "trace": traceback.format_exc(limit=5),
        }
        if args.json:
            _emit_json(payload)
        else:
            sys.stderr.write(payload["error"] + "\n")
        return 1

    best = _pick_best_result(results, args.keyword)
    payload = {
        "ok": bool(results),
        "keyword": args.keyword,
        "count": len(results),
        "results": [_serialize_standard(r) for r in results],
        "best": _serialize_standard(best),
    }
    if args.json:
        _emit_json(payload)
    elif best:
        sys.stdout.write(
            "%s | %s | %s | 下载: %s\n"
            % (best.std_no, best.std_name, best.status, best.download_status)
        )
    else:
        sys.stdout.write("未找到结果\n")
        return 1
    return 0


def cmd_download(args):
    out_dir = (args.out or "").strip()
    if out_dir:
        Config.DOWNLOAD_DIR = out_dir
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    engine = SpiderEngine()
    engine.download_dir = Path(Config.effective_download_dir())
    engine.download_dir.mkdir(parents=True, exist_ok=True)
    engine.progress_updated.connect(_progress_handler)

    try:
        results = engine.search_standard(args.keyword, check_download=True)
    except Exception as e:
        payload = {"ok": False, "stage": "search", "error": str(e)}
        if args.json:
            _emit_json(payload)
        else:
            sys.stderr.write("检索失败: %s\n" % e)
        return 1

    std = _pick_best_result(results, args.keyword)
    if std is None:
        payload = {"ok": False, "stage": "search", "error": "未找到标准"}
        if args.json:
            _emit_json(payload)
        else:
            sys.stderr.write("未找到标准\n")
        return 1

    if std.download_status not in ("可下载",):
        payload = {
            "ok": False,
            "stage": "probe",
            "error": "当前标记为不可下载: %s" % std.download_status,
            "best": _serialize_standard(std),
        }
        if args.json:
            _emit_json(payload)
        else:
            sys.stderr.write(payload["error"] + "\n")
        return 1

    success, message = engine.download_pdf(std)
    payload = {
        "ok": bool(success),
        "message": message,
        "best": _serialize_standard(std),
        "download_dir": str(engine.download_dir),
    }
    if success:
        if message.startswith("LOCAL:"):
            payload["local_copy"] = True
            payload["file_path"] = message.split("LOCAL:", 1)[1].strip()
        else:
            payload["local_copy"] = False
            payload["file_path"] = message if os.path.isfile(message) else str(
                engine.download_dir / ("%s.pdf" % std.std_no)
            )

    if args.json:
        _emit_json(payload)
    elif success:
        sys.stdout.write("下载成功: %s\n" % payload.get("file_path", message))
    else:
        sys.stderr.write("下载失败: %s\n" % message)

    if success:
        return 0
    if isinstance(message, str) and message.startswith("NEED_BROWSER:"):
        return 2
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="标准查新 CLI（DeepSeek++ Shell MCP 对接）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="检查环境与路径")
    p_status.add_argument("--json", action="store_true", help="JSON 输出到 stdout")

    p_search = sub.add_parser("search", help="检索标准（默认检测下载源）")
    p_search.add_argument("keyword", help="标准编号或名称")
    p_search.add_argument("--no-download-check", action="store_true", help="跳过下载源探测")
    p_search.add_argument("--json", action="store_true")

    p_dl = sub.add_parser("download", help="检索并下载 PDF")
    p_dl.add_argument("keyword", help="标准编号或名称")
    p_dl.add_argument("--out", help="覆盖下载目录（默认读 settings.json 或 电子规范）")
    p_dl.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "search":
        return cmd_search(args)
    if args.command == "download":
        return cmd_download(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
