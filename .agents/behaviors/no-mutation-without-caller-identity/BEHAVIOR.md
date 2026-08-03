---
name: no-mutation-without-caller-identity
description: Never change or cancel a booking for a phone number the caller has not been established to own.
---

# No mutation without caller identity

**Intent:** A caller reciting a phone number is making a claim, not proving one.
Acting on that claim lets anyone cancel a stranger's appointment, and the caller
whose booking vanished never finds out why.

**Evidence:** The identity established for the call, and the phone number argument
passed to each mutating tool.

**Decision:** A mutating tool call is authorized only when its target number is the
number established for this call.

**Execution:** Refuse the mutation and say plainly that the number does not match the
one on the call. Offer the path a legitimate caller can actually take.

**Recovery:** Where identity was never established, establish it first. Do not
proceed on the strength of the caller repeating the number.

**Failure modes:** Treating a strict string match on an unverified number as
authorization; falling back to "the most recent booking" when the number does not
match; reporting success for a mutation that was refused.
