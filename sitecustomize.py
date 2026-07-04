from __future__ import annotations

import sys
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import PathFinder
from types import ModuleType
from typing import Optional

_OWNER_ADDON_ATTACHED = False
_FINDER_INSTALLED = False


def _attach_owner_addon(module: ModuleType) -> None:
    global _OWNER_ADDON_ATTACHED
    if _OWNER_ADDON_ATTACHED:
        return
    try:
        import owner_addon

        owner_addon.install(module)
        _OWNER_ADDON_ATTACHED = True
    except Exception as exc:
        print(f"Owner addon failed to install: {type(exc).__name__}: {exc}", flush=True)


class _SupportBotLoader(Loader):
    def __init__(self, wrapped: Loader):
        self.wrapped = wrapped

    def create_module(self, spec):
        create_module = getattr(self.wrapped, "create_module", None)
        if create_module is None:
            return None
        return create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self.wrapped.exec_module(module)
        _attach_owner_addon(module)


class _SupportBotFinder(MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname != "support_bot":
            return None
        spec = PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _SupportBotLoader(spec.loader)
        return spec


def _install_import_hook() -> None:
    global _FINDER_INSTALLED
    if _FINDER_INSTALLED:
        return
    sys.meta_path.insert(0, _SupportBotFinder())
    _FINDER_INSTALLED = True


if "support_bot" in sys.modules:
    _attach_owner_addon(sys.modules["support_bot"])
else:
    _install_import_hook()
