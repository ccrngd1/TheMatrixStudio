# Documentation

The user documentation is in four parts: tutorials, how-to guides, reference and explanation
(the [Diátaxis](https://diataxis.fr/) layout). New to Matrix Studio? Start with
[Your first conversation](tutorials/first-conversation.md).

## Tutorials

Lessons to work through in order, for learning the app by using it.

- [Your first conversation](tutorials/first-conversation.md)
- [Your first ensemble](tutorials/first-ensemble.md)

## How-to guides

Steps for one task, for when you know what you want to do.

Setting up a run:

- [Start a run from a setup file](how-to/start-a-run-from-a-setup-file.md)
- [Start a run from a saved cast or a persona archetype](how-to/start-a-run-from-the-library.md)
- [Start a new run from an earlier run's setup](how-to/start-fresh-from-an-earlier-run.md)
- [Research a subject before a run](how-to/research-before-a-run.md)
- [Manage knowledge collections](how-to/manage-knowledge-collections.md)
- [Add consultants to a run](how-to/add-consultants.md)
- [Set working assumptions and scheduled messages](how-to/set-assumptions-and-scheduled-messages.md)
- [Run an ensemble](how-to/run-an-ensemble.md)

During and after a run:

- [Stop and resume a run](how-to/stop-and-resume-a-run.md)
- [Use the closing round and read each persona's final stance](how-to/use-the-closing-round.md)
- [Branch a run from a turn](how-to/branch-a-run.md)
- [Fork a run with a different assumption](how-to/fork-with-a-different-assumption.md)
- [Ask an aside](how-to/ask-an-aside.md)
- [Open a message's context and a persona's dossier](how-to/inspect-a-message-or-a-persona.md)
- [Read an ensemble's comparison report](how-to/read-an-ensemble-report.md)
- [Export a run or a decision brief](how-to/export-a-run-or-a-brief.md)
- [Replay a run in the 8-bit theatre](how-to/replay-in-the-theatre.md)
- [Hide and show runs](how-to/hide-and-show-runs.md)

Operating the stack:

- [Run Matrix Studio locally](how-to/run-locally.md)
- [Deploy Matrix Studio to AWS](how-to/deploy-to-aws.md)
- [Verify a deployment](how-to/verify-a-deployment.md)
- [Configure models and spend caps](how-to/configure-models-and-spend-caps.md)
- [Delete runs](how-to/delete-runs.md)

## Reference

Exact descriptions to look things up in.

- [HTTP API](reference/http-api.md)
- [Run configuration](reference/run-config.md)
- [Settings and environment variables](reference/settings.md)
- [Event log](reference/events.md)
- [Data model](reference/data-model.md)
- [CLI and scripts](reference/cli-and-scripts.md)

## Explanation

Background and reasoning: how the app works and why it is built the way it is.

- [What Matrix Studio is for, and what it is not](explanation/what-matrix-studio-is-for.md)
- [How a run works](explanation/how-a-run-works.md)
- [Personas and convictions](explanation/personas-and-convictions.md)
- [Evidence: retrieval, knowledge collections, research and consultants](explanation/evidence-and-retrieval.md)
- [Reading the results](explanation/reading-the-results.md)
- [Why the defaults are what they are](explanation/why-the-defaults.md)
- [Common misconceptions](explanation/common-misconceptions.md)

## Feature and architecture docs

Design documents for individual features and for the AWS architecture.

- [AWS-SERVERLESS-ARCHITECTURE.md](AWS-SERVERLESS-ARCHITECTURE.md): the AWS design, covering tenancy, storage keys, the turn loop, retrieval and sharing.
- [ENSEMBLE-CONVERSATIONS.md](ENSEMBLE-CONVERSATIONS.md): ensembles, or running the same brief many times.
- [PERSONA-RESEARCH.md](PERSONA-RESEARCH.md): pre-conversation research.
- [THEATRE.md](THEATRE.md): the 8-bit theatre.
- [MOBILE-UI.md](MOBILE-UI.md): the mobile-first UI redesign (design only), with its prototype in [mockups/](mockups/).

## Project records and studies

- [project/](project/README.md): the original spec, each build phase's requirements, design and measurement documents, and the AWS port plan.
- [studies/](studies/README.md): pre-registered comparisons and evaluations behind the defaults.

`labels/` holds the label, transcript and scoring data the measurements use, and `screenshots/` holds
the images the docs use.
