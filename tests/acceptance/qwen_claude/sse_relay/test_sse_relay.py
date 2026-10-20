import unittest

from relay import ProtocolError, UpstreamError, relay_events


class Source:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class RelayTests(unittest.TestCase):
    def test_fragmented_utf8_and_multiple_events(self):
        raw = 'event: delta\ndata: {"text":"café"}\n\ndata: [DONE]\n\n'.encode()
        source = Source([raw[:23], raw[23:25], raw[25:27], raw[27:]])
        self.assertEqual(list(relay_events(source)), [
            {"event": "delta", "data": {"text": "café"}}, {"event": "done", "data": None},
        ])
        self.assertTrue(source.closed)

    def test_missing_terminal_is_failure_and_closes_source(self):
        source = Source([b'data: {"ok":true}\n\n'])
        stream = relay_events(source)
        self.assertEqual(next(stream), {"event": "message", "data": {"ok": True}})
        with self.assertRaises(ProtocolError):
            list(stream)
        self.assertTrue(source.closed)

    def test_provider_error_and_invalid_json_are_failures(self):
        for raw, error in [
            (b'event: error\ndata: {"message":"quota"}\n\n', UpstreamError),
            (b'data: {"error":"overload"}\n\n', UpstreamError),
            (b'data: nope\n\n', ProtocolError),
        ]:
            with self.subTest(raw=raw):
                source = Source([raw])
                with self.assertRaises(error):
                    list(relay_events(source))
                self.assertTrue(source.closed)

    def test_consumer_cancellation_closes_source(self):
        source = Source([b'data: {"n":1}\n\n', b'data: [DONE]\n\n'])
        stream = relay_events(source)
        self.assertEqual(next(stream)["data"], {"n": 1})
        stream.close()
        self.assertTrue(source.closed)


if __name__ == "__main__":
    unittest.main()
