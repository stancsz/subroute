"""Unit tests may use local fixture servers, never live provider sockets."""

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def no_live_provider_connections_in_unit_tests(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def check(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            try:
                local = address[0] == "localhost" or ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = False
            # Local ephemeral fixture servers are allowed. A running gateway
            # or subscription sidecar is still a live provider entry point.
            deployed = local and address[1] in {4000, 4005, 4015, 11435}
            if not local or deployed:
                pytest.fail("A unit test attempted a live network connection; mock the provider or use explicit live opt-in")

    def guarded_connect(sock, address):
        check(sock, address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        check(sock, address)
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
