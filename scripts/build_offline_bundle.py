"""Stage 3 of build_installer.bat — vendor every offline dependency, then create the versioned ZIP.

  python scripts/build_offline_bundle.py              # vendor (cached) + zip
  python scripts/build_offline_bundle.py --refresh    # re-download everything
  python scripts/build_offline_bundle.py --skip-ml    # leave out llama.cpp CUDA, torch and models (much smaller ZIP)

Windows tools (Ollama, llama.cpp CUDA 12.9, Pandoc, Typst, Vale, Harper) and a portable Python 3.12 are taken
from conda-forge with micromamba as ready-to-run win-64 folders — this works from a Windows or Linux build PC.
  python scripts/build_offline_bundle.py --zip-only   # package what is already in vendor/

Output: dist/VU3INI_ResearchMate_v<VERSION>.zip — VERSION's patch number is bumped automatically when a ZIP
for the current version already exists (1.0.0 → 1.0.1).
"""
from __future__ import annotations

import argparse
import json
import platform
import tarfile
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"
DL = VENDOR / "_downloads"
DIST = ROOT / "dist"
MANIFEST = json.loads((ROOT / "scripts" / "vendor_manifest.json").read_text(encoding="utf-8"))
UA = {"User-Agent": "VU3INI-ResearchMate-builder/1.0"}
EXCLUDE_DIRS = {".venv", "dist", "data", "__pycache__", ".pytest_cache", "_downloads", ".git", "runtime"}
report: list[tuple[str, str, str]] = []


def log(status: str, name: str, detail: str = "") -> None:
    report.append((status, name, detail))
    print(f"  [{status:^7}] {name}{(' — ' + detail) if detail else ''}", flush=True)


def fetch(url: str, dest: Path, refresh: bool) -> Path:
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = dict(UA)
    if os.environ.get("GITHUB_TOKEN") and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
        total = int(r.headers.get("content-length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            fh.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r      {dest.name}: {done * 100 // total:3d}% of {total / 1e6:.0f} MB", end="", flush=True)
    print("\r", end="")
    tmp.replace(dest)
    return dest


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):   # zip-slip guard
                raise RuntimeError(f"unsafe path in {archive.name}: {m.filename}")
        z.extractall(dest)


def gh_json(url: str) -> dict:
    headers = dict(UA)
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- vendoring steps
WIN_MARKER = re.compile(r"(sys_platform|platform_system|os_name)\s*==\s*['\"](win32|Windows|nt)['\"]")


def windows_only_deps(wheels: Path) -> set[str]:
    """pip evaluates environment markers against the BUILD machine, so `colorama; sys_platform == "win32"`
    style dependencies are silently skipped when the bundle is built on Linux/macOS. Find them."""
    need: set[str] = set()
    have = {w.name.split("-")[0].lower().replace("_", "-") for w in wheels.glob("*.whl")}
    for w in wheels.glob("*.whl"):
        with zipfile.ZipFile(w) as z:
            meta = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
            if not meta:
                continue
            for line in z.read(meta).decode("utf-8", "replace").splitlines():
                if line.startswith("Requires-Dist:") and WIN_MARKER.search(line) and "extra ==" not in line:
                    spec = line[len("Requires-Dist:"):].split(";")[0].strip()
                    name = re.split(r"[ <>=!~\[(]", spec)[0].lower().replace("_", "-")
                    if name not in have:
                        need.add(spec.replace(" ", ""))
    return need


