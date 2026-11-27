"""Compatibility shim for genlayer-test 0.30.1 + GenVM runner v0.6 std layout.

Root cause (verified 2026-11-26): ``gltest.direct.sdk_compat`` was written
for the GenVM v0.3 SDK layout, where the codec and types lived at
``genlayer.calldata`` / ``genlayer.types``. In the v0.6 std lib shipped with
runner v0.6.0-rc17 they moved to ``genlayer.py.calldata`` /
``genlayer.py.types``. As a result ``sdk_compat.import_calldata()`` raised
``ImportError``, which ``loader._inject_message_to_fd0()`` swallows
(``except ImportError: return``) — so the message context was never injected
into fd 0 and the contract import read the process's real stdin instead:
it hangs forever on a pipe, or fails with
``DecodingError: unexpected end of memory`` on ``/dev/null``.

This shim rebinds the five helpers to the v0.6 locations in every module
that imported them directly (sdk_compat, loader, vm). It runs before any
test (conftest is loaded before fixtures), so ``direct_deploy`` works.

Remove this file if a future genlayer-test release fixes sdk_compat.
"""

import gltest.direct.loader as _loader
import gltest.direct.sdk_compat as _compat
import gltest.direct.vm as _vm
import gltest.direct.wasi_mock as _wasi_mock


def _import_calldata():
    from genlayer.py import calldata

    return calldata


def _import_types():
    from genlayer.py import types as sdk_types

    return sdk_types


def _import_address():
    return _import_types().Address


def _import_address_u256():
    _types = _import_types()
    return _types.Address, _types.u256


def _import_lazy():
    return _import_types().Lazy


for _module in (_compat, _loader, _vm, _wasi_mock):
    _module.import_calldata = _import_calldata
    _module.import_types = _import_types
    _module.import_address = _import_address
    _module.import_address_u256 = _import_address_u256
    _module.import_lazy = _import_lazy


def _allocate_contract_v06(contract_cls, vm, *args, **kwargs):
    """v0.6 replacement for ``gltest.direct.loader._allocate_contract``.

    The loader's version was written for the v0.3 SDK layout:
    ``from genlayer.storage import ROOT_SLOT_ID`` and
    ``_storage_build(_BuilderCtx.empty(), contract_cls)``. Neither exists
    in the v0.6 std (``genlayer.storage`` is gone, ``_BuilderCtx`` is gone,
    ``_storage_build`` now takes ``(cls, generics_map)``). Both of the
    loader's import attempts then raised ImportError, which it swallows,
    so it fell back to a plain ``contract_cls(...)`` — an instance without
    ``__type_desc__``/``_storage_slot``, which crashes on the first storage
    assignment in ``__init__`` (``AttributeError: ... has no attribute
    '__type_desc__'``).

    This mirrors the loader's first (v0.3) path with v0.6 imports and
    signature, allocating the instance from the VM's storage manager so
    state persists across calls in the test.
    """
    from genlayer.py.storage import ROOT_SLOT_ID
    from genlayer.py.storage._internal.generate import (
        ORIGINAL_INIT_ATTR,
        _storage_build,
    )

    td = _storage_build(contract_cls, {})
    slot = vm._storage.get_store_slot(ROOT_SLOT_ID)
    instance = td.get(slot, 0)

    init = getattr(td, "cls", None)
    if init is None:
        init = getattr(contract_cls, "__init__", None)
    else:
        init = getattr(init, "__init__", None)
    if init is not None:
        if hasattr(init, ORIGINAL_INIT_ATTR):
            init = getattr(init, ORIGINAL_INIT_ATTR)
        init(instance, *args, **kwargs)

    return instance


_loader._allocate_contract = _allocate_contract_v06
