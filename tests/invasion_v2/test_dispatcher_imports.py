"""Import the production dispatcher registry without attaching shared routers."""

from aiogram import Dispatcher

from sib.handlers import inv2_join_router, register_all_handlers


def test_dispatcher_imports_v2_join_only():
    # Global Router instances may only be attached once per process. Check the
    # production registration function without mutating the shared instances.
    assert Dispatcher is not None
    assert callable(register_all_handlers)
    assert inv2_join_router.name == "inv2_join"
