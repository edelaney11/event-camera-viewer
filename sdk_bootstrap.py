"""Locates a Prophesee event-camera SDK and makes it importable, before any
metavision_* module is imported.

Two kinds of install are supported:
  - OpenEB (preferred): a self-built install, found via OPENEB_INSTALL_DIR or
    ~/openeb/install, laid out lib/pythonX.Y/dist-packages, lib/metavision/hal/plugins.
  - Prophesee SDK (fallback): the official installer, found via
    PROPHESEE_INSTALL_DIR or the platform's default install path, laid out
    lib/python3/site-packages, lib/metavision/hal.

Call activate() as the first thing a script does. It may re-exec the current
process (os.execv) if environment changes need to take effect at the OS
loader level, and therefore not return.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

_REEXEC_GUARD = "_EVENT_VIEWER_SDK_REEXEC_DONE"

# Carries the pre-activate() environment across a re-exec (os.execv inherits
# the *modified* os.environ, so without this the re-exec'd process would have
# no way to recover what the environment looked like before any of this ran).
_ORIGINAL_ENV_CARRIER = "_EVENT_VIEWER_ORIGINAL_ENV"

# Snapshot of os.environ before activate() changes anything — see original_env().
_original_env: Optional[dict] = None


def original_env() -> dict:
    """A copy of the process environment as it was before activate() touched it
    — i.e. what a plain shell invocation would see.

    Use this (not os.environ) when spawning an external tool that might be a
    completely different build from this process's own SDK bindings (e.g. a
    system-packaged metavision_file_cutter alongside a self-built OpenEB
    install this process imports from). activate()'s LD_LIBRARY_PATH/plugin-
    path changes are aimed at making *this process's* imports resolve — handing
    them to an unrelated binary can point its dynamic linker at a mismatched
    version of a shared library it depends on, which is a good way to get a
    crash (observed: SIGFPE) that doesn't reproduce when run from a shell.
    """
    if _original_env is None:
        return dict(os.environ)  # activate() hasn't run — nothing to undo
    return dict(_original_env)


def _capture_original_env() -> dict:
    packed = os.environ.get(_ORIGINAL_ENV_CARRIER)
    if packed is not None:
        try:
            return json.loads(packed)
        except (ValueError, TypeError):
            pass  # fall through — treat this process as the original
    return dict(os.environ)


@dataclass
class _SdkLayout:
    name: str
    install_dir: str
    py_site_dir: str
    hal_plugin_dir: str
    hdf5_plugin_dir: str
    native_dirs: list  # dirs with shared libs/DLLs/executables (metavision_file_cutter etc.)


def _find_openeb() -> Optional[_SdkLayout]:
    install_dir = os.environ.get("OPENEB_INSTALL_DIR", os.path.expanduser("~/openeb/install"))
    matches = glob.glob(os.path.join(install_dir, "lib", "python*.*", "dist-packages"))
    if not matches:
        return None
    return _SdkLayout(
        name="OpenEB",
        install_dir=install_dir,
        py_site_dir=matches[0],
        hal_plugin_dir=os.path.join(install_dir, "lib", "metavision", "hal", "plugins"),
        hdf5_plugin_dir=os.path.join(install_dir, "lib", "hdf5", "plugin"),
        native_dirs=[os.path.join(install_dir, "lib")],
    )


def _find_prophesee() -> Optional[_SdkLayout]:
    default = r"C:\Program Files\Prophesee" if os.name == "nt" else "/opt/prophesee"
    install_dir = os.environ.get("PROPHESEE_INSTALL_DIR", default)
    site_dir = os.path.join(install_dir, "lib", "python3", "site-packages")
    if not os.path.isdir(site_dir):
        return None
    return _SdkLayout(
        name="Prophesee SDK",
        install_dir=install_dir,
        py_site_dir=site_dir,
        hal_plugin_dir=os.path.join(install_dir, "lib", "metavision", "hal"),
        hdf5_plugin_dir=os.path.join(install_dir, "lib", "hdf5", "plugin"),
        native_dirs=[
            os.path.join(install_dir, "bin"),
            os.path.join(install_dir, "third_party", "bin"),
        ],
    )


def find_tool(name: str):
    """Locate an SDK command-line tool (e.g. metavision_file_cutter).

    Returns (path, native_dirs):
      - Found on PATH (a system-packaged binary, independent of anything this
        process imports from): native_dirs is [] — it should resolve its own
        shared-library dependencies through the normal system search, and
        pointing it at one of our detected installs' libraries instead can
        load a mismatched version of something it depends on (observed:
        SIGFPE crashing metavision_file_cutter — see original_env()).
      - Found inside a detected install's own bin/ dir: native_dirs is that
        install's shared-library dirs. A self-built/installed tool like this
        commonly has no rpath baked in, so it needs those dirs on
        LD_LIBRARY_PATH (POSIX) / PATH (Windows) to find its own sibling
        libraries — it's the matching version, not an unrelated build.
      - Not found at all: (None, []).
    """
    exe = shutil.which(name)
    if exe:
        return exe, []

    exe_name = f"{name}.exe" if os.name == "nt" else name
    for sdk in (_find_openeb(), _find_prophesee()):
        if sdk is None:
            continue
        candidate = os.path.join(sdk.install_dir, "bin", exe_name)
        if os.path.exists(candidate):
            return candidate, sdk.native_dirs
    return None, []


def _prepend_env(name: str, dirs: list) -> bool:
    """Prepend any of `dirs` not already present in env var `name`. Returns
    True if the env var was actually changed."""
    current = os.environ.get(name, "")
    current_parts = current.split(os.pathsep) if current else []
    to_add = [d for d in dirs if d and d not in current_parts]
    if not to_add:
        return False
    os.environ[name] = os.pathsep.join(to_add + current_parts)
    return True


def _fix_cv2_qt_fonts() -> None:
    """cv2's bundled Qt platform plugin looks for a 'fonts' dir next to itself
    and can fail to start on some installs if it's missing; harmless to pre-create."""
    py_root = os.path.dirname(os.path.dirname(sys.executable))
    if os.name == "nt":
        fonts_dir = os.path.join(py_root, "Lib", "site-packages", "cv2", "qt", "fonts")
    else:
        pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        fonts_dir = os.path.join(py_root, "lib", pyver, "site-packages", "cv2", "qt", "fonts")
    try:
        os.makedirs(fonts_dir, exist_ok=True)
    except OSError:
        pass  # best-effort only (e.g. cv2 not installed under a writable venv path)


