# tests/

Test suite for the custom modules under `library/` and `module_utils/`.

## Layout

```
conftest.py     shared fixtures + params builders
unit/           mocked HTTP, fast. Parsing, edge cases, validation.
integration/    against a real RavenDB. End-to-end smoke.
```

## Running

```bash
PYTHONPATH=. pytest tests/unit -v          # fast, mocked
PYTHONPATH=. pytest tests/integration -v   # slower; needs a reachable RavenDB
PYTHONPATH=. pytest tests -v               # everything
```

Unit tests mock the `request()` helper so each "node" returns exactly what the test sets.
Integration tests run against a real server to confirm those assumptions still hold.

## Conventions

- One test file per diagnostic kind (or per helper module).
- Test names spell out the property under test; the name + assertion is the documentation.
- Keep each unit test short; heavy setup goes in `conftest.py` as a fixture.
