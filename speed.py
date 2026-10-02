"""Read-only retrospective throughput analysis. Never sends model requests."""

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics


@dataclass
class Sample:
    source: str
    model: str
    effort: str | None
    service_tier: str | None
    client_version: str | None
    started_at: str
    completed_at: str
    seconds: float
    output_tokens: int
    thinking_tokens: int | None
    visible_characters: int
    input_tokens: int
    cached_input_tokens: int
    has_tools: bool
    completed: bool
    response_ids: tuple[str, ...]


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamps must include a timezone')
    return parsed


def rows(path):
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f'invalid JSONL at line {number} of {path.name}') from None


def files(root):
    if not root.exists():
        raise ValueError(f'input does not exist: {root}')
    return [root] if root.is_file() else sorted(root.rglob('*.jsonl'))


def text_length(content):
    return sum(len(block.get('text', '')) for block in content
               if isinstance(block, dict) and block.get('type') in ('text', 'output_text'))


def make_sample(source, context, start, end, responses, has_tools, completed):
    if start is None or end is None or not responses:
        return None
    values = list(responses.values())
    models = {v['model'] for v in values}
    if len(models) != 1 or None in models:
        return None
    thinking = [v['thinking'] for v in values]
    return Sample(source, models.pop(), context.get('effort'), context.get('tier'),
                  context.get('version'), start.isoformat(), end.isoformat(),
                  (end - start).total_seconds(), sum(v['tokens'] for v in values),
                  sum(thinking) if all(v is not None for v in thinking) else None,
                  sum(v['characters'] for v in values), sum(v['input'] for v in values),
                  sum(v['cached'] for v in values), has_tools, completed,
                  tuple(sorted(responses)))


def read_codex(path):
    samples, context = [], {}
    start = end = None
    responses, texts = {}, []
    active = has_tools = False
    for row in rows(path):
        payload = row.get('payload', {})
        kind = row.get('type')
        event = payload.get('type')
        if kind == 'session_meta':
            source = payload.get('source')
            if isinstance(source, dict) and 'subagent' in source:
                return []
            context['version'] = payload.get('cli_version')
        elif kind == 'turn_context':
            context.update(model=payload.get('model'), effort=payload.get('effort'),
                           tier=payload.get('service_tier'))
        elif kind == 'event_msg' and event == 'task_started':
            start = end = None
            responses, texts = {}, []
            active, has_tools = True, False
        elif active and kind == 'event_msg' and event == 'item_completed':
            if payload.get('item', {}).get('type') == 'UserMessage':
                start = timestamp(row['timestamp'])
        elif active and kind == 'response_item':
            if event in ('function_call', 'custom_tool_call', 'web_search_call'):
                has_tools = True
            elif event == 'message' and payload.get('role') == 'assistant':
                end = timestamp(row['timestamp'])
                texts.append(text_length(payload.get('content', [])))
        elif active and kind == 'token_usage_record':
            response_id = payload.get('response_id')
            if not response_id:
                raise ValueError('Codex usage has no response_id; cannot deduplicate safely')
            usage = payload['usage']
            responses[response_id] = {'model': context.get('model'),
                'tokens': usage['output_tokens'], 'thinking': usage.get('reasoning_output_tokens'),
                'characters': 0, 'input': usage.get('input_tokens', 0),
                'cached': usage.get('cached_input_tokens', 0)}
        elif active and kind == 'event_msg' and event == 'task_complete':
            if responses:
                next(iter(responses.values()))['characters'] = sum(texts)
            sample = make_sample('codex', context, start, end, responses, has_tools, True)
            if sample:
                samples.append(sample)
            active = False
    return samples


def read_claude(path):
    samples, turn = [], None

    def finish():
        if not turn or not turn['responses']:
            return
        last = max(turn['responses'].values(), key=lambda value: value['end'])
        sample = make_sample('claude-desktop', turn['context'], turn['start'], last['end'],
                             turn['responses'], turn['tools'], last['stop'] == 'end_turn')
        if sample:
            samples.append(sample)

    for row in rows(path):
        if row.get('isSidechain'):
            continue
        message = row.get('message') or {}
        origin = (row.get('origin') or {}).get('kind')
        if row.get('type') == 'user' and origin:
            finish()
            turn = None
            if origin == 'human':
                turn = {'start': timestamp(row['timestamp']), 'context': {'version': row.get('version')},
                        'responses': {}, 'tools': False, 'blocks': set()}
        elif turn and row.get('type') == 'assistant':
            mid, model = message.get('id'), message.get('model')
            if not mid or model == '<synthetic>':
                continue
            usage = message.get('usage') or {}
            if 'output_tokens' not in usage:
                continue
            end = timestamp(row['timestamp'])
            value = turn['responses'].setdefault(mid, {'model': model, 'tokens': 0,
                'thinking': None, 'characters': 0, 'input': 0, 'cached': 0, 'end': end, 'stop': None})
            value['tokens'] = max(value['tokens'], usage['output_tokens'])
            thinking = (usage.get('output_tokens_details') or {}).get('thinking_tokens')
            if thinking is not None:
                value['thinking'] = max(value['thinking'] or 0, thinking)
            value['input'] = usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
            value['cached'] = usage.get('cache_read_input_tokens', 0)
            value['end'], value['stop'] = max(value['end'], end), message.get('stop_reason') or value['stop']
            blocks = message.get('content') or []
            block_key = (mid, row.get('apiBlockIndex', row.get('uuid')))
            if block_key[1] is None:
                raise ValueError('Claude content has no block index or UUID; cannot deduplicate safely')
            if block_key not in turn['blocks']:
                value['characters'] += text_length(blocks)
                turn['blocks'].add(block_key)
            turn['tools'] |= any(isinstance(b, dict) and b.get('type') in ('tool_use', 'server_tool_use') for b in blocks)
            turn['tools'] |= any(isinstance(v, (int, float)) and v > 0 for v in (usage.get('server_tool_use') or {}).values())
            effort = row.get('perTurnEffort') or row.get('effort')
            turn['context'].update(effort=effort if isinstance(effort, str) else None,
                                   tier=usage.get('service_tier'))
        elif turn and row.get('type') == 'system' and row.get('subtype') == 'stop_hook_summary':
            finish()
            turn = None
    finish()
    return samples


