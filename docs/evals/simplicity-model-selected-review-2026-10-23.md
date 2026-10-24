# Independent actual model-selected Advisor cycle verification

Verdict: PASS for this single model-selected consultation cycle through staging and private experts. This is actual read-only runtime verification, separate from the other peer's artifact authorship and mocked preparation checks.

## Public response and request semantics

Saved child/public artifacts and their hashes reconcile with runtime-before/result records. One POST uses tool_choice auto and asks the target to consult the available Advisor once. The caller supplies no tools; the configured optional native Advisor tool is exposed by the existing hook. Public output returns the expected ROUTE_OK marker, message_start/message_stop and end_turn, without leaking internal tool_use. Safe headers report zero retries and zero fallbacks. Before/after state is equal. The public-only label correctly does not establish consultation by itself; database and endpoint evidence below do.

## Independent all-row database verification

Read-only SQL selects every receipt created at or after db_window_start_ms 1792806791979, plus every row for request `894b506a-09f5-4b9c-8049-98a515cdfde5`, without kind/role/outcome filters. Exactly five rows return, all for this one nonempty request ID, ordered by created_at:

| Attempt | Kind / role | Upstream | Outcome | Input / cached / output | usage_complete |
| --- | --- | --- | --- | --- | --- |
| a16892ab-a78c-4588-b2e9-2a26d365bb48 | bridge / inference | openai/mimo-v2.6-pro | complete | 202 / absent / 188 | true |
| f3baa7ec-9122-4fed-a5ed-59cf3426f50d | provider / inference | openai/mimo-v2.6-pro | complete | 202 / 0 / 188 | true |
| 1db836d0-6f3c-4249-8754-6f3cf8a078c2 | provider / advisor | anthropic/gemini-3.8-flash-advisor | complete | 1239 / 0 / 40 | true |
| af1852bc-82d3-46cb-84ff-8c8100139729 | bridge / inference | openai/mimo-v2.6-pro | complete | 271 / 128 / 26 | true |
| 9230fc8d-e8dd-416b-b621-030df65b29cc | provider / inference | openai/mimo-v2.6-pro | complete | 399 / 128 / 26 | true |

Provider sequence is inference, advisor, inference. Both bridge rows remain visible and terminal; no pending/orphan/extra cohort is hidden by provider filtering. The final bridge's 271 uncached plus 128 cached reconciles to the final provider's 399 input. Public usage 271/26 matches that bridge representation. Bridge rows must not be summed as additional provider work.

All parent_attempt_id fields are null. Correlation is established by the common request ID, temporal sequence, configured route and endpoint log, not by a populated parent-attempt graph.

## Independent private endpoint projection and provenance

Read-only Docker log scan uses exact start `2026-10-24T01:53:11.979151+00:00` through runtime-result artifact timestamp `2026-10-24T01:53:27.720134+00:00`, with a 301-line saturation guard. Two lines were scanned; the safe generation projection contains exactly one POST `/v1/messages`, status 200. Saved projection: `independent-access-projection.json`. No raw provider bodies or credentials were persisted.

The gateway route `experts/gemini-3.8-flash-advisor` maps to `anthropic/gemini-3.8-flash-advisor` at `http://experts:4040`. Private experts then maps the alias to its configured Antigravity Gemini backend. Thus provider_id anthropic on the Advisor receipt is the gateway's native Anthropic-protocol HTTP hop, not evidence of a direct Anthropic vendor call or the Gemini CLI's internal model-call count. Exactly one private HTTP consultation is observed. No vendor billing total or hidden internal-call count is claimed.

The reviewed AdvisorPlugin pre-call hook exposes/binds the native optional tool with max_uses 1; it does not make a consultation itself or inject guidance into messages. Initial target execution before the Advisor hop and resumed target execution afterward match native model-selected orchestration for this explicit consultation request. This does not measure how often a model chooses to consult on ordinary prompts.

## Runtime, policy and source hashes

Independent Docker inspection matches runtime-before and accepted-runtime for all three containers, images, start timestamps and zero restart counts:

- Staging `b2490accbd7f5fd54aa58d17d6ac2e9f13c4c0aa9aa8582cd4e842e0e14134cd` and experts `f37f37c89d3bbdbb485ad012ae2a9612b49534f18b785b33799c4ba2da1cff84` use reviewed image `sha256:a99773d74df38fe676e17054ed9228f53a844449395ba420be7a112b59cd62cd`.
- Production remains `c448e447f6e4b117a79c08e22a5c105482539570e3afad5873a98958c85c2132` on its original image/start.

Current saved policy hash is `cd4cf008fba0e5da90062b756e683472d22c69dd5b22c913e6554767ce4bd9b9`, matching runtime-before. In-container staging accounting hash `0a1aa24786da79854757fad1ab5aaf8ec0f4a1f9b6299c4460928befae80f637`, Advisor plugin hash `725de86ee898634a87b86ac1e30978921e55737fe1654c73a3e7d4a531d481f0` and config hash `9cdbd089406974d48edb9ad65688e1d0682248bba1e1483f0285f2ec7309fe5e` independently match the immutable image manifest.

Child hash `515a8fe82fc8c238e1b7563529bcf49186fd7ead57b1ac2e6aa94fb6073da6ec`, supervisor hash `63a40239c24f2be1beeb4ba424d608028fd825b19a8d44d58008ec74d93635ef` and accepted-runtime hash `d0aff4ec341a95047adc25424c9cb786d6b3ada64bd7c4511e4e80569ae8ccf2` match their executed receipts.

## Scope

No provider request, source edit, SQL mutation, service restart/rollout, credential-file read or commit was performed by this reviewer. Reads were limited to safe artifacts, all-row SQL projection, bounded access-log projection and source/runtime hash inspection. Historical failures remain preserved. This verifies the one selected cycle and its accounting closure; broader production deployment, all aliases/protocols, generic ingress limits and general quota efficiency are not established by this receipt.
