# SPDX-License-Identifier: Apache-2.0
"""
Giving claims canonical labels, so the counts stop being noise.

## The bug being fixed, measured

The first live 5-replicate renewal ensemble produced **128 claims, every one counted 1/5**.
Every persona reported zero invariant demands. All 24 unresolved groups had count 1. Meanwhile
the synthesis, reading the same extractions, found four conclusions held in 5 of 5 — so the
numeric half of the report contradicted the prose half, and the numeric half looked the more
authoritative because it had numbers in it.

Cause: `ensemble._normalise` keys on sorted unique non-stopword tokens, so any rewording is a
different claim. Four runs saying "a state statute defining plans as regulated products" in
four ways counted as four singletons.

## Why the fix is dangerous, and what holds it in check

Over-merging **deletes a dissent**, and it does so invisibly — nothing downstream can detect
that two different requirements were combined. `_normalise` was crude on purpose for exactly
this reason. So the tests here are lopsided towards the merge that must NOT happen:

  bias        same topic at a different threshold, scope or trigger stays separate
  arithmetic  `apply_clusters` refuses a clustering that drops, duplicates or invents a claim,
              or that mixes a demand with a refusal — all checkable in code
  audit       every row keeps the phrasings that were grouped, with their runs

The arithmetic checks are the load-bearing ones, because they are the only part that does not
depend on a model behaving. A model asked to group 128 claims can silently drop twenty, which
would under-count exactly like the bug being fixed while looking like the fix.
"""

import json

import pytest

from matrix_studio import ensemble


# Real phrasings from the first live ensemble, trimmed. The first four are one requirement in
# four voices; the fifth is a DIFFERENT legal trigger and must not join them.
REWORDED = [
    ("demand", "A state statute defining specialty plans as regulated-only"),
    ("demand", "A state practice act would have to explicitly define specialty plans as "
               "regulated products"),
    ("demand", "A state that explicitly defines the specialty plan itself as a regulated "
               "product would be required"),
    ("demand", "A state practice act that reaches treatment, not just drugs"),
]


class FakeCall:
    """An `_acompletion`-shaped callable returning a canned clustering."""

    def __init__(self, payload, *, cost=0.02, finish="stop"):
        self.payload = payload
        self.cost = cost
        self.finish = finish
        self.prompts = []

    async def __call__(self, messages, model=None, temperature=0.4, max_tokens=None):
        self.prompts.append(messages[0]["content"])
        body = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return {"content": body, "tokens_in": 100, "tokens_out": 50,
                "cost_usd": self.cost, "finish_reason": self.finish}


# --------------------------------------------------------------------------- #
# the prompt
# --------------------------------------------------------------------------- #


class TestThePromptIsBiasedAgainstMerging:
    """Pinned, because the bias IS the safety mechanism.

    Loosening any of this makes over-merging likelier, and over-merging is the failure that
    cannot be detected downstream. A test that fails when the wording is softened is the only
    thing standing between "grouped" and "quietly averaged".
    """

    def test_it_states_the_satisfaction_rule(self):
        # Same topic is not enough; the test is whether satisfying one satisfies the other.
        text = ensemble._CLUSTER_PROMPT.replace("\n", " ")
        assert "satisfying one would satisfy the other" in text
        assert "Same TOPIC is not enough" in text

    def test_it_says_a_cluster_of_one_is_correct(self):
        text = ensemble._CLUSTER_PROMPT.replace("\n", " ")
        assert "DO NOT GROUP" in ensemble._CLUSTER_PROMPT
        assert "one claim is a correct answer" in text

    def test_it_names_the_cost_of_over_merging(self):
        # The model is told what it destroys, not merely what to do.
        text = ensemble._CLUSTER_PROMPT.replace("\n", " ")
        assert "deletes a disagreement" in text

    def test_it_separates_thresholds_scopes_and_triggers(self):
        text = ensemble._CLUSTER_PROMPT.replace("\n", " ")
        assert "different threshold" in text
        assert "different scope" in text
        assert "different legal triggers" in text

    def test_it_forbids_mixing_a_demand_with_a_refusal(self):
        text = ensemble._CLUSTER_PROMPT.replace("\n", " ")
        assert "different acts" in text

    def test_it_demands_a_total_assignment(self):
        text = ensemble._CLUSTER_PROMPT.replace("\n", " ")
        assert "exactly one cluster" in text
        assert "Do not omit any" in text

    def test_every_claim_is_numbered_for_the_model(self):
        rendered = ensemble._cluster_input(REWORDED)
        assert rendered.startswith("0. [demand] A state statute")
        assert "3. [demand] A state practice act that reaches treatment" in rendered


