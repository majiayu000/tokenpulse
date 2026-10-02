# Contributing

Keep changes focused on reproducible throughput analysis. Explain any accounting or timing changes with evidence from the relevant schema. Use synthetic fixtures; never commit real conversations, credentials or private configuration.

## Clone, build and test

Python 3.10+ is required. There are no dependencies to install.

```bash
git clone https://github.com/majiayu000/tokenpulse.git
cd tokenpulse
python3 -m compileall -q speed.py measurement.py test_speed.py test_measurement.py examples
python3 -m unittest -v
```

Add focused tests when changing parsing, timing or token accounting. Document unsupported or unobservable fields rather than estimating them silently. Preserve the CLI exit-code contract.

Keep the stream component provider-independent; existing benchmark runners own model calls and scoring. See the [measurement contract](docs/speed-methodology.md). Check integration with `python3 -m examples.stream_demo`; it uses synthetic timing and makes no requests.

## Pull requests

Describe the problem, the measurement implications and checks that actually finished. Sign off each commit with `git commit -s` to certify the [Developer Certificate of Origin](https://developercertificate.org/). Use the [code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately through [Security Advisories](https://github.com/majiayu000/tokenpulse/security/advisories/new).
