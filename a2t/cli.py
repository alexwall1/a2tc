"""A2T command line interface."""

from __future__ import annotations

import warnings
from pathlib import Path

import click

from a2t.detect.targets import detect_targets, find_proposal_date
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import ExtractionError, LayoutUnavailable, extract_lines
from a2t.model.assemble import AssemblyError, assemble
from a2t.model.reconcile import reconcile
from a2t.model.tree import Kind
from a2t.retrieval.base import RetrievalError, title_matches
from a2t.retrieval.acquire import acquire
from a2t.retrieval.eurlex_builder import DEFAULT_CACHE, EurlexBuilderProvider
from a2t.retrieval.versions import VersionError, publication_warning, select_version


def _echo_warnings() -> None:
    """Route the extractor's warnings through click, so they read like the rest.

    They travel as warnings rather than return values because every command extracts,
    and none of them can do anything about it — but the reader of the output needs to
    know what the document will be missing.
    """
    def show(message, category, filename, lineno, file=None, line=None):  # noqa: ARG001
        if issubclass(category, LayoutUnavailable):
            click.echo(f"WARNING {message}", err=True)
        else:
            click.echo(f"{category.__name__}: {message}", err=True)

    warnings.showwarning = show
    warnings.simplefilter("once", LayoutUnavailable)


@click.group()
@click.version_option("0.1.0", prog_name="a2t")
def main() -> None:
    """Convert EU amendment proposals into tracked-changes Word documents."""
    _echo_warnings()


# `convert` exit status when at least one act was converted and at least one was not.
# 0 is success, 1 is nothing converted, and 2 is click's own for a usage error.
EXIT_PARTIAL = 3


def _open_proposal(pdf: Path, as_of):
    """Lines, operative part, detection and date for a command that needs all four.

    Commands that read the proposal each did this inline and failed differently — a
    traceback for a missing `pdftotext`, a `None` date passed on to retrieval. One place
    now turns each of those into a message and a non-zero exit.
    """
    try:
        lines = extract_lines(pdf)
        operative = operative_part(lines).lines
        detection = detect_targets(operative, lines)
    except (ExtractionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not detection.targets:
        raise click.ClickException(detection.explain_empty())
    on_date = as_of.date() if as_of else find_proposal_date(lines)
    if on_date is None:
        raise click.ClickException("could not find the proposal date; pass --as-of")
    return lines, operative, detection, on_date


def _unused_name(outdir: Path, stem: str, suffix: str, taken: set[Path]) -> Path:
    """`outdir/stem+suffix`, or with _2, _3 … when this run already wrote that name.

    Two blocks for one act that could not be merged (they amend different scopes of it)
    name the same file; the second must not replace the first.
    """
    candidate, n = outdir / f"{stem}{suffix}", 1
    while candidate in taken:
        n += 1
        candidate = outdir / f"{stem}_{n}{suffix}"
    return candidate


@main.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path),
              help="Write the cleaned operative part here.")
