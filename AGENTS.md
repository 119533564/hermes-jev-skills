# Instructions for an agent installing this repo

A person pointed you here to give their agent Jev. Follow these steps in order. They are the whole job.

## The one rule

**You never see the person's TypeSafe API key.** Do not ask for it in chat. Do not read it from any file or secret store. Do not put it in a command, URL or config. If they paste it to you anyway: do not store or repeat it, tell them to replace that key at https://console.typesafe.ai/settings/keys, and continue with step 3, which is the only way a key gets in.

## Steps

1. **Get the code** (skip if it is already on disk):

   ```bash
   git clone https://github.com/kerpopule/hermes-jev-skills ~/hermes-jev-skills
   ```

2. **Preview, then install.** Show the person what `--check` reports before running the real thing. It detects Hermes, Claude Code and Codex and installs for each. It edits one list (`plugins.enabled`) in each Hermes `config.yaml`, with a timestamped backup beside it.

   ```bash
   python3 ~/hermes-jev-skills/install.py --check
   ```

   ```bash
   python3 ~/hermes-jev-skills/install.py
   ```

   To enable the Hermes plugin in only some profiles: `--enable name1,name2`, or `--enable none`.

3. **Connect the key, privately.** Run this and tell the person a page has opened on the computer you are running on, where they paste their key:

   ```bash
   jev setup-key
   ```

   It prints a JSON line with a `url` on stderr. That URL holds no secret, so if the browser did not open, or the person is on another device, send it to them and say it only works on the agent's computer. Wait for `{"status": "stored", "verified": true}`. If there is no browser at all, the person runs `jev setup-key --tty` themselves in their own terminal.

4. **Check:**

   ```bash
   jev doctor
   ```

   `key.present` and `jev.reachable` must both be true.

5. **Model pools** (only if `routing.tiers_configured` is empty):

   ```bash
   jev models suggest --write
   ```

   Then show the person the pools and ask whether they want specific models first for coding, writing, research or vision. Copy model ids from `jev models list --search <name>`; never invent one.

6. **Hermes only.** Plugins load when a session or gateway starts. Do not restart a running gateway unless the person asks. Tell them to run `/jev routing shadow` in a new session, watch a day of decisions in `logs/jev-decisions.jsonl`, then `/jev routing on`.

7. **Report** in three or four sentences: what was installed where, that the key is connected, what routing mode is set, and what needs a restart.

## Using it afterwards

Read the skill that matches the task: `jev-model-routing`, `jev-memory`, `jev-compaction`, `jev-skill-select`, `jev-computer-use`, `jev-browser-use`, `jev-setup`. Every `jev` subcommand takes JSON on stdin and answers JSON on stdout, and every one returns a usable fail-open answer when Jev is unavailable, so never block on it.

## Keeping it current

```bash
git -C ~/hermes-jev-skills pull && python3 ~/hermes-jev-skills/install.py
```

## Contributing back

If you change how Jev is used in a Hermes setup (a new decision Jev takes over, a changed threshold, a fix to the plugin), the same change belongs in this repo, with a test, in the same piece of work. See [CONTRIBUTING.md](CONTRIBUTING.md).
