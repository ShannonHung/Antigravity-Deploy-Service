# Context

Glossary for `deploy-service`. Terms only — no implementation detail, no specs.
When a term here conflicts with how a word is being used in conversation or in
code, the conflict is the interesting part: resolve it and update this file.

## Command execution

**Command** — A named, whitelisted operation a user may run against a host.
Identified by `command_name`, defined by an operator in a whitelist file, never
composed by the caller.

**Pipeline** — The concrete steps a Command expands into. Built as a list of
argument lists so user-supplied values are always passed as positional
arguments, never interpolated into a shell string.

**Run** — A single execution of a Command, identified by a `command_id`.

**Whitelist** — The per-user declaration of which Commands that user may run,
against which hosts, with which argument constraints. Operator-owned; a
malformed one is a server misconfiguration, not a caller error.

## Output

**Output** — The text a Run makes available on the poll endpoint. **Not a
synonym for stdout.** Four different producers write it, and only the first is
the command's actual standard output:

1. *Fast-path success* — real stdout, captured by the pod that launched the Run.
2. *Failure* — a **log tail**: the last N lines of the run log, backfilled so a
   failure shows why it failed. Text, essentially never structured.
3. *Healed success* — the **empty string**. Cross-pod recovery reconstructs the
   exit code from the control_node marker and never recovers the output.
4. *Logged success* — also the **empty string**, deliberately. A Logged Run's
   output lives in the control_node run log, served by the trace and view
   endpoints; none is persisted on the Run's state.

Any feature that interprets Output must say which of the four it applies to.
Note that (3) and (4) are indistinguishable from the state alone, yet mean
opposite things: (3) is data loss, (4) is by design.

**Output format** — The operator's declaration, in the Whitelist, of what a
Command emits on stdout (`text` or `json`). Part of the Command's *contract*: it
states what the remote script promises. Declaring `json` **permits** a caller to
request parsing; it never causes parsing on its own.

**Format** — The *caller's* request preference on a poll (`raw` or `json`).
Distinct from Output format: one is what the Command promises, the other is what
this particular request wants. A caller may only ask for `json` from a Command
whose contract declares it.

**Parse failure** — A Command declared its Output format as `json` and still
emitted something that is not JSON. The remote script broke its own contract.
Exceptional, and treated as such.

**Output unavailable** — A Run succeeded, but no Output was persisted for it
(producer 3 above). Distinguished from Parse failure because nothing was emitted
that could have failed to parse — a different fix, a different owner. Producer 4
would be indistinguishable from it, which is why declaring an Output format of
`json` on a Logged Command is rejected outright rather than allowed to report a
data loss that did not happen.

## Lifecycle

**Killable** — Whether *the system* may terminate a Run on its own (timeout,
shutdown). A human explicitly forcing a kill is a separate decision and is not
constrained by it.

**Logged** — Whether a Run tees its output to a per-run file on the control_node,
which is what makes the log viewer and cross-pod recovery possible.

**Heal** — Reconstructing a Run's true outcome from the control_node exit marker
when the launching pod died mid-run. The log, not Redis, is the source of truth.

**Orphan run** — A Run whose launching pod died while the remote work continued.
