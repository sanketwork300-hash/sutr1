"""Import every model module so its table is registered on ``SQLModel.metadata``.

Alembic autogenerate and the test suite build the schema from the shared metadata,
which only knows about tables whose module has been imported. Discovering the
modules here means a new model can't be forgotten in one of those places.
"""

import importlib
import pkgutil

import sutr.models


def register_all_models() -> None:
    for module in pkgutil.iter_modules(sutr.models.__path__):
        importlib.import_module(f"{sutr.models.__name__}.{module.name}")
