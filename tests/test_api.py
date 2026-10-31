import os
import ssl
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lighter_api as api  # noqa: E402


class SslContext(unittest.TestCase):
    def test_certifi_added_when_system_store_is_empty(self):
        """Windows с ещё не пополненным хранилищем сертификатов: корневые берутся из certifi."""
        bare = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with mock.patch("ssl.create_default_context", return_value=bare):
            ctx = api.ssl_context()
        self.assertIs(ctx, bare)
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 50)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_requests_use_context(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("сеть")) as op, mock.patch("time.sleep"):
            with self.assertRaises(api.ApiError):
                api.get("rh", "orderBookDetails", tries=1)
        self.assertIs(op.call_args.kwargs["context"], api.SSL)


if __name__ == "__main__":
    unittest.main()
