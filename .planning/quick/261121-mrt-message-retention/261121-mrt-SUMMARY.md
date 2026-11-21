# Message delivery and retention

[PR #96](https://github.com/wnz99/llm-bus/pull/96)

- Relay first delivery expires at 48 hours. Relay and main local mailboxes retain message content for 14 days. Idle mailboxes physically purge on the next bus activity.
- Bounded cleanup preserves recent replies, replay protection, and content-free coordination records. Report access also respects the original advertised deadline.
- Read-only previews show counts and oldest eligible timestamps. The [offline cutover](../../../docs/remote-bus.md#offline-retention-cutover) covers compatible versions, stopping older processes, and recovery.

## Verification

- All five implementation steps passed independent review. Plan conformance and the disposable local and relay cutover rehearsals passed.
- The integrated verification passed 1,174 Python tests, 64 relay tests, static checks, and package acceptance. PR review round 1 and GitHub CI reproduced a timing-dependent test. The test now uses a single controlled enqueue age and still exercises the exact cutoff.
- Round 2 approved `be53995e` with no findings. Both GitHub quality checks passed on that commit. This tracking update is followed by tracking validation and current-head CI before merge.

Production deployment and first deletion require separate authorization. The disposable rehearsal does not establish that live provider controls have been applied.
