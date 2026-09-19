# Changelog

## 0.2.1 (2026-09-19)

- `jev-computer-use` and `jev-browser-use` now carry a **Managed fleets** section: the driver command, credential source, vendor checkout and machine map belong to the fleet, not this repo, and the fleet note they point at is authoritative for them.
- `jev-computer-use` documents the withdrawn preview schema `hermes.cua_jev_choice_request_v1` (`capture_id`, pixel `bounds`, per-region `confidence`, model `jev-1.13.0`) as incompatible with `jev.action_choice_request_v1`. Scripts must be updated, not renamed.
- `jev-browser-use` states that a fleet may make Jev Ultrafast the required default, with the own-browser-tool path reserved for Ultrafast's documented gaps, and that Jev is never bypassed in either path.

## 0.2.0 (2026-09-18)

- The model routing dashboard ships in the repo (`router-dashboard/`, `jev dashboard`): per-profile models, an All-profiles target with confirmation, an Off / Shadow / On switch for Jev routing, and a live view of decisions.
- `scripts/build_release.sh` builds the shareable zip from the committed tree.

## 0.1.1 (2026-09-18)

- Plugin manifest: `config_schema` in the flat shape Hermes expects (it logged a warning and skipped the old one).
- Key page: no reverse-DNS lookup on bind (stalled for seconds on some Macs).
- Shared `routing.json` / `state.json` in the Hermes root are the default for every profile; `/jev <switch> <value> all`.

## 0.1.0 (2026-09-18)

First release.

- `jevkit`: strict Jev client, key store, private key-entry page, privacy gate, model catalog, router, memory filter, compaction selector, two-stage skill picker, bounded action chooser, `jev` command.
- Hermes plugin `hermes-jev`: per-turn model routing through `llm_request` middleware, per-turn skill suggestion through `pre_llm_call`, three tools, `/jev` with per-profile and all-profile switches, decision log.
- Seven agent-agnostic skills.
- Installer for Hermes, Claude Code and Codex, with `--check` and `--uninstall`.
