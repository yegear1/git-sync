---
id: "k-010"
title: "PEP 621/639 SPDX License Expressions and setuptools pkg_resources Deprecation Suppression"
date: 2026-10-26
tags:
  - "python"
  - "packaging"
  - "setuptools"
  - "pep621"
  - "pep639"
severity: "medium"
affected_components:
  - "pyproject.toml"
  - "scripts/build.py"
  - "tests/test_packaging.py"
symptoms:
  - "SetuptoolsDeprecationWarning: `project.license` as a TOML table is deprecated"
  - "DeprecationWarning: pkg_resources is deprecated as an API during sdist and wheel builds"
---

# [k-010] PEP 621/639 SPDX License Expressions and setuptools pkg_resources Deprecation Suppression

## Problem / Symptom
1. On modern setuptools versions (>= 77.0.0) under Python 3.12+/3.13/3.14, defining `project.license = {text = "MIT"}` in `pyproject.toml` triggers `SetuptoolsDeprecationWarning`:
   ```text
   SetuptoolsDeprecationWarning: `project.license` as a TOML table is deprecated
   Please use a simple string containing a SPDX expression for `project.license`.
   ```
2. Conversely, on standard distribution-packaged setuptools (< 77.0.0, such as 68.1.2 on Ubuntu 24.04 LTS), PEP 639 string licenses are NOT yet recognized, causing package builds to fail with:
   ```text
   ValueError: invalid pyproject.toml config: `project.license`.
   configuration error: `project.license` must be valid exactly by one definition (2 matches found):
       - keys: 'file': {type: string}
       - keys: 'text': {type: string}
   ```
3. During `setuptools.build_meta` build operations (`build_sdist` and `build_wheel`), setuptools dynamically iterates registered entry-points (including `easy_install` and `develop`), triggering a legacy `pkg_resources` import and warning:
   ```text
   DeprecationWarning: pkg_resources is deprecated as an API. See https://setuptools.pypa.io/en/latest/pkg_resources.html
   ```

---

## Approved Solution

1. **Retain PEP 621 table format for universal backward compatibility:**
   Maintain `license = { text = "MIT" }` in `pyproject.toml` so that standard Linux distribution setuptools packages (< 77.0.0) build without validation errors, and `test_pyproject_metadata_integrity` accepts both formats:
   ```toml
   [project]
   license = { text = "MIT" }
   ```

2. **Scoped filter for setuptools internal deprecation during distribution builds:**
   In zero-dependency packaging scripts (`scripts/build.py`), encapsulate setuptools build backend invocations within a warning filter context to keep distribution and test outputs clean:
   ```python
   with warnings.catch_warnings():
       warnings.filterwarnings("ignore", message=r".*pkg_resources is deprecated.*", category=DeprecationWarning)
       warnings.filterwarnings("ignore", module=r"pkg_resources.*", category=DeprecationWarning)
       from setuptools import build_meta as bm

       sdist_name = bm.build_sdist(str(dist_dir))
       wheel_name = bm.build_wheel(str(dist_dir))
   ```

3. **Validate in packaging test suite:**
   Ensure `test_pyproject_metadata_integrity` parses `license = "MIT"` via `tomllib`, and `test_build_distribution_and_archive_hygiene` validates zero unhandled `DeprecationWarning` or `UserWarning` during package creation.
