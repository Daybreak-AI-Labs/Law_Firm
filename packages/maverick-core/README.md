# maverick-core

The Maverick law-firm kernel: matter-bound orchestration, a durable world model,
hard budget and egress caps, attorney release gates, and governed local
learning.

See the [top-level README](../../README.md) and the retained
[architecture](../../docs/architecture.md) for the full picture.

Do not install this package alone or from a public index. Install the complete
five-package cohort from one reviewed private checkout:

```bash
python scripts/install_release_cohort.py --source-root . \
  --target-python python --core-extra release-runtime
```

Install the five-package release cohort from one reviewed checkout for the
dashboard, Shield, knowledge, and installer surfaces described in the top-level
README.
