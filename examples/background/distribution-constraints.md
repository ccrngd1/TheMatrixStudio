# Distribution & Packaging Constraints

## Install story (decided)

The tool ships two ways and only two ways: a Python project installable with
`pip install .`, and a Docker container image. Both must reach a working run in
under five minutes on a clean machine.

## Storage

State lives in a single embedded SQLite database at `./data/matrix_studio.db`.
This was chosen over Postgres and flat event-log files because it ships easily
and needs no external service for a distributable single-node tool.

## Why additional services are resisted

A quickstart that requires standing up a separate service loses users before they
ever see the product work. Every additional process in the install path becomes a
support burden owned by whoever ships the container. Historical evidence: a prior
product whose quickstart required a standalone vector database saw adoption stall
at the install step, with support tickets almost entirely environment setup.

## Acceptable dependency shapes

In descending order of preference: something already built into SQLite; a single
file alongside the database; a loadable extension with prebuilt wheels; a
separate process (last resort, opt-in only, never in the quickstart).
