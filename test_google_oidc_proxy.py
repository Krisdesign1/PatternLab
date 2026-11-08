"""The local Google relay cannot become a general Internet proxy."""
import socket
import threading
import unittest
from unittest.mock import Mock

from deployment.linux import google_oidc_proxy as proxy


def request(authority='accounts.google.com:443', method='CONNECT', extra=b''):
    return f'{method} {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode() + extra


class GoogleRelayPolicyTests(unittest.TestCase):
    def test_only_exact_google_names_and_https_are_accepted(self):
        for host in proxy.HOSTS:
            self.assertEqual(proxy.parse_connect(request(host+':443')), host)
        for target in ('accounts.google.com:80', '127.0.0.1:443', '[::1]:443',
                       '169.254.169.254:443', 'accounts.google.com.evil.example:443',
                       'v3.football.api-sports.io:443', 'user@accounts.google.com:443',
                       'https://accounts.google.com:443', 'accounts.google.com.:443'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                proxy.parse_connect(request(target))

    def test_malformed_headers_and_pipelined_payload_are_refused(self):
        malformed = [request(method='GET'), request(extra=b'GET / HTTP/1.1\r\n\r\n'),
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nHost: evil.example\r\n\r\n',
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nHost: accounts.google.com:443\r\nHost: accounts.google.com:443\r\n\r\n',
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n',
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nContent-Length: 4\r\n\r\n',
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nFolded: hi\r\n injected\r\n\r\n',
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nX: hi\nBad: x\r\n\r\n',
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nX: \xff\r\n\r\n',
                     b'CONNECT accounts.google.com:443 HTTP/1.1\r\nX: '+b'a'*8192+b'\r\n\r\n']
        for header in malformed:
            with self.subTest(header=header[:70]), self.assertRaises(ValueError):
                proxy.parse_connect(header)

    def test_dns_cannot_redirect_google_to_private_or_metadata_address(self):
        factory = Mock()
        for address in ('127.0.0.1', '169.254.169.254', '10.0.0.1', '192.168.1.2',
                        '::1', 'fd00::1', '::ffff:8.8.8.8'):
            resolver = Mock(return_value=[(socket.AF_INET6 if ':' in address else socket.AF_INET,
                                           socket.SOCK_STREAM, 6, '', (address, 443))])
            with self.subTest(address=address), self.assertRaises(ValueError):
                proxy.connect_google('accounts.google.com', resolver=resolver, socket_factory=factory)
        factory.assert_not_called()

    def test_forbidden_host_does_not_even_reach_dns(self):
        resolver = Mock()
        with self.assertRaises(ValueError):
            proxy.connect_google('example.com', resolver=resolver)
        resolver.assert_not_called()

    def test_validated_numeric_address_is_used_without_a_second_dns_lookup(self):
        resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
        factory = Mock()
        result = proxy.connect_google('accounts.google.com', resolver=resolver, socket_factory=factory)
        self.assertIs(result, factory.return_value)
        factory.return_value.connect.assert_called_once_with(('8.8.8.8', 443))
        factory.return_value.settimeout.assert_called_once_with(proxy.SOCKET_TIMEOUT)


class GoogleRelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.connector = Mock()
        self.server = proxy.GoogleOIDCProxy(('127.0.0.1', 0), connector=self.connector)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def client(self):
        value = socket.create_connection(self.server.server_address, timeout=2)
        value.settimeout(2)
        self.addCleanup(value.close)
        return value

    def test_connect_tunnel_relays_bytes_both_ways_without_interpreting_them(self):
        upstream, origin = socket.socketpair()
        self.addCleanup(origin.close)
        self.connector.return_value = upstream
        client = self.client()
        client.sendall(request())
        self.assertEqual(client.recv(1024), b'HTTP/1.1 200 Connection Established\r\n\r\n')
        client.sendall(b'\x16\x03\x01opaque-tls-client')
        self.assertEqual(origin.recv(1024), b'\x16\x03\x01opaque-tls-client')
        origin.sendall(b'\x17\x03\x03opaque-tls-server')
        self.assertEqual(client.recv(1024), b'\x17\x03\x03opaque-tls-server')
        self.connector.assert_called_once_with('accounts.google.com')

    def test_other_api_and_pipelining_never_open_an_upstream_socket(self):
        for header in (request('v3.football.api-sports.io:443'), request(extra=b'INJECTED')):
            client = self.client()
            client.sendall(header)
            self.assertIn(b'403 Forbidden', client.recv(1024))
        self.connector.assert_not_called()

    def test_oversized_headers_are_closed_before_connection(self):
        client = self.client()
        client.sendall(b'CONNECT accounts.google.com:443 HTTP/1.1\r\nX: '+b'a'*(proxy.MAX_HEADER+1))
        self.assertIn(b'400 Bad Request', client.recv(1024))
        self.connector.assert_not_called()


if __name__ == '__main__':
    unittest.main()
