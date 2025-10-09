# download_amulet_icons.py
# -*- coding: utf-8 -*-
import os
import csv
import time
import shutil
import string
import random
import pathlib
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://game.mahjongsoul.com/v0.11.172.w/scene/Assets/Resource/amulet/fu_{id}.png"
ID_START = 1
ID_END = 195  # 包含 195
PAD_LEN = 4  # fu_0001 这种 4 位补零
OUT_DIR_USED = "amulet_icons"
OUT_DIR_UNUSED = "amulet_unused"
CSV_SUMMARY = "amulet_download_summary.csv"
MAX_WORKERS = 8
RETRIES = 3
TIMEOUT = 15

EXCLUDE_IDS = {
    3, 5, 8, 10, 15, 21, 22, 26, 34, 39, 41, 43, 44, 45, 48, 50, 51, 52, 53, 54, 55, 56, 57, 59, 60, 61, 62, 63, 68, 69, 70, 72, 73, 78, 81, 82, 83, 84, 85, 86, 87, 88, 103, 104, 107, 109, 114, 117, 121, 123, 125, 140, 150, 155,
}

# =======================================

HEADERS = {
    "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AmuletFetcher/{random.randint(1000, 9999)}"
}

os.makedirs(OUT_DIR_USED, exist_ok=True)
os.makedirs(OUT_DIR_UNUSED, exist_ok=True)


def fmt_id(i: int) -> str:
    return str(i).zfill(PAD_LEN)


def url_for(i: int) -> str:
    return BASE_URL.format(id=fmt_id(i))


def is_png(bytes_head: bytes) -> bool:
    # PNG magic: 89 50 4E 47 0D 0A 1A 0A
    return bytes_head.startswith(b"\x89PNG\r\n\x1a\n")


def download_one(i: int) -> dict:
    """
    返回字典: {id, filename, url, status, note}
      status: ok_used / ok_excluded / skip_exist / http_error / bad_content / error
    """
    padded = fmt_id(i)
    fname = f"fu_{padded}.png"
    url = url_for(i)
    dst_used = os.path.join(OUT_DIR_USED, fname)
    dst_unused = os.path.join(OUT_DIR_UNUSED, fname)

    # 已存在就直接按规则放置（避免重复网络请求）
    if os.path.exists(dst_used):
        if i in EXCLUDE_IDS:
            # 如果之前在 used，但现在进入了黑名单，就移动到 unused
            try:
                shutil.move(dst_used, dst_unused)
                return dict(id=i, filename=fname, url=url, status="moved_to_excluded", note="moved existing used -> unused")
            except Exception as e:
                return dict(id=i, filename=fname, url=url, status="error", note=f"move used->unused failed: {e}")
        else:
            return dict(id=i, filename=fname, url=url, status="skip_exist", note="already in used")
    if os.path.exists(dst_unused):
        if i in EXCLUDE_IDS:
            return dict(id=i, filename=fname, url=url, status="skip_exist", note="already in unused")
        else:
            # 如果之前在 unused，但现在不在黑名单，就移回 used
            try:
                shutil.move(dst_unused, dst_used)
                return dict(id=i, filename=fname, url=url, status="moved_to_used", note="moved existing unused -> used")
            except Exception as e:
                return dict(id=i, filename=fname, url=url, status="error", note=f"move unused->used failed: {e}")

    # 下载
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code != 200:
                last_err = f"http {resp.status_code}"
                time.sleep(0.5 * attempt)
                continue

            content = resp.content or b""
            if len(content) < 8 or not is_png(content[:8]):
                # 有些地址虽返回 200，但可能是 HTML 或空内容
                last_err = "not a valid PNG"
                time.sleep(0.5 * attempt)
                continue

            # 根据黑名单决定放哪
            dst = dst_unused if i in EXCLUDE_IDS else dst_used
            with open(dst, "wb") as f:
                f.write(content)

            return dict(
                id=i,
                filename=fname,
                url=url,
                status=("ok_excluded" if i in EXCLUDE_IDS else "ok_used"),
                note=""
            )
        except Exception as e:
            last_err = str(e)
            time.sleep(0.6 * attempt)

    return dict(id=i, filename=fname, url=url, status="error", note=last_err or "unknown error")


def main():
    ids = list(range(ID_START, ID_END + 1))

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(download_one, i): i for i in ids}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            st = r["status"]
            prefix = {
                "ok_used": "✅",
                "ok_excluded": "✅",
                "moved_to_used": "🔁",
                "moved_to_excluded": "🔁",
                "skip_exist": "⏭️",
                "http_error": "⚠️",
                "bad_content": "⚠️",
                "error": "❌",
            }.get(st, "•")
            print(f"{prefix} {r['id']:>4} -> {r['status']}  {r['note']}")

    # 写汇总 CSV
    cols = ["id", "filename", "url", "status", "note"]
    with open(CSV_SUMMARY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(results, key=lambda x: x["id"]):
            w.writerow(r)

    # 简单统计
    from collections import Counter
    c = Counter(r["status"] for r in results)
    print("\n== Stats ==")
    for k, v in c.items():
        print(f"{k:>18}: {v}")
    print(f"CSV written: {CSV_SUMMARY}")
    print(f"Used dir   : {OUT_DIR_USED}")
    print(f"Unused dir : {OUT_DIR_UNUSED}")


if __name__ == "__main__":
    main()
