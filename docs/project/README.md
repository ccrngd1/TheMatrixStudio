# Project: spec, phases and plans

How the project was scoped and built: the original spec, each build phase's requirements, design
and measurement documents, and the plan for the AWS port. These are records of decisions as they
were made; for how the product works today, start with the docs one level up.

Two phase numberings appear here. The product's own build phases run from 0 to 6; the phase plan
in `PROJECT-SPEC.md` covers 0 to 3, and 4 to 6 came later. The AWS port has its own Phases 0 to 7,
set out in `AWS-IMPLEMENTATION-PLAN.md`; the design and measurement documents for that plan are
listed under "AWS port" below.

## Spec and plan

- [PROJECT-SPEC.md](PROJECT-SPEC.md): the original ideation and architecture spec, with the phase plan.
- [AWS-IMPLEMENTATION-PLAN.md](AWS-IMPLEMENTATION-PLAN.md): the AWS port, phase by phase, with what proved each step, including the cancelled Phase 4.

## Product build phases

- [PHASE0-RESEARCH.md](PHASE0-RESEARCH.md): Phase 0 technical decisions on web stack, Bedrock image model, packaging, licence and storage.
- [PHASE0-REQUIREMENTS.md](PHASE0-REQUIREMENTS.md): Phase 0 requirements, extracting and de-coupling the engine.
- [PHASE1-REQUIREMENTS.md](PHASE1-REQUIREMENTS.md): Phase 1 requirements, the control-room UI over the existing engine.
- [PHASE1.5-REQUIREMENTS.md](PHASE1.5-REQUIREMENTS.md): Phase 1.5 requirements, the post-run summary and read-only asides.
- [PHASE2A-REQUIREMENTS.md](PHASE2A-REQUIREMENTS.md): Phase 2a requirements, per-turn checkpoints, the branch primitive and the scrubber.
- [PHASE2B-REQUIREMENTS.md](PHASE2B-REQUIREMENTS.md): Phase 2b requirements, interventions as branch-with-one-mutation.
- [PHASE2C-REQUIREMENTS.md](PHASE2C-REQUIREMENTS.md): Phase 2c requirements, the cognitive layer, dossier and "why did it say that?" trace.
- [PHASE3-REQUIREMENTS.md](PHASE3-REQUIREMENTS.md): Phase 3 requirements, release polish.
- [PHASE4-REQUIREMENTS.md](PHASE4-REQUIREMENTS.md): Phase 4 requirements, the validation gate, pending threads, adaptive pressure and structured output.
- [PHASE5-RETRIEVAL-DESIGN.md](PHASE5-RETRIEVAL-DESIGN.md): Phase 5 design for per-persona document retrieval.
- [PHASE5-RETRIEVAL-MEASUREMENT.md](PHASE5-RETRIEVAL-MEASUREMENT.md): Phase 5 retrieval recall measured on FTS5 and sqlite-vec, now the pre-port baseline.
- [PHASE5-PREMISE-VALIDATION.md](PHASE5-PREMISE-VALIDATION.md): the three-arm experiment that tested structured personas and source grounding before Phase 6.
- [PHASE6-STRUCTURED-PERSONAS.md](PHASE6-STRUCTURED-PERSONAS.md): Phase 6 structured personas, built and measured (Arm D).
- [PHASE6-DISMISSAL-RETUNE.md](PHASE6-DISMISSAL-RETUNE.md): the pre-registered re-tune of the Phase 6 dismissal rule.
- [PHASE6-COGNITION-INTERACTION.md](PHASE6-COGNITION-INTERACTION.md): the pre-registered test of structured personas with cognition on.

## AWS port

- [PHASE2-STORAGE-KEY-DESIGN.md](PHASE2-STORAGE-KEY-DESIGN.md): AWS port Phase 2, the DynamoDB key design settled before the storage port.
- [PHASE3-RECALL-MEASUREMENT.md](PHASE3-RECALL-MEASUREMENT.md): AWS port Phase 3, retrieval recall re-measured on S3 Vectors.
- [PHASE5-ORCHESTRATION-DESIGN.md](PHASE5-ORCHESTRATION-DESIGN.md): AWS port Phase 5, moving the turn loop into Step Functions.
- [PHASE6-KB-DESIGN.md](PHASE6-KB-DESIGN.md): AWS port Phase 6, knowledge bases, grants and the tenancy exception.
