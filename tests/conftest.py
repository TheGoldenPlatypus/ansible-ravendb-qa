import ast
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "library", REPO_ROOT / "module_utils"):
    sys.path.insert(0, str(p))


def _install_ansible_shim():
    if "ansible.module_utils.ravendb_client" in sys.modules:
        return

    if "ansible" not in sys.modules:
        sys.modules["ansible"] = types.ModuleType("ansible")
    if "ansible.module_utils" not in sys.modules:
        mu = types.ModuleType("ansible.module_utils")
        sys.modules["ansible.module_utils"] = mu
        sys.modules["ansible"].module_utils = mu

    def _install(util_name):
        """Load module_utils/<util_name>.py under both dotted and bare names."""
        spec = importlib.util.spec_from_file_location(
            "ansible.module_utils." + util_name,
            str(REPO_ROOT / "module_utils" / (util_name + ".py")),
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sys.modules["ansible.module_utils." + util_name] = m
        setattr(sys.modules["ansible.module_utils"], util_name, m)
        sys.modules[util_name] = m
        return m

    mod = _install("ravendb_client")
    _install("polling")

    if "ansible.module_utils.basic" not in sys.modules:
        basic = types.ModuleType("ansible.module_utils.basic")
        class _StubModule:
            def __init__(self, argument_spec=None, **kw):
                self.params = {}
            def fail_json(self, **kw): raise SystemExit(kw)
            def exit_json(self, **kw): return kw
        basic.AnsibleModule = _StubModule
        sys.modules["ansible.module_utils.basic"] = basic
        sys.modules["ansible.module_utils"].basic = basic


_install_ansible_shim()


_BANNER_SEEN = set()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    file_path = nodeid.split("::", 1)[0]
    if file_path in _BANNER_SEEN:
        return
    _BANNER_SEEN.add(file_path)

    candidates = [
        REPO_ROOT / file_path,
        REPO_ROOT / "tests" / file_path,
        Path(file_path),
    ]
    abs_path = next((c for c in candidates if c.exists()), None)
    if abs_path is None:
        return

    try:
        source = abs_path.read_text(encoding="utf-8")
        docstring = ast.get_docstring(ast.parse(source))
    except (OSError, SyntaxError):
        return
    if not docstring:
        return

    out = sys.__stdout__
    out.write("\n\n\n")
    out.write("#" * 100 + "\n")
    out.write("#" + " " * 98 + "#\n")
    out.write("#  " + f"FILE: {file_path}".ljust(95) + " #\n")
    out.write("#" + " " * 98 + "#\n")
    out.write("#" * 100 + "\n")
    out.write(docstring.rstrip() + "\n")
    out.write("#" * 100 + "\n\n")
    out.flush()
