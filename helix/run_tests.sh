#!/usr/bin/env bash
# Run every Helix test file, one at a time, and print one line per file.
# Some test files are pytest suites, some are plain scripts that print PASS/FAIL; this handles both.
#   ~/helix/run_tests.sh            all files
#   ~/helix/run_tests.sh -v         also show the first failure lines of each failing file
cd "$(dirname "$(readlink -f "$0")")"
PY="${PY:-./.venv/bin/python}"
export PATH="$PWD/.venv/bin:$PATH"   # tests call plain python3; make it the one with Helix's packages
VERBOSE="${1:-}"
# Tests that check the ORIGINAL owner's own history (old build orders, his git commits, his calibrated
# eval set, his category list). They cannot pass on a fresh install and say nothing about yours.
SKIP=(
  "build_orders/tools/test_orderlint.py::test_a_landed_54d_lints_with_exactly_its_two_new_files"
  "test_build_queue.py::test_old_builder_run_edited_mid_run_breaks_the_run"
  "workspace/coldread/test_coldread.py::test_each_plant_names_a_contradicting_real_fact_that_exists"
  "workspace/memretrieve/test_memretrieve.py::test_tokenizer_stop_list_and_bm25_params_equal_score_py"
  "workspace/memretrieve/test_memretrieve.py::test_live_floors_are_the_calibrated_ones"
  "workspace/regate/test_regate.py::test_the_real_registry_and_its_ledger_are_never_touched"
  "workspace/regate/test_regate.py::test_the_fixture_copy_is_the_pre_cutover_registry"
  "workspace/sealgate/test_sealgate.py::test_a_token_issued_before_p8_still_verifies"
  "workspace/sealgate/test_sealgate.py::test_a_token_issued_before_p9_still_verifies"
  "workspace/sealgate/test_sealgate.py::test_a_token_issued_before_p10_still_verifies"
  "workspace/categories/test_categories.py::test_real_list_loads_clean"
  "workspace/categories/test_categories.py::test_backfill_positive_control"
  "test_fact_categories.py::test_the_real_module_loads"
  "test_fact_categories.py::test_memory_declared_grouped_in_list_order"
  "test_fact_categories.py::test_blueprint_declared_wins_over_tag_and_filename"
  "test_fact_categories.py::test_broken_module_memory_identical_to_before"
  "test_fact_categories.py::test_broken_module_blueprint_identical_to_before"
  "test_fact_categories.py::test_missing_module_identical_to_before"
  "test_factgate_hook.py::test_62b_rows_20_and_29_are_the_logged_where_text"
  # orders 72-74 (2026-10-23): the owner's own category names, cold read and pre-order-74 backup file
  "test_fact_categories.py::test_memory_places_assigned_facts"
  "test_fact_categories.py::test_blueprint_places_assigned_facts"
  "test_fact_categories.py::test_no_test_run_opens_the_live_assigned_csv"
  "workspace/corrections/test_corrections.py::test_the_real_read_parses_verdict_runners_up_no_plant_no_duplicates"
  "workspace/memretrieve/test_memretrieve.py::test_74_all_arms_off_is_byte_identical_to_before_order_74[bm25]"
  "workspace/memretrieve/test_memretrieve.py::test_74_all_arms_off_is_byte_identical_to_before_order_74[embed]"
  "workspace/memretrieve/test_memretrieve.py::test_74_all_arms_off_is_byte_identical_to_before_order_74[hybrid]"
  # pin the original owner's own git commits, heartbeat targets, or his removed research-download line
  "workspace/memretrieve/test_memretrieve.py::test_real_model_finds_the_picture_generator_by_meaning"
  "workspace/seam/test_heartbeat.py::test_the_render_target_is_a_tcp_connect_to_the_local_daemon"
  "workspace/seam/test_heartbeat.py::test_the_targets_file_has_the_five_and_cites_a_source_for_each"
  "workspace/seam/test_nightly_regate.py::test_a_failing_registry_line_is_unchanged"
  "workspace/seam/test_nightly_regate.py::test_a_past_due_experiment_gets_only_the_overdue_line"
  "workspace/seam/test_nightly_regate.py::test_a_raising_hookcorpus_line_does_not_stop_the_nightly_and_appears_once"
  "workspace/seam/test_nightly_regate.py::test_a_refusal_writes_nothing_and_is_reported"
  "workspace/seam/test_nightly_regate.py::test_audit_cohort_and_rate_are_byte_identical_to_the_before_commit"
  "workspace/seam/test_nightly_regate.py::test_needs_seal_is_quiet_at_31_days"
  "workspace/seam/test_nightly_regate.py::test_needs_seal_is_quiet_for_a_prediction_with_no_kind"
  "workspace/seam/test_nightly_regate.py::test_needs_seal_is_quiet_for_a_scored_prediction"
  "workspace/seam/test_nightly_regate.py::test_needs_seal_is_quiet_for_a_time_prediction"
  "workspace/seam/test_nightly_regate.py::test_needs_seal_is_quiet_once_a_prereg_is_attached"
  "workspace/seam/test_nightly_regate.py::test_needs_seal_warns_at_29_days"
  "workspace/seam/test_nightly_regate.py::test_needs_seal_writes_nothing"
  "workspace/seam/test_nightly_regate.py::test_order41_the_nightlys_regate_refusals_go_to_a_temp_log"
  "workspace/seam/test_nightly_regate.py::test_registry_copy_reads_the_pinned_commit_never_the_live_file"
  "workspace/seam/test_nightly_regate.py::test_the_current_nightly_crashes_when_a_manual_metric_comes_due"
  "workspace/seam/test_nightly_regate.py::test_the_live_registry_and_its_ledger_are_never_touched"
  "workspace/seam/test_nightly_regate.py::test_the_maths_is_unchanged_from_the_before_commit"
  "workspace/seam/test_nightly_regate.py::test_the_nightly_scores_an_automatic_metric_through_regate"
  "workspace/seam/test_nightly_regate.py::test_the_optc_retry_is_byte_identical_to_the_before_commit"
  "workspace/seam/test_nightly_regate.py::test_the_other_lines_keep_their_place_and_shape"
  "workspace/seam/test_nightly_regate.py::test_the_real_provenance_line_on_a_temp_registry_reads_only"
  "workspace/seam/test_nightly_regate.py::test_the_registry_line_names_the_first_problem"
  "workspace/seam/test_nightly_regate.py::test_the_registry_line_repeats_regate_when_it_reports_no_narrative_count"
  "workspace/seam/test_nightly_regate.py::test_the_registry_line_reports_no_narratives_as_zero_when_regate_says_zero"
  "workspace/seam/test_nightly_regate.py::test_the_registry_line_reports_the_narrative_count"
  "workspace/seam/test_nightly_regate.py::test_the_registry_line_says_no_ledger_and_creates_none"
  "workspace/seam/test_nightly_regate.py::test_the_registry_line_says_ok_when_the_ledger_agrees"
  "workspace/seam/test_nightly_regate.py::test_the_rewired_nightly_reports_it_as_overdue_instead"
  "workspace/seam/test_nightly_regate.py::test_the_same_night_side_by_side"
  "workspace/seam/test_nightly_regate.py::test_underpowered_goes_through_regate_too"
  "workspace/seam/test_timeforward.py::test_an_automatic_metric_is_scored_by_the_real_regate_on_the_copy_only"
  "workspace/seam/test_timeforward.py::test_an_in_process_crash_in_the_nightly_is_caught_with_its_last_frame"
  "workspace/seam/test_timeforward.py::test_each_due_date_is_simulated_once_and_the_first_crash_is_named"
  "workspace/seam/test_timeforward.py::test_no_unscored_predictions_is_zero_dates"
  "workspace/seam/test_timeforward.py::test_one_prediction_due_2027_01_31_is_one_date_simulated_ok"
  "workspace/seam/test_timeforward.py::test_positive_control_a_regate_that_raises_keyerror_brier_on_that_date"
  "workspace/seam/test_timeforward.py::test_the_fake_regate_only_crashes_on_the_simulated_date"
  "workspace/seam/test_timeforward.py::test_the_nightly_constants_are_restored_even_after_a_crash"
  "workspace/seam/test_timeforward.py::test_the_simulated_regate_never_touches_the_live_refusals_logs"
  # needs pytest installed for the SYSTEM python3, not just Helix's own environment
  "build_orders/tools/test_ordertests.py::test_the_brain_root_needs_its_venv"
)
DESEL=(); for t in "${SKIP[@]}"; do DESEL+=(--deselect "$t"); done
pass=0; fail=0
for f in $(ls test_*.py workspace/*/test_*.py vault/test_*.py build_orders/tools/test_*.py studio/tests/test_*.py 2>/dev/null); do
  out="$(timeout 600 "$PY" -m pytest -q -p no:cacheprovider "${DESEL[@]}" "$f" 2>&1)"; last="$(printf '%s\n' "$out" | tail -1)"
  if printf '%s' "$last" | grep -q -E "no tests ran|INTERNALERROR" || printf '%s' "$out" | grep -q INTERNALERROR; then
    out="$(timeout 600 "$PY" "$f" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | grep -v '^\s*$' | tail -1)"
    [ $rc -eq 0 ] && ok=1 || ok=0
  else
    printf '%s' "$last" | grep -q -E "[0-9]+ (failed|errors?)" && ok=0 || ok=1
  fi
  if [ $ok = 1 ]; then pass=$((pass+1)); printf 'ok    %-55s %s\n' "$f" "$last"
  else fail=$((fail+1)); printf 'FAIL  %-55s %s\n' "$f" "$last"
       [ "$VERBOSE" = -v ] && printf '%s\n' "$out" | grep -E "^E |FAIL|Error" | head -5 | sed 's/^/        /'; fi
done
echo; echo "$pass files ok, $fail files failing"
[ $fail -eq 0 ]
