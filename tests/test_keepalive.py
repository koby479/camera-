"""keepalive_if_due must never wait for the device: a wait there is a visible freeze in the video."""
import socket
import time
import unittest

from app.core import dvrip


def _client_pair():
    a, b = socket.socketpair()
    c = dvrip.DVRIPClient("x")
    c.sock = a
    c.alive_interval = 10
    c._last_keepalive = 0.0          # due immediately
    return c, a, b


class KeepaliveTests(unittest.TestCase):
    def test_does_not_block_when_device_never_replies(self):
        c, a, b = _client_pair()
        c.media_sock = socket.socketpair()[0]       # video on its own socket, control socket silent
        t0 = time.monotonic()
        c.keepalive_if_due()
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(b.recv(64)[:2], b"\xff\x00")      # the keepalive really was sent

    def test_same_socket_does_not_swallow_video(self):
        c, a, b = _client_pair()
        c.media_sock = a                            # video and control share one socket
        payload = b"\x00\x00\x01\xfc" + b"x" * 20
        b.sendall(dvrip.HEADER.pack(0xFF, 0, 0, 1, dvrip.MSG_MONITOR_DATA, len(payload)) + payload)
        c.keepalive_if_due()
        msgid, _s, _q, got = c._recv_packet(a)      # the video packet must still be there, untouched
        self.assertEqual((msgid, got), (dvrip.MSG_MONITOR_DATA, payload))

    def test_drains_reply_on_separate_control_socket(self):
        c, a, b = _client_pair()
        c.media_sock = socket.socketpair()[0]
        reply = b'{"Ret":100}\x0a\x00'
        b.sendall(dvrip.HEADER.pack(0xFF, 0, 0, 1, 1007, len(reply)) + reply)
        c.keepalive_if_due()
        a.settimeout(0.2)
        with self.assertRaises(socket.timeout):     # buffer is empty: the reply was consumed
            a.recv(1)

    def test_not_due_does_nothing(self):
        c, a, b = _client_pair()
        c._last_keepalive = time.monotonic()
        c.keepalive_if_due()
        b.settimeout(0.1)
        with self.assertRaises(socket.timeout):
            b.recv(1)


if __name__ == "__main__":
    unittest.main()