def vendor_wheels(refresh: bool, skip_ml: bool, no_ml_wheels: bool = False) -> None:
    print("\n▶ Python wheels (win_amd64, offline install)")
    wheels = VENDOR / "wheels"
    if refresh and wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir(parents=True, exist_ok=True)
    pyver = MANIFEST["pip_python_version"]
    base = [sys.executable, "-m", "pip", "download", "--dest", str(wheels), "--platform", "win_amd64",
            "--python-version", pyver, "--only-binary=:all:", "--disable-pip-version-check"]
    rc = subprocess.call(base + ["-r", str(ROOT / "requirements.txt"), "pip", "setuptools", "wheel"])
    log("OK" if rc == 0 else "FAILED", "core wheels (requirements.txt)", "" if rc == 0 else f"pip exit {rc}")
    if rc != 0:
        raise SystemExit("Core wheels could not be downloaded — cannot build an offline bundle.")
    for _ in range(5):  # Windows-only dependencies (e.g. colorama for pytest/click), resolved until none are missing
        missing = windows_only_deps(wheels)
        if not missing:
            break
        rc = subprocess.call(base + sorted(missing))
        log("OK" if rc == 0 else "FAILED", "Windows-only dependencies", ", ".join(sorted(missing)))
    if skip_ml or no_ml_wheels:
        log("SKIPPED", "ML wheels", "--skip-ml / --no-ml-wheels")
        return
    for line in (ROOT / "requirements-ml.txt").read_text().splitlines():
        spec = line.split("#")[0].strip()
        if not spec:
            continue
        extra = ["--extra-index-url", MANIFEST["torch_index"]] if spec.startswith("torch") else []
        rc = subprocess.call(base + extra + [spec.split(";")[0].strip()])
        log("OK" if rc == 0 else "WARN", f"ML wheel {spec.split(';')[0].strip()}",
            "" if rc == 0 else "not available as a win_amd64 wheel — feature falls back to built-in engine")


def vendor_python(refresh: bool) -> None:
    print("\n▶ Python runtime installer (optional; vendor/python-runtime already covers PCs without Python)")
    p = MANIFEST.get("python_installer")
    if not p:
        log("SKIPPED", "python.org installer", "portable Python bundled instead")
        return
    try:
        fetch(p["url"], VENDOR / p["dest"], refresh)
        log("OK", f"Python {p['version']} installer")
    except Exception as exc:  # noqa: BLE001
        log("WARN", "Python installer", str(exc))


def vendor_binaries(refresh: bool, skip_ml: bool) -> None:
    print("\n▶ Windows x64 CLI binaries (GitHub releases)")
    for b in MANIFEST["github_release_binaries"]:
        if skip_ml and b.get("ml"):
            log("SKIPPED", b["name"], "--skip-ml")
            continue
        try:
            rel = gh_json(f"https://api.github.com/repos/{b['repo']}/releases/latest")
            asset = next((a for a in rel.get("assets", []) if re.search(b["asset"], a["name"], re.I)), None)
            if not asset:
                log("WARN", b["name"], f"no asset matching /{b['asset']}/ in {rel.get('tag_name')} — built-in fallback will be used")
                continue
            archive = fetch(asset["browser_download_url"], DL / asset["name"], refresh)
            dest = VENDOR / b["dest"]
            if b.get("extract", True) and archive.suffix == ".zip":
                extract(archive, dest)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archive, dest / archive.name)
            log("OK", b["name"], f"{rel.get('tag_name')} · {asset['name']}")
        except Exception as exc:  # noqa: BLE001
            log("WARN", b["name"], str(exc))


def vendor_direct(refresh: bool) -> None:
    print("\n▶ Direct downloads (Java runtime, LanguageTool server, Zotero installer)")
    for d in MANIFEST["direct_downloads"]:
        try:
            archive = fetch(d["url"], DL / d["file"], refresh)
            dest = VENDOR / d["dest"]
            if not d.get("extract", True):
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archive, dest / d["file"])
                log("OK", d["name"])
                continue
            if refresh and dest.exists():
                shutil.rmtree(dest)
            if not dest.exists():
                extract(archive, dest)
            log("OK", d["name"])
        except Exception as exc:  # noqa: BLE001
            log("WARN", d["name"], str(exc))


