# Parsed JSON output is an additive field, not a change to `output`

Some whitelisted commands emit JSON on stdout, and callers wanted it parsed
rather than double-parsing the string themselves. We added an opt-in
`?format=json` on `GET /command/execution/{id}` that populates a **new**
`output_json` field, gated on the command's whitelist entry declaring
`output_format: "json"`. The existing `output` field keeps its raw string value
in every case, so a caller that does not pass the parameter cannot reach any of
the new behaviour.

## Considered options

**Changing `output`'s type based on the parameter** (string when raw, object
when json) was rejected. It makes one JSON field polymorphic, which degrades the
OpenAPI schema to `Any` and breaks typed clients. More importantly it makes
backward compatibility contingent on callers *not* passing a parameter, rather
than structural. The cost of the chosen approach is duplicated data in the
response when `format=json`; we accepted that in exchange for a compatibility
guarantee that cannot be accidentally violated.

**Returning 4xx/5xx when parsing fails** was rejected for two reasons. First,
parse failure is a failure of the extra processing the caller requested, not of
the query — the command itself may well have succeeded, and its `status`,
`exit_status` and `output` are still valuable. Second, this codebase's error
envelope is `{"error": {...}, "request_id": ...}` with no `data` field, so an
error response *structurally cannot* carry the execution result; the only escape
hatch would be stuffing the whole result into `detail`, giving one endpoint two
different success shapes. Instead we return 200 with a closed-enum
`output_json_error` and log `parse_failed` at ERROR — the caller gets the data,
the operator gets the alarm, and neither channel borrows the other's semantics.

**Parsing whenever the command declares `output_format: "json"`**, without a
caller parameter, was rejected because it would let an operator's whitelist edit
silently change the response of an endpoint for callers who never asked. The
declaration permits; the parameter triggers.

## Consequences

`output` has four producers and only one is real stdout — the failure path
backfills a log tail, cross-pod heal leaves it empty, and a `logged` command
persists nothing on success by design (its output lives in the control_node run
log). Parsing is therefore attempted **only** on a non-empty successful
`output`; the non-success case reports `not_applicable` and the empty-output
case reports `output_unavailable`. Without this, every failed run would report
`parse_failed` and the ERROR-level signal that a script broke its contract would
be pure noise.

The `logged` producer is the awkward one: it is indistinguishable from cross-pod
data loss when looking at the state alone, but means the opposite — nothing was
lost. Rather than add a fourth enum value for a configuration that can never
return useful JSON anyway, `CommandWhitelistConfig` **rejects
`output_format: "json"` together with `logged: true`** at load time (and
likewise with `disconnects_ssh: true`, which never writes a `CommandState` at
all). This keeps every `output_json_error` value tied to a distinct operator
action, and follows the file's existing habit of failing loudly on operator
mistakes at load rather than at request time.

`output_format` is snapshotted onto `CommandState` at launch rather than re-read
from the whitelist at poll time, so editing a whitelist cannot retroactively
change how an already-finished run is interpreted. It defaults to `text` so
states written by an older pod deserialise correctly during a rolling upgrade.

Because `output_format` lives on `CommandWhitelistConfig`, it is now visible in
the `GET /command/info` responses. That is deliberate: it is how a caller
discovers which commands accept `?format=json`.