# --------------------------------------------------------------------------- #
# the call
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestClusterClaims:
    async def test_it_returns_the_clusters(self):
        call = FakeCall({"clusters": [
            {"label": "state statute defining plans as regulated", "members": [0, 1, 2]},
            {"label": "practice act reaching treatment", "members": [3]},
        ]})
        out = await ensemble.cluster_claims(REWORDED, call=call)
        assert [c["label"] for c in out] == [
            "state statute defining plans as regulated", "practice act reaching treatment",
        ]
        assert out[0]["_cost_usd"] == 0.02

    async def test_it_asks_for_temperature_zero(self):
        # ASKS for it. On the deployed path this role is Sonnet 5, which accepts only
        # temperature=1, and litellm's `drop_params` discards the rest silently — so this is
        # NOT a determinism guarantee and the code says so. It is asked for because the role is
        # overridable: a caller pointing clustering at a temperature-honouring model should get
        # reproducibility rather than have it dropped on their behalf.
        seen = {}

        async def call(messages, model=None, temperature=0.4, max_tokens=None):
            seen["t"] = temperature
            return {"content": json.dumps({"clusters": [{"label": "x", "members": [0, 1, 2, 3]}]}),
                    "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}

        await ensemble.cluster_claims(REWORDED, call=call)
        assert seen["t"] == 0.0

    async def test_one_claim_needs_no_clustering(self):
        called = FakeCall({"clusters": []})
        assert await ensemble.cluster_claims([("demand", "x")], call=called) is None
        assert called.prompts == [], "no money should be spent to group a single claim"

    async def test_empty_content_returns_none_and_is_logged_as_truncation(self, caplog):
        # A truncated reply comes back EMPTY, not partial — the third time this project has hit
        # it. The log has to point at the token budget, not at a refusal.
        call = FakeCall("", finish="length")
        with caplog.at_level("ERROR"):
            assert await ensemble.cluster_claims(REWORDED, call=call) is None
        assert "truncated reply comes back EMPTY" in caplog.text

    async def test_prose_instead_of_json_returns_none(self):
        call = FakeCall("Sure! Here are the groups: the first three go together.")
        assert await ensemble.cluster_claims(REWORDED, call=call) is None

    async def test_a_missing_clusters_key_returns_none(self):
        call = FakeCall({"groups": [{"label": "x", "members": [0]}]})
        assert await ensemble.cluster_claims(REWORDED, call=call) is None


# --------------------------------------------------------------------------- #
# the arithmetic checks
# --------------------------------------------------------------------------- #


class TestApplyClustersRefusesUnsoundness:
    """The part that does not depend on a model behaving.

    Each of these is a way for a clustering to change the counts rather than only group them,
    and each would otherwise look like a successful fix.
    """

    def test_a_sound_clustering_maps_every_claim(self):
        keys = ensemble.apply_clusters(REWORDED, [
            {"label": "statute defines plans as regulated", "members": [0, 1, 2]},
            {"label": "practice act reaches treatment", "members": [3]},
        ])
        assert keys[0] == keys[1] == keys[2]
        assert keys[3] != keys[0]
        assert keys[0][1] == "statute defines plans as regulated"

    def test_a_dropped_claim_is_refused(self):
        # THE important one. Dropping claims under-counts exactly like the text matching this
        # replaces, so a partial clustering must not be trusted at all.
        with pytest.raises(ensemble.ClusteringRejected, match="not assigned"):
            ensemble.apply_clusters(REWORDED, [{"label": "x", "members": [0, 1]}])

    def test_a_duplicated_claim_is_refused(self):
        # Would inflate a count: one claim contributing to two clusters.
        with pytest.raises(ensemble.ClusteringRejected, match="two clusters"):
            ensemble.apply_clusters(REWORDED, [
                {"label": "a", "members": [0, 1, 2, 3]},
                {"label": "b", "members": [2]},
            ])

    def test_an_invented_claim_is_refused(self):
        with pytest.raises(ensemble.ClusteringRejected, match="does not exist"):
            ensemble.apply_clusters(REWORDED, [
                {"label": "a", "members": [0, 1, 2, 3, 9]},
            ])

    def test_mixing_a_demand_with_a_refusal_is_refused(self):
        claims = [("demand", "requires labwork"), ("refusal", "will not accept a checkbox")]
        with pytest.raises(ensemble.ClusteringRejected, match="different acts"):
            ensemble.apply_clusters(claims, [{"label": "labwork", "members": [0, 1]}])

    def test_an_unlabelled_cluster_is_refused(self):
        # The label becomes the row heading; an empty one would render a blank claim.
        with pytest.raises(ensemble.ClusteringRejected, match="no label"):
            ensemble.apply_clusters(REWORDED, [{"label": "  ", "members": [0, 1, 2, 3]}])

    def test_an_empty_cluster_is_refused(self):
        with pytest.raises(ensemble.ClusteringRejected, match="no members"):
            ensemble.apply_clusters(REWORDED, [
                {"label": "a", "members": [0, 1, 2, 3]}, {"label": "b", "members": []},
            ])

    def test_a_non_numeric_member_is_refused(self):
        with pytest.raises(ensemble.ClusteringRejected, match="non-numeric"):
            ensemble.apply_clusters(REWORDED, [{"label": "a", "members": ["first"]}])


# --------------------------------------------------------------------------- #
# one call per kind
# --------------------------------------------------------------------------- #


MIXED = [
    ("demand", "a verified weight before approval"),
    ("refusal", "will not accept a checkbox"),
    ("demand", "a confirmed weight is required first"),
    ("refusal", "refuses to sign off without a live exam"),
]


class KindAwareCall:
    """Answers each per-kind call separately, and records what it was shown."""

    def __init__(self, *, fail_kind=None, cost=0.02):
        self.fail_kind = fail_kind
        self.cost = cost
        self.shown = []

    async def __call__(self, messages, model=None, temperature=0.4, max_tokens=None):
        prompt = messages[0]["content"]
        kinds = {"demand" if "[demand]" in prompt else None,
                 "refusal" if "[refusal]" in prompt else None} - {None}
        self.shown.append(sorted(kinds))
        if self.fail_kind and self.fail_kind in kinds:
            return {"content": "", "cost_usd": self.cost, "tokens_in": 1,
                    "tokens_out": max_tokens, "finish_reason": "length"}
        # Group everything this call was shown into one cluster, numbered LOCALLY.
        n = sum(1 for line in prompt.splitlines() if line[:1].isdigit())
        return {"content": json.dumps({"clusters": [
            {"label": f"{sorted(kinds)[0]} cluster", "members": list(range(n))},
        ]}), "cost_usd": self.cost, "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}


@pytest.mark.asyncio
class TestOneCallPerKind:
    """Splitting by kind makes the no-mixing rule structural, not just checked.

    It is also what fixed the real failure: 128 claims in one call produced
    `finish_reason=length` with EMPTY content at max_tokens=8000.
    """

    async def test_each_kind_is_shown_alone(self):
        call = KindAwareCall()
        await ensemble.cluster_claims(MIXED, call=call)
        # Two calls, each shown exactly one kind. The model is never offered a mixture, so it
        # cannot propose one.
        assert call.shown == [["demand"], ["refusal"]]

    async def test_global_indices_survive_the_split(self):
        # The model numbers each kind from zero. If the mapping back were wrong, labels would
        # attach to the wrong claims and no arithmetic check would notice.
        call = KindAwareCall()
        clusters = await ensemble.cluster_claims(MIXED, call=call)
        keys = ensemble.apply_clusters(MIXED, clusters)

        assert keys[0] == keys[2], "the two demands grouped"
        assert keys[1] == keys[3], "the two refusals grouped"
        assert keys[0] != keys[1], "demands and refusals stayed apart"

    async def test_cost_is_summed_across_the_calls(self):
        call = KindAwareCall(cost=0.03)
        clusters = await ensemble.cluster_claims(MIXED, call=call)
        assert clusters[0]["_cost_usd"] == pytest.approx(0.06)

    async def test_a_kind_with_one_claim_costs_nothing(self):
        claims = [("demand", "a"), ("demand", "b"), ("refusal", "only refusal")]
        call = KindAwareCall()
        clusters = await ensemble.cluster_claims(claims, call=call)

        assert call.shown == [["demand"]], "no call for a kind with nothing to group"
        keys = ensemble.apply_clusters(claims, clusters)
        assert keys[2][1] == "only refusal"

    async def test_one_kind_failing_abandons_the_whole_clustering(self):
        # Clustered demands beside unclustered refusals would leave half the table trustworthy
        # with no way for a reader to tell which half.
        call = KindAwareCall(fail_kind="refusal")
        assert await ensemble.cluster_claims(MIXED, call=call) is None

    async def test_the_output_budget_is_sized_for_reasoning_not_for_the_answer(self):
        # Measured: 65 refusal claims produced a 4,332-character answer (~1,100 tokens) and
        # `tokens_out` of 29,260 — roughly 28,000 tokens of reasoning, billed and counted
        # against max_tokens. 8000 and 24000 both returned EMPTY with finish=length and a real
        # bill; 60000 returned finish=stop on the same input.
        #
        # Pinned as a number because the symptom of regressing it is silence: an empty reply,
        # a fallback, and a report whose counts quietly become noise.
        assert ensemble.CLUSTER_MAX_TOKENS >= 60000

    async def test_the_budget_is_passed_through(self):
        seen = {}

        async def call(messages, model=None, temperature=0.0, max_tokens=None):
            seen["max"] = max_tokens
            return {"content": json.dumps({"clusters": [{"label": "x", "members": [0, 1, 2, 3]}]}),
                    "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}

        await ensemble.cluster_claims(REWORDED, call=call)
        assert seen["max"] == ensemble.CLUSTER_MAX_TOKENS
