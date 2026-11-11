# Ordinary-question attention: explicit per-run opt-in

Use this instruction only when the operator explicitly asks you to read it
in a newly instrumented run with G2C_ATTENTION_RUN and G2C_ATTENTION_HELPER set.
It grants no permission to start/stop/attach sessions or bypass approvals.

The helper validates process identity in its own procfs/PID namespace. A tool
running in a fresh sandbox namespace may not see the bound host processes.
An identity refusal now retains the original error prefix and adds only a
fixed `reason=...` category (for example `server_namespace_unavailable`,
`server_namespace_mismatch` or `caller_ancestry_unconfirmed`). It creates no
waiting marker. Report the category only; never dump environment, process
arguments or conversation files. Do not disable namespace checks, change the
binding, alter sandbox permissions or rerun the helper outside its sandbox
to force success. Cross-namespace signaling is not implemented by this patch.

Immediately before asking an ordinary question that needs the user's answer,
run `"$G2C_ATTENTION_HELPER" attention mark`. Save the returned ATTENTION_WAIT
identifier for that question. Never pass the question, answer, prompt or command
contents to this helper. If it fails, report that attention is unavailable;
do not invent a marker, change permissions or switch to another run.

After the user's reply, before other work and before marking any new question,
run `"$G2C_ATTENTION_HELPER" attention clear --wait EXACT_SAVED_IDENTIFIER`.
The identifier is metadata, not a secret. Never clear the newest marker by
guessing its identifier: a delayed clear must carry its original question ID.
Do not clear an unanswered question just because you inspected its tab.

This covers explicit ordinary-question waiting only. Native approval dialogs,
automatic UserPromptSubmit clearing, red/green status and task completion are
not supported. Do not install hooks or edit global AGENTS/configuration.
