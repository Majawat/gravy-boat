# Contributing

Bug reports and ideas are welcome as issues. For code:

1. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) to run it locally.
2. Keep changes small and focused, with tests for new behaviour (`pytest`).
3. Schema changes need an Alembic migration (`flask --app "app:create_app()" db migrate -m "..."`).
4. Open a pull request against `main`; CI must pass.
