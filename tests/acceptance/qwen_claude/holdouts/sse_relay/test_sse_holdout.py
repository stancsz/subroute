import unittest

from relay import ProtocolError, relay_events
from sse_parser import parse_sse


class Source:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
    def __iter__(self):
        return iter(self.chunks)
    def close(self):
        self.closed = True


class Holdout(unittest.TestCase):
    def test_crlf_split_comments_and_multiline_data(self):
        raw = b': ping\r\nevent: data\r\nid: 7\r\ndata: {"n":\r\ndata: 2}\r\n\r\ndata: [DONE]\r\n\r\n'
        chunks = [raw[i:i + 1] for i in range(len(raw))]
        self.assertEqual(list(relay_events(Source(chunks))), [
            {"event": "data", "data": {"n": 2}}, {"event": "done", "data": None},
        ])

    def test_late_event_cannot_hide_after_done(self):
        source = Source([b'data: [DONE]\n\ndata: {"late":true}\n\n'])
        with self.assertRaises(ProtocolError):
            list(relay_events(source))
        self.assertTrue(source.closed)

    def test_bounded_event_and_invalid_utf8(self):
        with self.assertRaises(ProtocolError):
            list(parse_sse([b'data: ' + b'x' * 1000], max_event_bytes=100))
        with self.assertRaises(ProtocolError):
            list(parse_sse([b'data: \xff\n\n']))


if __name__ == '__main__':
    unittest.main()
