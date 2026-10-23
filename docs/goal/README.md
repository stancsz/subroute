# Goal records

- **Current LAN coordination:** [Device A server and device B Docker client setup](../operations/relay-devices.md), [design and recovery](../operations/relay.md), and [test results and limits](../evals/relay-2026-10-23.md). Same-host client registration/restart and independent code review passed. Default workers need no skills bundle; the current Relay checks pass 53 tests, independently rerun. All 11 repeatable isolated Docker fault groups passed with cleanup verified; see the design guide. Two later real-Claude Docker rounds produced one completed goal and one failed worker handoff at the existing turn cap; A revised leaf handoff contract then passed one bounded four-task round with no observed coordinator-state reconnaissance. Repeatable unattended, physical two-device and sustained acceptance remain open.

- **Current context delivery:** [4M input, Sift targets, 1M client trigger and gateway error observation](../evals/subroute-4m-context-2026-10-23.md).

- **Completed:** [Subroute dashboard and shared Badlands identity](badlands-redesign.md).

- **目标记录：** [Subroute 维护性与可靠性](../../GOAL.md)（已完成，状态见目标文件）
- **历史归档：** [2026-09-28 之前的 Subroute 核心目标](archive/GOAL-2026-09-28-legacy.md)、[Control desk readiness goal](archive/control-desk-readiness-2026-09-28-legacy.md)

归档文件保留旧目标的原文，供追溯历史范围和验收证据。它们不再是活动工作合同。
