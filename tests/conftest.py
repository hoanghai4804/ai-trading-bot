"""Tests never touch the network: any real HTTP request (Alpaca, Discord) fails loudly."""

import pytest
import requests


def _blocked(*args, **kwargs):
    raise RuntimeError("network access is disabled in tests")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", _blocked)
    monkeypatch.setattr(requests, "post", _blocked)