def activate() -> None:
    global _original_env
    if _original_env is None:
        # Must happen before any of the _prepend_env calls below, and the
        # marker must land in os.environ now — os.execv (if a re-exec follows)
        # only inherits os.environ, not this module's own Python state.
        _original_env = _capture_original_env()
        os.environ[_ORIGINAL_ENV_CARRIER] = json.dumps(_original_env)

    sdk = _find_openeb() or _find_prophesee()
    if sdk is None:
        print(
            "[WARN] No Prophesee event-camera SDK found (checked OPENEB_INSTALL_DIR / "
            "~/openeb/install, then PROPHESEE_INSTALL_DIR / the default Prophesee SDK "
            "install path). Continuing and hoping metavision_* is already importable.",
            file=sys.stderr,
        )
        return

    if sdk.py_site_dir not in sys.path:
        sys.path.insert(0, sdk.py_site_dir)

    # MV_HAL_PLUGIN_PATH / HDF5_PLUGIN_PATH are read by SDK code the first time
    # it's actually used (after this function returns), not by the OS loader
    # at process start — safe to set without a re-exec, on any platform.
    _prepend_env("MV_HAL_PLUGIN_PATH", [sdk.hal_plugin_dir])
    _prepend_env("HDF5_PLUGIN_PATH", [sdk.hdf5_plugin_dir])

    need_reexec = False

    if os.name == "nt":
        # PATH additions help subprocess tools (metavision_file_cutter); for the
        # extension modules' own DLL dependencies, os.add_dll_directory() is what
        # actually matters on Python 3.8+, and it's effective immediately — no
        # re-exec needed just for native library loading on Windows.
        _prepend_env("PATH", sdk.native_dirs)
        for d in sdk.native_dirs:
            if os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except (OSError, AttributeError):
                    pass
    else:
        # POSIX: the dynamic linker reads LD_LIBRARY_PATH at process start, so a
        # runtime change to os.environ isn't reliably honored — re-exec instead.
        if _prepend_env("LD_LIBRARY_PATH", sdk.native_dirs):
            need_reexec = True
        # Required for the GenX320 (and IMX636) v4l2 plugin on Raspberry Pi: parse
        # MIPI frame-end markers and use dma-heap allocation instead of mmap.
        # Harmless no-ops on non-v4l2 (e.g. USB) setups.
        for envvar, val in [("PSEE_VAR_V4L2_BSIZE", "1"), ("V4L2_HEAP", "vidbuf_cached")]:
            if envvar not in os.environ:
                os.environ[envvar] = val
                need_reexec = True

    _fix_cv2_qt_fonts()

    if need_reexec and not os.environ.get(_REEXEC_GUARD):
        os.environ[_REEXEC_GUARD] = "1"
        os.execv(sys.executable, [sys.executable] + sys.argv)
