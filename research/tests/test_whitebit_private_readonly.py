"""Real upstream HMAC code + httpx MockTransport; NO account or network calls."""
import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

try:
    import httpx
except ImportError:
    raise unittest.SkipTest("optional WhiteBIT httpx dependency is not installed")

from autotrade_research.integrations.whitebit_private_readonly import (
    MAIN_BALANCE_PATH, MAX_RESPONSE_BYTES, PrivateReadError,
    ReadOnlyGate, WhitebitPrivateReader,
)


class WhitebitSdkReadTests(unittest.TestCase):
    def test_real_upstream_signature_matches_wire_body(self):
        seen = []
        def handle(r):
            body = r.read()
            self.assertEqual(base64.b64decode(r.headers['X-TXC-PAYLOAD']), body)
            expected = hmac.new(b'fixture-signing-only', r.headers['X-TXC-PAYLOAD'].encode(), hashlib.sha512).hexdigest()
            self.assertEqual(r.headers['X-TXC-SIGNATURE'], expected)
            self.assertEqual(r.headers['X-TXC-APIKEY'], 'fixture-key-only')
            seen.append(json.loads(body))
            return httpx.Response(200, json={"USDT": {"main_balance": "0"}})
        with WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only', transport=httpx.MockTransport(handle)) as reader:
            result = reader.main_balance()
        self.assertEqual(len(seen), 1)
        self.assertFalse(result.verified_account_binding)
        self.assertEqual(result.trading_authority, 'NONE')
        self.assertNotIn('main_balance', repr(result))
        self.assertEqual(hashlib.sha256(result.body).hexdigest(), result.raw_sha256)

    def test_default_network_is_disabled(self):
        with self.assertRaisesRegex(PrivateReadError, 'NOT_ENABLED'):
            WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only')

    def test_nonce_stays_unique_when_clock_does_not_move(self):
        seen = []
        def handle(r):
            seen.append(json.loads(r.content)['nonce'])
            return httpx.Response(200, json={})
        with patch('time.time_ns', return_value=1000000000000000):
            with WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only', transport=httpx.MockTransport(handle)) as reader:
                reader.main_balance(); reader.main_balance()
        self.assertEqual(seen, [1000000000, 1000000001])

    def test_live_order_withdrawal_and_other_hosts_never_reach_inner(self):
        calls = []
        gate = ReadOnlyGate(httpx.MockTransport(lambda r: calls.append(r)))
        urls = ['https://whitebit.com/api/v4/order/new',
                'https://whitebit.com/api/v4/main-account/withdraw',
                'https://whitebit.com.evil.invalid'+MAIN_BALANCE_PATH,
                'http://whitebit.com'+MAIN_BALANCE_PATH,
                'https://whitebit.com:444'+MAIN_BALANCE_PATH,
                'https://whitebit.com'+MAIN_BALANCE_PATH+'?x=1',
                'https://whitebit.com'+MAIN_BALANCE_PATH+'#x',
                'https://u:p@whitebit.com'+MAIN_BALANCE_PATH,
                'https://whitebit.com/api/v4/main-account/%62alance']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(PrivateReadError):
                gate.handle_request(httpx.Request('POST', url, json={}))
        self.assertEqual(calls, [])

    def test_http_error_response_is_not_echoed(self):
        for code in (302,401,429,500):
            with self.subTest(code=code):
                transport = httpx.MockTransport(lambda r: httpx.Response(code, text='private-payload-do-not-echo', headers={'location':'https://evil.invalid'}))
                with WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only', transport=transport) as reader:
                    with self.assertRaises(PrivateReadError) as e:
                        reader.main_balance()
                    self.assertNotIn('private-payload', str(e.exception))

    def test_network_exception_hides_sensitive_message(self):
        def fail(r): raise httpx.ConnectError('fixture-signing-only', request=r)
        with WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only', transport=httpx.MockTransport(fail)) as reader:
            with self.assertRaisesRegex(PrivateReadError, 'IO_FAILED') as e:
                reader.main_balance()
            self.assertNotIn('fixture-signing-only', str(e.exception))

    def test_bad_response_media_is_rejected(self):
        with WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only', transport=httpx.MockTransport(lambda r: httpx.Response(200,text='<html>'))) as reader:
            with self.assertRaisesRegex(PrivateReadError,'MEDIA_INVALID'): reader.main_balance()

    def test_oversized_response_is_rejected(self):
        with WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only', transport=httpx.MockTransport(lambda r: httpx.Response(200,content=b'x'*(MAX_RESPONSE_BYTES+1),headers={'content-type':'application/json'}))) as reader:
            with self.assertRaisesRegex(PrivateReadError,'TOO_LARGE'): reader.main_balance()

    def test_empty_response_is_rejected(self):
        with WhitebitPrivateReader('fixture-key-only', 'fixture-signing-only', transport=httpx.MockTransport(lambda r: httpx.Response(200,content=b'',headers={'content-type':'application/json'}))) as reader:
            with self.assertRaisesRegex(PrivateReadError,'EMPTY_RESPONSE'): reader.main_balance()

    def test_closed_session_and_transport_cleanup(self):
        class Transport(httpx.BaseTransport):
            calls = 0
            def close(self): self.calls += 1
        t = Transport()
        reader = WhitebitPrivateReader('fixture-key-only','fixture-signing-only',transport=t)
        reader.close(); reader.close()
        self.assertEqual(t.calls, 1)
        with self.assertRaisesRegex(PrivateReadError,'CLOSED'): reader.main_balance()

    def test_credentials_do_not_accept_header_injection(self):
        for value in ('', 'x\ny', 'x y', 1, True):
            with self.subTest(value=value),self.assertRaises(PrivateReadError):
                WhitebitPrivateReader(value,'fixture-signing-only')

    def test_foreign_body_or_method_is_denied_before_signing(self):
        gate = ReadOnlyGate(httpx.MockTransport(lambda r: self.fail('I/O reached')))
        for method,body in [('GET',{}),('POST',[]),('POST',{'request':MAIN_BALANCE_PATH,'nonce':True,'nonceWindow':True}),('POST',{'request':MAIN_BALANCE_PATH,'nonce':1,'nonceWindow':True,'ticker':'X'})]:
            with self.subTest(method=method,body=body),self.assertRaises(PrivateReadError):
                gate.handle_request(httpx.Request(method,'https://whitebit.com'+MAIN_BALANCE_PATH,json=body))


if __name__ == '__main__': unittest.main()
