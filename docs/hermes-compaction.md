# Putting Jev in front of your Hermes compaction or handoff step

Hermes has no "which messages survive" callback inside its built-in compressor, and the `session:compress` hook fires after the fact. What does work today without touching core:

**A handoff hook.** If you run a gateway hook on `session:compress` that writes a handoff with a text model, let Jev choose what that model sees. Export the transcript with the stable CLI (`hermes sessions export --session-id <id> --format jsonl -`), build a list of `{role, content}` messages, and:

```python
import sys
sys.path.insert(0, str(hermes_root / "plugins" / "hermes-jev"))
from jevkit import compact

selection = compact.select(messages[-240:], keep_last=8, timeout=10)
if selection["status"] == "ok":
    transcript = compact.digest(messages[-240:], selection, limit=24000)
# else: fall back to the plain transcript tail, exactly as before
```

Tell the writing model that `[KEEP VERBATIM]` lines go into the handoff unchanged and `[background]` lines only need their gist. Skip the step for profiles that handle customer data.

Measured on a real 71-turn session: 35 keep, 35 summarize, 1 drop, 0.95 s, two Jev requests.

**The agent itself.** The `jev_compact_select` tool and the `jev-compaction` skill cover handoffs the agent writes on request.

**Not yet:** replacing the built-in compressor. Hermes accepts a whole replacement `ContextEngine` through `ctx.register_context_engine`, which is the right seam for a Jev-guided compressor, but it is all-or-nothing and owns the threading contract. That is a separate, larger piece of work.