@click.option("--show-removed", is_flag=True, help="List what was stripped and why.")
def extract(pdf: Path, out: Path | None, show_removed: bool) -> None:
    """Extract and de-boilerplate the operative part of an amending act."""
    try:
        lines = extract_lines(pdf)
        result = operative_part(lines)
    except (ExtractionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    first, last = result.lines[0], result.lines[-1]
    click.echo(f"{pdf.name}: {len(lines)} lines, {lines[-1].page} pages")
    click.echo(
        f"operative part: source lines {first.lineno}-{last.lineno}, "
        f"pages {first.page}-{last.page}"
    )
    click.echo(f"kept {len(result.lines)} lines; removed {len(result.removed)}:")
    for reason, count in sorted(result.reasons().items()):
        click.echo(f"  {count:4}  {reason}")

    if show_removed:
        click.echo("\nremoved lines:")
        for line, reason in result.removed:
            click.echo(f"  p{line.page}:{line.lineno} [{reason}] {line.text[:70]!r}")

    if out:
        out.write_text("\n".join(ln.text for ln in result.lines), encoding="utf-8")
        click.echo(f"\nwrote {out}")


@main.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def detect(pdf: Path) -> None:
    """Identify which acts a proposal amends, and resolve their CELEX ids.

    Reads the PDF only — no network access.
    """
    try:
        lines = extract_lines(pdf)
        result = detect_targets(operative_part(lines).lines, lines)
    except (ExtractionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if not result.targets:
        raise click.ClickException(result.explain_empty())

    click.echo(f"detected {len(result.targets)} target act(s):\n")
    width = max(len(t.citation.raw) for t in result.targets)
    for target in result.targets:
        scope = f"Article {target.root_scope}" if target.root_scope else "whole act"
        click.echo(
            f"  {target.citation.raw:<{width}}  ->  {target.consolidated_celex}"
            f"  ({target.celex_source})  {scope}"
            f"   [Art. {target.amending_article}, line {target.anchor_line}]"
        )

    if result.discrepancies:
        click.echo(f"\n{len(result.discrepancies)} discrepancy(ies):")
        for item in result.discrepancies:
            where = f"line {item.line}" if item.line else "—"
            click.echo(f"  [{item.kind}] {where}: {item.message}")


@main.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--show", type=str, default=None,
              help="Print one instruction in full, by id (e.g. 1.15.b.i).")
@click.option("--tree", is_flag=True, help="Print the full instruction tree.")
def parse(pdf: Path, show: str | None, tree: bool) -> None:
    """Parse the operative part into a tree of amendment instructions."""
    import collections

    from a2t.parse.rules import classify_all
    from a2t.parse.segment import segment

    try:
        lines = extract_lines(pdf)
        operative = operative_part(lines).lines
        blocks = segment(operative, detect_targets(operative, lines).targets)
    except (ExtractionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    classify_all(blocks)

    every = [i for block in blocks for i in block.walk()]

    if show:
        found = next((i for i in every if i.id == show), None)
        if found is None:
            raise click.ClickException(f"no instruction with id {show}")
        click.echo(f"id       {found.id}   (level {found.level}, line {found.source_line})")
        click.echo(f"op       {found.op.value}")
        click.echo(f"raw      {found.raw}")
        for label, value in [("scope", found.scope_text), ("target", found.target_text),
                             ("find", found.find_text), ("replace", found.replace_text)]:
            if value:
                click.echo(f"{label:8} {value}")
        if found.payload:
            click.echo(f"payload  ({len(found.payload)} chars, lines "
                       f"{found.payload_lines[0]}-{found.payload_lines[1]})")
            click.echo(f"         {found.payload[:300]}...")
        for note in found.notes:
            click.echo(f"note     {note}")
        return

    for block in blocks:
        click.echo(f"{block.citation}  ({block.celex})")
        click.echo(f"  {len(block.instructions)} top-level, "
                   f"{len(list(block.walk()))} instructions total")
        if tree:
            for instruction in block.walk():
                indent = "  " * instruction.level
                click.echo(f"  {indent}{instruction.id:<12} {instruction.op.value:<13} "
                           f"{instruction.raw[:64]}")
        click.echo()

    counts = collections.Counter(i.op.value for i in every)
    click.echo("operations:")
    for op, count in counts.most_common():
        click.echo(f"  {op:<14} {count}")
    unknown = counts.get("unknown", 0)
    click.echo(f"\nclassified {len(every) - unknown}/{len(every)}")
    notes = [(i.id, n) for i in every for n in i.notes if "malformed" in n or "N.B" in n]
    if notes:
        click.echo(f"\nsource defects repaired ({len(notes)}):")
        for ident, note in notes:
            click.echo(f"  {ident:<12} {note}")


@main.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--cache", type=click.Path(path_type=Path), default=DEFAULT_CACHE,
              show_default=True, help="Where retrieved acts are cached.")
@click.option("--refresh", is_flag=True, help="Ignore the cache and re-fetch.")
@click.option("--as-of", type=click.DateTime(["%Y-%m-%d"]), default=None,
              help="Override the proposal date used to pick the version.")
def retrieve(pdf: Path, cache: Path, refresh: bool, as_of) -> None:
    """Retrieve and structure the base acts a proposal amends."""
    try:
        lines = extract_lines(pdf)
        detection = detect_targets(operative_part(lines).lines, lines)
    except (ExtractionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    on_date = as_of.date() if as_of else find_proposal_date(lines)
    if on_date is None:
        raise click.ClickException(
            "could not find the proposal date; pass --as-of YYYY-MM-DD"
        )
    click.echo(f"proposal date: {on_date.isoformat()}\n")

    provider = EurlexBuilderProvider(cache_dir=cache, refresh=refresh)
    failures = 0

    for target in detection.targets:
        base = target.consolidated_celex
        click.echo(f"{target.citation.raw}")
        # The same acquisition `convert` uses, fallbacks included: a command that shows
        # what would be retrieved must not fail where the conversion would succeed.
        try:
            acquired = acquire(provider, base, on_date)
        except (VersionError, RetrievalError) as exc:
            click.echo(f"  retrieval failed: {exc}")
            failures += 1
            continue
        pinned, retrieved = acquired.pinned, acquired.retrieved
        click.echo(f"  version   {pinned}  (in force at {on_date.isoformat()})")
        if acquired.warning:
            click.echo(f"  WARNING   {acquired.warning}")

        if not title_matches(target.citation, retrieved.title):
            click.echo(
                f"  TITLE MISMATCH — retrieved {retrieved.title[:60]!r}; "
                f"refusing to use it for {target.citation.raw}"
            )
            failures += 1
            continue
        click.echo(f"  title     {retrieved.title[:66]}")

        try:
            act = assemble(
                retrieved.full_text_html, pinned, retrieved.title, retrieved.version_date
            )
        except AssemblyError as exc:
            click.echo(f"  assembly failed: {exc}")
            failures += 1
            continue
        click.echo(
            f"  structure {len(act.articles)} articles, "
            f"{act.count(Kind.PARAGRAPH)} paragraphs, "
            f"{act.count(Kind.SUBPARAGRAPH)} subparagraphs, "
            f"{act.count(Kind.POINT)} points"
        )

        check = reconcile(act, retrieved.text_units)
        click.echo(f"  crosscheck {check.summary()}")
        for note in check.expected:
            click.echo(f"    (expected) {note}")
        for note in check.unexpected:
            click.echo(f"    WARNING    {note}")
        click.echo()

    if failures:
        raise click.ClickException(f"{failures} target(s) could not be prepared")


@main.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--cache", type=click.Path(path_type=Path), default=DEFAULT_CACHE,
              show_default=True)
@click.option("--as-of", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--verbose", is_flag=True, help="List every resolved instruction.")
@click.option("--llm/--no-llm", default=False,
              help="Offer unresolved instructions to the LLM tier.")
@click.option("--model", default=None, help="Requesty model id (or set A2T_MODEL).")
@click.option("--llm-cache", type=click.Path(path_type=Path),
              default=Path("out/llmcache"), show_default=True)
def resolve(pdf: Path, cache: Path, as_of, verbose: bool, llm: bool,
            model: str | None, llm_cache: Path) -> None:
    """Resolve every instruction against the base act it amends."""
    from a2t.config import ConfigError, load_settings
    from a2t.parse.rules import classify_all
    from a2t.parse.segment import segment
    from a2t.resolve.fallback import apply_llm_fallback
    from a2t.resolve.llm import LlmResolver, LlmClient
    from a2t.resolve.resolver import resolve_block

    llm_resolver = None
    if llm:
        settings = load_settings(pdf.resolve().parent, model=model)
        try:
            settings.require_key()
            settings.require_model()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        llm_resolver = LlmResolver(LlmClient(settings, cache_dir=llm_cache))
        click.echo(f"LLM tier: {settings.model}\n")

    try:
        lines = extract_lines(pdf)
        operative = operative_part(lines).lines
        detection = detect_targets(operative, lines)
        blocks = segment(operative, detection.targets)
    except (ExtractionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    classify_all(blocks)

    on_date = as_of.date() if as_of else find_proposal_date(lines)
    if on_date is None:
        raise click.ClickException("could not find the proposal date; pass --as-of")

    provider = EurlexBuilderProvider(cache_dir=cache)
    by_celex = {t.celex: t for t in detection.targets}
    total = resolved = 0
    review: list = []

    for block in blocks:
        target = by_celex[block.celex]
        try:
            acquired = acquire(provider, target.consolidated_celex, on_date)
        except (VersionError, RetrievalError) as exc:
            click.echo(f"{block.citation}: SKIPPED — {exc}\n")
            continue
        pinned, retrieved = acquired.pinned, acquired.retrieved
        if acquired.warning:
            click.echo(f"{block.citation}: WARNING {acquired.warning}")
        if not title_matches(target.citation, retrieved.title):
            click.echo(f"{block.citation}: SKIPPED — title mismatch\n")
            continue

        act = assemble(retrieved.full_text_html, pinned, retrieved.title,
                       retrieved.version_date)
        report = resolve_block(block, act)
        if llm_resolver is not None and report.unresolved:
            outcome = apply_llm_fallback(report, act, llm_resolver)
            click.echo(f"{block.citation}: LLM tier {outcome.summary()}")
        total += len(report.applicable)
        resolved += len(report.resolved)
        review.extend(report.doubtful)

        click.echo(f"{block.citation}  ({pinned})")
        click.echo(f"  {report.summary()}")
        for item in report.unresolved:
            click.echo(f"    UNRESOLVED {item.instruction.id:<12} {item.reason}")
        if verbose:
            for item in report.resolved:
                node = item.node.citation if item.node else "(act root)"
                click.echo(f"    {item.instruction.id:<12} "
                           f"{item.instruction.op.value:<13} {node}")
        click.echo()

    click.echo(f"rule-engine coverage: {resolved}/{total} "
               f"({resolved / total:.1%})" if total else "nothing to resolve")

    if review:
        click.echo(f"\n{len(review)} instruction(s) need human review:")
        for item in review:
            click.echo(f"  {item.instruction.id:<12} line {item.instruction.source_line}")
            click.echo(f"    {item.doubt}")


@main.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", "outdir", type=click.Path(path_type=Path),
              default=Path("out"), show_default=True)
@click.option("--cache", type=click.Path(path_type=Path), default=DEFAULT_CACHE,
              show_default=True)
@click.option("--as-of", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--llm/--no-llm", default=False,
              help="Offer unresolved instructions to the LLM tier.")
@click.option("--model", default=None, help="Requesty model id (or set A2T_MODEL).")
@click.option("--llm-cache", type=click.Path(path_type=Path),
              default=Path("out/llmcache"), show_default=True)
@click.option("--author", default="A2T", show_default=True,
              help="Author name recorded on every tracked change.")
@click.option("--recitals/--no-recitals", default=False,
              help="Insert the proposal's own preamble — title, citations and "
                   "recitals — ahead of the articles, as tracked insertions.")
def convert(pdf: Path, outdir: Path, cache: Path, as_of, llm: bool,
            model: str | None, llm_cache: Path, author: str,
            recitals: bool) -> None:
    """Convert an amendment proposal into tracked-changes Word documents."""
    from a2t.apply.changeset import build_changeset
    from a2t.config import ConfigError, load_settings
    from a2t.emit.document import emit_document
    from a2t.emit.report import ActOutcome, RunReport, write_report
    from a2t.extract.preamble import PreambleError, preamble as read_preamble
    from a2t.parse.rules import classify_all
    from a2t.parse.segment import own_provisions, segment
    from a2t.resolve.fallback import apply_llm_fallback
    from a2t.resolve.llm import LlmResolver, LlmClient
    from a2t.resolve.resolver import resolve_block

    llm_resolver = None
    if llm:
        settings = load_settings(pdf.resolve().parent, model=model)
        try:
            settings.require_key()
            settings.require_model()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        llm_resolver = LlmResolver(LlmClient(settings, cache_dir=llm_cache))

    try:
        lines = extract_lines(pdf)
        operative = operative_part(lines).lines
        detection = detect_targets(operative, lines)
        blocks = segment(operative, detection.targets)
    except (ExtractionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not detection.targets:
        # Writing an empty report and exiting 0 looks like success.
        raise click.ClickException(detection.explain_empty())
    classify_all(blocks)

    on_date = as_of.date() if as_of else find_proposal_date(lines)
    if on_date is None:
        raise click.ClickException("could not find the proposal date; pass --as-of")

    front = None
    if recitals:
        try:
            front = read_preamble(lines, pdf)
        except PreambleError as exc:
            raise click.ClickException(f"--recitals: {exc}") from exc
        click.echo(f"preamble: {front.summary()} "
                   f"(source lines {front.source_lines[0]}-{front.source_lines[1]})")
        for note in front.notes:
            click.echo(f"  note: {note}")

    provider = EurlexBuilderProvider(cache_dir=cache)
    by_celex = {t.celex: t for t in detection.targets}
    run = RunReport(source=pdf, proposal_date=on_date.isoformat(),
                    detection_targets=detection.targets,
                    own_provisions=own_provisions(operative, detection.targets),
                    discrepancies=detection.discrepancies,
                    preamble=front)

    click.echo(f"{pdf.name} — proposal date {on_date.isoformat()}")
    for target in detection.targets:
        click.echo(f"  {target.citation.raw} -> {target.consolidated_celex} "
                   f"({target.celex_source})")
    for item in detection.discrepancies:
        click.echo(f"  note: {item.message}")
    click.echo()

    failures: list[str] = []
    written: set[Path] = set()
    for block in blocks:
        target = by_celex[block.celex]
        try:
            acquired = acquire(provider, target.consolidated_celex, on_date)
        except (VersionError, RetrievalError) as exc:
            # One unobtainable act must not cost the others their documents.
            click.echo(f"{block.citation}: SKIPPED — {exc}\n")
            failures.append(f"{block.citation}: {exc}")
            continue
        pinned, retrieved = acquired.pinned, acquired.retrieved
        if acquired.warning:
            click.echo(f"{block.citation}: WARNING {acquired.warning}")

        if not title_matches(target.citation, retrieved.title):
            click.echo(f"{block.citation}: SKIPPED — title mismatch; retrieved "
                       f"{retrieved.title[:60]!r}\n")
            failures.append(f"{block.citation}: title mismatch")
            continue

        try:
            act = assemble(retrieved.full_text_html, pinned, retrieved.title,
                           retrieved.version_date)
        except AssemblyError as exc:
            # Acts adopted before roughly 2005 use HTML templates the reader does not
            # know; that only surfaces here, on the sector-3 path, since every
            # consolidation is in the current template.
            click.echo(f"{block.citation}: SKIPPED — {exc}\n")
            failures.append(f"{block.citation}: {exc}")
            continue
        resolution = resolve_block(block, act)
        if llm_resolver is not None and resolution.unresolved:
            apply_llm_fallback(resolution, act, llm_resolver)
        changeset = build_changeset(resolution, act)

        document = _unused_name(outdir, f"{pinned}_tracked", ".docx", written)
        written.add(document)
        emitted = emit_document(act, changeset, document, author=author,
                                version_warning=acquired.warning,
                                preamble=front)

        run.outcomes.append(ActOutcome(
            block=block, celex=block.celex, version=pinned, title=retrieved.title,
            resolution=resolution, changeset=changeset, emitted=emitted,
            version_warning=acquired.warning,
        ))

        click.echo(f"{block.citation}  ({pinned})")
        click.echo(f"  {resolution.summary()}")
        click.echo(f"  {changeset.summary()}")
        click.echo(f"  wrote {document}  — {emitted.summary()}")
        if emitted.unrendered:
            click.echo(f"  WARNING {len(emitted.unrendered)} change(s) not placed")
        click.echo()

    run.failures = failures
    report_path = write_report(run, outdir / "report.md")
    click.echo(f"wrote {report_path}")
    for failure in failures:
        click.echo(f"NOT CONVERTED  {failure}")

    flagged = sum(
        1 for o in run.outcomes for c in o.changeset.renderable if c.needs_review
    )
    if flagged:
        click.echo(f"{flagged} change(s) need human review — see the report "
                   f"and the Word comments.")
    if failures:
        # Exit 0 after "NOT CONVERTED" reads as success to a script or a CI step. Some
        # acts converted and others not is a different outcome from nothing converting:
        # the documents and the report exist, so a caller may want to use them.
        raise SystemExit(EXIT_PARTIAL if run.outcomes else 1)


@main.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--cache", type=click.Path(path_type=Path), default=DEFAULT_CACHE,
              show_default=True)
@click.option("--as-of", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--show", default=None, help="Print the diff for one instruction id.")
def diff(pdf: Path, cache: Path, as_of, show: str | None) -> None:
    """Build the set of edits the proposal makes, with word-level diffs."""
    import collections

    from a2t.apply.changeset import ChangeKind, build_changeset
    from a2t.apply.diff import summarise
    from a2t.parse.rules import classify_all
    from a2t.parse.segment import segment
    from a2t.resolve.resolver import resolve_block

    lines, operative, detection, on_date = _open_proposal(pdf, as_of)
    blocks = segment(operative, detection.targets)
    classify_all(blocks)

    provider = EurlexBuilderProvider(cache_dir=cache)
    by_celex = {t.celex: t for t in detection.targets}

    for block in blocks:
        target = by_celex[block.celex]
        try:
            acquired = acquire(provider, target.consolidated_celex, on_date)
            pinned, retrieved = acquired.pinned, acquired.retrieved
            act = assemble(retrieved.full_text_html, pinned, retrieved.title)
        except (VersionError, RetrievalError, AssemblyError) as exc:
            click.echo(f"{block.citation}: SKIPPED — {exc}\n")
            continue
        changeset = build_changeset(resolve_block(block, act), act)

        if show:
            for change in changeset.renderable:
                if change.instruction.id != show:
                    continue
                click.echo(f"{change.instruction.id}  {change.kind.value}  "
                           f"{change.citation}")
                click.echo(f"  {summarise(change.edit)}")
                for span in change.edit.spans:
                    marker = {"equal": " ", "delete": "-", "insert": "+"}[span.op]
                    body = span.text if span.changed else _elide(span.text)
                    click.echo(f"  {marker} {body}")
            continue

        click.echo(f"{block.citation}  ({pinned})")
        click.echo(f"  {changeset.summary()}")
        modes = collections.Counter(
            c.edit.mode for c in changeset.renderable if c.edit
        )
        click.echo(f"  diff modes: {dict(modes)}")
        flagged = [c for c in changeset.renderable if c.needs_review]
        if flagged:
            click.echo(f"  {len(flagged)} change(s) carry a review flag")
        for change in changeset.skipped:
            if change.kind is ChangeKind.SKIPPED and change.note and (
                "container" not in change.note
            ):
                click.echo(f"    skipped {change.instruction.id:<12} {change.note}")
        click.echo()


def _elide(text: str, keep: int = 40) -> str:
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]} […] {text[-keep:]}"


@main.command("llm-eval")
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--cache", type=click.Path(path_type=Path), default=DEFAULT_CACHE,
              show_default=True)
