# Private values in Wiki answers

The server replaces recognized private values with random, request-scoped
placeholders before invoking the remote model. Classification and replacement
run locally; the model is never used to decide whether a value is private.

Each request reads one local snapshot of the eligible notes. The document
selector receives opaque document IDs, sanitized titles/headings and sanitized
keyword hints. The answer model receives sanitized selected documents. Original
notes and their line numbers remain available through the authenticated source
viewer, and source files are never modified by this process.

## Protection boundary

Explicit `<!-- private -->` blocks are the authoritative way to designate an
unusual private value. Automatic recognition is an additional safeguard for
labeled credentials and common identifiers; it cannot decide whether arbitrary
unmarked prose is confidential.

All note, question and history text now follows one analysis path:

1. `privacy_analysis.py` returns source spans and decoded scalar atoms. Markdown
   structure and YAML/JSON syntax use parsers; secret classification remains a
   local policy. YAML objects are never constructed and aliases are not expanded.
2. `privacy.py` registers recognized values from **every input before masking any
   field**, then replaces them with request-scoped tokens. This includes new
   credentials pasted into a question and values first identified in history.
3. `model_input.py` constructs only the selector and answer payload schemas,
   freezes their content and checks it against the registry.
   The model entrypoint accepts only a prepared prompt and repeats that check before
   transmitting it to the isolated worker (or invoking legacy direct Codex).
   Conversation roles, generated document IDs and citation
   numbers are validated as structure, separately from question/history text,
   titles, headings, search hints and evidence. Unknown fields, invalid roles or
   document IDs, and evidence with a combined raw line/number string are rejected.
   There is no global exemption for text such as `user`: it remains protected when
   it appears in actual content.
   Auditing treats known replacement tokens as opaque regions and preserves the
   source's adjacent character classes. Removing tokens or reinterpreting their
   brackets must not invent a new match in neighboring text. Unknown token-looking
   strings are not exempt from the audit.

The selector's catalog includes Markdown headings and short plain-text labels
adjacent to masked values, so a flat note can still be found by product or field
name. These labels are extracted only after all inputs are registered and masked,
and pass the same outbound audit as the rest of the catalog. Inline placeholders
and their surrounding syntax are retained rather than cut into new text fragments.

Explicit private ranges remain protected when inferred syntax is malformed.
If a recognized sensitive construct is malformed or uses a reference that cannot
be resolved without expanding objects, the whole request is rejected rather than
exporting partially analyzed data. Analysis limit failures also stop the request
before further model calls. These guarantees apply equally to questions and history.
The final outbound check verifies recognized values; it is not a second classifier
that can discover arbitrary unmarked secrets.

Protected syntax adapters report unresolved values with `PrivateSyntaxError`.
The common vault boundary invalidates the entire request context on that error;
masking, restoration and further prompt preparation then all fail. A provisional
single-line quote span may be resolved by a complete multiline YAML scalar, but
unresolved quoted/table values cannot fall back to their raw spelling. Resolved
source ranges identify the responsible dialect so shell escaping and multiline
YAML are not reinterpreted as independent YAML fragments. Markdown code values
are explicitly literal. Unrelated prose is not required to be valid YAML.

Only placeholders present in selected evidence can be restored. The authenticated
response contains the restored answer plus a display template and the values
actually referenced by that template. The browser inserts those values as literal
text after parsing Markdown, so special characters cannot become markup.

Follow-up requests contain completed job IDs instead of rendered answer text.
The server checks ownership and uses its own sanitized history. Known values
from the same conversation, including recognized passwords inside protected
rows and tables, remain available for local re-masking even when the source note
changes. Only values from selected evidence or the question are carried forward;
this internal set is never included in an API response. Job state stays in process
memory, expires under the existing one-hour job policy, and disappears when the
service restarts. This feature does not change the authentication or AI usage limits.

## Detection scope

