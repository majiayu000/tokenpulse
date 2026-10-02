import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from measurement import StreamMeasurement, fingerprint, summarize_stream


def trace(*, expected='甲乙abcdef', valid=None, completed=True, error=None,
          tokens=100, warmup=False, model='synthetic-model', route='synthetic-route'):
    # A nonzero epoch proves all times are request-relative, not wall-clock.
    with patch('measurement.time.perf_counter_ns', side_effect=[9_000_000_000,
               10_000_000_000, 11_000_000_000, 14_000_000_000, 15_000_000_000]):
        meter = StreamMeasurement(model, 'synthetic-case', fingerprint({'prompt': 'synthetic'}),
                                  expected_text=expected, warmup=warmup, route=route)
        meter.text('')
        meter.thinking('')
        meter.thinking('private synthetic thinking')
        meter.text('甲乙ab')
        meter.text('cdef')
        return meter.finish(tokens, thinking_tokens=20 if tokens is not None else None,
                            valid_output=valid, completed=completed, error=error,
                            stop_reason='synthetic-stop')


class StreamMetrics(unittest.TestCase):
    def test_boundaries_units_and_first_chunk_numerator(self):
        record = trace()
        sample = summarize_stream([record])['samples'][0]
        self.assertEqual(sample['end_to_end_seconds'], 6)
        self.assertEqual(sample['first_observable_output_seconds'], 1)
        self.assertEqual(sample['first_answer_seconds'], 2)
        self.assertEqual(sample['answer_span_seconds'], 3)
        self.assertEqual(sample['visible_characters'], 8)
        self.assertEqual(sample['first_chunk_characters'], 4)
        self.assertAlmostEqual(sample['end_to_end_native_tokens_per_second'], 100 / 6)
        self.assertAlmostEqual(sample['after_first_chunk_characters_per_second'], 4 / 3)
        self.assertIsNone(summarize_stream([record])['first_token_speed'])

    def test_empty_events_and_single_chunk(self):
        with patch('measurement.time.perf_counter_ns', side_effect=[0, 2_000_000_000, 5_000_000_000]):
            meter = StreamMeasurement('synthetic', 'case', fingerprint({}), expected_text='OK')
            meter.text('')
            meter.thinking('')
            meter.text('OK')
            record = meter.finish(2)
        sample = summarize_stream([record])['samples'][0]
        self.assertEqual(sample['first_observable_output_seconds'], 2)
        self.assertIsNone(sample['after_first_chunk_characters_per_second'])

    def test_no_visible_text_has_unknown_first_answer(self):
        with patch('measurement.time.perf_counter_ns', side_effect=[0, 1_000_000_000, 3_000_000_000]):
            meter = StreamMeasurement('synthetic', 'case', fingerprint({}))
            meter.thinking('observable thinking')
            record = meter.finish(10, valid_output=True)
        sample = summarize_stream([record])['samples'][0]
        self.assertEqual(sample['first_observable_output_seconds'], 1)
        self.assertIsNone(sample['first_answer_seconds'])

    def test_warmups_failures_mismatches_and_unverified_stay_visible(self):
        records = [trace(), trace(warmup=True), trace(expected='wrong'),
                   trace(expected=None), trace(completed=False),
                   trace(error=TimeoutError('SECRET_ERROR_MESSAGE'))]
        result = summarize_stream(records)
        group = result['groups'][0]
        self.assertEqual((group['attempts'], group['warmups'], group['completed'], group['failed']), (5, 1, 3, 2))
        self.assertEqual((group['invalid_outputs'], group['unverified_outputs'], group['samples']), (1, 1, 1))
        self.assertEqual(group['failure_rate'], 2 / 5)
        self.assertEqual(len(result['samples']), 6)
        self.assertNotIn('SECRET_ERROR_MESSAGE', json.dumps(result))

    def test_unicode_hash_is_independent_of_chunk_boundaries(self):
        with patch('measurement.time.perf_counter_ns', side_effect=[0, 1, 2, 3]):
            meter = StreamMeasurement('synthetic', 'case', fingerprint({}), expected_text='你🙂')
            meter.text('你')
            meter.text('🙂')
            record = meter.finish(2)
        self.assertTrue(record['valid_output'])
        self.assertEqual(record['visible_characters'], 2)

    def test_external_verifier_failure_is_not_overridden_by_hash_match(self):
        self.assertFalse(trace(valid=False)['valid_output'])

    def test_verification_after_completion_does_not_extend_latency(self):
        record = trace(expected=None)
        with patch('measurement.time.perf_counter_ns', side_effect=AssertionError('clock must already be frozen')):
            record['valid_output'] = True
            sample = summarize_stream([record])['samples'][0]
        self.assertEqual(sample['end_to_end_seconds'], 6)

    def test_missing_usage_does_not_turn_into_zero_tokens(self):
        result = summarize_stream([trace(), trace(tokens=None)])
        group = result['groups'][0]
        self.assertEqual((group['samples'], group['token_usage_samples']), (2, 1))
        self.assertEqual((group['known_output_tokens'], group['token_usage_elapsed_seconds'], group['elapsed_seconds']), (100, 6, 12))
        self.assertAlmostEqual(group['weighted_end_to_end_native_tokens_per_second'], 100 / 6)

    def test_rates_are_weighted_by_the_matching_time_intervals(self):
        first, second = trace(), trace()
        second['elapsed_ns'] = 12_000_000_000
        second['output_tokens'] = 200
        second['last_text_ns'] = 8_000_000_000
        second['answer_chunk_gaps_ns'] = [6_000_000_000]
        group = summarize_stream([first, second])['groups'][0]
        self.assertAlmostEqual(group['weighted_end_to_end_native_tokens_per_second'], 300 / 18)
        self.assertAlmostEqual(group['weighted_after_first_chunk_characters_per_second'], 8 / 9)
        self.assertEqual(group['p95_end_to_end_seconds'], 12)
        self.assertEqual(group['p95_answer_chunk_gap_seconds'], 6)

    def test_different_workloads_and_conditions_do_not_pool(self):
        records = [trace(), trace(model='other-model'), trace(route='other-route')]
        other = trace()
        other['request_fingerprint'] = fingerprint({'prompt': 'other'})
        records.append(other)
        self.assertEqual(len(summarize_stream(records)['groups']), 4)

    def test_duplicate_attempts_and_conflicts(self):
        record = trace()
        self.assertEqual(summarize_stream([record, copy.deepcopy(record)])['duplicate_attempts'], 1)
        broken = dict(record, output_tokens=200)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            summarize_stream([record, broken])

    def test_impossible_timings_counts_hash_and_clock_fail(self):
        record = trace()
        changes = [{'first_text_ns': 7_000_000_000}, {'elapsed_ns': float('nan')},
                   {'output_tokens': True}, {'thinking_tokens': 101},
                   {'input_tokens': 2, 'cached_input_tokens': 3},
                   {'answer_chunk_gaps_ns': [-1]}, {'clock': 'wall-clock'},
                   {'expected_output_sha256': 'different-hash'},
                   {'completed': 'yes'}, {'error_type': 'TimeoutError'},
                   {'started_at': '2026-01-01T00:00:00'}, {'visible_characters': 1}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                summarize_stream([dict(record, **change)])

    def test_no_text_and_zero_duration_are_not_divided_by_zero(self):
        with patch('measurement.time.perf_counter_ns', return_value=0):
            meter = StreamMeasurement('synthetic', 'case', fingerprint({}), expected_text='')
            record = meter.finish(0)
        group = summarize_stream([record])['groups'][0]
        self.assertEqual(group['zero_duration_samples'], 1)
        self.assertIsNone(group['weighted_end_to_end_native_tokens_per_second'])

    def test_same_timestamp_chunks_have_unknown_post_first_speed(self):
        with patch('measurement.time.perf_counter_ns', side_effect=[0, 1, 1, 2]):
            meter = StreamMeasurement('synthetic', 'case', fingerprint({}), expected_text='ab')
            meter.text('a')
            meter.text('b')
            record = meter.finish(2)
        self.assertIsNone(summarize_stream([record])['samples'][0]['after_first_chunk_characters_per_second'])

    def test_finished_measurement_cannot_be_reused(self):
        meter = StreamMeasurement('synthetic', 'case', fingerprint({}))
        meter.finish()
        with self.assertRaises(ValueError):
            meter.finish()
        with self.assertRaises(ValueError):
            meter.text('new text')

    def test_report_does_not_echo_extra_private_fields(self):
        record = trace()
        record['prompt'] = 'PRIVATE_PROMPT'
        record['thinking'] = 'PRIVATE_THINKING'
        encoded = json.dumps(summarize_stream([record]))
        for secret in ('PRIVATE_PROMPT', 'PRIVATE_THINKING', 'private synthetic thinking', '甲乙abcdef'):
            self.assertNotIn(secret, encoded)

    def test_fingerprint_is_deterministic_and_rejects_nonfinite_values(self):
        self.assertEqual(fingerprint({'b': 2, 'a': 1}), fingerprint({'a': 1, 'b': 2}))
        with self.assertRaises(ValueError):
            fingerprint({'temperature': float('nan')})

    def test_cli_round_trip_and_error_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'attempts.jsonl', root / 'report.json'
            command = [sys.executable, str(Path(__file__).with_name('speed.py')),
                       '--stream', str(source), '--output', str(output)]
            source.write_text(json.dumps(trace()) + '\n')
            process = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(output.read_text())['groups'][0]['samples'], 1)
            source.write_text(json.dumps(trace(completed=False)) + '\n')
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)
            before = output.read_bytes()
            source.write_text(json.dumps(dict(trace(), thinking_tokens=101)) + '\n')
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual(output.read_bytes(), before)
            process = subprocess.run(command + ['--codex', str(source)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 2)
            self.assertIn('cannot be mixed', process.stderr)


if __name__ == '__main__':
    unittest.main()
