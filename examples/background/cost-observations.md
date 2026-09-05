# Cost & Token Observations

## Measured baseline

A one-turn simulation on Claude Haiku 4.5 via Bedrock costs approximately
$0.0006 in total. A fifteen-turn run with five personas measured at $0.053.

## Cognition overhead

Enabling cognition mode raises token usage by roughly 20 to 40 percent. It uses
structured JSON output (one call per turn rather than plain text), adds memory
retrieval to every prompt, and triggers periodic reflection calls.

## The demonstration risk

Stakeholders watch the cost meter live during demonstrations. The absolute number
is rarely the problem; an unexplained jump mid-demonstration is. A feature whose
cost was never measured is the actual hazard, not a feature that costs more.

## Requirement before enabling any retrieval by default

A measured net token delta on a realistic run: the embedding and retrieval
overhead must cost less than the prompt tokens saved by not placing the whole
document in context. Measured on a real document at realistic conversation
length, not estimated.
