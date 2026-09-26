# Factory workflow policy

This project uses Archon's shared SDLC workflows. The requirements below are
project guidance for those workflows; they do not replace workflow gates or grant
permission to publish, comment, merge, deploy, or change schedules.

## Scope and backlog bootstrap

Read `MISSION.md` in full. Its product scope, exclusions, hard invariants, and
human-owned decisions govern every backlog and delivery decision. Leave work that
contradicts them out of a proposed backlog and report the conflict.

For a product-document bootstrap, make the first ticket the smallest real core path
that can be run and verified end to end. The ticket must name an exact start-command
argv with a port placeholder, health path, build-identity path, persistent-state
path or environment variable, test command, and pull-request CI check. HTTP apps
report `FACTORY_RUNTIME_CANDIDATE` exactly at the build-identity path when it is set,
and otherwise report their Git commit. Reuse the project's declared ordinary gate;
adding tests or CI is allowed, but weakening its judge is not. In an existing
codebase that already provides this foundation, the first ticket is an ordinary
first story.

## Factory-owned workflow inputs

Factory supplies this state-label mapping to `archon-triage`, `archon-ship`, and
`archon-lifecycle` unless the caller explicitly supplies `state_labels`, including
an explicit `{}`:

| State | GitHub label |
|---|---|
| `READY` | `archon-ready` |
| `DESIGN_FIRST` | `archon-design-first` |
| `NEEDS_CONTRACT_WORK` | `archon-needs-contract` |
| `BLOCKED` | `archon-blocked` |
| `NO_ACTION` | `archon-close` |

Automatic lifecycle intake requires the complete mapping and `publish=true` to
mark selected issues as touched. Publication remains off by default. Hold-comment
publication separately requires `publish_holds=true` in approve or auto merge mode;
`merge_mode=preview` is always read-only, and a published hold never authorizes a
merge.

## Code intelligence authority

The operator-owned `code_intelligence.mode` in `.factory/consumer.json` is the
only Factory consent authority. It defaults to `off` and may be changed only with
`factory code-intelligence enable --mode optional|required`, `disable`, or a
deliberate operator edit that passes the same strict validation. Workflow inputs,
schedules, Bridge, JEV, and repository files cannot enable or elevate it.
In particular, an input such as `--input codegraph=required` is ordinary workflow
data; it does not change the engine policy sealed by Factory's native
`--codegraph` flag.

Factory never installs, initializes, updates, watches, indexes, or serves
CodeGraph, and must not store or accept an adapter path, MCP command, secret, or
derived index. Archon resolves the operator-owned `codegraph_managed_v1` registry
and prepares the exact candidate worktree before providers start. In `optional`
mode it records and uses ordinary navigation when the managed resource cannot be
prepared; in `required` mode it fails before provider spend. A provider without
the native MCP capability uses ordinary navigation only in `optional` mode; in
`required` mode Archon refuses to start it. Such a provider must never claim
CodeGraph evidence.

## Runtime and review evidence

Runtime and holdout evidence must exercise the delivered candidate and match its
current source identity. When using the factory runtime host, follow
`factory/RUNTIME_HOST.md`: prepare the resource from the delivering checkout at its
expected revision, require the configured health and build-identity checks, use
fresh state for each attempt, and preserve the typed result as evidence. Ordinary
checks do not substitute for required runtime or holdout evidence.

Independent review evidence may be Archon's canonical current-head report, a
genuine review by an independent human, or an external independent report format
that project guidance accepts. In every case it must identify the PR's current head,
be credible and independent of the implementer, state a ready verdict, and contain
no unresolved blocking findings. Stale, ambiguous, or self-attested evidence holds.
