# Independent ingress-bound retirement challenge

Finding: no removed generic early HTTP body-byte limit was found in the inspected pre-change routing/Sift evidence. Treating that missing generic limit as pre-existing debt is supported. Do not describe token admission or endpoint-specific byte caps as a universal pre-parse HTTP limit.

## Inspected evidence

- Preserved `.runs/goal-simplicity-20261023/sift_plugin.before.py` and `sift_plugin.slice-before.py` are request-dictionary pre-call callbacks, not ASGI/body-read middleware. Their old RAW_INTAKE_LIMIT is 4194304, passed as Sift raw_limit.
- The token-bound path consumes already parsed messages/input, calls LiteLLM's token counter, and ultimately uses `raw = _count(count_tokens, messages)` in `src/sift/core.py` before comparing raw_limit. It measures tokens after JSON parsing, not serialized HTTP bytes before parsing. The target hook also deep-copies parsed request dictionaries. Retiring that hook does not remove an early byte-reading guard because that hook never supplied one.
- Tracked original-root baseline df7de14 was searched for configured request-size middleware/body limits across configuration and the gateway source. Its normal LiteLLM config has no max_request_size_mb setting. Current normal/private configs also have no such setting. Private experts config was not a tracked file at that original baseline, so no claim is made here about a missing tracked pre-task private config.
- The preserved before-sync accounting source has a genuine 16384-byte Content-Length and streamed-body guard. Its routes are only `/internal/accounting/receipts` and `/internal/accounting/ack`; that limit is specifically for receipt acknowledgements. It was not a generic public inference-body guard and remains in current accounting source.

## Installed native mechanism

Installed LiteLLM 1.106.2 RequestSizeLimitMiddleware does contain an early Content-Length/streamed-byte limiter. Proxy installation passes general_settings.get(max_request_size_mb), but enables the limiter only when premium_user is True. GeneralSettings defaults max_request_size_mb to None. The middleware bypasses itself if the maximum is absent/nonpositive or the enable callback is false. Merely pointing at the class or setting an undocumented entitlement flag does not establish an active native cap.

This verifies the installed mechanism and configuration omission. It does not inspect licensing credentials or prove any external reverse proxy's settings. No entitlement bypass, custom middleware or additional service is justified by this retirement review.

## Preserve the real existing bounds

The Antigravity sidecar independently checks its declared Content-Length before reading, at the existing audio-request ceiling of 29 MiB; after parsing it applies the text-only 1000000-byte request limit and decoded audio cap of 20 MiB. The public provider handler also bounds its serialized outgoing bridge payload. Those are specific downstream endpoint/transport bounds and do not protect arbitrary gateway bodies before the gateway parses them.

Keep current native token admission and supported media/image handling as separately documented requirements. This review does not independently re-verify every route's context admission or image/media cap. A future generic ingress cap needs an explicit byte budget compatible with those valid payloads and an applicable supported deployment mechanism; it is separately scoped work.

## Review outcome and scope

It is accurate to record the absent generic early body-byte cap as pre-existing debt and avoid claiming universal bounded ingress. The evidence does not support calling that cap a Sift-retirement regression. This negative finding is bounded to the preserved source/config artifacts and installed native implementation inspected here, not a proof about every historical environment.

No implementation, provider request, service operation, source edit or credential inspection occurred. Only this review artifact was written. The owner retains responsibility for TECH_DEBT/GOAL wording and whole-goal completion scope.
