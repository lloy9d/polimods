#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Збірник модпаків
Парсить modlist.txt, завантажує моди та пакує у два зіпи
"""

import hashlib
import json
import os
import re
import sys
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

# ── Константи ──────────────────────────────────────────────────────────────────
MODLIST_DEFAULT  = Path("modlist.txt")
DIST_DEFAULT     = Path("dist")
MAX_WORKERS      = 8
DOWNLOAD_TIMEOUT = 120
USER_AGENT       = "POLI-PackBuilder/1.0"

SECTION_LITE   = "POLI LITE"
SECTION_FUSION = "POLI FUSION"


# ── Парсинг ────────────────────────────────────────────────────────────────────
def parse_modlist(path: Path) -> tuple[list[dict], list[dict], str]:
    lite: list[dict] = []
    fusion: list[dict] = []
    section: str | None = None
    version = "unknown"

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if line.startswith("#"):
            if SECTION_LITE in line:
                section = "lite"
                if m := re.search(r"\[v\.(.+?)\]", line):
                    version = m.group(1)
            elif SECTION_FUSION in line:
                section = "fusion"
            continue

        if not line or "|" not in line:
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            continue

        name_raw, ver, url = parts
        entry = {
            "name":    re.sub(r"\s*\[CF\]", "", name_raw).strip(),
            "version": ver,
            "url":     url,
            "cf":      "[CF]" in name_raw,
        }

        if section == "lite":
            lite.append(entry)
        elif section == "fusion":
            fusion.append(entry)

    return lite, fusion, version


# ── Завантаження ───────────────────────────────────────────────────────────────
def _filename_from_response(r: requests.Response, url: str) -> str:
    cd = r.headers.get("Content-Disposition", "")
    if cd:
        m = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            return urllib.parse.unquote(m.group(1).strip())
    return urllib.parse.unquote(url.split("/")[-1])


def _download_one(entry: dict, dest: Path) -> tuple[dict, Path | None]:
    url = entry["url"]

    # For URLs ending in /download the filename is unknown until we fetch
    guessed = urllib.parse.unquote(url.split("/")[-1])
    if guessed not in ("download", ""):
        target = dest / guessed
        if target.exists():
            print(f"  ↩  {entry['name']}  {entry['version']}  (кеш)")
            return entry, target

    try:
        with requests.get(
            url,
            timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        ) as r:
            r.raise_for_status()
            filename = _filename_from_response(r, url)
            target   = dest / filename
            if target.exists():
                print(f"  ↩  {entry['name']}  {entry['version']}  (кеш)")
                return entry, target
            target.write_bytes(r.content)

        print(f"  ✓  {entry['name']}  {entry['version']}")
        return entry, target

    except Exception as exc:
        print(f"  ✗  {entry['name']}: {exc}", file=sys.stderr)
        return entry, None


def download_all(mods: list[dict], dest: Path) -> dict[str, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_download_one, m, dest): m for m in mods}
        for fut in as_completed(futures):
            entry, path = fut.result()
            if path:
                results[entry["name"]] = path

    return results


# ── Пакування ──────────────────────────────────────────────────────────────────
def build_zip(zip_name: str, mods: list[dict], downloaded: dict[str, Path], dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    out_path = dest / zip_name
    missing: list[str] = []

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for mod in mods:
            path = downloaded.get(mod["name"])
            if path and path.exists():
                zf.write(path, path.name)
            else:
                missing.append(mod["name"])

    if missing:
        print(f"\n  [!] Відсутні у {zip_name}:", file=sys.stderr)
        for name in missing:
            print(f"      – {name}", file=sys.stderr)

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  → {zip_name}  ({size_mb:.1f} МБ,  {len(mods) - len(missing)} / {len(mods)} модів)")
    return out_path


# ── Утиліти ────────────────────────────────────────────────────────────────────
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ── version.json ───────────────────────────────────────────────────────────────
def write_version_json(
    version: str,
    lite_path: Path,
    fusion_path: Path,
    modlist_path: Path,
    dest: Path,
) -> Path:
    payload = {
        "version":       version,
        "minecraft":     "1.21.1",
        "loader":        "NeoForge",
        "built_at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_hash": sha256_file(modlist_path),
        "checksums": {
            "poli-lite":   sha256_file(lite_path),
            "poli-fusion": sha256_file(fusion_path),
        },
    }

    out = dest / "version.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → {out.name}")
    return out


# ── Нотатки до релізу ──────────────────────────────────────────────────────────
def write_release_notes(
    lite: list[dict],
    fusion: list[dict],
    version: str,
    dest: Path,
) -> Path:
    kyiv = ZoneInfo("Europe/Kyiv")
    built_at = datetime.now(kyiv).strftime("%d.%m.%Y %H:%M (Київ)")

    def mod_table(mods: list[dict]) -> str:
        rows = "\n".join(f"| {m['name']} | `{m['version']}` |" for m in mods)
        return f"| Мод | Версія |\n|---|---|\n{rows}"

    notes = f"""\
<sub>Minecraft 1.21.1 · NeoForge</sub>

> 🕐 Зібрано: {built_at}

---

<details>
<summary>📦 poli-lite.zip — базовий пакет · {len(lite)} модів</summary>

{mod_table(lite)}

</details>

---

<details>
<summary>⚡ poli-fusion.zip — повний пакет · {len(lite) + len(fusion)} модів (містить усі моди з poli-lite + нижченаведені)</summary>

{mod_table(fusion)}

</details>
"""

    out = dest / "release_notes.md"
    out.write_text(notes, encoding="utf-8")
    print(f"  → {out.name}")
    return out


# ── Точка входу ────────────────────────────────────────────────────────────────
def main() -> None:
    modlist_path = Path(sys.argv[1]) if len(sys.argv) > 1 else MODLIST_DEFAULT
    dist_path    = Path(sys.argv[2]) if len(sys.argv) > 2 else DIST_DEFAULT
    cache_path   = dist_path / ".cache"

    if not modlist_path.exists():
        print(f"Помилка: файл '{modlist_path}' не знайдено.", file=sys.stderr)
        sys.exit(1)

    print("── Парсинг списку модів ──────────────────────────────────────────────")
    lite, fusion, version = parse_modlist(modlist_path)
    print(f"  Версія пакету : {version}")
    print(f"  LITE          : {len(lite)} модів")
    print(f"  FUSION (доп.) : {len(fusion)} модів")

    print("\n── Завантаження модів ───────────────────────────────────────────────")
    downloaded = download_all(lite + fusion, cache_path)

    failed = (len(lite) + len(fusion)) - len(downloaded)
    if failed > 0:
        print(f"\n  [!] Не вдалося завантажити: {failed} модів", file=sys.stderr)

    print("\n── Пакування ────────────────────────────────────────────────────────")
    lite_path   = build_zip("poli-lite.zip",   lite,          downloaded, dist_path)
    fusion_path = build_zip("poli-fusion.zip", lite + fusion, downloaded, dist_path)

    print("\n── Генерація метаданих ──────────────────────────────────────────────")
    write_version_json(version, lite_path, fusion_path, modlist_path, dist_path)
    write_release_notes(lite, fusion, version, dist_path)

    # Передача версії у GitHub Actions environment
    if gh_env := os.environ.get("GITHUB_ENV"):
        with open(gh_env, "a", encoding="utf-8") as f:
            f.write(f"PACK_VERSION={version}\n")
        print(f"\n  PACK_VERSION={version}  →  GITHUB_ENV")

    if failed > 0:
        sys.exit(1)

    print("\n── Готово ───────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
