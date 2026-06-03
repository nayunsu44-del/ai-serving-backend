# Contributing

Thanks for your interest in improving AI Serving Backend.

## Development setup

Requires **Python 3.13**.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env   # then fill in keys
```

## Running tests

Tests are mock-based and make no external API calls.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Always invoke the suite with `python -m pytest` (not the bare `pytest`
console script) so the project root is on `sys.path` and `import app`
resolves. CI runs this exact command on every push and pull request.

## Dependencies

`requirements.txt` is a fully pinned lock captured on Python 3.13. After
changing dependencies, regenerate it:

```powershell
.\.venv\Scripts\python.exe -m pip freeze > requirements.txt
```

## Pull requests

1. Branch off `main`.
2. Keep changes focused; match the existing code style.
3. Add or update tests for behavior changes — keep the suite green.
4. Update `README.md` / `.env.example` when you add or change configuration.
5. Open a PR; CI must pass before merge.

## Reporting security issues

Do not file public issues for vulnerabilities — see [SECURITY.md](SECURITY.md).
