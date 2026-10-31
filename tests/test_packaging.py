"""
Unit tests for packaging, release distribution hygiene, and zero-dependency compliance.
"""

import ast
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import warnings
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from git_sync import __version__
from git_sync.cli import main
import build as build_tool  # type: ignore


class TestPackagingAndDistributionHygiene(unittest.TestCase):
    """Test suite ensuring distribution hygiene, packaging compliance, and zero dependencies."""

    def test_zero_external_runtime_dependencies(self) -> None:
        """INVARIANT: git-sync runtime modules MUST have zero external third-party dependencies."""
        stdlib = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
        src_dir = REPO_ROOT / "src" / "git_sync"

        for py_file in src_dir.rglob("*.py"):
            with self.subTest(file=py_file.name):
                content = py_file.read_text(encoding="utf-8")
                tree = ast.parse(content, filename=str(py_file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            root_pkg = alias.name.split(".")[0]
                            self.assertTrue(
                                root_pkg in stdlib or root_pkg == "git_sync",
                                f"Forbidden third-party import '{root_pkg}' detected in {py_file.name}",
                            )
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            root_pkg = node.module.split(".")[0]
                            self.assertTrue(
                                root_pkg in stdlib or root_pkg == "git_sync",
                                f"Forbidden third-party import '{root_pkg}' detected in {py_file.name}",
                            )

    def test_pyproject_metadata_integrity(self) -> None:
        """Validate pyproject.toml configuration, metadata, and entrypoints."""
        pyproject_path = REPO_ROOT / "pyproject.toml"
        self.assertTrue(pyproject_path.exists(), "pyproject.toml must exist in repo root")

        content = pyproject_path.read_text(encoding="utf-8")

        # Basic metadata checks
        self.assertIn('name = "git-sync-cli"', content)
        self.assertIn(f'version = "{__version__}"', content)
        self.assertIn('git-sync = "git_sync.cli:main"', content)
        self.assertTrue('license = "MIT"' in content or 'license = { text = "MIT" }' in content)
        self.assertIn('requires = ["setuptools>=61.0"]', content)
        self.assertIn('build-backend = "setuptools.build_meta"', content)

        # Email validation pattern check
        email_match = re.search(r'email\s*=\s*"([^"]+)"', content)
        self.assertIsNotNone(email_match, "Author email must be defined in pyproject.toml")
        email = email_match.group(1) if email_match else ""
        self.assertRegex(email, r"^[^@]+@[^@]+\.[^@]+$", f"Author email '{email}' is invalid")

        # If tomllib is available, parse structured TOML
        try:
            import tomllib
            data = tomllib.loads(content)
            self.assertEqual(data["project"]["name"], "git-sync-cli")
            self.assertEqual(data["project"]["version"], __version__)
            self.assertIn(data["project"]["license"], ("MIT", {"text": "MIT"}))
            self.assertEqual(
                data["project"]["scripts"]["git-sync"],
                "git_sync.cli:main",
            )
            self.assertEqual(
                data["build-system"]["build-backend"],
                "setuptools.build_meta",
            )
        except ImportError:
            pass

    def test_build_distribution_and_archive_hygiene(self) -> None:
        """Build sdist and wheel in a temporary directory and audit their hygiene."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dist_path = Path(tmpdir)
            with warnings.catch_warnings(record=True) as recorded_warnings:
                warnings.simplefilter("always")
                sdist_file, wheel_file = build_tool.build_distribution(dist_path, REPO_ROOT)

            self.assertTrue(sdist_file.exists(), "sdist tarball was not created")
            self.assertTrue(wheel_file.exists(), "wheel archive was not created")

            # Validate that build process emitted no SetuptoolsDeprecationWarning or unhandled DeprecationWarning
            dep_warnings = [
                f"{w.category.__name__}: {w.message}"
                for w in recorded_warnings
                if issubclass(w.category, (DeprecationWarning, UserWarning))
                and "'cgi' is deprecated" not in str(w.message)
            ]
            self.assertEqual(dep_warnings, [], f"Unexpected warnings emitted during build: {dep_warnings}")

            # Validate sdist hygiene
            sdist_errors = build_tool.validate_sdist(sdist_file)
            self.assertEqual(sdist_errors, [], f"sdist validation errors: {sdist_errors}")

            # Validate wheel hygiene
            wheel_errors = build_tool.validate_wheel(wheel_file)
            self.assertEqual(wheel_errors, [], f"wheel validation errors: {wheel_errors}")

            # Test check_dist function
            self.assertTrue(build_tool.check_dist(dist_path))

    def test_validator_detects_corrupted_or_empty_packages(self) -> None:
        """Test that validate_sdist and validate_wheel catch missing/corrupted files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_sdist = Path(tmpdir) / "empty.tar.gz"
            fake_sdist.write_bytes(b"invalid tar data")
            errs = build_tool.validate_sdist(fake_sdist)
            self.assertTrue(len(errs) > 0)

            fake_wheel = Path(tmpdir) / "empty.whl"
            fake_wheel.write_bytes(b"invalid zip data")
            errs = build_tool.validate_wheel(fake_wheel)
            self.assertTrue(len(errs) > 0)

            non_existent = Path(tmpdir) / "missing.whl"
            errs = build_tool.validate_wheel(non_existent)
            self.assertTrue(len(errs) > 0)

    def test_cli_version_flag(self) -> None:
        """Validate -v and --version CLI flags output."""
        for flag in ["-v", "--version"]:
            with self.subTest(flag=flag):
                stdout_buf = io.StringIO()
                with patch("sys.stdout", stdout_buf), self.assertRaises(SystemExit) as ctx:
                    main([flag])
                self.assertEqual(ctx.exception.code, 0)
                self.assertIn(f"git-sync {__version__}", stdout_buf.getvalue())

    def test_root_launcher_executable_version(self) -> None:
        """Verify the repo root launcher script responds to --version."""
        launcher = REPO_ROOT / "git-sync"
        self.assertTrue(launcher.exists())
        self.assertTrue(os.access(launcher, os.X_OK))

        res = subprocess.run(
            [sys.executable, str(launcher), "--version"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn(f"git-sync {__version__}", res.stdout)


if __name__ == "__main__":
    unittest.main()
