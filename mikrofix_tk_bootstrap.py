"""Windows priprava Tcl/Tk pred prvim uvozom tkinterja."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


_TCL_RUNTIME = None
_TCL_DLL_DIRECTORY = None
_TCL_ENCODING_OBJECTS = None


def prepare_tcl_tk() -> bool:
    """Inicializira Tcl poti, ki jih Python Install Manager včasih izpusti."""
    global _TCL_RUNTIME, _TCL_DLL_DIRECTORY, _TCL_ENCODING_OBJECTS
    if os.name != "nt":
        return True
    if "_tkinter" in sys.modules:
        return True
    if _TCL_RUNTIME is not None:
        return True

    runtime_root = Path(sys.base_prefix).resolve()
    dll_dir = runtime_root / "DLLs"
    tcl_root = runtime_root / "tcl"

    # Python Install Manager lahko namesti Tcl/Tk, ne da bi nastavil poti do
    # skriptnih knjižnic. Brez njiju se tkinter ustavi z napako init.tcl.
    tcl_library = next(
        (
            path
            for path in (tcl_root / "tcl9.0", tcl_root / "tcl8.6")
            if (path / "init.tcl").exists()
        ),
        None,
    )
    tk_library = next(
        (
            path
            for path in (tcl_root / "tk9.0", tcl_root / "tk8.6")
            if (path / "tk.tcl").exists()
        ),
        None,
    )
    previous_tcl_library = os.environ.pop("TCL_LIBRARY", None)
    previous_tk_library = os.environ.pop("TK_LIBRARY", None)

    if hasattr(os, "add_dll_directory") and dll_dir.exists():
        try:
            # Ročaj mora ostati živ, dokler tkinter uporablja DLL-je.
            _TCL_DLL_DIRECTORY = os.add_dll_directory(str(dll_dir))
        except OSError:
            _TCL_DLL_DIRECTORY = None

    candidates = (
        dll_dir / "tcl90.dll",
        dll_dir / "tcl86t.dll",
    )
    tcl_dll = next((path for path in candidates if path.exists()), None)
    if tcl_dll is None:
        if previous_tcl_library is not None:
            os.environ["TCL_LIBRARY"] = previous_tcl_library
        if previous_tk_library is not None:
            os.environ["TK_LIBRARY"] = previous_tk_library
        return False

    try:
        runtime = ctypes.WinDLL(str(tcl_dll))
        find_executable = runtime.Tcl_FindExecutable
        find_executable.argtypes = [ctypes.c_char_p]
        find_executable.restype = None
        base_executable = runtime_root / "python.exe"
        executable = base_executable if base_executable.exists() else Path(sys.executable)
        find_executable(str(executable).encode("utf-8"))

        # Nekateri Python Install Manager paketi imajo krožno inicializacijo:
        # Windows zahteva cp1252, preden Tcl prek TCL_LIBRARY najde cp1252.enc.
        # Iskalno pot kodiranj zato podamo neposredno prek javnega Tcl C API-ja.
        encoding_dir = tcl_library / "encoding" if tcl_library else None
        if encoding_dir is not None and encoding_dir.is_dir():
            new_string = runtime.Tcl_NewStringObj
            new_string.argtypes = [ctypes.c_char_p, ctypes.c_int]
            new_string.restype = ctypes.c_void_p
            new_list = runtime.Tcl_NewListObj
            new_list.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
            new_list.restype = ctypes.c_void_p
            append_element = runtime.Tcl_ListObjAppendElement
            append_element.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            ]
            append_element.restype = ctypes.c_int
            set_encoding_path = runtime.Tcl_SetEncodingSearchPath
            set_encoding_path.argtypes = [ctypes.c_void_p]
            set_encoding_path.restype = ctypes.c_int

            path_object = new_string(encoding_dir.as_posix().encode("utf-8"), -1)
            list_object = new_list(0, None)
            if (
                path_object
                and list_object
                and append_element(None, list_object, path_object) == 0
                and set_encoding_path(list_object) == 0
            ):
                _TCL_ENCODING_OBJECTS = (path_object, list_object)
    except (OSError, AttributeError):
        if previous_tcl_library is not None:
            os.environ["TCL_LIBRARY"] = previous_tcl_library
        if previous_tk_library is not None:
            os.environ["TK_LIBRARY"] = previous_tk_library
        return False

    if tcl_library is not None:
        os.environ["TCL_LIBRARY"] = tcl_library.as_posix()
    elif previous_tcl_library is not None:
        os.environ["TCL_LIBRARY"] = previous_tcl_library
    if tk_library is not None:
        os.environ["TK_LIBRARY"] = tk_library.as_posix()
    elif previous_tk_library is not None:
        os.environ["TK_LIBRARY"] = previous_tk_library

    # DLL mora ostati naložen do uvoza _tkinterja.
    _TCL_RUNTIME = runtime
    return True
