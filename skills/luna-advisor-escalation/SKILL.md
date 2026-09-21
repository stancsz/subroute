---
name: luna-advisor-escalation
description: Use a compact Sol or Astra API consultation to unblock a Luna-led task, while keeping execution and verification with Luna.
---

# Luna Advisor Escalation

Keep the current task with Luna. Use this skill only after a concrete blocker
remains following a focused investigation, test, or failed implementation
attempt. The advisor is a short-lived reviewer, never the task owner.

## Choose whether to consult

Do not consult for routine implementation, an untested hypothesis, a question
answerable by reading local code or docs, or to have an expert perform work.
Before consulting, make the smallest useful local check and compact its result.

Consult only when all four conditions hold:

1. Luna has a specific objective and a concrete blocker or decision.
2. A focused source/doc inspection plus a targeted test or bounded attempt has
   produced ambiguity, competing paths, or a repeatable failure.
3. There is no cheaper local observation that would resolve the question.
4. One precise question could change Luna's next experiment or plan. A second
   question is allowed only when it is necessary to interpret the first.

Do not consult for missing credentials or permissions, an unresolved user
choice, a task that merely needs implementation time, or generic reassurance.

Use Sol first for a diagnosis, design fork, failed test path, or an independent
review of an implementation plan. Use Astra only when the unresolved question
has high leverage or spans multiple subsystems, security or data-loss risk, or
when direct evidence and a Sol answer leave a specific decision unresolved.
Escalate from Sol to Astra only when Sol identifies a system-level tradeoff, its
recommended experiment fails, or two evidence-backed paths remain with
materially different consequences. Do not ask both models the same question
just to vote.

Track consultations in the current task. Use one by default and at most two
total: Sol first, then only one distinct follow-up after its suggested check or
an evidence-backed Astra escalation. State the reason for the second call in
its packet. Never retry a failed consultation against another model
automatically, and do not make a third call without the user's direction.

## Make a compact advice request

The dedicated expert API is `http://127.0.0.1:4040/v1`, not the worker gateway
on port 4000. It exposes only `codex-sol-advisor` and
`codex-astra-advisor`; do not substitute an executor alias or silently fall
back to port 4000.

Write a high-density packet, normally under 4,000 characters and never over
6,000. Keep only information that could change the recommendation:

1. The decision or blocker in one sentence.
2. Three to six observed facts, each as one bullet. Include at most one exact
   error or code excerpt, no longer than 400 characters.
3. Constraints and already-rejected paths in one short bullet.
4. One decision question. Add a second only when it directly depends on the
   first.

Remove chronology, raw logs, full source files, duplicated conclusions,
speculation, and any fact that cannot change the decision. Strip secrets.
Ask for a verdict, the smallest next experiment, and a material stop condition.
The advisor must not write code, use tools, issue commands with side effects,
or complete the task.

Run the bundled caller. It rejects packets above 6,000 characters, requests a
compact answer, and emits the answer, selected model, packet/output size, and
reported provider usage:

```powershell
py C:\Users\stanc\.codex\skills\luna-advisor-escalation\scripts\ask_expert.py --model sol --input-file .\advisor-packet.txt
```

Use `--model astra` only under the Astra criteria above. The experts service
must already be running with `docker compose up -d experts` in the unified
gateway repository. Set `EXPERTS_API_KEY` in the caller environment only if
that optional service key is configured. Never put a credential in a packet or
write it to a file.

## Resume Luna ownership

Treat advice as a hypothesis. Choose the smallest safe experiment that can
confirm or reject it, implement the resulting change yourself, and run the
relevant verification. Record actual reported usage when it affects a cost or
quota decision. An unavailable, malformed, or incomplete advisor answer is
visible failure, not permission to proceed as if advice was received.

Before consulting, state Luna's intended next action in one sentence. After
verification, record `decision_changed: true` only if the advice changed that
implementation or verification action; otherwise record `false`. Pair that
boolean with provider usage. This is the minimum receipt for deciding whether
advisor tokens created practical value rather than merely restating a plan.

An advisor response is not proof that the implementation works. Luna remains
responsible for changes, tests, external actions, and the final evidence.
