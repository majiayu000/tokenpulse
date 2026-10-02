"""Synthetic measurement only. No network, model calls, or real speed result."""

import json
from unittest.mock import patch

from measurement import StreamMeasurement, fingerprint


def main():
    with patch('measurement.time.perf_counter_ns', side_effect=[0, 1_000_000_000,
               2_000_000_000, 5_000_000_000, 6_000_000_000]):
        meter = StreamMeasurement('synthetic-model', 'synthetic-demo',
                                  fingerprint({'prompt': 'synthetic prompt', 'max_output_tokens': 100}),
                                  expected_text='甲乙abcdef', route='synthetic')
        meter.thinking('synthetic visible thinking')
        meter.text('甲乙ab')
        meter.text('cdef')
        record = meter.finish(100, thinking_tokens=20, stop_reason='synthetic-stop')
    print(json.dumps(record, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
