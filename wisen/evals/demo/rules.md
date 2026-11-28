# Demo rules

- Install and run Python packages with `uv` (`uv add`, `uv run`); never suggest `pip install`.
- Do not write docstrings or summary comments above functions; good names explain the code.
- Write tests as plain pytest functions with bare `assert`; never use `unittest.TestCase`.
- Never suggest `git push --force` or `-f`; use `git push --force-with-lease`.
- Give every Python function parameter and return value a type hint.
- Work with file paths through `pathlib.Path`; do not use `os.path`, `os.listdir` or `os.walk`.
