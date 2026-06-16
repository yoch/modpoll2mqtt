# Typing audit (basedpyright `strict`)

Baseline captured before tightening the application package. Full-package analysis uses
`[tool.pyright]` with `typeCheckingMode = "strict"` in `pyproject.toml`.

## Application package (strict)

| Module | Lines | Errors | Warnings |
|--------|------:|-------:|---------:|
| `modpoll/__init__.py` | 6 | 0 | 0 |
| `modpoll/__main__.py` | 3 | 0 | 0 |
| `modpoll/utils.py` | 27 | 0 | 0 |
| `modpoll/register_decode.py` | 191 | 0 | 0 |
| `modpoll/reference_common.py` | 67 | 0 | 0 |
| `modpoll/reference_read.py` | 303 | 0 | 0 |
| `modpoll/reference_write.py` | 326 | 0 | 0 |
| `modpoll/modbus_connection.py` | 186 | 0 | 0 |
| `modpoll/mqtt_task.py` | 367 | 0 | 0 |
| `modpoll/arg_parser.py` | 315 | 0 | 0 |
| `modpoll/main.py` | 329 | 0 | 0 |
| `modpoll/modbus_task.py` | 1103 | 0 | 0 |

Full-package `strict` analysis of `modpoll/` is the gate (`make typecheck`).

## Dominant pre-typing categories

- `reportMissingParameterType` / `reportMissingTypeArgument`
- `reportUnknownMemberType` (pymodbus / paho callbacks)
- `reportExplicitAny` / `reportAny` where `Any` remains intentional

## Rollout order

See project plan: leaves → `types.py` → connection → references → I/O →
`modbus_task` (by class) → `main.py`.
