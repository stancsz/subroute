# Streaming relay task

Implement a small SSE relay for an API gateway. Input is an iterable of byte chunks from an upstream HTTP response. Chunks may split UTF-8 characters, line endings, and events anywhere. `parse_sse` yields `(event_name, data_text)` for complete events. Support LF and CRLF, `event:` and one or more `data:` lines; join data lines with a newline. Ignore comment and `id:` lines. An event with no data is ignored. Reject invalid UTF-8 and any event larger than `max_event_bytes` before buffering without bound.

`relay_events` decodes each data payload as JSON and yields `{"event": name, "data": object}`. The literal `[DONE]` is the only successful terminal: yield `{"event": "done", "data": None}` once. An `event: error` or a JSON object containing a nonempty `error` field raises `UpstreamError`. Invalid JSON, data after `[DONE]`, and EOF without `[DONE]` raise `ProtocolError`. Always close the upstream if it has a `close()` method when the relay completes, fails, or its consumer closes the generator. Do not add retries or synthesize a successful completion.

Only edit `sse_parser.py` and `relay.py`. Run `python -m unittest -q` before and after.
