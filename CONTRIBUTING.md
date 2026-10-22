# Contributing

## Prerequisites

- Python 3.13+
- [pre-commit](https://pre-commit.com/)

## Setup

```bash
git clone https://github.com/tomwilkie/pyhive-integration.git
cd pyhive-integration
make setup
```

Or manually:

```bash
pip install -e ".[dev]"
pre-commit install
```

## Running tests

```bash
pytest tests/
```

With coverage:

```bash
pytest tests/ --cov
```

## Running linters

```bash
pre-commit run --all-files
```

Individual tools:

```bash
ruff check src/ tests/ scripts/ examples/    # lint (every ruff rule is enabled)
ruff format src/ tests/ scripts/ examples/   # format
mypy src/                                    # type check (strict)
pylint src/
```

## Submitting a PR

1. Branch off `main`
2. Make your changes and ensure `pre-commit run --all-files` and `pytest tests/` pass
3. Push and open a PR against `main`

Please read our [Code of Conduct](CODE_OF_CONDUCT.md) before contributing.