@click.option("--llm-cache", type=click.Path(path_type=Path),
              default=Path("out/llmcache"), show_default=True)
@click.option("--model", default=None, help="Requesty model id (or set A2T_MODEL).")
@click.option("--limit", type=int, default=25, show_default=True,
              help="How many instructions to evaluate.")
@click.option("--as-of", type=click.DateTime(["%Y-%m-%d"]), default=None)
def llm_eval(pdf: Path, cache: Path, llm_cache: Path, model: str | None,
             limit: int, as_of) -> None:
    """Measure the LLM tier against the rule engine on real instructions."""
    from a2t.config import ConfigError, load_settings
    from a2t.parse.rules import classify_all
    from a2t.parse.segment import segment
    from a2t.resolve.evaluate import evaluate
    from a2t.resolve.llm import LlmResolver, LlmClient
    from a2t.resolve.resolver import resolve_block

    settings = load_settings(pdf.resolve().parent, model=model)
    try:
        settings.require_key()
        settings.require_model()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    lines, operative, detection, on_date = _open_proposal(pdf, as_of)
    blocks = segment(operative, detection.targets)
    classify_all(blocks)

    provider = EurlexBuilderProvider(cache_dir=cache)
    by_celex = {t.celex: t for t in detection.targets}

    client = LlmClient(settings, cache_dir=llm_cache)
    resolver = LlmResolver(client)
    click.echo(f"model: {settings.model}\n")

    remaining = limit
    for block in blocks:
        if remaining <= 0:
            break
        target = by_celex[block.celex]
        try:
            acquired = acquire(provider, target.consolidated_celex, on_date)
            pinned, retrieved = acquired.pinned, acquired.retrieved
            act = assemble(retrieved.full_text_html, pinned, retrieved.title)
        except (VersionError, RetrievalError, AssemblyError) as exc:
            click.echo(f"{block.citation}: SKIPPED — {exc}\n")
            continue
        report = resolve_block(block, act)

        result = evaluate(report.resolved, act, resolver, limit=remaining)
        remaining -= len(result.trials)
        if not result.trials:
            continue

        click.echo(f"{block.citation}: {result.summary()}")
        for trial in result.disagreements:
            click.echo(f"  DISAGREED {trial.instruction_id:<12} "
                       f"rules={trial.expected}")
            click.echo(f"            {'':<12} llm  ={trial.chosen}")
        for trial in result.abstentions:
            click.echo(f"  abstained {trial.instruction_id:<12} {trial.rejected}")
        click.echo()

    stats = resolver.stats
    click.echo(f"calls {stats.calls} (cache hits {stats.cache_hits}), "
               f"chose {stats.resolved}, declined {stats.chose_none}, "
               f"rejected {stats.rejected}")
    for error in stats.errors[:5]:
        click.echo(f"  error: {error}")


