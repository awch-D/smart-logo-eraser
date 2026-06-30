#!/usr/bin/env python3
"""按目录批量擦除一组图里的 logo。

逻辑：
- 扫描 root 下所有含目标图（默认 main_* 文件名）的目录
- 每个目录一次性上传给本地服务，跑 process 拿到 cleaned
- 输出落地到该目录同级的 images_cleaned/
- 已存在的 cleaned 跳过（支持断点续跑）
- 失败列表在结束时汇总

用法：
    # 先启动桌面端，确保模板库里已经有 logo 模板
    # 然后跑：
    python scripts/batch_clean.py /path/to/images_root \\
        --api http://127.0.0.1:PORT \\
        --template-id <tpl_id>

    # dry-run 看会处理多少张
    python scripts/batch_clean.py /path/to/images_root \\
        --api http://127.0.0.1:PORT --template-id <id> --dry-run

    # 自定义匹配文件名（默认 ^main[_-].*\\.(jpe?g|png|webp)$）
    python scripts/batch_clean.py /path/to/images_root \\
        --api ... --template-id ... \\
        --pattern '.*_logo\\.png$'
"""
import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_API = "http://127.0.0.1:8000"
DEFAULT_PATTERN = r"^main[_\-].*\.(jpe?g|png|webp)$"

# 这三个变量由 main() 在解析 CLI 参数后注入
API = DEFAULT_API
TEMPLATE_ID = ""
MAIN_PATTERN = re.compile(DEFAULT_PATTERN, re.IGNORECASE)


def http_get(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def http_post_json(url: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_multipart(url: str, files: list[Path], timeout: int = 300) -> dict:
    """手写 multipart/form-data，避免引入 requests 依赖。"""
    boundary = f"----jimuBoundary{int(time.time()*1000)}"
    body = bytearray()
    for fp in files:
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="files"; filename="{fp.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        body += fp.read_bytes()
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_image_dirs(root: Path) -> list[Path]:
    """返回所有含 main_*.jpg 的目录。"""
    seen = set()
    for f in root.rglob("*"):
        if f.is_file() and MAIN_PATTERN.match(f.name):
            seen.add(f.parent)
    return sorted(seen)


def process_one_dir(src_dir: Path, dry_run: bool = False) -> dict:
    """处理单个目录，返回统计 dict。"""
    main_files = sorted(
        [f for f in src_dir.iterdir() if f.is_file() and MAIN_PATTERN.match(f.name)]
    )
    if not main_files:
        return {"dir": str(src_dir), "skipped": True, "reason": "no main_*"}

    out_dir = src_dir.parent / "images_cleaned"
    out_dir.mkdir(exist_ok=True)

    # 断点续跑：已处理过的跳过
    pending = [f for f in main_files if not (out_dir / f.name).exists()]
    already = len(main_files) - len(pending)
    if not pending:
        return {
            "dir": str(src_dir),
            "total": len(main_files),
            "already": already,
            "ok": 0,
            "fail": 0,
            "skipped_all": True,
        }

    if dry_run:
        return {
            "dir": str(src_dir),
            "total": len(main_files),
            "already": already,
            "pending": len(pending),
            "dry_run": True,
        }

    # 1) 上传
    upload = http_post_multipart(f"{API}/api/upload-batch", pending)
    batch_id = upload["batch_id"]
    name_map = {f["name"]: f["orig"] for f in upload["files"]}

    # 2) 处理
    proc = http_post_json(
        f"{API}/api/process",
        {
            "batch_id": batch_id,
            "template_ids": [TEMPLATE_ID],
            "mode": "fill",
            "min_score": 0.55,
            # 让服务端用我们新设的对称默认 padding
        },
        timeout=180,
    )

    ok = 0
    fail = []
    for r in proc.get("results", []):
        orig_name = name_map.get(r["file"], r["file"])
        if r.get("status") == "ok" and r.get("output"):
            try:
                data = http_get(f"{API}/api/result/{batch_id}/{r['output']}")
                (out_dir / orig_name).write_bytes(data)
                ok += 1
            except Exception as e:
                fail.append({"file": orig_name, "reason": f"download: {e}"})
        else:
            fail.append(
                {
                    "file": orig_name,
                    "reason": f"status={r.get('status')} score={r.get('score')}",
                }
            )

    return {
        "dir": str(src_dir),
        "total": len(main_files),
        "already": already,
        "ok": ok,
        "fail_count": len(fail),
        "fails": fail,
    }


def main():
    ap = argparse.ArgumentParser(
        description="按目录批量擦除 logo（用本地 smart-logo-eraser 服务）"
    )
    ap.add_argument("root", help="要扫描的根目录")
    ap.add_argument("--api", default=DEFAULT_API,
                    help=f"服务端 URL，默认 {DEFAULT_API}")
    ap.add_argument("--template-id", required=True,
                    help="要使用的模板 ID（在桌面端模板库里查看）")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN,
                    help=f"文件名匹配正则，默认 {DEFAULT_PATTERN!r}")
    ap.add_argument("--dry-run", action="store_true", help="只统计不执行")
    args = ap.parse_args()

    globals()["API"] = args.api
    globals()["TEMPLATE_ID"] = args.template_id
    globals()["MAIN_PATTERN"] = re.compile(args.pattern, re.IGNORECASE)

    root = Path(args.root)
    if not root.exists():
        sys.exit(f"目录不存在：{root}")

    dirs = find_image_dirs(root)
    print(f"扫描到 {len(dirs)} 个含目标图的目录")
    print()

    total_ok = 0
    total_fail = 0
    total_files = 0
    all_fails = []
    t0 = time.time()

    for i, d in enumerate(dirs, 1):
        rel = d.relative_to(root)
        print(f"[{i}/{len(dirs)}] {rel}")
        try:
            r = process_one_dir(d, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ❌ 异常：{e}")
            total_fail += 1
            all_fails.append({"dir": str(d), "reason": f"exception: {e}"})
            continue

        if r.get("dry_run"):
            print(
                f"  共 {r['total']} 张 | 已处理 {r['already']} | 待处理 {r['pending']}"
            )
            total_files += r["pending"]
        elif r.get("skipped_all"):
            print(f"  ✅ 共 {r['total']} 张全部已处理过，跳过")
            total_ok += r["already"]
        elif r.get("skipped"):
            print(f"  ⏭  跳过：{r['reason']}")
        else:
            print(
                f"  ✅ 共 {r['total']} | 已处理 {r['already']} | "
                f"本次成功 {r['ok']} | 失败 {r['fail_count']}"
            )
            total_ok += r["ok"] + r["already"]
            total_fail += r["fail_count"]
            total_files += r["total"]
            all_fails.extend(r.get("fails", []))

    elapsed = time.time() - t0
    print()
    print("=" * 60)
    if args.dry_run:
        print(f"[Dry Run] 待处理 {total_files} 张，分布在 {len(dirs)} 个目录")
    else:
        print(
            f"完成：成功 {total_ok} / 失败 {total_fail} / 总计 {total_files} 张  "
            f"耗时 {elapsed:.1f}s"
        )
        if all_fails:
            print()
            print("失败详情：")
            for f in all_fails:
                print(f"  - {f}")


if __name__ == "__main__":
    main()
