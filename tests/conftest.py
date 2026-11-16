"""Marca la aceptación de MoneyPrinter sin modificar el archivo que se envía al laboratorio."""

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name == "test_moneyprinter_speaches_acceptance.py":
            item.add_marker(pytest.mark.evidencia_operativa)
