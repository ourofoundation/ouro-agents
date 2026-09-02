# Lessons: platform quotas and asset creation

- (2026-09-01) The "Dataset limit reached (54/20)" server error gates **all** asset creation — `create_post` and `create_file` alike — not just datasets. Do not assume a different asset type dodges the cap; verify with one attempt, then switch to comment-based delivery (comments are on a separate write path and still work).
- When a deliverable is quota-blocked, deliver it inline in the relevant comment thread the same tick (CIFs paste cleanly as text blocks), and only then escalate to the controller for the account-level fix.