def vendor_repos(refresh: bool) -> None:
    print("\n▶ GitHub repository snapshots (vendor/repos)")
    for r in MANIFEST["repos"]:
        if r.get("snapshot") is False:
            log("SKIPPED", r["name"], r.get("why", "snapshot disabled in vendor_manifest.json"))
            continue
        if not r["repo"]:
            log("WARN", r["name"], "no repository URL set in vendor_manifest.json — ResearchMate's built-in engine is used")
            continue
        dest = VENDOR / "repos" / r["name"].replace(" ", "_")
        if dest.exists() and not refresh:
            log("CACHED", r["name"])
            continue
        if dest.exists():
            shutil.rmtree(dest)
        if shutil.which("git"):
            env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1", GIT_TERMINAL_PROMPT="0")
            rc = subprocess.call(["git", "clone", "--depth", "1", "-q", f"https://github.com/{r['repo']}", str(dest)],
                                 env=env, timeout=1200)
            if rc == 0:
                shutil.rmtree(dest / ".git", ignore_errors=True)
                log("OK", r["name"], r["repo"])
                continue
        try:
            archive = fetch(f"https://api.github.com/repos/{r['repo']}/zipball", DL / f"repo_{r['name'].replace(' ', '_')}.zip", refresh)
            extract(archive, dest)
            log("OK", r["name"], r["repo"])
        except Exception as exc:  # noqa: BLE001
            log("WARN", r["name"], str(exc))


def vendor_csl(refresh: bool) -> None:
    print("\n▶ CSL citation styles")
    for name in MANIFEST["csl_styles"]:
        try:
            src = MANIFEST.get("csl_sources", {}).get(name, name)   # vancouver.csl is published as nlm-citation-sequence
            fetch(MANIFEST["csl_base"] + f"{src}.csl", VENDOR / "csl" / f"{name}.csl", refresh)
            log("OK", f"{name}.csl")
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"{name}.csl", str(exc))


# ---------------------------------------------------------------- conda-forge (micromamba) Windows environments
def host_subdir() -> str:
    m = platform.machine().lower()
    if sys.platform.startswith("win"):
        return "win-64"
    if sys.platform == "darwin":
        return "osx-arm64" if m in ("arm64", "aarch64") else "osx-64"
    return "linux-aarch64" if m in ("arm64", "aarch64") else "linux-64"


def get_micromamba(refresh: bool) -> Path:
    sub = host_subdir()
    exe = DL / "micromamba" / ("Library/bin/micromamba.exe" if sub == "win-64" else "bin/micromamba")
    if exe.exists() and not refresh:
        return exe
    base = MANIFEST["conda"]["channel_url"]
    idx = fetch(f"{base}/{sub}/current_repodata.json", DL / f"current_repodata_{sub}.json", refresh)
    data = json.loads(idx.read_text(encoding="utf-8"))
    pkgs = {**data.get("packages", {}), **data.get("packages.conda", {})}
    fn = max(((m.get("timestamp", 0), f) for f, m in pkgs.items() if m["name"] == "micromamba"))[1]
    archive = fetch(f"{base}/{sub}/{fn}", DL / fn, refresh)
    dest = DL / "micromamba"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if fn.endswith(".tar.bz2"):
        with tarfile.open(archive) as t:
            t.extractall(dest)
    else:  # .conda = zip containing pkg-*.tar.zst
        import zstandard  # pip install zstandard on the build PC if this branch is ever needed
        with zipfile.ZipFile(archive) as z:
            inner = next(n for n in z.namelist() if n.startswith("pkg-"))
            with z.open(inner) as fh, tarfile.open(fileobj=zstandard.ZstdDecompressor().stream_reader(fh), mode="r|") as t:
                t.extractall(dest)
    if not sys.platform.startswith("win"):
        exe.chmod(0o755)
    return exe


def mamba_create(mm: Path, prefix: Path, specs: list[str], refresh: bool) -> int:
    if prefix.exists() and not refresh:
        return 0
    shutil.rmtree(prefix, ignore_errors=True)
    env = dict(os.environ, MAMBA_ROOT_PREFIX=str(DL / "mamba-root"), CONDA_OVERRIDE_CUDA=MANIFEST["conda"]["cuda"])
    cmd = [str(mm), "create", "-y", "-q", "--platform", "win-64", "-p", str(prefix), "-c", "conda-forge",
           "--override-channels", *specs]
    return subprocess.call(cmd, env=env)


