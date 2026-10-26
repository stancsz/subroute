# Claude Code to port 4000 acceptance

Run these three opt-in tests. The launcher creates and revokes a short-lived local inference key for each case:

```powershell
python tests/acceptance/claude_cli_4000/run_live.py
```

The launcher reads `GATEWAY_MASTER_KEY` from the process or local `.env` solely to create the inference key; it never passes the master key to Claude. Alternatively, set `RUN_SUBROUTE_CLAUDE_ACCEPTANCE=1` and `SUBROUTE_ACCEPTANCE_API_KEY` to run pytest with an existing authorized inference key. The runner isolates Claude's config, disables unsupported `[1m]` aliases, bounds thinking, sends a 60-second LiteLLM stream timeout header, points native Messages traffic to `http://127.0.0.1:4000`, and copies three real coding fixtures into ignored `.runs/` folders. Each run keeps the fixture workspace separate from audit logs and Claude config. It never changes the gateway's saved routing policy. The requested names cover `default`, a Claude model card, and a deliberately unknown model name, which should resolve to the saved default.

Each test fails unless Claude completes successfully, performs at least 20 reported CLI turns across fresh calls for the same fixture and 180 seconds of real elapsed CLI work, uses native tools, changes only its permitted source files, and passes the fixture's Python tests. The unknown-name case also requires a direct Messages response whose deployment ID matches the saved default while its client-visible identity stays unchanged. Each case has a 15-minute total bound, each CLI call has a 450-second bound, and output idleness is limited to 150 seconds. The launcher runs all three cases even if one fails, with separate short-lived inference keys. Each run saves redacted CLI JSONL and stderr in `audit/` and a concise `receipt.json` in the run folder so API errors and incomplete tool continuations can be inspected. A passing test confirms these three sampled configurations, not every possible model card or production rollout.
