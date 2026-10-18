import unittest

from research.autotrade_research.learning.generalization import (
    AssetClassProfile,
    CrossMarketGeneralizationError,
    CrossMarketTrainingProtocol,
    MarketRegimeCell,
    MarketRegimeEvidence,
    assess_cross_market_generalization,
)
from research.autotrade_research.learning.population_coverage import (
    PopulationCoverageManifest,
    _digest as population_digest,
    _summary as population_summary,
)


BUILD = "a" * 40
CANDIDATE = "sha256:" + "1" * 64
POPULATION_PROTOCOL = "sha256:" + "2" * 64
SNAPSHOT = "sha256:" + "3" * 64


def h(char):
    return "sha256:" + char * 64


def manifest(
    *,
    episode_id,
    episode_digest_char,
    family,
    regime,
    observations=1,
    candidate_hash=CANDIDATE,
    protocol_hash=POPULATION_PROTOCOL,
    labels_complete=True,
):
    if observations < 1:
        raise ValueError("test manifest observations must be positive")
    episode_ids = tuple(
        f"{episode_id}-{index:02d}"
        for index in range(observations)
    )
    episode_digests = tuple(
        (identity, h(episode_digest_char))
        for identity in episode_ids
    )
    outcome_rows = tuple(
        ("POSITIVE", h(episode_digest_char), False)
        for _ in episode_ids
    )
    summary = population_summary(outcome_rows)
    values = {
        "candidate_hash": candidate_hash,
        "frozen_protocol_hash": protocol_hash,
        "input_snapshot_hash": SNAPSHOT,
        "causal_cutoff": "2030-01-01T00:00:00+00:00",
        "permission_classes": ("research",),
        "task": "research",
        "instrument_family": family,
        "eligible_episode_ids": episode_ids,
        "included_episode_ids": episode_ids,
        "exclusions": (),
        "episode_digests": episode_digests,
        "eligible_outcomes": summary,
        "included_outcomes": summary,
        "eligible_no_trade_count": 0,
        "included_no_trade_count": 0,
        "included_regime_counts": ((regime, observations),),
        "included_labels_complete_by_regime": ((regime, labels_complete),),
    }
    values["digest"] = population_digest(values)
    return PopulationCoverageManifest(**values)


def profile(asset_class, family, features):
    return AssetClassProfile.create(
        asset_class=asset_class,
        instrument_families=(family,),
        specialized_feature_namespaces=features,
    )


def protocol(*, minimum=1, cells=None, profiles=None):
    profiles = (
        (
            profile(
                "crypto",
                "crypto_spot",
                ("crypto_funding", "crypto_orderbook"),
            ),
            profile(
                "equity",
                "equity",
                ("equity_corporate_actions", "equity_session"),
            ),
        )
        if profiles is None
        else profiles
    )
    cells = (
        (
            MarketRegimeCell("crypto", "bull"),
            MarketRegimeCell("crypto", "crisis"),
            MarketRegimeCell("equity", "bull"),
            MarketRegimeCell("equity", "crisis"),
        )
        if cells is None
        else cells
    )
    return CrossMarketTrainingProtocol.create(
        protocol_id="school-v1",
        candidate_hash=CANDIDATE,
        population_protocol_hash=POPULATION_PROTOCOL,
        exact_build_sha=BUILD,
        asset_profiles=profiles,
        required_cells=cells,
        min_observations_per_cell=minimum,
    )


def evidence(
    asset_class,
    regime,
    *,
    family,
    digest_char,
    score="0.01",
    observations=1,
    costs_complete=True,
    labels_complete=True,
    candidate_hash=CANDIDATE,
    protocol_hash=POPULATION_PROTOCOL,
    features=None,
):
    if features is None:
        features = (
            ("crypto_funding", "crypto_orderbook")
            if asset_class == "crypto"
            else ("equity_corporate_actions", "equity_session")
        )
    return MarketRegimeEvidence.create(
        asset_class=asset_class,
        regime=regime,
        exact_build_sha=BUILD,
        population=manifest(
            episode_id=f"{asset_class}-{regime}",
            episode_digest_char=digest_char,
            family=family,
            regime=regime,
            observations=observations,
            candidate_hash=candidate_hash,
            protocol_hash=protocol_hash,
            labels_complete=labels_complete,
        ),
        execution_adjusted_net_score=score,
        observations=observations,
        costs_complete=costs_complete,
        observed_feature_namespaces=features,
    )


