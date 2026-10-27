"""Configuracio dels tests: arrel del repositori al sys.path i xarxa bloquejada."""

import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Cap test pot obrir una connexio de xarxa real."""

    def blocked(*args, **kwargs):
        raise RuntimeError("Xarxa bloquejada als tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
