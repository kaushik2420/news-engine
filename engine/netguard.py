"""Refuse URLs that point inside the network (SSRF guard) before the engine fetches anything a user typed."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURL(ValueError):
    pass


def assert_public_url(url: str) -> None:
    parts = urlsplit(url or "")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UnsafeURL("URL must start with https:// (or http://) and name a host")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeURL(f"{host} is not a public host")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve {host}: {e.strerror or e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise UnsafeURL(f"{host} resolves to a non-public address")
