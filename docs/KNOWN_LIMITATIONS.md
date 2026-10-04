# Known Limitations

None of these are blockers — the dashboard still gives you useful information. They're the rough edges you'll notice if you look hard.

## Skills token counts are partial

The Skills route shows every skill Claude Code invoked, how many times, across how many sessions, and when. The **tokens-per-call** column is populated only for skills whose `SKILL.md` lives under `~/.claude/skills/`, `~/.claude/scheduled-tasks/`, or `~/.claude/plugins/`. Skills registered elsewhere (project-local `.claude/skills/`, or invocations that go through the `Task` tool with a skill-shaped `subagent_type`) show invocation counts but leave the token column blank.

It's still a useful view — you can see which skills dominate your session time — just don't expect a complete per-skill token cost. PRs to broaden the catalog scan welcome.

## Cost for Pro / Max / Max-20x users is shown as API-equivalent, not subscription value

The Settings route lets you select your pricing plan, but the Overview cost number is always the API-equivalent (what the same usage would have cost on pay-per-token rates). If you're on Pro you pay a flat $20/month regardless of how much of that API-equivalent number you rack up. We don't do "subscription ROI" math yet — Anthropic doesn't publish per-plan rate limits as public JSON, and faking it would be worse than not doing it.

## Cowork sessions are invisible

If you use Claude's Cowork mode (server-side sessions, not local `claude` CLI), those sessions don't write JSONL to `~/.claude/projects/` and the dashboard can't see them.

## Non-standard model names get tier-fallback pricing

If a transcript references a model ID not in `pricing.json` (e.g. a future snapshot that isn't in our table yet), cost is estimated from the tier substring (`opus` / `sonnet` / `haiku`) in the name. The UI marks these as `estimated: true`. If the model name contains none of those substrings, cost is reported as null.

## First scan can be slow

The first `python3 cli.py scan` on a heavy user's machine can read tens of MB across hundreds of JSONLs. Subsequent scans are incremental (mtime + byte-offset tracking in the `files` table), so they're fast.

## Running two dashboards against the same DB

Both will fight over the SQLite file and you'll see inconsistent numbers and occasional `database is locked` errors. Only run one at a time. If you want to view the dashboard from a second device, use `HOST=0.0.0.0` on the one running machine and point the second device's browser at it.

## opencode subscription rows show API-equivalent cost, not billed cost

opencode v2 rows from subscription providers report cost 0; the dashboard shows the API-equivalent estimate for those rows, not the billed cost. Cost display is stored-cost-first: a row's native `cost_usd` wins only when it is NOT NULL **and > 0**, so 0-cost rows (subscription/free providers — most of the v2 history) and v1/Claude rows (which store no cost) fall back to the pricing estimate. Totals stay comparable across backends; individual subscription rows just don't reflect what you actually paid.

## In-flight opencode tool calls are counted as errors

A tool call is an error whenever its state is not `completed` — so calls still `running` or `streaming` at the moment a scan imports them count as errors. And because rows are keyed on `time_created`, which never changes, the import watermark never revisits them: a message imported mid-stream keeps its partial tokens, cost and tool state forever (the v1 leg has the same limitation). Re-scanning cannot fix these rows — the numbers are a snapshot of what the import happened to see.

## Same-second opencode rows get re-read on every run

The import watermark has second granularity (it truncates to whole seconds), so rows created within the same second as the watermark are read again on the next run. Harmless: the re-imports are idempotent upserts — no duplicate rows and no lost rows — just a slightly larger incremental read than strictly necessary.
