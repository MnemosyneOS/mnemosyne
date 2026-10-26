# Contributing to Mnemosyne OS

Thanks for considering a contribution. This document describes the parts of
the workflow that are not obvious from the code.

## The one rule that matters

**The core carries zero third-party dependencies.** `install_requires` is an
empty list, and that is a product commitment, not an accident. A PR that adds
a runtime dependency to the core will be declined regardless of how useful it
is.

Optional integrations live in `mnemosyne_plugins/` and are declared as
extras. If your change needs a new provider, it belongs there.

## Before you open an issue

Please include:

- Mnemosyne OS version (`pip show mnemosyne-os`)
- Python version and operating system
- The smallest command or script that reproduces the behaviour
- What you expected, and what happened instead

For recall-quality questions, run `python scripts/verify_recall_quality.py`
first and paste the output. It answers most of them.

## Before you open a pull request

1. Run `python verify.py` — it must pass
2. Keep the change to one concern; split unrelated refactors into their own PR
3. Add or update a test for the behaviour you changed
4. Do not reformat files you did not otherwise touch

## Development setup

```bash
git clone https://github.com/MnemosyneOS/mnemosyne
cd mnemosyne
pip install -e .
python verify.py
```

## Areas where help is especially useful

- Recall-quality harnesses for datasets we do not cover yet
- Adapters for vector stores, embedding providers and rerankers
  (`mnemosyne_plugins/`)
- Documentation and examples
- Benchmarks — see [MnemosyneOS/mnemosyne-bench](https://github.com/MnemosyneOS/mnemosyne-bench)

## Code of conduct

Be specific, be kind, assume the other person is competent. Reports go to
the maintainers through the contact page at
[ai-memory.net/#/contact](https://ai-memory.net/#/contact).
