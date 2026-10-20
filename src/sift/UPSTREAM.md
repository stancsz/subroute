Subroute vendors Sift 0.10.0 (`e8dc791ba76d99012a3cd43e6114c01faedeeb6a`) from https://github.com/stancsz/sift.

The local Qwen route imports this copy directly so Docker deployment uses the
reviewed code without a startup install or mutable external dependency. Keep
`core.py`, `litellm_proxy.py`, `litellm_responses.py`, and `__init__.py` identical to the tagged upstream release when
updating. Run both Sift's standalone suite and Subroute's local-context tests,
then repeat a live staging compaction request before restarting production.

This vendored copy can be removed if Subroute adopts a pinned, reproducible
Sift package artifact in its deployment image.