@main.group()
def corpus() -> None:
    """Build and run a test corpus of adopted amending acts."""


@corpus.command("triples")
@click.argument("base_celex", nargs=-1, required=True)
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="Write the discovered triples here, one per line.")
def corpus_triples(base_celex: tuple[str, ...], out: Path | None) -> None:
    """Find amending acts whose published outcome can be checked.

    Give consolidated (sector 0) CELEX ids, e.g. 02003L0087.
    """
    from a2t.corpus.discover import find_triples

    found: list = []
    for base in base_celex:
        triples, rejected = find_triples(base)
        found.extend(triples)
        click.echo(f"{base}: {len(triples)} usable, {len(rejected)} rejected")
        for triple in triples:
            click.echo(f"  OK       {triple}")
        for item in rejected:
            detail = f" ({', '.join(item.acts)})" if item.acts else ""
            click.echo(f"  rejected {item.before} -> {item.after}: "
                       f"{item.reason}{detail}")
        click.echo()

    click.echo(f"{len(found)} usable triple(s) in total")
    if out and found:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(
            f"{t.base}\t{t.before}\t{t.amending}\t{t.after}" for t in found
        ) + "\n", encoding="utf-8")
        click.echo(f"wrote {out}")


@corpus.command("fetch")
@click.argument("celex", nargs=-1, required=True)
@click.option("--dir", "cache", type=click.Path(path_type=Path),
              default=Path("out/corpus/pdf"), show_default=True)