def prune(prefix: Path, patterns: list[str]) -> None:
    for pat in patterns:
        for p in prefix.glob(pat):
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def vendor_conda(refresh: bool, skip_ml: bool) -> None:
    print("\n▶ Windows tools + portable Python from conda-forge (micromamba, platform win-64)")
    c = MANIFEST["conda"]
    try:
        mm = get_micromamba(refresh)
    except Exception as exc:  # noqa: BLE001
        log("FAILED", "micromamba", str(exc))
        return
    specs = list(c["tools"]) + ([] if skip_ml else list(c["tools_gpu"]))
    rc = mamba_create(mm, VENDOR / "bin" / "tools", specs, refresh)
    if rc == 0:
        prune(VENDOR / "bin" / "tools", c["prune"])
        log("OK", "tools env (vendor/bin/tools)", ", ".join(specs))
    else:
        log("FAILED", "tools env", f"micromamba exit {rc}")
    rc = mamba_create(mm, VENDOR / "python-runtime", c["python_runtime"], refresh)
    log("OK" if rc == 0 else "FAILED", "portable Python (vendor/python-runtime)", ", ".join(c["python_runtime"]))


def vendor_ollama_model(refresh: bool, skip_ml: bool) -> None:
    print("\n▶ Default local LLM for Step 4 (lets install_offline.bat set up llama3.1:8b with no internet)")
    for m in MANIFEST.get("ollama_models", []):
        if skip_ml and m.get("ml"):
            log("SKIPPED", m["name"], "--skip-ml (Step 4 will `ollama pull` it when online)")
            continue
        try:
            fetch(m["url"], VENDOR / m["dest"], refresh)
            log("OK", m["name"])
        except Exception as exc:  # noqa: BLE001
            log("WARN", m["name"], str(exc))


def vendor_models(refresh: bool, skip_ml: bool) -> None:
    print("\n▶ Small local models (Hugging Face)")
    for m in MANIFEST["hf_models"]:
        if skip_ml:
            log("SKIPPED", m["name"], "--skip-ml")
            continue
        try:
            for f in m["files"]:
                fetch(f"https://huggingface.co/{m['repo']}/resolve/main/{f}", VENDOR / m["dest"] / f, refresh)
            log("OK", m["name"], m["repo"])
        except Exception as exc:  # noqa: BLE001
            log("WARN", m["name"], str(exc))


# ---------------------------------------------------------------- versioned ZIP
def bump_version() -> str:
    vfile = ROOT / "VERSION"
    ver = vfile.read_text().strip() or "1.0.0"
    major, minor, patch = (int(x) for x in ver.split("."))
    while (DIST / f"VU3INI_ResearchMate_v{major}.{minor}.{patch}.zip").exists():
        patch += 1
    new = f"{major}.{minor}.{patch}"
    if new != ver:
        vfile.write_text(new + "\n")
        print(f"  VERSION bumped {ver} → {new} (a ZIP for {ver} already exists)")
    return new


