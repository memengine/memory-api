"""Verify the authenticated transport's redirect policy without network I/O."""

import io
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.request import HTTPHandler
from urllib.response import addinfourl

from inspect_memory_history import HttpTransport, MemoryAPIError


class RedirectPolicyTest(unittest.TestCase):
    def test_authenticated_redirect_is_rejected_without_follow_up(self):
        origin = "http://origin.invalid"
        path = "/v1/memories/test"
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                headers = Message()
                headers["Location"] = "http://destination.invalid/next"
                response = addinfourl(io.BytesIO(b""), headers, origin + path, code)
                response.msg = "Redirect"
                with (
                    patch("urllib.request.getproxies", return_value={}),
                    patch.object(HTTPHandler, "http_open", return_value=response) as send,
                ):
                    client = HttpTransport(origin, "synthetic-test-key")
                    with self.assertRaises(MemoryAPIError) as caught:
                        client.get(path)
                    send.assert_called_once()
                    self.assertEqual(send.call_args.args[0].full_url, origin + path)
                self.assertEqual(caught.exception.code, "REDIRECT_REJECTED")
                self.assertEqual(caught.exception.status, code)


if __name__ == "__main__":
    unittest.main()
