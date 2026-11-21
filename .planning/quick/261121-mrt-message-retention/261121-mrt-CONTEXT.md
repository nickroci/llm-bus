# Message delivery and retention

## Registration

This is a tracked fallback record created without GSD tooling. The approved draft is .planning/drafts/message-retention/PLAN.md in the permanent checkout.

## Decisions

- Undelivered relay messages have a 48-hour delivery window.
- Retain messages for 14 days in relay D1 and local agent mailboxes. Idle local mailboxes purge on the next bus activity.
- Preserve legacy report deadlines, reply integrity, and content-free replay protection.
- Production deployment and first deletion require separate authorization and an offline cutover.

The relay currently shares a 30-day predicate for delivery and reports. Both stores have dependent records that must be handled before deleting a message. Revision 2 accounts for those dependencies and requires a compatible version before processes access activated stores.