@click.option("--delay", type=float, default=2.0, show_default=True,
              help="Seconds between requests to EUR-Lex.")
def corpus_fetch(celex: tuple[str, ...], cache: Path, delay: float) -> None:
    """Download acts from EUR-Lex by CELEX."""
    from a2t.corpus.fetch import FetchError, Fetcher

    fetcher = Fetcher(cache_dir=cache, delay=delay)
    for item in celex:
        if fetcher.is_cached(item):
            click.echo(f"  cached {item}")
            continue
        try:
            path = fetcher.fetch(item)
            click.echo(f"  got    {item}  {path.stat().st_size:>9,} bytes")
        except FetchError as exc:
            click.echo(f"  FAILED {exc}")


@corpus.command("run")
@click.argument("pdfs", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--triples", type=click.Path(exists=True, path_type=Path), default=None,
              help="Triples file from `corpus triples`; enables ground-truth checking.")
@click.option("--pdf-dir", type=click.Path(path_type=Path),
              default=Path("out/corpus/pdf"), show_default=True)
@click.option("--cache", type=click.Path(path_type=Path), default=DEFAULT_CACHE,
              show_default=True)
@click.option("--report", type=click.Path(path_type=Path),
              default=Path("out/corpus/report.txt"), show_default=True)
@click.option("--classifier", type=click.Choice(["rules", "llm"]), default="rules",
              show_default=True, help="How instructions are interpreted.")