def complete_evidence(overrides=None):
    rows = {
        ("crypto", "bull"): evidence(
            "crypto",
            "bull",
            family="crypto_spot",
            digest_char="4",
        ),
        ("crypto", "crisis"): evidence(
            "crypto",
            "crisis",
            family="crypto_spot",
            digest_char="5",
        ),
        ("equity", "bull"): evidence(
            "equity",
            "bull",
            family="equity",
            digest_char="6",
        ),
        ("equity", "crisis"): evidence(
            "equity",
            "crisis",
            family="equity",
            digest_char="7",
        ),
    }
    if overrides:
        rows.update(overrides)
    return tuple(rows[key] for key in sorted(rows))


class CrossMarketGeneralizationTests(unittest.TestCase):
    def test_complete_multi_asset_multi_regime_school_passes_coverage(self):
        result = assess_cross_market_generalization(
            protocol(),
            complete_evidence(),
        )
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.coverage_ready)
        self.assertEqual(
            set(result.cell_statuses.values()),
            {"PASS"},
        )

    def test_coverage_pass_never_establishes_economic_edge(self):
        result = assess_cross_market_generalization(
            protocol(),
            complete_evidence(),
        )
        self.assertEqual(
            result.economic_edge_status,
            "NOT_ESTABLISHED",
        )

    def test_negative_execution_adjusted_score_is_retained_not_cherry_picked(self):
        rows = complete_evidence(
            {
                ("crypto", "crisis"): evidence(
                    "crypto",
                    "crisis",
                    family="crypto_spot",
                    digest_char="8",
                    score="-0.25",
                )
            }
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            rows[1].execution_adjusted_net_score.as_tuple().sign,
            1,
        )

    def test_missing_registered_cell_is_inconclusive(self):
        rows = complete_evidence()[:-1]
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.coverage_ready)
        self.assertIn(
            "missing required cell: equity::crisis",
            result.reasons,
        )

    def test_unregistered_post_hoc_cell_fails(self):
        rows = complete_evidence() + (
            evidence(
                "crypto",
                "sideways",
                family="crypto_spot",
                digest_char="8",
            ),
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "FAIL")
        self.assertIn(
            "unregistered evidence cell: crypto::sideways",
            result.reasons,
        )

    def test_evidence_build_mismatch_fails(self):
        rows = list(complete_evidence())
        object.__setattr__(rows[0], "exact_build_sha", "b" * 40)
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(
            any("exact build mismatch" in reason for reason in result.reasons)
        )

    def test_candidate_population_mismatch_fails(self):
        rows = list(complete_evidence())
        rows[0] = evidence(
            "crypto",
            "bull",
            family="crypto_spot",
            digest_char="8",
            candidate_hash=h("9"),
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(
            any("candidate hash mismatch" in reason for reason in result.reasons)
        )

    def test_population_protocol_mismatch_fails(self):
        rows = list(complete_evidence())
        rows[0] = evidence(
            "crypto",
            "bull",
            family="crypto_spot",
            digest_char="8",
            protocol_hash=h("9"),
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(
            any("frozen protocol mismatch" in reason for reason in result.reasons)
        )

    def test_asset_family_laundering_fails(self):
        rows = list(complete_evidence())
        rows[0] = evidence(
            "crypto",
            "bull",
            family="equity",
            digest_char="8",
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(
            any("instrument family" in reason for reason in result.reasons)
        )

    def test_regime_count_binding_mismatch_fails(self):
        row = evidence(
            "crypto",
            "bull",
            family="crypto_spot",
            digest_char="8",
            observations=2,
        )
        object.__setattr__(row, "observations", 1)
        rows = list(complete_evidence())
        rows[0] = row
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(
            any("regime/count" in reason for reason in result.reasons)
        )

    def test_incomplete_labels_are_inconclusive(self):
        rows = list(complete_evidence())
        rows[0] = evidence(
            "crypto",
            "bull",
            family="crypto_spot",
            digest_char="8",
            labels_complete=False,
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertTrue(
            any("labels are incomplete" in reason for reason in result.reasons)
        )

    def test_missing_asset_specific_features_are_inconclusive(self):
        rows = list(complete_evidence())
        rows[0] = evidence(
            "crypto",
            "bull",
            family="crypto_spot",
            digest_char="8",
            features=("crypto_orderbook",),
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertTrue(
            any(
                "crypto_funding" in reason
                for reason in result.reasons
            )
        )

    def test_incomplete_execution_costs_are_inconclusive(self):
        rows = list(complete_evidence())
        rows[0] = evidence(
            "crypto",
            "bull",
            family="crypto_spot",
            digest_char="8",
            costs_complete=False,
        )
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertTrue(
            any("cost evidence is incomplete" in reason for reason in result.reasons)
        )

    def test_minimum_observation_requirement_is_enforced(self):
        result = assess_cross_market_generalization(
            protocol(minimum=2),
            complete_evidence(),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertTrue(
            any(
                "minimum observations per cell" in reason
                for reason in result.reasons
            )
        )

    def test_mutated_population_manifest_is_revalidated_before_use(self):
        rows = list(complete_evidence())
        object.__setattr__(
            rows[0].population,
            "included_regime_counts",
            (("bull", 999),),
        )
        with self.assertRaises(ValueError):
            assess_cross_market_generalization(protocol(), rows)

    def test_duplicate_evidence_cell_is_rejected(self):
        rows = complete_evidence()
        with self.assertRaisesRegex(
            CrossMarketGeneralizationError,
            "duplicate evidence cell",
        ):
            assess_cross_market_generalization(
                protocol(),
                rows + (rows[0],),
            )

    def test_same_population_manifest_cannot_substitute_for_two_cells(self):
        rows = list(complete_evidence())
        shared = rows[0].population
        object.__setattr__(rows[1], "population", shared)
        result = assess_cross_market_generalization(protocol(), rows)
        self.assertEqual(result.status, "FAIL")
        self.assertIn(
            "one population manifest cannot substitute for multiple cells",
            result.reasons,
        )

    def test_protocol_requires_more_than_one_asset_class(self):
        crypto = profile(
            "crypto",
            "crypto_spot",
            ("crypto_funding",),
        )
        with self.assertRaisesRegex(
            CrossMarketGeneralizationError,
            "at least two asset classes",
        ):
            CrossMarketTrainingProtocol.create(
                protocol_id="bad",
                candidate_hash=CANDIDATE,
                population_protocol_hash=POPULATION_PROTOCOL,
                exact_build_sha=BUILD,
                asset_profiles=(crypto,),
                required_cells=(
                    MarketRegimeCell("crypto", "bull"),
                    MarketRegimeCell("crypto", "crisis"),
                ),
                min_observations_per_cell=1,
            )

    def test_protocol_requires_multiple_regimes(self):
        with self.assertRaisesRegex(
            CrossMarketGeneralizationError,
            "at least two distinct market regimes",
        ):
            CrossMarketTrainingProtocol.create(
                protocol_id="bad",
                candidate_hash=CANDIDATE,
                population_protocol_hash=POPULATION_PROTOCOL,
                exact_build_sha=BUILD,
                asset_profiles=(
                    profile("crypto", "crypto_spot", ("crypto_orderbook",)),
                    profile("equity", "equity", ("equity_session",)),
                ),
                required_cells=(
                    MarketRegimeCell("crypto", "bull"),
                    MarketRegimeCell("equity", "bull"),
                ),
                min_observations_per_cell=1,
            )

    def test_every_asset_class_requires_registered_cell(self):
        with self.assertRaisesRegex(
            CrossMarketGeneralizationError,
            "every asset class",
        ):
            CrossMarketTrainingProtocol.create(
                protocol_id="bad",
                candidate_hash=CANDIDATE,
                population_protocol_hash=POPULATION_PROTOCOL,
                exact_build_sha=BUILD,
                asset_profiles=(
                    profile("crypto", "crypto_spot", ("crypto_orderbook",)),
                    profile("equity", "equity", ("equity_session",)),
                ),
                required_cells=(
                    MarketRegimeCell("crypto", "bull"),
                    MarketRegimeCell("crypto", "crisis"),
                ),
                min_observations_per_cell=1,
            )

    def test_instrument_family_cannot_be_shared_between_asset_profiles(self):
        with self.assertRaisesRegex(
            CrossMarketGeneralizationError,
            "multiple asset-class profiles",
        ):
            CrossMarketTrainingProtocol.create(
                protocol_id="bad",
                candidate_hash=CANDIDATE,
                population_protocol_hash=POPULATION_PROTOCOL,
                exact_build_sha=BUILD,
                asset_profiles=(
                    profile("crypto", "shared", ("crypto_orderbook",)),
                    profile("equity", "shared", ("equity_session",)),
                ),
                required_cells=(
                    MarketRegimeCell("crypto", "bull"),
                    MarketRegimeCell("equity", "crisis"),
                ),
                min_observations_per_cell=1,
            )

    def test_asset_profile_requires_specialized_features(self):
        with self.assertRaisesRegex(
            CrossMarketGeneralizationError,
            "must not be empty",
        ):
            AssetClassProfile.create(
                asset_class="equity",
                instrument_families=("equity",),
                specialized_feature_namespaces=(),
            )

    def test_binary_float_score_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "exact Decimal"):
            evidence(
                "crypto",
                "bull",
                family="crypto_spot",
                digest_char="8",
                score=0.1,
            )

    def test_cell_statuses_are_read_only(self):
        result = assess_cross_market_generalization(
            protocol(),
            complete_evidence(),
        )
        with self.assertRaises(TypeError):
            result.cell_statuses["crypto::bull"] = "FAIL"

    def test_input_evidence_order_does_not_change_digest(self):
        rows = complete_evidence()
        first = assess_cross_market_generalization(protocol(), rows)
        second = assess_cross_market_generalization(
            protocol(),
            tuple(reversed(rows)),
        )
        self.assertEqual(first.evidence_sha256, second.evidence_sha256)

    def test_protocol_digest_changes_when_required_school_changes(self):
        first = protocol()
        second = protocol(
            cells=(
                MarketRegimeCell("crypto", "bull"),
                MarketRegimeCell("crypto", "crisis"),
                MarketRegimeCell("equity", "bull"),
                MarketRegimeCell("equity", "sideways"),
            )
        )
        self.assertNotEqual(first.digest, second.digest)


    def test_coverage_pass_never_establishes_regime_routing_strategy_or_trading_authority(self):
        result = assess_cross_market_generalization(
            protocol(),
            complete_evidence(),
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.economic_edge_status, "NOT_ESTABLISHED")
        self.assertEqual(result.regime_routing_status, "NOT_ESTABLISHED")
        self.assertEqual(result.strategy_comparison_status, "NOT_ESTABLISHED")
        self.assertFalse(result.grants_trading_authority)

    def test_hostile_profile_sequence_is_rejected_before_iteration(self):
        touched = {"count": 0}

        class HostileSequence:
            def __iter__(self):
                touched["count"] += 1
                raise AssertionError("hostile iterator executed")

        with self.assertRaisesRegex(TypeError, "exact tuple or list"):
            AssetClassProfile.create(
                asset_class="crypto",
                instrument_families=HostileSequence(),
                specialized_feature_namespaces=("crypto_orderbook",),
            )
        self.assertEqual(touched["count"], 0)

    def test_hostile_evidence_sequence_is_rejected_before_iteration(self):
        touched = {"count": 0}

        class HostileSequence:
            def __iter__(self):
                touched["count"] += 1
                raise AssertionError("hostile iterator executed")

        with self.assertRaisesRegex(TypeError, "exact tuple or list"):
            assess_cross_market_generalization(
                protocol(),
                HostileSequence(),
            )
        self.assertEqual(touched["count"], 0)


if __name__ == "__main__":
    unittest.main()
