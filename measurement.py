"""Per-request stream measurement for an existing benchmark. No model calls."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import time
import uuid


COHORT_FIELDS = ('model', 'workload_id', 'request_fingerprint', 'effort',
                 'service_tier', 'client_version', 'route')


def fingerprint(request):
    """Hash canonical, credential-free benchmark input and generation settings."""
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


class StreamMeasurement:
    """Create immediately before a single request; feed unique decoded deltas."""

    def __init__(self, model, workload_id, request_fingerprint, *, effort=None,
                 service_tier=None, client_version=None, route=None,
                 expected_text=None, warmup=False):
        self.record = dict(model=model, workload_id=workload_id,
                           request_fingerprint=request_fingerprint, effort=effort,
                           service_tier=service_tier, client_version=client_version,
                           route=route, warmup=warmup, attempt_id=uuid.uuid4().hex,
                           clock='perf_counter_ns', first_output_ns=None,
                           first_text_ns=None, last_text_ns=None, text_chunks=0,
                           visible_characters=0, first_chunk_characters=0,
                           answer_chunk_gaps_ns=[], expected_output_sha256=None)
        if expected_text is not None:
            self.record['expected_output_sha256'] = hashlib.sha256(expected_text.encode('utf-8')).hexdigest()
        self._output_hash = hashlib.sha256()
        self._closed = False
        self.record['started_at'] = datetime.now(timezone.utc).isoformat()
        self._start = time.perf_counter_ns()

    def _elapsed(self):
        if self._closed:
            raise ValueError('measurement is already finished')
        return time.perf_counter_ns() - self._start

    def text(self, delta):
        if not delta:
            return
        at = self._elapsed()
        if self.record['first_output_ns'] is None:
            self.record['first_output_ns'] = at
        if self.record['first_text_ns'] is None:
            self.record['first_text_ns'] = at
            self.record['first_chunk_characters'] = len(delta)
        else:
            self.record['answer_chunk_gaps_ns'].append(at - self.record['last_text_ns'])
        self.record['last_text_ns'] = at
        self.record['text_chunks'] += 1
        self.record['visible_characters'] += len(delta)
        self._output_hash.update(delta.encode('utf-8'))

    def thinking(self, delta):
        """Nonempty observable thinking only; its text is never retained."""
        if delta:
            at = self._elapsed()
            if self.record['first_output_ns'] is None:
                self.record['first_output_ns'] = at

    def finish(self, output_tokens=None, *, thinking_tokens=None, input_tokens=None,
               cached_input_tokens=None, completed=True, valid_output=None,
               stop_reason=None, error=None):
        elapsed = self._elapsed()
        self._closed = True
        actual_hash = self._output_hash.hexdigest()
        expected_hash = self.record['expected_output_sha256']
        if expected_hash is not None:
            matches = actual_hash == expected_hash
            valid_output = matches if valid_output is None else matches and valid_output
        return dict(self.record, elapsed_ns=elapsed, output_sha256=actual_hash,
                    output_tokens=output_tokens, thinking_tokens=thinking_tokens,
                    input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
                    completed=completed and error is None, valid_output=valid_output,
                    stop_reason=stop_reason,
                    error_type=type(error).__name__ if error is not None else None)


def percentile(values, quantile):
    """Empirical nearest-rank percentile; not a confidence interval."""
    return sorted(values)[math.ceil(len(values) * quantile) - 1] if values else None


def _metrics(record):
    """Validate imported measurement data once, then expose only known fields."""
    if record['clock'] != 'perf_counter_ns':
        raise ValueError('stream timing requires perf_counter_ns')
    for key in ('attempt_id', 'model', 'workload_id', 'request_fingerprint'):
        if not isinstance(record[key], str) or not record[key]:
            raise ValueError(f'stream measurement requires {key}')
    for key in ('completed', 'warmup'):
        if type(record[key]) is not bool:
            raise ValueError(f'{key} must be boolean')
    if record['valid_output'] is not None and type(record['valid_output']) is not bool:
        raise ValueError('valid_output must be boolean or null')
    try:
        started_at = datetime.fromisoformat(record['started_at'].replace('Z', '+00:00'))
        if started_at.tzinfo is None:
            raise ValueError('missing timezone')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('started_at must be an ISO timestamp with timezone') from None
    counts = ('elapsed_ns', 'text_chunks', 'visible_characters', 'first_chunk_characters')
    nullable = ('first_output_ns', 'first_text_ns', 'last_text_ns',
                'output_tokens', 'thinking_tokens', 'input_tokens', 'cached_input_tokens')
    for key in (*counts, *nullable):
        value = record[key]
        if value is None and key in nullable:
            continue
        if type(value) is not int or value < 0:
            raise ValueError(f'{key} must be a nonnegative integer or an allowed null')
    elapsed, first, last = record['elapsed_ns'], record['first_text_ns'], record['last_text_ns']
    observed = record['first_output_ns']
    if observed is not None and observed > elapsed:
        raise ValueError('first output cannot follow completion')
    if record['text_chunks']:
        if first is None or last is None or observed is None or not observed <= first <= last <= elapsed:
            raise ValueError('inconsistent stream timing boundaries')
        if not 0 < record['first_chunk_characters'] <= record['visible_characters']:
            raise ValueError('inconsistent first-chunk character count')
        if record['visible_characters'] < record['text_chunks'] or (record['text_chunks'] == 1 and record['visible_characters'] != record['first_chunk_characters']):
            raise ValueError('inconsistent text chunk character counts')
    elif first is not None or last is not None or record['visible_characters'] or record['first_chunk_characters']:
        raise ValueError('text counts require observed text chunks')
    gaps = record['answer_chunk_gaps_ns']
    if len(gaps) != max(0, record['text_chunks'] - 1) or any(type(g) is not int or g < 0 for g in gaps):
        raise ValueError('inconsistent answer chunk gaps')
    if first is not None and sum(gaps) != last - first:
        raise ValueError('chunk gaps do not match answer span')
    output, thinking = record['output_tokens'], record['thinking_tokens']
    if output is not None and thinking is not None and thinking > output:
        raise ValueError('thinking tokens cannot exceed output tokens')
    inputs, cached = record['input_tokens'], record['cached_input_tokens']
    if inputs is not None and cached is not None and cached > inputs:
        raise ValueError('cached input cannot exceed total input tokens')
    expected = record['expected_output_sha256']
    if expected is not None and record['valid_output'] is True and expected != record['output_sha256']:
        raise ValueError('verified output does not match expected hash')
    if record['completed'] and record['error_type'] is not None:
        raise ValueError('a failed attempt cannot be completed')
    public = {key: record.get(key) for key in (*COHORT_FIELDS, 'attempt_id', 'warmup',
              'completed', 'valid_output', 'stop_reason', 'error_type', *counts, *nullable,
              'output_sha256', 'expected_output_sha256', 'answer_chunk_gaps_ns')}
    public['started_at'] = started_at.astimezone(timezone.utc).isoformat()
    seconds = elapsed / 1e9
    span = (last - first) / 1e9 if first is not None else None
    after_first = record['visible_characters'] - record['first_chunk_characters']
    public.update(end_to_end_seconds=seconds,
                  first_observable_output_seconds=observed / 1e9 if observed is not None else None,
                  first_answer_seconds=first / 1e9 if first is not None else None,
                  answer_span_seconds=span,
                  end_to_end_native_tokens_per_second=output / seconds if output is not None and seconds > 0 else None,
                  end_to_end_characters_per_second=record['visible_characters'] / seconds if seconds > 0 else None,
                  after_first_chunk_characters_per_second=after_first / span if span and span > 0 else None)
    return public


def summarize_stream(records):
    seen, groups, samples = {}, defaultdict(list), []
    duplicates = 0
    for record in records:
        sample = _metrics(record)
        attempt_id = sample['attempt_id']
        if attempt_id in seen:
            if seen[attempt_id] != sample:
                raise ValueError('conflicting records for the same attempt_id')
            duplicates += 1
            continue
        seen[attempt_id] = sample
        samples.append(sample)
        groups[tuple(sample.get(key) for key in COHORT_FIELDS)].append(sample)
    summaries = []
    for cohort, group in sorted(groups.items(), key=lambda item: str(item[0])):
        attempts = [s for s in group if not s['warmup']]
        completed = [s for s in attempts if s['completed']]
        valid = [s for s in completed if s['valid_output'] is True]
        usable = [s for s in valid if s['elapsed_ns'] > 0]
        with_usage = [s for s in usable if s['output_tokens'] is not None]
        spans = [s for s in usable if s['answer_span_seconds'] and s['answer_span_seconds'] > 0]
        latencies = [s['end_to_end_seconds'] for s in usable]
        first_answers = [s['first_answer_seconds'] for s in usable if s['first_answer_seconds'] is not None]
        gaps = [g / 1e9 for s in usable for g in s['answer_chunk_gaps_ns']]
        seconds = sum(latencies)
        usage_seconds = sum(s['end_to_end_seconds'] for s in with_usage)
        span_seconds = sum(s['answer_span_seconds'] for s in spans)
        tokens = sum(s['output_tokens'] for s in with_usage) if with_usage else None
        characters = sum(s['visible_characters'] for s in usable)
        after_first_characters = sum(s['visible_characters'] - s['first_chunk_characters'] for s in spans)
        summaries.append(dict(zip(COHORT_FIELDS, cohort), attempts=len(attempts),
            first_request_at=min(s['started_at'] for s in group),
            last_request_at=max(s['started_at'] for s in group),
            warmups=len(group) - len(attempts), completed=len(completed),
            failed=len(attempts) - len(completed),
            failure_rate=(len(attempts) - len(completed)) / len(attempts) if attempts else None,
            invalid_outputs=sum(s['valid_output'] is False for s in completed),
            unverified_outputs=sum(s['valid_output'] is None for s in completed),
            valid_completions=len(valid), samples=len(usable), token_usage_samples=len(with_usage),
            zero_duration_samples=len(valid) - len(usable),
            elapsed_seconds=seconds,
            known_output_tokens=tokens, token_usage_elapsed_seconds=usage_seconds,
            visible_characters=characters, after_first_chunk_characters=after_first_characters,
            answer_span_seconds=span_seconds,
            median_end_to_end_seconds=statistics.median(latencies) if latencies else None,
            p95_end_to_end_seconds=percentile(latencies, .95),
            first_answer_samples=len(first_answers),
            median_first_answer_seconds=statistics.median(first_answers) if first_answers else None,
            p95_first_answer_seconds=percentile(first_answers, .95),
            weighted_end_to_end_native_tokens_per_second=tokens / usage_seconds if usage_seconds else None,
            weighted_end_to_end_characters_per_second=characters / seconds if seconds else None,
            after_first_chunk_samples=len(spans),
            weighted_after_first_chunk_characters_per_second=after_first_characters / span_seconds if span_seconds else None,
            answer_chunk_gap_samples=len(gaps), p95_answer_chunk_gap_seconds=percentile(gaps, .95)))
    return {'measurement_kind': 'client-stream', 'clock': 'perf_counter_ns',
            'method': 'request start to stream completion with final usage; verified completed outputs only in speed summaries; warmups separate',
            'interpretation': 'client-observed per-request performance, not decoder speed, system throughput or an automatic model ranking',
            'first_token_speed': None, 'duplicate_attempts': duplicates,
            'percentile_method': 'empirical nearest rank; descriptive, not a confidence interval',
            'groups': summaries, 'samples': samples}
