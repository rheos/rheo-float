# Agent instructions

Read `CLAUDE.md` in this directory in full before doing anything. Every rule in it applies to
Codex and any other agent, not only to Claude. The name is historical.

Two rules there need extra care outside Claude Code:

- `.claude/settings.json` denies `finance sink approve` and `finance confirm bulk` for Claude
  sessions. Other agents have no such deny list, so the rule is yours to keep: never run either
  command, in any form, from an agent session, and never set a sink's `enabled` key to true
  outside a test fixture.
- This repo is public. Nothing personal goes into code, fixtures, docs, commit messages or PR
  text. When you read real files to learn a layout, report counts and pass/fail only.