The filter covers labeled passwords, tokens and license keys; structured IDs,
card/account numbers, private keys and authentication strings; and conservative
private-filename/section rules for family, personal information, accounts and wallets.
Being under the `Personal/` directory alone does not make a document private;
ordinary prose and gift/asset amounts remain usable as model evidence.
Generic property/value tables also require a private filename, section or block;
credential-column tables such as `ID | PW` establish their own private scope.
Sensitive columns remain protected when unrelated columns are added or headers
use Markdown emphasis/code, including `**Password:**` labels. A header requires a
matching table separator, so a
data row cannot redefine the protected columns. One- and two-hyphen separators
are accepted to match the installed SilverBullet parser. Setext and blockquoted
headings establish private scopes too. Headings inside fenced
code cannot end an enclosing private section or a pending credential field.
Emphasized credential labels have the same meaning as their plain forms.
Labeled YAML block scalars, including tags and anchors before their indicators,
protect every indented value line through the next nonblank dedent.
Leading code, emphasis or quoted credential values are recognized separately
from trailing descriptions, so copying just the value into a question is protected.
It also masks recognized values when repeated in questions or elsewhere in the
notebook. A final check stops transmission when a known private value remains.
YAML comments, tags, anchors and multiline quoted scalars, JSON escaped keys and
string values, and quoted shell credential arguments are interpreted locally
so that both source spelling and decoded credential values remain protected in
questions and follow-ups. Restoration retains the original source spelling.

Shell credential arguments support a static POSIX literal subset: unquoted
escaped characters, single/double quotes, adjacent quoted segments, and escaped
newlines. Double quotes use POSIX backslash rules, including `\$` and escaped
backticks. Variable/command expansion, globbing and unquoted operators in a
credential operand are unresolved and reject the request; commands are never
executed to discover their values. Use an explicit literal value (for example a
Markdown code value) when storing a password containing such syntax. A failed
analysis identifies the question, history, or affected note without echoing the
private value. Because notes are registered before selection, a malformed
protected value in an eligible note blocks the request even if that note would
not have been selected.

Arbitrary unlabeled text cannot reliably be recognized as a secret. For unusual
values, explicitly mark a block in the note:

```markdown
## Example service
<!-- private -->
An unusual secret whose format has no recognizable label
<!-- /private -->
```

Content inside that block, including headings, is protected locally. Markdown
fence delimiters and a fixed set of known language labels remain structural;
arbitrary fence info is protected, including before an inline closing marker. Nested
blocks remain protected until the outer closing marker; an unclosed block
protects through the end of the note. Recognizable individual credentials inside
the block are also learned for re-masking in questions and follow-ups.

Use separate marker lines, as above, for a whole line or multiline block.
SilverBullet can treat an entire physical line beginning with an HTML comment
as hidden, including a list item whose content begins with that comment.
Put markers around the whole list item, preserving its bullet. Inline markers are
appropriate after a visible field label or inside a table cell:

```markdown
Password: <!-- private -->example-secret<!-- /private -->
```

This remains a remote AI application: ordinary note content, sanitized questions
and sanitized history still go to the model. It does not remove data sent by earlier versions.

## Response and file boundaries

Each request can learn at most 4,096 private values totaling 1 MiB of UTF-8 text
and 8,192 decoded-value associations. Wrapper nesting is limited to 32 layers;
intermediate wrapper strings are not retained. A bounded prefix matcher replaces
the previous combined regular expression and repeated per-value history searches.
It allows 262,144 states, 4,194,304 construction steps and 65,536 match candidates
per scan; history extraction shares its match allowance across all selected values.
Exceeding a privacy limit fails the entire request before further model calls,
rather than continuing with incomplete masking. Matcher construction, scanning
and history extraction check cancellation and the job deadline during their work.

Each analysis accepts at most 1,048,576 characters, 65,536 parser tokens,
64 nesting levels and 65,536 result spans/atoms, with a 4,194,304-unit parser work
allowance. Prepared model payloads are bounded to 2 MiB. Dependency APIs and
source positions are exercised by synthetic parser tests rather than assumed
from regular-expression matches.

Codex result files and worker responses are limited to 1 MiB. AGY's streamed event
output is limited to 4 MiB before parsing, including non-answer events. Model answer text
is limited to 64,000 UTF-8 bytes and 128 source entries. Restored answer text is
limited to 512,000 UTF-8 bytes; expansion is counted before repeated private
values are joined. Invalid Unicode and empty model answers fail with a readable
error rather than producing an unusable completed job. These limits are enforced
locally even when a model does not follow its output schema.
In isolated mode, provider output also passes a shared strict shape validator before
returning to the web API; document IDs and line references are then checked against
the selected notebook snapshot. Provider errors never cause a fallback or retry.
Overly nested request JSON receives the same structured 400 response as other
invalid request bodies and cannot admit a job or a cancellation record.

Citation validation computes document line counts once and deduplicates with a
set. Cancellation and the existing 600-second job deadline are checked during
postprocessing and before publication, as well as during model execution.

