"""Desktop chat UI for Columbo (Chainlit front-end over the unchanged engine).

The engine glue lives in `events` and `session` (no Chainlit import, so it is
type-checked and unit-tested normally); `app` is the thin Chainlit entrypoint,
launched via `columbo ui` / `chainlit run columbo_py/ui/app.py`.
"""
