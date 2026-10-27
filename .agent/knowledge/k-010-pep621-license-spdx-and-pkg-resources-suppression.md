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
On modern setuptools versions (>= 77.0.0) under Python 3.12+/3.13/3.14:
1. Defining `project.license = {text = "MIT"}` in `pyproject.toml` triggers `SetuptoolsDeprecationWarning`:
   ```text
   SetuptoolsDeprecationWarning: `project.license` as a TOML table is deprecated
   Please use a simple string containing a SPDX expression for `project.license`.
   ```
2. During `setuptools.build_meta` build operations (`build_sdist` and `build_wheel`), setuptools dynamically iterates registered entry-points (including `easy_install` and `develop`), triggering a legacy `pkg_resources` import and warning:
   ```text
   DeprecationWarning: pkg_resources is deprecated as an API. See https://setuptools.pypa.io/en/latest/pkg_resources.html
   ```

---

## Approved Solution

1. **Standardize SPDX license string in `pyproject.toml`:**
   Replace the legacy table `license = {text = "MIT"}` with the PEP 639 SPDX expression:
   ```toml
   [project]
   license = "MIT"
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
