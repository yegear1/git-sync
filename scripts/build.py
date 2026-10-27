#!/usr/bin/env python3
"""
scripts/build.py — Distribution build and packaging hygiene validation.
Builds clean sdist and wheel artifacts and validates package integrity
without external runtime dependencies.
"""

import argparse
import os
import re
import shutil
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent


def clean_artifacts(repo_root: Path = REPO_ROOT) -> None:
    """Remove transient build artifacts, egg-info folders, and cache directories."""
    patterns = ["build", "*.egg-info", "src/*.egg-info", ".tmp-*"]
    for pattern in patterns:
        for p in repo_root.glob(pattern):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink(missing_ok=True)


def build_distribution(dist_dir: Path, repo_root: Path = REPO_ROOT) -> Tuple[Path, Path]:
    """
    Build sdist (.tar.gz) and binary wheel (.whl) into dist_dir.
    Uses setuptools.build_meta to avoid external build dependencies.
    Filters legacy/internal setuptools deprecation warnings (e.g. pkg_resources).
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    orig_cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        # INVARIANT: Filter internal setuptools legacy deprecation warnings (e.g. pkg_resources, cgi)
        # to ensure warning-free build outputs across Python 3.12+/3.13 environments.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r".*pkg_resources is deprecated.*", category=DeprecationWarning)
            warnings.filterwarnings("ignore", message=r".*'cgi' is deprecated.*", category=DeprecationWarning)
            warnings.filterwarnings("ignore", module=r"pkg_resources.*", category=DeprecationWarning)
            from setuptools import build_meta as bm

            sdist_name = bm.build_sdist(str(dist_dir))
            wheel_name = bm.build_wheel(str(dist_dir))
            return dist_dir / sdist_name, dist_dir / wheel_name
    finally:
        os.chdir(orig_cwd)
        clean_artifacts(repo_root)


def validate_sdist(sdist_path: Path) -> List[str]:
    """
    Validate sdist tarball contents and hygiene.
    Returns a list of error messages (empty if valid).
    """
    errors: List[str] = []
    if not sdist_path.exists():
        return [f"sdist archive not found: {sdist_path}"]

    try:
        with tarfile.open(sdist_path, "r:gz") as tar:
            members = tar.getnames()
    except Exception as exc:
        return [f"Failed to open sdist tarball {sdist_path}: {exc}"]

    required_endings = [
        "pyproject.toml",
        "README.md",
        "src/git_sync/__init__.py",
        "src/git_sync/cli.py",
        "src/git_sync/core.py",
        "src/git_sync/config.py",
        "src/git_sync/installer.py",
        "src/git_sync/telemetry.py",
        "src/git_sync/engine/__init__.py",
        "src/git_sync/engine/concurrency.py",
        "src/git_sync/engine/git.py",
        "src/git_sync/ui/__init__.py",
        "src/git_sync/ui/progress.py",
        "PKG-INFO",
    ]

    for req in required_endings:
        if not any(name.endswith(req) for name in members):
            errors.append(f"sdist missing required member ending with '{req}'")

    forbidden_patterns = [
        r"\.git/",
        r"\.env",
        r"__pycache__",
        r"\.pytest_cache",
        r"\.mypy_cache",
        r"\.DS_Store",
    ]

    for name in members:
        for pat in forbidden_patterns:
            if re.search(pat, name):
                errors.append(f"sdist contains prohibited member '{name}' matching pattern '{pat}'")

    return errors


def validate_wheel(wheel_path: Path) -> List[str]:
    """
    Validate binary wheel archive contents, metadata, and entry-points.
    Returns a list of error messages (empty if valid).
    """
    errors: List[str] = []
    if not wheel_path.exists():
        return [f"wheel archive not found: {wheel_path}"]

    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            members = zf.namelist()
            entry_points_text = ""
            for name in members:
                if name.endswith("entry_points.txt"):
                    entry_points_text = zf.read(name).decode("utf-8")
    except Exception as exc:
        return [f"Failed to inspect wheel archive {wheel_path}: {exc}"]

    required_members = [
        "git_sync/__init__.py",
        "git_sync/cli.py",
        "git_sync/core.py",
        "git_sync/config.py",
        "git_sync/installer.py",
        "git_sync/telemetry.py",
        "git_sync/engine/__init__.py",
        "git_sync/engine/concurrency.py",
        "git_sync/engine/git.py",
        "git_sync/ui/__init__.py",
        "git_sync/ui/progress.py",
    ]

    for req in required_members:
        if req not in members:
            errors.append(f"wheel missing required module member '{req}'")

    if "git-sync = git_sync.cli:main" not in entry_points_text:
        errors.append("wheel entry_points.txt missing CLI console script 'git-sync = git_sync.cli:main'")

    forbidden_patterns = [
        r"^tests/",
        r"\.git/",
        r"\.env",
        r"__pycache__",
        r"\.DS_Store",
    ]

    for name in members:
        for pat in forbidden_patterns:
            if re.search(pat, name):
                errors.append(f"wheel contains prohibited member '{name}' matching pattern '{pat}'")

    return errors


def check_dist(dist_dir: Path) -> bool:
    """Audit all sdist and wheel packages in dist_dir."""
    sdists = list(dist_dir.glob("*.tar.gz"))
    wheels = list(dist_dir.glob("*.whl"))

    if not sdists and not wheels:
        print(f"❌ Error: No distribution packages found in {dist_dir}")
        return False

    all_ok = True
    print(f"📦 Inspecting distribution packages in {dist_dir}:")

    for sdist in sdists:
        errs = validate_sdist(sdist)
        if errs:
            all_ok = False
            print(f"  ❌ {sdist.name} failed validation:")
            for err in errs:
                print(f"     - {err}")
        else:
            print(f"  ✅ sdist: {sdist.name} (valid structure & hygiene)")

    for wheel in wheels:
        errs = validate_wheel(wheel)
        if errs:
            all_ok = False
            print(f"  ❌ {wheel.name} failed validation:")
            for err in errs:
                print(f"     - {err}")
        else:
            print(f"  ✅ wheel: {wheel.name} (valid modules & entry points)")

    return all_ok


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="git-sync release packaging, build, and distribution hygiene validator."
    )
    parser.add_argument(
        "--dist-dir",
        default=str(REPO_ROOT / "dist"),
        help="Directory to store or inspect distribution packages (default: dist/)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean transient build artifacts and exit",
    )
    parser.add_argument(
        "--check-dist",
        action="store_true",
        help="Validate existing distribution packages without building new ones",
    )

    args = parser.parse_args(argv)
    dist_dir = Path(args.dist_dir).resolve()

    if args.clean:
        print("🧹 Cleaning transient build artifacts...")
        clean_artifacts(REPO_ROOT)
        if dist_dir.exists():
            shutil.rmtree(dist_dir, ignore_errors=True)
        print("✅ Clean complete.")
        return 0

    if args.check_dist:
        success = check_dist(dist_dir)
        return 0 if success else 1

    print(f"🔨 Building git-sync distribution artifacts into {dist_dir}...")
    sdist, wheel = build_distribution(dist_dir, REPO_ROOT)
    print(f"   Created sdist: {sdist.name} ({sdist.stat().st_size} bytes)")
    print(f"   Created wheel: {wheel.name} ({wheel.stat().st_size} bytes)")

    success = check_dist(dist_dir)
    if success:
        print("\n🚀 Distribution hygiene check PASSED! Package ready for release.")
        return 0
    else:
        print("\n❌ Distribution hygiene check FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
