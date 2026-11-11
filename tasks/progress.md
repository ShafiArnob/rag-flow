# Build-plan progress

Which subphases of the build plan (`../plan/`) are done. `/explore` reads this to check a subphase's dependencies, and `/ship` updates the row when a change completes a subphase. Add a phase's rows when work on it starts.

Status is one of: Not started, In progress, Done.

## P01: Foundation

| Subphase | Title | Status | Evidence |
| --- | --- | --- | --- |
| P01_S01 | Runner Spike | Done | PR #1, `docs/runner-contract.md` |
| P01_S02 | Monorepo, CI and Observability | Done | PR #3 |
| P01_S03 | Core Schema and Row-Level Security | Not started | |
| P01_S04 | Signup, Login and Sessions | Not started | |
| P01_S05 | Envelope Encryption and Key Provider | Not started | |
| P01_S06 | Egress Guard HTTP Client | Not started | |
| P01_S07 | Connector Registry and CRUD | Not started | |
| P01_S08 | Node Contract and Registry | Not started | |
| P01_S09 | Schema Settings Forms | Not started | |
| P01_S10 | Workflow Canvas | Not started | |
| P01_S11 | Graph Compiler and Runner | Not started | |
| P01_S12 | Foundation Acceptance | Not started | |