Document reads open every path component relative to an already-open directory
with symlink following disabled. The opened file must be a regular file and stay
within the document byte limit, including when a file changes during a read.

## Browser recovery

A running accepted job stores only its ID, prior job IDs, acceptance state and
cancellation intent in session storage. Its question is retained only while
submission is unconfirmed; restored answers and private values are never saved
there. Reload and reauthentication retain prior conversation references. Accepted
jobs are never recreated after expiration. Cancellation can register a bounded,
owner-scoped server record before query admission; a delayed original submission
then remains cancelled without starting AI work. The record expires under the
job policy. Responses from an older job cannot change the next job's controls or status.
There are at most 64 pending cancellation records per owner and 256 in total;
retries do not extend their lifetime, and existing-job cancellation remains
available when these limits are full. A temporary cancellation failure followed
by a rejected submission preserves cancellation intent and continues recovery.
Closing or resetting the source viewer clears the source text from its DOM.

The browser has one session phase (`checking`, `ready` or `required`) and derives
all interaction permissions from that phase and the active job. Initial status
checking precedes submission/recovery. A login requirement disables new questions
and source requests; cancellation remains available as saved local intent until
login resumes. Metadata/network failures leave question/recovery retry available.

## Validation

The [isolated worker](deploy/README.md) has no original note files or privacy vault.
Codex and AGY receive the same masked application payloads and structured-output
contracts. Codex reuses an isolated temporary HOME and workspace, with a new ephemeral
thread for each call. AGY creates a temporary HOME and workspace for each invocation.
Only selected login files are copied in and refreshed credentials copied back. No
previous history, rules or plugins are imported. Codex's process and temporary files
are recycled after 32 calls, 30 minutes of age, or 5 idle minutes, and discarded on
failure/cancellation. AGY cleans up after each call. An abrupt host crash can leave files
in the service's private temporary directory. This does not control remote-provider
retention or guarantee secure disk erasure. Filesystem separation must be verified
in the actual service namespace, not inferred from unit templates alone.

Streaming previews contain generated text with opaque values hidden. They have no
restored values or citations and are rendered as literal text. Only a completed,
validated answer can supply protected values and source links. Previews are removed
on failure/cancellation and are never persisted as conversation history. Stream GETs
require the same JWT and job ownership as normal job reads, renew every 20 seconds,
and reconnect to the existing job without another model submission.

The single runner includes privacy, HTTP admission, real local child-process
contracts, portable configuration, and frontend build tests. Application
imports use a temporary copy with synthetic configuration and a disabled default
CLI; tests do not read production configuration or invoke the real AI CLI.
The shared boundary matrix exercises the same formats through all three input
channels, then removes the source and checks follow-up prompts. It also checks
exact source restoration, line mapping and selected-only value restoration.
The failure matrix covers malformed/unsupported YAML, table and shell values in
all three input channels, with and without explicit private markers, and requires
zero model calls. A fixed synthetic shell oracle checks literal decoding against
Bash; it never uses note/user text. Metadata-collision tests verify both successful
follow-ups and rejection of the same strings when present in content.
This replaces repeated format-specific worker tests; focused parser and real
subprocess contracts remain separate.
Run from this directory:

```sh
python3 -B run_tests.py
```

With Node.js and Playwright available, include browser scenarios:

```sh
python3 -B run_tests.py --browser
```

Runtime Python dependencies are declared in `requirements.txt`. Their imports
and source-position behavior are exercised by the tests. The analysis uses
[PyYAML scanner/events](https://pyyaml.org/wiki/PyYAMLDocumentation) and
[markdown-it token maps](https://markdown-it-py.readthedocs.io/en/latest/using.html).

Browser tests use local fixtures, including two origins for redirect/CORS behavior.
They verify rendering and state transitions. Optional screenshots are manual
review artifacts, not visual comparison tests.

Set `WIKI_TEST_PUBLIC_ROOT`, `WIKI_TEST_SERVER_SOURCE`, or `WIKI_TEST_PLAYWRIGHT`
to use isolated source copies or another existing Playwright installation.
`WIKI_TEST_SCREENSHOTS` optionally names a directory for manual review images.

All tests use synthetic notes and simulated model responses, including the real
local process contract tests. Production note contents and credentials must never
be printed in test output or sent as test prompts. Model answer quality is not
established by these deterministic tests.
