# Contributing

## The sync rule

This repo is the public home of everything Jev does for a Hermes agent. If Jev's role changes in a working Hermes setup, this repo changes in the same piece of work:

- a new decision handed to Jev, or one taken away
- a changed question, threshold, time budget or fail-open behaviour
- a change to what is sent to Jev (the privacy boundary)
- a fix to the plugin, the key flow or the installer
- a new Hermes seam the plugin uses, or one that went away

Each change comes with a test in `tests/`, an updated `SKILL.md` if an agent would do something differently, and a line in `CHANGELOG.md`. Bump `jevkit/__init__.py` and `hermes/plugin/hermes-jev/plugin.yaml` together.

## What never goes in

Keys, tokens, `.env` files, routing pools or decision logs from a real machine, customer or personal data, and machine-specific absolute paths. Run the check before you push:

```bash
python3 -m unittest discover -s tests && python3 scripts/check_release.py
```

## Ground rules for the code

- Standard library only. The point is that any agent can run it with whatever `python3` is on the machine.
- Every call to Jev goes through `jevkit/client.py`, and everything sent goes through `jevkit/privacy.py` first.
- Every feature has a fail-open path that is exactly what the agent would have done without Jev, and a test that proves it.
- Jev picks from closed sets that code built. It never produces text, coordinates, commands or arguments that get executed.
- Tests are offline: fake the transport, never call the real API, never touch a real secret store.