def build_zip() -> Path:
    print("\n▶ Packaging")
    version = bump_version()
    DIST.mkdir(exist_ok=True)
    out = DIST / f"VU3INI_ResearchMate_v{version}.zip"
    top = "VU3INI-ResearchMate"
    (VENDOR / "BUNDLE_REPORT.txt").write_text(
        f"VU3INI ResearchMate v{version} offline bundle\n\n" +
        "\n".join(f"[{s}] {n}{(' — ' + d) if d else ''}" for s, n, d in report) + "\n", encoding="utf-8")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if any(part in EXCLUDE_DIRS for part in rel.parts) or path.suffix in (".pyc", ".part"):
                continue
            if path.is_file():
                z.write(path, f"{top}/{rel.as_posix()}")
                count += 1
    print(f"  {count} files → {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def split_zip(single: Path, max_mb: int) -> list[Path]:
    """Split one bundle into N self-contained ZIPs (each < max_mb MiB) that merge into the same folder.

    * Files whose compressed size would not fit are cut into raw byte pieces (<file>.piece001 …); a generated
      vendor/_join.bat glues them back with Windows' own `copy /b` (install_offline.bat runs it).
    * Every part carries vendor/_partK_of_N.ok so install_offline.bat can verify all parts were extracted.
    * App files (batch files, backend, frontend) always go into part 1.
    """
    limit = max_mb * 1024 * 1024 - 8 * 1024 * 1024          # headroom for headers / central directory
    with zipfile.ZipFile(single) as z:
        infos = z.infolist()
        top = infos[0].filename.split("/")[0]
        items: list[tuple[str, int, object]] = []           # (arcname, est_compressed, source)
        joins: list[str] = []
        for i in infos:
            if i.compress_size <= limit * 0.6:
                items.append((i.filename, i.compress_size, i))
                continue
            ratio = max(i.compress_size / max(i.file_size, 1), 0.05)
            piece_raw = max(1, int(limit * 0.45 / ratio))
            data = z.read(i)
            names = []
            for k, off in enumerate(range(0, len(data), piece_raw), 1):
                name = f"{i.filename}.piece{k:03d}"
                names.append(name)
                items.append((name, int(min(piece_raw, len(data) - off) * ratio), data[off:off + piece_raw]))
            rel = lambda n: n[len(top) + 1:].replace("/", "\\")  # noqa: E731
            joins.append(f'copy /b {"+".join(chr(34) + rel(n) + chr(34) for n in names)} "{rel(i.filename)}" >nul '
                         f'&& del {" ".join(chr(34) + rel(n) + chr(34) for n in names)}')
            del data
        if joins:
            items.append((f"{top}/vendor/_join.bat", 1024,
                          ("@echo off\r\nREM generated by build_offline_bundle.py: rejoin files split for upload limits\r\n"
                           "cd /d \"%~dp0..\"\r\n" + "\r\n".join(joins) + "\r\nexit /b 0\r\n").encode()))
        app = [it for it in items if "/vendor/" not in it[0] or it[0].endswith("/vendor/_join.bat")]
        rest = sorted([it for it in items if it not in app], key=lambda it: -it[1])
        bins: list[list] = [list(app)]
        size = lambda b: sum(x[1] for x in b)  # noqa: E731
        for it in rest:                                      # first-fit decreasing
            for b in bins:
                if size(b) + it[1] <= limit:
                    b.append(it)
                    break
            else:
                bins.append([it])
        n = len(bins)
        out = []
        for k, b in enumerate(bins, 1):
            part = single.with_name(f"{single.stem}_part{k}of{n}.zip")
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as w:
                for name, _, src in b:
                    if isinstance(src, zipfile.ZipInfo):
                        w.writestr(src, z.read(src), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
                    else:
                        w.writestr(name, src)
                w.writestr(f"{top}/vendor/_part{k}_of_{n}.ok", f"part {k} of {n}\n")
            mib = part.stat().st_size / 2**20
            if mib > max_mb:
                raise SystemExit(f"{part.name} is {mib:.0f} MiB > {max_mb} MiB — lower --max-part-mb and retry")
            out.append(part)
            print(f"  part {k}/{n}: {part.name}  ({mib:.0f} MiB)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--skip-ml", action="store_true")
    ap.add_argument("--zip-only", action="store_true")
    ap.add_argument("--no-ml-wheels", action="store_true", help="skip torch/transformers wheels only")
    ap.add_argument("--max-part-mb", type=int, default=0, help="also split the ZIP into parts of at most N MiB")
    args, _unknown = ap.parse_known_args()
    print("=" * 78 + "\n  STAGE 3/3 — Offline dependency vendoring & versioned ZIP\n" + "=" * 78)
    if not args.zip_only:
        vendor_wheels(args.refresh, args.skip_ml, args.no_ml_wheels)
        vendor_conda(args.refresh, args.skip_ml)
        vendor_python(args.refresh)
        vendor_binaries(args.refresh, args.skip_ml)
        vendor_direct(args.refresh)
        vendor_repos(args.refresh)
        vendor_csl(args.refresh)
        vendor_models(args.refresh, args.skip_ml)
        vendor_ollama_model(args.refresh, args.skip_ml)
    out = build_zip()
    if args.max_part_mb:
        print("\n▶ Splitting into parts")
        split_zip(out, args.max_part_mb)
    warns = [r for r in report if r[0] in ("WARN", "FAILED")]
    print(f"\n  Done: {out.name}" + (f"  ({len(warns)} warnings — see vendor/BUNDLE_REPORT.txt)" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
