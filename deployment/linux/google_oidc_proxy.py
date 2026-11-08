#!/usr/bin/env python3
"""Loopback CONNECT relay restricted to Google's four OIDC HTTPS hosts.

TLS remains end-to-end between Authlib and Google. No request headers, tokens,
response bodies, or connection errors are logged. This service has no app keys.
"""
from __future__ import annotations

import ipaddress
import re
import select
import socket
import socketserver
import threading
import time


BIND = ('127.0.0.1', 18081)
HOSTS = frozenset({'accounts.google.com', 'oauth2.googleapis.com',
                   'www.googleapis.com', 'openidconnect.googleapis.com'})
MAX_HEADER = 8192
SOCKET_TIMEOUT = 10
IDLE_TIMEOUT = 45
TUNNEL_TIMEOUT = 120
MAX_CLIENTS = 16
TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def parse_connect(header: bytes) -> str:
    """Validate the entire HTTP preface before resolving or opening any host."""
    if len(header) > MAX_HEADER or not header.endswith(b'\r\n\r\n'):
        raise ValueError('invalid_preface')
    try:
        lines = header[:-4].decode('ascii').split('\r\n')
    except UnicodeError:
        raise ValueError('invalid_preface') from None
    request = lines[0].split(' ')
    if (len(request) != 3 or request[0] != 'CONNECT'
            or request[2] not in ('HTTP/1.1', 'HTTP/1.0')):
        raise ValueError('invalid_method')
    authority = request[1]
    host, separator, port = authority.rpartition(':')
    if separator != ':' or port != '443' or host not in HOSTS:
        raise ValueError('forbidden_destination')
    seen = set()
    for line in lines[1:]:
        name, separator, value = line.partition(':')
        if (not separator or not TOKEN.fullmatch(name)
                or any(ord(char) < 32 and char != '\t' or ord(char) == 127 for char in value)):
            raise ValueError('invalid_header')
        name, value = name.lower(), value.strip()
        if name in seen:
            raise ValueError('duplicate_header')
        seen.add(name)
        if (name in ('transfer-encoding', 'upgrade', 'proxy-authorization')
                or name == 'content-length' and value != '0'
                or name == 'host' and value != authority):
            raise ValueError('invalid_header')
    return host


def connect_google(host: str, *, resolver=socket.getaddrinfo,
                   socket_factory=socket.socket) -> socket.socket:
    """Pin validated public DNS results; never let connect perform another lookup."""
    if host not in HOSTS:
        raise ValueError('forbidden_destination')
    addresses = resolver(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise OSError('no_destination')
    for family, kind, protocol, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or kind != socket.SOCK_STREAM or not ip.is_global
                or getattr(ip, 'ipv4_mapped', None) is not None):
            raise ValueError('non_public_destination')
    for family, kind, protocol, _, address in addresses:
        connection = socket_factory(family, kind, protocol)
        try:
            connection.settimeout(SOCKET_TIMEOUT)
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise OSError('destination_unavailable')


def relay(client: socket.socket, upstream: socket.socket) -> None:
    deadline = time.monotonic() + TUNNEL_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        readable, _, _ = select.select([client, upstream], [], [], min(IDLE_TIMEOUT, remaining))
        if not readable:
            return
        for source in readable:
            content = source.recv(16384)
            if not content:
                return
            destination = upstream if source is client else client
            destination.sendall(content)


class ConnectHandler(socketserver.BaseRequestHandler):
    def reply(self, status: int) -> None:
        reason = {200: 'Connection Established', 400: 'Bad Request',
                  403: 'Forbidden', 502: 'Bad Gateway', 503: 'Service Unavailable'}[status]
        ending = '' if status == 200 else 'Connection: close\r\nContent-Length: 0\r\n'
        self.request.sendall(f'HTTP/1.1 {status} {reason}\r\n{ending}\r\n'.encode('ascii'))

    def handle(self) -> None:
        self.request.settimeout(SOCKET_TIMEOUT)
        try:
            header = b''
            while b'\r\n\r\n' not in header:
                chunk = self.request.recv(min(1024, MAX_HEADER + 1 - len(header)))
                if not chunk:
                    return
                header += chunk
                if len(header) > MAX_HEADER:
                    self.reply(400)
                    return
            try:
                host = parse_connect(header)
            except ValueError:
                self.reply(403)
                return
            try:
                upstream = self.server.connector(host)
            except (OSError, ValueError):
                self.reply(502)
                return
            with upstream:
                upstream.settimeout(SOCKET_TIMEOUT)
                self.reply(200)
                relay(self.request, upstream)
        except OSError:
            # Do not log client or provider data on failed/disconnected tunnels.
            return


class GoogleOIDCProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_CLIENTS

    def __init__(self, address=BIND, *, connector=connect_google):
        self.connector = connector
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(address, ConnectHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Fixed silence: the standard implementation prints exception context.
        pass


def main():
    with GoogleOIDCProxy() as server:
        server.serve_forever(poll_interval=0.5)


if __name__ == '__main__':
    main()
