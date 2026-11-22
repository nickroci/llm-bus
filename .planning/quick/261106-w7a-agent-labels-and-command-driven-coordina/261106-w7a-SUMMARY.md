# Agent labels and command-driven coordination: rollout summary

Source and staged artifacts implement approved revision 3. Tasks 1–6 have Clean
independent task reviews. Final planner conformance is CONFORMANT_SOURCE_AND_STAGING.
Full-branch aggregate/PR review approved requirements and quality on
a9259fe91853c740233396e64faa0ab68c706059, with no High/Medium findings or
requirement gaps. Its sole Low documentation finding is corrected: unsupported
availability uses `unsupported_availability`; direction errors retain
`unsupported_coordination`. Final tracking and this diagnostic correction leave
reviewed executable code, configuration and tests unchanged. Prior review/test
receipts are reused under the documentation-followup exception; the new head is
not independently code-reviewed. PR #62 merged as
fabcb4043dd47e9ef3e241c5a8eb890c7a7d3108 after current-head CI passed.
The Quick Tasks Completed row references PR #62 for source/staging delivery.
This follow-up records the subsequently authorized and verified live rollout.

Labels identify main sessions for display and addressing while immutable native
identities retain authority. Direct owner coordinate/join commands support bounded
session consent and recovery. Remote availability requires receiver-owner binding
and both endpoints' policy; ordinary messages cannot authorize enrollment.

Final verification: `uv run poe check` passed with 463 Python tests, static checks
and package validation. Relay typecheck and 41 tests plus deployment dry-run passed
on unchanged relay source. Fresh product hashes, approved-plan equality, Markdown
links/fences and canonical CLI parser checks passed. Canonical evidence lives in
/tmp/llm-bus-agent-labels.RRRIOf/: final-verify.md, final-conformance.md and
task6-review-r1.md, all task1–task6 review receipts and pr-review-r1.md. PLAN
remains the verbatim approved revision. Final documentation validation and exact
reviewed-state equality evidence are recorded in finalize-merge.md there.

## Authorized post-merge rollout

Owner authorized installation on all configured systems. All four installed
clients match source pin fabcb4043dd47e9ef3e241c5a8eb890c7a7d3108 across all
105 Python modules, with guidance 8 and mailbox schema 12. Independent bounded
operational verification returned PASS in rollout-verify.md; clients-upgrade.md
and relay-upgrade.md retain operator receipts in the evidence directory above.

This table is a historical operator record, not shared topology or permission
configuration. Each machine retains owner-controlled policy.

| Host | Actual user | Python | Installed extra | Bridge result |
|---|---|---|---|---|
| whitenoise Mac | davidfava | 3.14.8 | collab | Running, original configuration/cwd retained |
| home.wnz.sh (London) | whitenoise | 3.13.5 | core | Remains inactive; remote configuration remains unconfigured |
| herdr-dev-1 (retired) | david | 3.13.16 | core | Active at the time of rollout, original configuration/cwd retained |
| herdr-dev-2-lightsail | david | 3.13.17 | core | Active, original configuration/cwd retained |

All four passed checks of six canonical hook entries, three staged skill files,
two global coordination markers with actual home substitution, and configured
shell resolution of llm-bus and bd. Linux hooks remained byte-identical.
Other settings, native metadata, local policy and global text outside markers
were preserved. Private backups passed copied-file digest and SQLite integrity
checks. Original agent/message identifiers remain; migration-time full-row
preservation is operator evidence, not a claim that live mutable fields freeze.
Seven old timer helpers are absent; the replacement uses the new runtime.
The peer lock row was preserved; parent renewals retained ownership.

Relay live version b24212da-e6d9-4578-8d0a-20918b0e11f2 runs the matching source.
D1 retains migrations 0001–0006. Authenticated HTTP 200 probes verified exact v1
reply_to/batch_status/batch_reports booleans, coordination version 1 and
availability_version 1. Four machine authentication rows and seven grant pairs
were unchanged; all prior 706 sessions and 10 message identifiers were retained.
Backups remain private. No new trust, grants, enrollment or project activation
was authorized or inferred from installation.

Installed-package smoke tests passed: 28 label/session-consent tests imported the
actual Mac installation with synthetic temporary databases. Fresh independent
proof covers installed hashes, settings isolation, backup integrity, live bridge
interpreters/cwds and relay contracts. Earlier 463 Python/41 relay tests remain
unchanged-source receipts, not newly rerun rollout tests.

Native SessionStart/UserPromptSubmit/SessionEnd frontend delivery, frontend hook
trust approval, native context reload/compaction, native wake handling and
cross-machine agent reachability were not observed. Installed callbacks and
synthetic smoke tests do not establish those frontend events. Labels remain
aliases; exact native identities retain authority and owner commands supply
consent. Retain pre-upgrade backups for recovery; older clients reject schema 12.
