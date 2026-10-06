"""Decision-boundary tests using synthetic fixtures, not real runtime claims."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import unittest

from reference.core import (
    GateContext, Usage, assess_completion, canonical_digest, negotiate_mode,
    stop_decision, strict_json_loads, updated_failure_count, updated_stall_count,
    validate_task_semantics,
)
from reference.fixtures import fixture_context, load_example, make_fixture, make_record


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.task, self.records, self.context = make_fixture()

    def outcome(self, records=None, context=None, task=None):
        return assess_completion(self.task if task is None else task,
                                 self.records if records is None else records,
                                 self.context if context is None else context).outcome

    def attest_changed_records(self):
        self.context = replace(self.context, verified_record_digests=frozenset(
            canonical_digest(record) for record in self.records))

    def test_all_checks_required_for_success(self):
        self.assertEqual(self.outcome(), "PASS")
        self.assertEqual(self.outcome(records=self.records[:1]), "NEED_EVIDENCE")

    def test_empty_batch_cannot_pass(self):
        self.assertEqual(self.outcome(records=[]), "NEED_EVIDENCE")

    def test_a_real_failure_overrides_partial_success(self):
        self.records[1].update(result="fail", exit_code=1)
        self.attest_changed_records()
        self.assertEqual(self.outcome(), "REVISE")

    def test_zero_exit_cannot_override_failed_assertion(self):
        self.records[1].update(result="fail", exit_code=0)
        self.attest_changed_records()
        self.assertEqual(self.outcome(), "REVISE")

    def test_nonzero_exit_cannot_claim_success(self):
        self.records[0]["exit_code"] = 2
        self.attest_changed_records()
        self.assertEqual(self.outcome(), "REVISE")

    def test_inconclusive_is_not_success(self):
        self.records[1]["result"] = "inconclusive"
        self.attest_changed_records()
        self.assertEqual(self.outcome(), "NEED_EVIDENCE")

    def test_changed_dirty_tree_invalidates_evidence(self):
        changed = replace(self.context, snapshot_digest=canonical_digest({"dirty_change": 1}))
        self.assertEqual(self.outcome(context=changed), "NEED_EVIDENCE")

    def test_environment_change_invalidates_evidence(self):
        changed = replace(self.context, environment_digest=canonical_digest({"fixture_version": 2}))
        self.assertEqual(self.outcome(context=changed), "NEED_EVIDENCE")

    def test_unauthorized_contract_change_rejected(self):
        self.task["criteria"][0]["description"] = "Accept anything"
        self.assertEqual(self.outcome(), "REJECT")

    def test_authorized_revision_needs_fresh_evidence(self):
        self.task["revision"] += 1
        context = replace(self.context, approved_contract_digest=canonical_digest(self.task))
        self.assertEqual(self.outcome(context=context), "NEED_EVIDENCE")

    def test_wrong_check_digest_is_stale(self):
        self.records[0]["check_digest"] = canonical_digest({"different_check": 1})
        self.attest_changed_records()
        self.assertEqual(self.outcome(), "NEED_EVIDENCE")

    def test_agent_cannot_authenticate_own_record(self):
        self.records[0]["trusted"] = True
        self.assertEqual(self.outcome(), "REJECT")

    def test_artifact_tampering_changes_record_identity(self):
        self.records[0]["artifacts"][0]["sha256"] = canonical_digest({"forged_log": 1})
        self.assertEqual(self.outcome(), "REJECT")

    def test_default_context_does_not_clear_policy_or_effects(self):
        context = GateContext(self.context.approved_contract_digest,
                              self.context.snapshot_digest, self.context.environment_digest)
        self.assertEqual(self.outcome(context=context), "REJECT")

    def test_policy_unresolved_effect_or_finding_blocks_success(self):
        for changes in ({"policy_clear": False}, {"effects_reconciled": False},
                        {"blocking_findings": ("Behavior regression",)}):
            with self.subTest(changes=changes):
                self.assertEqual(self.outcome(context=replace(self.context, **changes)), "REJECT")

    def test_untrusted_record_rejected(self):
        self.assertEqual(self.outcome(context=replace(self.context,
                         verified_record_digests=frozenset())), "REJECT")

    def test_duplicate_check_record_rejected(self):
        self.assertEqual(self.outcome(records=self.records + [deepcopy(self.records[0])]), "REJECT")

    def test_duplicate_evidence_id_rejected(self):
        self.records[1]["evidence_id"] = self.records[0]["evidence_id"]
        self.attest_changed_records()
        self.assertEqual(self.outcome(), "REJECT")

    def test_evidence_for_other_task_or_unknown_check_rejected(self):
        for field, value in (("task_id", "another-task"), ("check_id", "unknown-check")):
            with self.subTest(field=field):
                task, records, context = make_fixture()
                records[0][field] = value
                context = fixture_context(task, records)
                self.assertEqual(self.outcome(records=records, context=context), "REJECT")

    def test_missing_artifacts_rejected_even_with_attested_fixture(self):
        self.records[0]["artifacts"] = []
        self.attest_changed_records()
        self.assertEqual(self.outcome(), "REJECT")

    def test_invalid_result_and_boolean_exit_rejected(self):
        for field, value in (("result", "probably-pass"), ("exit_code", True)):
            with self.subTest(field=field):
                task, records, context = make_fixture()
                records[0][field] = value
                self.assertEqual(assess_completion(task, records, fixture_context(task, records)).outcome, "REJECT")

    def test_reversed_or_naive_timestamps_rejected(self):
        for finished in ("2026-10-02T04:59:00Z", "2026-10-02T05:00:01"):
            with self.subTest(finished=finished):
                self.records[0]["finished_at"] = finished
                self.attest_changed_records()
                self.assertEqual(self.outcome(), "REJECT")

    def test_independent_review_requires_external_attribution(self):
        review = {"id": "review", "type": "review", "description": "Review against criteria",
                  "procedure": ["Inspect behavior, diff, and evidence"], "independent": True}
        self.task["checks"].append(review)
        self.records = [make_record(self.task, check, self.context.snapshot_digest,
                        self.context.environment_digest) for check in self.task["checks"]]
        context = fixture_context(self.task, self.records)
        self.assertEqual(self.outcome(context=context), "NEED_EVIDENCE")
        independent = replace(context, independent_record_digests=frozenset({canonical_digest(self.records[-1])}))
        self.assertEqual(self.outcome(context=independent), "PASS")

    def test_noncommand_cannot_use_exit_zero_as_review(self):
        self.task["checks"] = [{"id": "human", "type": "human", "description": "Inspect output",
                                "procedure": ["Confirm visible behavior"]}]
        self.task["criteria"] = [{"id": "visible", "description": "Visible behavior is correct",
                                  "check_ids": ["human"]}]
        record = make_record(self.task, self.task["checks"][0], self.context.snapshot_digest,
                             self.context.environment_digest)
        record["exit_code"] = 0
        self.assertEqual(self.outcome(records=[record], context=fixture_context(self.task, [record])), "REJECT")


class TaskAndDigestTests(unittest.TestCase):
    def test_cross_field_task_validation(self):
        original, _, _ = make_fixture()
        mutations = [
            lambda task: task.update(criteria=[]),
            lambda task: task["criteria"][0].update(check_ids=["not-declared"]),
            lambda task: task["checks"].append(deepcopy(task["checks"][0])),
            lambda task: task["criteria"].append(deepcopy(task["criteria"][0])),
            lambda task: task["checks"][0].update(independent=True),
            lambda task: task["checks"][0].update(argv="python test.py"),
        ]
        for mutate in mutations:
            task = deepcopy(original)
            mutate(task)
            with self.subTest(task=task), self.assertRaises(ValueError):
                validate_task_semantics(task)

    def test_example_tasks_have_valid_references(self):
        for path in ("templates/task.json", "examples/ui-task.json"):
            validate_task_semantics(load_example(path))

    def test_canonical_digest_is_order_independent_for_objects(self):
        expected = "sha256:" + hashlib.sha256(b'{"a":1,"z":[true,null]}').hexdigest()
        self.assertEqual(canonical_digest({"z": [True, None], "a": 1}), expected)
        self.assertEqual(canonical_digest({"a": 1, "z": [True, None]}), expected)

    def test_array_order_remains_significant(self):
        self.assertNotEqual(canonical_digest([1, 2]), canonical_digest([2, 1]))

    def test_floats_non_json_values_and_invalid_unicode_rejected(self):
        for value in (1.0, float("nan"), {1: "bad key"}, {"bad": {1, 2}}, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises((ValueError, UnicodeError)):
                canonical_digest(value)

    def test_strict_json_rejects_duplicate_keys_and_noninteger_numbers(self):
        for raw in ('{"a":1,"a":2}', '{"tokens":1.0}', '{"tokens":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json_loads(raw)

    def test_unsafe_cross_language_integers_rejected(self):
        for value in (9007199254740992, -9007199254740992):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_digest(value)

    def test_shared_canonical_vectors(self):
        for vector in load_example("examples/digest-vectors.json")["vectors"]:
            with self.subTest(vector=vector["id"]):
                self.assertEqual(canonical_digest(vector["input"]), vector["digest"])
                self.assertEqual(vector["digest"], "sha256:" + hashlib.sha256(
                    vector["canonical_json"].encode("utf-8")).hexdigest())

    def test_saved_evidence_example_matches_current_task(self):
        task, records, _ = make_fixture()
        self.assertEqual(load_example("examples/evidence.json"), records[0])
        self.assertEqual(load_example("examples/state.json")["contract_digest"], canonical_digest(task))


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.adapter = load_example("examples/native-adapter.json")
        self.runtime = load_example("examples/native-runtime.json")

    def test_native_fixture_can_select_unattended(self):
        self.assertEqual(negotiate_mode("unattended", self.adapter, self.runtime).mode, "unattended")

    def test_missing_runtime_control_downgrades_to_assisted(self):
        self.runtime["capabilities"]["isolated_workspace"] = False
        mode = negotiate_mode("unattended", self.adapter, self.runtime)
        self.assertEqual(mode.mode, "assisted")
        self.assertIn("isolated_workspace", mode.reasons[0])

    def test_omitted_host_capabilities_use_manual(self):
        self.assertEqual(negotiate_mode("unattended", self.adapter, {"capabilities": {}}).mode, "manual")

    def test_a_required_capability_is_not_silently_waived(self):
        mode = negotiate_mode("assisted", self.adapter, self.runtime,
                              required_runtime=["browser_interaction"])
        self.assertIsNone(mode.mode)

    def test_truthy_string_is_not_a_capability(self):
        self.adapter["capabilities"]["headless_execution"] = "true"
        self.assertEqual(negotiate_mode("unattended", self.adapter, self.runtime).mode, "assisted")

    def test_text_input_is_always_required(self):
        self.adapter["capabilities"].pop("text_input")
        self.assertIsNone(negotiate_mode("manual", self.adapter, self.runtime).mode)

    def test_never_exceeds_requested_autonomy(self):
        self.assertEqual(negotiate_mode("manual", self.adapter, self.runtime).mode, "manual")
        self.assertEqual(negotiate_mode("assisted", self.adapter, self.runtime).mode, "assisted")


class BudgetAndProgressTests(unittest.TestCase):
    def setUp(self):
        self.limits = load_example("templates/task.json")["limits"]

    def test_continue_within_supported_limits(self):
        self.assertIsNone(stop_decision(self.limits, Usage(iterations=1, wall_seconds=1)))

    def test_iteration_and_wall_boundaries(self):
        for usage in (Usage(iterations=12), Usage(wall_seconds=3600)):
            self.assertEqual(stop_decision(self.limits, usage).outcome, "BUDGET_EXHAUSTED")

    def test_cancellation_takes_priority(self):
        self.assertEqual(stop_decision(self.limits, Usage(iterations=12), cancelled=True).outcome, "CANCELLED")

    def test_unknown_tokens_do_not_become_zero(self):
        self.limits["max_tokens"] = 100
        self.assertEqual(stop_decision(self.limits, Usage()).outcome, "AWAITING_INPUT")
        self.assertEqual(stop_decision(self.limits, Usage(tokens=100)).outcome, "BUDGET_EXHAUSTED")

    def test_cost_requires_matching_currency(self):
        self.limits["max_cost_microunits"] = 1000000
        self.assertEqual(stop_decision(self.limits, Usage()).outcome, "AWAITING_INPUT")
        self.assertEqual(stop_decision(self.limits, Usage(cost_microunits=1, cost_currency="EUR")).outcome, "AWAITING_INPUT")
        self.assertIsNone(stop_decision(self.limits, Usage(cost_microunits=1, cost_currency="USD")))
        self.assertEqual(stop_decision(self.limits, Usage(cost_microunits=1000000, cost_currency="USD")).outcome, "BUDGET_EXHAUSTED")

    def test_stall_and_repeat_breakers(self):
        for usage in (Usage(stalled_iterations=3), Usage(repeated_failure_count=2)):
            self.assertEqual(stop_decision(self.limits, usage).outcome, "STALLED")

    def test_malformed_budgets_and_negative_usage_fail_closed(self):
        self.assertEqual(stop_decision(self.limits, Usage(wall_seconds=-1)).outcome, "FAILED")
        self.limits["max_iterations"] = True
        self.assertEqual(stop_decision(self.limits, Usage()).outcome, "FAILED")

    def test_old_progress_fact_does_not_reset_stall(self):
        seen = frozenset({"reproduced-bug"})
        self.assertEqual(updated_stall_count(2, seen, seen), 3)
        self.assertEqual(updated_stall_count(2, frozenset({"root-cause-confirmed"}), seen), 0)

    def test_same_root_error_keeps_counting(self):
        self.assertEqual(updated_failure_count("assertion-empty", "assertion-empty", 1), 2)
        self.assertEqual(updated_failure_count("assertion-empty", "different-error", 1), 1)
        self.assertEqual(updated_failure_count("assertion-empty", None, 2), 0)


if __name__ == "__main__":
    unittest.main()
