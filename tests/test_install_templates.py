"""H12.4: the installer finds what it copies in the package, or in the checkout.

Outside review #5: the installer walked four directories up from its own
file to find ``narada/`` and ``memory_template/``, which a wheel doesn't
carry, so ``pip install smriti`` couldn't set itself up. A wheel now carries
copies under ``smriti/_data/`` (pyproject's force-include), read through
``importlib.resources``; a checkout reads its own files, as before.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from smriti.install import core, templates

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _force_include() -> dict[str, str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    table: dict[str, str] = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    return table


def test_the_wheel_carries_every_template_where_the_resolver_looks() -> None:
    table = _force_include()
    for relative in templates.ALL:
        assert table.get(relative) == f"smriti/{templates.PACKAGED_DIR}/{relative}", relative
        assert (REPO / relative).exists(), relative


def test_a_checkout_reads_its_own_files() -> None:
    """The live install is an editable one: its installer must compare the
    deployed scripts against the checkout's, not against a copy."""
    assert templates.checkout() == REPO
    for relative in templates.ALL:
        assert templates.packaged(relative) is None
        assert templates.template(relative) == REPO / relative
    assert core.SCRIPTS_DIR == REPO / "narada" / ".smriti"
    assert core.MEMORY_TEMPLATE == REPO / "memory_template"


def _site(tmp_path: Path, *, data: bool) -> Path:
    """An installed copy of the package's resolver, with or without the wheel's data."""
    site = tmp_path / "site-packages"
    for module in ("__init__.py", "install/__init__.py", "install/templates.py"):
        target = site / "smriti" / module
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SRC / "smriti" / module, target)
    if data:
        for source, packaged in _force_include().items():
            if not packaged.startswith(f"smriti/{templates.PACKAGED_DIR}/"):
                continue
            target = site / packaged
            if (REPO / source).is_dir():
                shutil.copytree(REPO / source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO / source, target)
    return site


def _resolve_in(site: Path, cwd: Path) -> dict[str, object]:
    probe = (
        "import json\n"
        "import smriti\n"
        "from smriti.install import templates as t\n"
        "print(json.dumps({'package': smriti.__file__,\n"
        "                  'checkout': str(t.checkout()) if t.checkout() else None,\n"
        "                  'found': {r: str(t.template(r)) if t.template(r) else None\n"
        "                            for r in t.ALL}}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(site)
    out = subprocess.run([sys.executable, "-c", probe], cwd=cwd, env=env,
                         capture_output=True, text=True, timeout=60, check=True)
    result: dict[str, object] = json.loads(out.stdout)
    return result


def test_an_installed_wheel_reads_its_packaged_copies(tmp_path: Path) -> None:
    site = _site(tmp_path, data=True)
    result = _resolve_in(site, cwd=tmp_path)

    assert Path(str(result["package"])).parent == site / "smriti"
    assert result["checkout"] is None
    found = result["found"]
    assert isinstance(found, dict)
    for relative in templates.ALL:
        assert found[relative] == str(site / "smriti" / templates.PACKAGED_DIR / relative)
    copied = site / "smriti" / templates.PACKAGED_DIR
    assert (copied / "narada/.smriti/wake.py").read_bytes() == (
        REPO / "narada/.smriti/wake.py").read_bytes()
    assert (copied / "memory_template/journal/.gitkeep").is_file()


def test_an_installed_package_never_reads_outside_itself(tmp_path: Path) -> None:
    """Without its data, an installed package finds nothing, even with a
    checkout-shaped directory beside its site-packages."""
    site = _site(tmp_path, data=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'decoy'\n", encoding="utf-8")
    (tmp_path / "memory_template").mkdir()
    (tmp_path / "narada" / ".smriti").mkdir(parents=True)

    result = _resolve_in(site, cwd=tmp_path)

    assert result["checkout"] is None
    assert result["found"] == {relative: None for relative in templates.ALL}


def test_the_privacy_doc_is_the_local_copy() -> None:
    from smriti.install import privacy

    assert privacy.privacy_doc().startswith(str(REPO / "docs" / "PRIVACY.md"))


def test_a_missing_template_is_reported_and_the_install_goes_on(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(core, "MEMORY_TEMPLATE", None)
    monkeypatch.setattr(core, "SCRIPTS_DIR", None)
    core.install_memory_template(tmp_path / "root")
    core.install_wake_files(tmp_path / "root")
    out = capsys.readouterr().out
    assert "[memory] template not found (memory_template)" in out
    assert out.count("[wake] template missing") == len(core.DEPLOYED)
