import json
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import speed


def stamp(second):
    return f'2026-01-01T00:00:{second:02d}+00:00'


def codex_row(kind, second, **payload):
    return {'type': kind, 'timestamp': stamp(second), 'payload': payload}


def human(second, origin='human'):
    return {'type': 'user', 'timestamp': stamp(second), 'origin': {'kind': origin},
            'version': 'synthetic-1', 'message': {'role': 'user', 'content': 'synthetic input'}}


def assistant(second, block_type='text', index=0, tokens=100, stop='end_turn', mid='synthetic-message'):
    return {'type': 'assistant', 'timestamp': stamp(second), 'apiBlockIndex': index,
            'uuid': f'synthetic-block-{index}', 'effort': 'medium',
            'message': {'id': mid, 'model': 'synthetic-model', 'stop_reason': stop,
                        'content': [{'type': block_type, 'text': '答案abc'}],
                        'usage': {'output_tokens': tokens,
                                  'output_tokens_details': {'thinking_tokens': 20},
                                  'service_tier': 'standard', 'input_tokens': 2,
                                  'cache_creation_input_tokens': 10, 'cache_read_input_tokens': 8}}}


class Measurements(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.jsonl'

    def write(self, records):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in records))
        return self.path

    def codex(self, completed=True):
        records = [codex_row('session_meta', 0, cli_version='synthetic-1'),
                   codex_row('event_msg', 0, type='task_started'),
                   codex_row('turn_context', 1, model='synthetic-model', effort='high'),
                   codex_row('event_msg', 2, type='item_completed', item={'type': 'UserMessage'}),
                   codex_row('response_item', 6, type='message', role='assistant',
                             content=[{'type': 'output_text', 'text': '答案abc'}]),
                   codex_row('token_usage_record', 6, response_id='synthetic-response',
                             usage={'output_tokens': 100, 'reasoning_output_tokens': 20}),
                   codex_row('token_usage_record', 7, response_id='synthetic-response',
                             usage={'output_tokens': 110, 'reasoning_output_tokens': 20})]
        if completed:
            records.append(codex_row('event_msg', 9, type='task_complete', duration_ms=9000))
        return records

    def test_codex_deduplication_and_message_boundaries(self):
        sample = speed.read_codex(self.write(self.codex()))[0]
        self.assertEqual((sample.seconds, sample.output_tokens, sample.thinking_tokens), (4, 110, 20))
        self.assertEqual(sample.visible_characters, 5)

    def test_codex_unfinished_turn_is_not_counted(self):
        samples = speed.read_codex(self.write(self.codex(False)))
        self.assertEqual(speed.report(samples)['skipped'], {'unfinished': 1})

    def test_codex_abort_cannot_be_marked_complete_later(self):
        records = self.codex(False)
        records.extend([codex_row('event_msg', 8, type='turn_aborted'),
                        codex_row('event_msg', 9, type='task_complete')])
        self.assertEqual(speed.report(speed.read_codex(self.write(records)))['skipped'], {'unfinished': 1})

    def test_codex_subagent_session_is_excluded(self):
        records = self.codex()
        records[0]['payload']['source'] = {'subagent': {'thread_spawn': {}}}
        self.assertEqual(speed.read_codex(self.write(records)), [])

    def test_claude_cumulative_usage_and_blocks(self):
        text = assistant(5, index=1)
        records = [human(0), assistant(2, 'thinking', 0, tokens=40, stop=None), text, text,
                   {'type': 'system', 'subtype': 'stop_hook_summary', 'timestamp': stamp(50)}]
        sample = speed.read_claude(self.write(records))[0]
        self.assertEqual((sample.seconds, sample.output_tokens, sample.thinking_tokens), (5, 100, 20))
        self.assertEqual((sample.visible_characters, sample.input_tokens), (5, 20))

    def test_replayed_claude_block_does_not_extend_elapsed_time(self):
        records = [human(0), assistant(5), assistant(20)]
        sample = speed.read_claude(self.write(records))[0]
        self.assertEqual((sample.seconds, sample.visible_characters), (5, 5))
        records[-1]['message']['content'][0]['text'] = 'conflicting text'
        with self.assertRaisesRegex(ValueError, 'conflicting content'):
            speed.read_claude(self.write(records))

    def test_single_claude_block_has_a_valid_duration(self):
        sample = speed.read_claude(self.write([human(0), assistant(5)]))[0]
        result = speed.report([sample])
        self.assertEqual(result['groups'][0]['weighted_native_tokens_per_second'], 20)
        self.assertIsNone(result['first_token_speed'])

    def test_tools_and_interrupted_turns_are_excluded(self):
        records = [human(0), assistant(3, 'tool_use', tokens=30, stop='tool_use'),
                   assistant(5, index=1)]
        sample = speed.read_claude(self.write(records))[0]
        self.assertEqual(speed.report([sample])['skipped'], {'tool_turns': 1})
        partial = speed.read_claude(self.write([human(0), assistant(5, stop='tool_use')]))
        self.assertEqual(speed.report(partial)['skipped'], {'unfinished': 1})

    def test_server_tools_are_not_mistaken_for_plain_text(self):
        record = assistant(5)
        record['message']['usage']['server_tool_use'] = {'web_search_requests': 1}
        sample = speed.read_claude(self.write([human(0), record]))[0]
        self.assertTrue(sample.has_tools)

    def test_automatic_notification_does_not_extend_human_turn(self):
        records = [human(0), assistant(5), human(20, 'task-notification'),
                   assistant(30, mid='automatic-response')]
        samples = speed.read_claude(self.write(records))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].seconds, 5)

    def test_weighted_rate_and_privacy(self):
        first = speed.read_claude(self.write([human(0), assistant(5)]))[0]
        second = speed.read_claude(self.write([human(0), assistant(10, mid='second-response')]))[0]
        result = speed.report([first, first, second])
        self.assertAlmostEqual(result['groups'][0]['weighted_native_tokens_per_second'], 200 / 15)
        self.assertEqual(result['skipped'], {'duplicate_responses': 1})
        serialized = json.dumps(result)
        self.assertNotIn('synthetic-message', serialized)
        self.assertNotIn('synthetic input', serialized)
        self.assertNotIn('答案abc', serialized)
        self.assertNotIn('response_ids', serialized)

    def test_missing_input_usage_is_unknown(self):
        sample = speed.read_codex(self.write(self.codex()))[0]
        self.assertIsNone(sample.input_tokens)
        self.assertIsNone(sample.cached_input_tokens)
        group = speed.report([sample])['groups'][0]
        self.assertIsNone(group['median_input_tokens'])
        self.assertEqual(group['input_tokens_known_samples'], 0)
        row = assistant(5)
        del row['message']['usage']['cache_read_input_tokens']
        claude = speed.read_claude(self.write([human(0), row]))[0]
        self.assertIsNone(claude.input_tokens)
        self.assertIsNone(claude.cached_input_tokens)

    def test_conflicting_duplicate_is_an_error(self):
        sample = speed.read_codex(self.write(self.codex()))[0]
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            speed.report([sample, replace(sample, output_tokens=111)])

    def test_impossible_or_fractional_usage_fails(self):
        sample = speed.read_codex(self.write(self.codex()))[0]
        for broken in (replace(sample, output_tokens=True),
                       replace(sample, thinking_tokens=111),
                       replace(sample, input_tokens=2, cached_input_tokens=3)):
            with self.assertRaises(ValueError):
                speed.report([broken])

    def test_timezone_and_zero_duration(self):
        with self.assertRaises(ValueError):
            speed.timestamp('2026-01-01T00:00:00')
        sample = speed.read_claude(self.write([human(5), assistant(5)]))[0]
        self.assertEqual(speed.report([sample])['skipped'], {'nonpositive_duration': 1})

    def test_malformed_json_fails_without_partial_report(self):
        self.path.write_text('{invalid\n')
        with self.assertRaisesRegex(ValueError, 'line 1'):
            speed.read_codex(self.path)
        output = Path(self.temp.name) / 'report.json'
        process = subprocess.run([sys.executable, str(Path(speed.__file__)), '--codex',
                                  str(self.path), '--output', str(output)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertFalse(output.exists())

    def test_cli_refuses_to_overwrite_a_source(self):
        self.write(self.codex())
        before = self.path.read_bytes()
        alias = Path(self.temp.name) / 'alias.jsonl'
        alias.symlink_to(self.path)
        process = subprocess.run([sys.executable, str(Path(speed.__file__)), '--codex',
                                  str(self.path), '--output', str(alias)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertIn('overwrite', process.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_report_replacement_preserves_previous_file(self):
        output = Path(self.temp.name) / 'report.json'
        output.write_text('previous report')
        with patch.object(Path, 'replace', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                speed.write_report(output, {'groups': []})
        self.assertEqual(output.read_text(), 'previous report')
        self.assertEqual(list(Path(self.temp.name).glob('.tokenpulse-*')), [])


if __name__ == '__main__':
    unittest.main()
