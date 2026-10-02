# ADR 0001: Ports and adapters; engine owns decisions

**Status:** accepted

**Context.** The brief is to make GHL work *alongside* FieldPulse, then repeat the build for other home-service
clients on other field-service CRMs (ServiceTitan, Housecall Pro). Both vendors' APIs have gaps and will change.

**Decision.** All vendor access sits behind two `Protocol`s (`CRMPort`, `FieldServicePort`). Business rules
(consent, stop-on-convert, campaign sizing) live in services that only see ports. A mock adapter implements both ports
and powers tests and the demo.

**Consequences.**
- A new client on ServiceTitan = one new `FieldServicePort` adapter plus a contract-test file; services unchanged.
- Everything is testable offline (139 tests, no network).
- Cost: one extra layer of indirection and a mapping step per vendor.

**Alternative rejected.** Pure GHL workflows + Zapier. Fast to start, but consent logic, the capacity model, anomaly
guards and idempotency are hard to express, test and audit in a no-code canvas.
