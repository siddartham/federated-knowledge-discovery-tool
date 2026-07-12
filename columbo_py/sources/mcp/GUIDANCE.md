Offloaded automated tests run on the enterprise **Testing Platform** (live
integration tests, performance tests, and component tests moved out of Jenkins /
OnePipeline stages onto a dedicated platform). Use this source for questions
about **test outcomes and health**, e.g.:

- why a pipeline/release failed a test stage, and which tests failed
- flaky tests for a service or suite
- performance-test results and regressions (latency/throughput deltas)
- integration/component test coverage and recent run history for a service

Query style: free text. Include the **service or component name**, the **test
kind** ("integration", "performance", "component"), and intent words like
"flaky", "regression", "failed", "latest run", or a **pipeline/release id**.
Examples:
- `payment-service integration test failures latest run`
- `flaky tests in virtual-card-number suite`
- `performance regression money-movement-accounts p99 latency`

Not for source code, tickets, or design docs - use github/jira/confluence for
those. This source answers "what happened when we ran the tests", not "how the
code works".