@click.option("--model", default=None, help="Requesty model id (or set A2T_MODEL).")
@click.option("--llm-cache", type=click.Path(path_type=Path),
              default=Path("out/llmcache"), show_default=True)
@click.option("--no-fallback", is_flag=True,
              help="Do not fall back to the grammar when the LLM is rejected.")
@click.option("--llm-resolve", is_flag=True,
              help="Offer unresolved targets to the LLM resolution tier.")
def corpus_run(pdfs: tuple[Path, ...], triples: Path | None, pdf_dir: Path,
               cache: Path, report: Path, classifier: str, model: str | None,
               llm_cache: Path, no_fallback: bool, llm_resolve: bool) -> None:
    """Run the pipeline over a corpus and rank what it could not handle."""
    from a2t.corpus.fetch import FetchError, Fetcher
    from a2t.corpus.run import run_document, summarise

    interpreter = None
    if classifier == "llm":
        from a2t.config import ConfigError, load_settings
        from a2t.parse.llm_classify import LlmClassifier
        from a2t.resolve.llm import LlmClient

        settings = load_settings(Path.cwd(), model=model)
        try:
            settings.require_key()
            settings.require_model()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        interpreter = LlmClassifier(
            LlmClient(settings, cache_dir=llm_cache),
            fall_back=not no_fallback,
        )
        click.echo(f"classifier: LLM ({settings.model}), "
                   f"grammar fallback {'off' if no_fallback else 'on'}\n")

    resolver_tier = None
    if llm_resolve:
        from a2t.config import ConfigError, load_settings
        from a2t.resolve.llm import LlmResolver, LlmClient

        settings = load_settings(Path.cwd(), model=model)
        try:
            settings.require_key()
            settings.require_model()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        resolver_tier = LlmResolver(LlmClient(settings, cache_dir=llm_cache))
        click.echo(f"resolution tier: LLM ({settings.model})\n")

    provider = EurlexBuilderProvider(cache_dir=cache)
    jobs: list[tuple[Path, str | None, str | None, str | None]] = [
        (p, None, None, None) for p in pdfs
    ]

    if triples:
        fetcher = Fetcher(cache_dir=pdf_dir)
        for line in triples.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            base, before, amending, after = line.split("\t")
            try:
                jobs.append((fetcher.fetch(amending), after, "3" + base[1:], before))
            except FetchError as exc:
                click.echo(f"  skipped {exc}")

    results = []
    for pdf, after_celex, base_celex, before_celex in jobs:
        truth = None
        if after_celex:
            try:
                retrieved = provider.fetch(after_celex)
                truth = assemble(retrieved.full_text_html, after_celex,
                                 retrieved.title)
            except Exception as exc:
                click.echo(f"  {pdf.name}: no ground truth ({exc})")
        outcome = run_document(pdf, provider, truth, base_celex, before_celex,
                               classifier=interpreter, llm_resolver=resolver_tier)
        results.append(outcome)
        status = "ok" if outcome.ok else "FAILED"
        detail = "; ".join(
            f"{a.citation}: {a.resolved}/{a.applicable} resolved"
            + (f", {a.agreed}/{a.compared} match published" if a.compared else "")
            + (f", ERROR {a.error}" if a.error else "")
            for a in outcome.acts
        ) or (outcome.error or "")
        click.echo(f"{pdf.name:<24} {status:<7} {detail}")

    text = summarise(results)
    if resolver_tier is not None:
        stats = resolver_tier.stats
        text += (f"\n\nLLM resolver: {stats.calls} calls "
                 f"({stats.cache_hits} cached), chose {stats.resolved}, "
                 f"declined {stats.chose_none}, rejected {stats.rejected}")
    if interpreter is not None:
        stats = interpreter.stats
        text += (f"\n\nLLM classifier: {stats.calls} calls "
                 f"({stats.cache_hits} cached), "
                 f"{stats.rejected_operation} bad operation, "
                 f"{stats.rejected_span} non-verbatim span, "
                 f"{stats.fell_back} fell back to the grammar")
    click.echo("\n" + text)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text + "\n", encoding="utf-8")
    click.echo(f"\nwrote {report}")


if __name__ == "__main__":
    main()
