# Draft skills from successful runs

`distill.py` uses the standard library and the add-on's own front-matter parser. It reads run
exports, without executing their scripts, running Blender, scoring runs or calling evals.
Only task records with `passed: true` in `results.json` produce packets. Passing is the recorded
harness result, not a new judgment of visual quality or evidence that every proposed fact holds.

```sh
python3 loopcut/harness/distill.py packet RUN_DIR [RUN_DIR...] --out .distill-out
python3 loopcut/harness/distill.py draft .distill-out/FILE.packet.md --out .distill-out/drafts --drafter none
python3 loopcut/harness/distill.py draft .distill-out/FILE.packet.md --out .distill-out/drafts --drafter codex
python3 loopcut/harness/distill.py check .distill-out/drafts/distilled/ID.md
python3 loopcut/harness/distill.py promote .distill-out/drafts/distilled/ID.md --baseline
python3 loopcut/harness/distill.py promote .distill-out/drafts/distilled/ID.md --premium --destination /tmp/premium-staging
```

Packets include the submitted brief, the last assistant summary, short failed-call and candidate
recovery excerpts, visible settings, and existing image paths referenced by the run. The
conversation export is preferred, with Markdown transcripts and results' steps as fallbacks.
Workspace Python files are read only at the top level, excluding installed dependencies. Failed
calls are recognized by status, nonzero shell exit markers, and traceback/error output (even when
a log-reading command itself exited zero). Recovery pairing uses shared identifiers, tool names,
script references and explicit success evidence; a human must confirm the relationship. It does
not infer a causal lesson just from temporal proximity.

Settings are source observations. Later visible assignments replace earlier assignments to the
same target; final workspace files and successful live Python calls supply the evidence. Scene
variable prefixes are normalized for compactness; different scenes may share a normalized target.
Computed expressions, branches and later runtime mutations are not evaluated. The
packet keeps a bounded sample of render, lighting, material and size values, with source labels.
Names found in datablock access, constructor helpers and scene-info rows become neutral numbered
placeholders in fact excerpts. Briefs, final reports and render paths retain their source identity.
Image paths are checked for existence, not decoded or judged; captures and reference images are
excluded. Character budgets use the add-on's estimate of 3.4 characters per token: each packet is
at most 13,600 characters, and each full draft file at most 3,060 (about 900 tokens). Truncated
excerpts carry a marker. Extraction writes a `.metadata.json` with source identity, known names
and packet SHA-256, so later checks can catch stale packet metadata and leaked names.

The Codex mode uses `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` with
`exec -s workspace-write -C SCRATCH --skip-git-repo-check -o FILE`, `/dev/null` stdin and an
environment containing only `CODEX_HOME=$HOME/.codex`. It sends the prompt as an argument.
Codex writes each complete skill
as a separate UTF-8 `skills/<slug>.md` file in a fresh scratch directory under the draft output
directory. Optional `notes/<slug>.txt` files hold provenance and caveats for each skill. The
Git-directory check is skipped because scratch output can live outside the checkout. The
last-message reply is retained verbatim as an adjacent `*.response.txt` in the output directory;
it is not parsed, so malformed JSON or stray reply characters cannot lose the skills.

The harness validates all 1–3 skill files before writing any final drafts. Parsing or lint
failure triggers exactly one retry in a fresh scratch directory, with the error quoted in the
retry prompt. Each attempt's scratch files, prompt and validation error are retained, including
on final failure. If the second attempt fails only prose lint (workflow wording, hedge-heavy
audit text or run-specific names), its drafts are kept for human review. Malformed contracts,
oversize files, invalid selectors, empty bodies and unsafe file sets still prevent all final
drafts from being written. Execution errors and output collisions fail without a parse retry. Each
`distilled/<id>.md` gets an adjacent `<id>.provenance.json` containing source runs, task,
packet SHA-256, drafter, UTC date and known
source names, literal `renamed_object_names`, and a `check` object with `passed`, `errors` and
`warnings`, plus `draft_notes` when a notes file was supplied. Retained lint failures have
`check.passed: false`; `check` still exits nonzero and promotion still rejects them.
Codex drafting reads the packet
without writing alongside it. The CLI may need network access; extraction, checks and promotion
do not.

Skills describe transferable Blender knowledge plainly: available libraries, API behavior,
costly traps, and concrete starting values with units and relationships for a similar look.
Local script helpers become Blender concepts and property/socket names. Provenance, source
labels, evidence gaps and compatibility caveats belong in notes sidecars. A body may include
one short note that values came from one approved run when approval is explicit in the evidence;
a passing harness record does not itself establish owner approval. The prompt's examples guide
wording, not facts or values to invent. Skills remain environmental knowledge, without workflow
steps or run-specific object and helper names.

`--drafter none` writes the same prompt alongside the packet and a `.draft-provenance.json`
template, and creates the requested draft output directory. For manual drafting, save each skill
there as `distilled/<id>.md` and copy the provenance template to its `<id>.provenance.json`
sidecar. `check --packet FILE.packet.md DRAFT...` can also lint source names before sidecars exist.
No model is called in this mode.

Checks accept exactly `id`, `version`, `title`, `when`, `roles`, `requires_tools`, `tier`, `kind`,
plus optional `words`, `leads`, `craft`. They reject local routing keys, malformed fields,
oversize files, numbered step lists, transition sequences, dense always/never/must language,
known run-specific names, and incompatible or nested-repetition selection regexes. Three or
more audit phrases in the body (such as "does not establish", "unresolved" and "excerpt")
are rejected as hedge-heavy text; brief scope notes and factual API negatives remain valid.
The name lint ignores Blender defaults (Light, Camera, Cube, Sun, Area, Point, Spot, World,
Material, Plane, Cylinder, Sphere, Empty, Collection and Scene), including numeric suffixes
such as `.001`, unless a literal `.name` assignment records an explicit rename to that name.
Distinctive datablock factory helper names are also tracked. A bounded vocabulary of ordinary capitalised
words is ignored at sentence starts in body prose; selectors, quoted identities and custom
identifiers still count. Computed renames cannot be inferred from static evidence, and older
packets without rename metadata only supply the known names.
Isolated imperatives, possible unnamed CamelCase identities and missing selectors/routing produce
warnings. This is a heuristic lint, not a quality screen. Narrow `words` selectors let knowledge
join matching requests; `craft` with `roles: [worker]` joins that craft's topics without local
`part: topic`. Nonmatching requests retain the existing prompt output.

Baseline promotion copies a free skill into `scripts/addons_core/loopcut/skills/distilled/`.
The recursive built-in loader reads it when the add-on restarts; restart is needed because the
built-in catalog is cached. Premium's documented default is
`/Volumes/BackupFiles/MasterCoding/BlenderCursor/loopcut-web-client-shared/skills/distilled/`;
use `--destination DIR` for writable staging or tests. Both modes copy any provenance sidecar
and preserve the draft's tier and bytes. Promotion rejects existing destinations and built-in
id collisions. It does not evaluate, publish, commit, or imply that a draft passed the separate
screen. Invoke promotion only after that screen approves it.

`.distill-out/` is ignored by Git. The distillation lane does not write to `loopcut/harness/evals/`,
`loopcut/track/` or source run directories.
