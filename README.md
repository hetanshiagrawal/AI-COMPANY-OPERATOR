# AI-COMPANY-OPERATOR
# AI Company Operator (prototype)

An autonomous operator that turns a plain-language company request into
completed, verified work across documents, files and an internal web system.

**Demo task:** process monthly expense reports end to end - read policy,
classify claims, ask for approval, file them in a finance portal, verify,
and deliver a summary with evidence.

## Run
    python company_operator.py --approver yes
    python company_operator.py --selftest
Requires Python 3.9+. No dependencies.

## Loop
Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete

## Highlights
- Guard layer enforces policy in code (approval thresholds, path and tool limits)
- Recovers from 503s, a stale cost center, and silent write loss (caught by re-reading the portal)
- Human-in-the-loop for ambiguous goals and over-threshold approvals
- JSON trace + markdown summary as evidence

## Limits
Narrow by design. Simulated company environment. Default planner is
deterministic; `--planner llm` (needs ANTHROPIC_API_KEY) is optional.
New tools (browser, desktop) plug in as `t_<name>` methods in ToolBox.
