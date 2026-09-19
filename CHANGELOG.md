# Changelog

## 0.2.3 (2026-09-19)

- **`jev-browser-use` path B actually runs now.** The runner required a CDP browser to already exist (`BU_CDP_WS` or a Chrome with remote debugging on) and simply failed on a machine without one. It now launches its own headless Chrome on a throwaway profile when no endpoint is given, closes it on exit, SIGINT and SIGTERM, and reports `browser: owned|attached`. `--no-launch-chrome`, `--chrome-path` and `BH_CHROME_PATH` control it. The person's everyday browser is never attached to.
- Fixed the launch order: the browser is started *after* the vendored-venv re-exec. Starting it before meant the exec replaced the process and orphaned the browser and its throwaway profile.
- New `tests/test_browser_runner.py`: chrome discovery, launch flags, CDP polling, the cleanup contract, the launch-order regression and the allowlist.

## 0.2.2 (2026-09-19)

- **Memory filter safety fix.** A passage the privacy gate refuses to send is never injection-checked, but it was still returned in `selected_ids` with no warning — so text shaped like an injection could reach the agent as though it had been judged. Withheld passages are now screened locally (no network) for instruction shapes; matches are dropped into `dropped_injection_ids` and listed in the new `local_screen_ids`. Harmless withheld passages are still kept, so no memory is silently lost.
- `jev_memory_filter`'s tool description and `jev-memory` now say that `unjudged_ids` is *unchecked*, not verified.

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