def report(samples, since=None):
    accepted, skipped, seen = [], Counter(), set()
    for sample in samples:
        if not sample.completed:
            skipped['unfinished'] += 1
        elif sample.has_tools:
            skipped['tool_turns'] += 1
        elif sample.seconds <= 0:
            skipped['nonpositive_duration'] += 1
        elif since and timestamp(sample.started_at) < since:
            skipped['before_since'] += 1
        elif (sample.source, sample.response_ids) in seen:
            skipped['duplicate_responses'] += 1
        else:
            if sample.output_tokens < 0 or (sample.thinking_tokens is not None and not 0 <= sample.thinking_tokens <= sample.output_tokens):
                raise ValueError('inconsistent output/thinking token counts')
            seen.add((sample.source, sample.response_ids))
            accepted.append(sample)
    groups = defaultdict(list)
    for sample in accepted:
        groups[(sample.source, sample.model, sample.effort, sample.service_tier, sample.client_version)].append(sample)
    summaries = []
    for (source, model, effort, tier, version), group in sorted(groups.items(), key=lambda item: str(item[0])):
        seconds = sum(s.seconds for s in group)
        tokens = sum(s.output_tokens for s in group)
        characters = sum(s.visible_characters for s in group)
        summaries.append({'source': source, 'model': model, 'effort': effort,
            'service_tier': tier, 'client_version': version, 'samples': len(group),
            'output_tokens': tokens, 'seconds': seconds, 'visible_characters': characters,
            'weighted_native_tokens_per_second': tokens / seconds,
            'weighted_visible_characters_per_second': characters / seconds,
            'median_elapsed_seconds': statistics.median(s.seconds for s in group),
            'min_elapsed_seconds': min(s.seconds for s in group),
            'max_elapsed_seconds': max(s.seconds for s in group),
            'median_input_tokens': statistics.median(s.input_tokens for s in group)})
    sanitized = []
    for sample in accepted:
        record = asdict(sample)
        record.pop('response_ids')
        record['sample_id'] = hashlib.sha256('\n'.join(sample.response_ids).encode()).hexdigest()[:16]
        sanitized.append(record)
    return {'method': 'input-message log timestamp to final assistant-message log timestamp; completed human turns without tools; includes thinking and waiting; excludes pre-input setup and post-output hooks',
            'interpretation': 'retrospective measurements, not a controlled model ranking; native tokenizers, prompts, contexts and effort settings differ',
            'first_token_speed': None, 'skipped': dict(skipped), 'groups': summaries, 'samples': sanitized}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', type=Path, help='Codex rollout file or sessions directory')
    parser.add_argument('--claude', type=Path, help='Claude projects directory or transcript file')
    parser.add_argument('--desktop', type=Path, help='Desktop claude-code-sessions metadata directory')
    parser.add_argument('--since', help='ISO timestamp with timezone; filters input timestamps')
    parser.add_argument('--output', type=Path, default=Path('reports/summary.json'))
    args = parser.parse_args()
    try:
        if not args.codex and not args.claude:
            raise ValueError('provide --codex or --claude')
        if args.claude and not args.desktop:
            raise ValueError('--claude requires --desktop to identify Desktop sessions')
        since = timestamp(args.since) if args.since else None
        samples = []
        if args.codex:
            for path in files(args.codex):
                samples.extend(read_codex(path))
        if args.claude:
            if not args.desktop.is_dir():
                raise ValueError('--desktop must be an existing metadata directory')
            metadata = {}
            for path in args.desktop.rglob('*.json'):
                data = json.loads(path.read_text())
                if data.get('cliSessionId'):
                    metadata[data['cliSessionId']] = data
            for path in files(args.claude):
                if path.stem in metadata and 'subagents' not in path.parts:
                    samples.extend(read_claude(path))
        result = report(samples, since)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'groups': result['groups'], 'skipped': result['skipped']}, ensure_ascii=False, indent=2))
        return 0 if result['groups'] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f'analysis failed: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
