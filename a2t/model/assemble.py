"""Build an `Act` tree from EUR-Lex consolidated HTML.

`eurlex-builder`'s Parquet output is the *coordinate* source but not a sufficient
structural one: its splitter handles single-letter points only (so `(aa)`–`(ai)` get
absorbed into the `(z)` unit) and does not split unnumbered subparagraphs — and since
its `normalize_string()` collapses all whitespace, those boundaries are unrecoverable
from the text afterwards. See PHASE0.md.

The source HTML, exposed as `works.parquet.full_text_html` when `store_raw_html` is
set, carries the full structure in a stable class vocabulary:

    div.eli-subdivision[id=art_10a]   article, with a machine-readable id
    p.title-article-norm              "Article 10a"
    div.eli-title                     article title
    div.norm > span.no-parag          numbered paragraph, key in the span ("1a.")
    p.norm                            subparagraph
    div.grid-container                point: column-1 is the marker, column-2 the text
    p.modref                          ▼M provenance markers

A `p.norm` appearing *after* a numbered paragraph, as a sibling rather than a child,
is a further subparagraph of that paragraph — this is how Article 10a(1a) gets its five
subparagraphs.
"""

from __future__ import annotations

import re

from lxml import html as LH

from a2t.model.tree import Act, Kind, Node


class AssemblyError(RuntimeError):
    """The HTML could not be turned into a structured act."""

ARTICLE_NUMBER = re.compile(r"^Article\s+(?P<key>[0-9]+[a-z]*(?:\s*\([^)]*\))?)\s*$", re.I)
POINT_MARKER = re.compile(r"^\(?(?P<key>[a-z]{1,3}|[ivx]{1,5}|\d{1,2})\)?\.?$", re.I)
# Keys include hyphenated forms: the ETS Directive has Article 12(3-a)..(3-e).
PARAGRAPH_KEY = re.compile(r"^(?P<key>\d+(?:-?[a-z]+)?)\.?$", re.I)
CHAPTER_NUMBER = re.compile(r"^CHAPTER\s+(?P<key>[IVXLC]+[a-z]?)\s*$", re.I)

# Consolidation markers: "►M9 system ◄" means M9 inserted "system".
# Strip the marker glyphs, keep the text they wrap.
OPEN_MARKER = re.compile(r"►\s*[A-Z]+\d*\s*")
CLOSE_MARKER = re.compile(r"\s*◄")
MOD_MARKER = re.compile(r"^\s*[▼►]\s*[A-Z]+\d*\s*$")


def _has_class(el, name: str) -> bool:
    return name in (el.get("class") or "").split()


def clean_text(el) -> str:
    """Text content with consolidation markers removed and whitespace collapsed."""
    parts: list[str] = []
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        if _has_class(node, "modref"):
            continue
        if node.text:
            parts.append(node.text)
        # tail text belongs to the parent's flow
        if node is not el and node.tail:
            parts.append(node.tail)
    text = " ".join(parts)
    text = OPEN_MARKER.sub("", text)
    text = CLOSE_MARKER.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    # "a ►M9 Union ◄ -wide system" -> "a Union-wide system": removing the marker
    # leaves a space that was never in the legislation.
    text = re.sub(r"(\w) -(\w)", r"\1-\2", text)
    return text


def _direct_grid_containers(el) -> list:
    """Top-level point containers within `el`, not those nested inside other points."""
    out = []
    for candidate in el.iter():
        if not isinstance(candidate.tag, str) or not _has_class(candidate, "grid-container"):
            continue
        parent = candidate.getparent()
        nested = False
        while parent is not None and parent is not el:
            if _has_class(parent, "grid-container"):
                nested = True
                break
            parent = parent.getparent()
        if not nested:
            out.append(candidate)
    return out


def _point_parts(container) -> tuple[str | None, str]:
    """(marker, text) for a grid-list point."""
    col1 = container.find_class("grid-list-column-1")
    col2 = container.find_class("grid-list-column-2")
    marker = clean_text(col1[0]) if col1 else ""
    match = POINT_MARKER.match(marker)
    key = match.group("key") if match else (marker.strip("().") or None)

    if not col2:
        return key, ""
    # A point's text excludes any sub-points nested inside it.
    body = col2[0]
    nested = _direct_grid_containers(body)
    if not nested:
        return key, clean_text(body)
    for sub in nested:
        sub.getparent().remove(sub)
    text = clean_text(body)
    for sub in nested:
        body.append(sub)
    return key, text


def _add_points(parent: Node, container, kind: Kind = Kind.POINT) -> None:
    for grid in _direct_grid_containers(container):
        key, text = _point_parts(grid)
        point = parent.add(Node(kind, key=key, text=text))
        col2 = grid.find_class("grid-list-column-2")
        if col2:
            sub_kind = Kind.SUBPOINT if kind is Kind.POINT else Kind.SUBPOINT
            _add_points(point, col2[0], sub_kind)


def _within_point(el, stop) -> bool:
    """Is `el` inside a point, rather than a sibling of one?"""
    parent = el.getparent()
    while parent is not None and parent is not stop:
        if _has_class(parent, "grid-container"):
            return True
        parent = parent.getparent()
    return False


def _add_body(target: Node, el) -> None:
    """Add the subparagraphs and points inside `el` to `target`, in document order.

    Both in one pass, because their order carries meaning: Article 10(3) opens with a
    subparagraph, lists fourteen points, and closes with four more subparagraphs that
    refer back to them ("For the purposes of point (a) …"). Collected in two passes
    the points end up after every subparagraph, and the act refers forward to points
    the reader has not reached.

    A point's text is itself marked up as `p.norm`, so a `p.norm` inside a
    `grid-container` belongs to the point holding it and is not a subparagraph of its
    own — otherwise every point would be added twice, once without its marker.
    `lxml`'s `iter()` is pre-order, which is document order.
    """
    added = False
    for child in el.iter():
        if not isinstance(child.tag, str) or _has_class(child, "modref"):
            continue
        if _within_point(child, el):
            continue  # inside a point, which owns everything under it

        if _has_class(child, "grid-container"):
            key, text = _point_parts(child)
            point = target.add(Node(Kind.POINT, key=key, text=text))
            col2 = child.find_class("grid-list-column-2")
            if col2:
                _add_points(point, col2[0], Kind.SUBPOINT)
            added = True
        elif child.tag == "p" and _has_class(child, "norm"):
            text = clean_text(child)
            if text:
                target.add(Node(Kind.SUBPARAGRAPH, text=text))
                added = True

    if not added:
        text = clean_text(el)
        if text:
            target.add(Node(Kind.SUBPARAGRAPH, text=text))


def _build_article(subdivision) -> Node | None:
    """Article from a wrapped `eli-subdivision` (the current EUR-Lex template)."""
    titles = [
        el for el in subdivision.iter()
        if isinstance(el.tag, str) and _has_class(el, "title-article-norm")
    ]
    if not titles:
        return None
    match = ARTICLE_NUMBER.match(clean_text(titles[0]))
    if match is None:
        return None

    article = Node(
        Kind.ARTICLE,
        key=match.group("key").strip(),
        source_id=subdivision.get("id"),
    )
    heading = subdivision.find_class("eli-title")
    if heading:
        article.title = clean_text(heading[0]) or None

    blocks = [
        child for child in subdivision
        if isinstance(child.tag, str)
        and child not in titles
        and not _has_class(child, "eli-title")
    ]
    _populate_article(article, blocks)
    return article


def _populate_article(article: Node, blocks) -> Node:
    """Attach paragraphs, subparagraphs and points to an article.

    Shared by both templates: the element vocabulary inside an article is the same,
    only the way articles are delimited differs.
    """
    current: Node = article  # where loose subparagraphs and points attach
    provenance: str | None = None

    for child in blocks:
        if not isinstance(child.tag, str):
            continue

        if _has_class(child, "stitle-article-norm"):
            if article.title is None:
                article.title = clean_text(child) or None
            continue

        if _has_class(child, "modref"):
            provenance = clean_text(child) or MOD_MARKER.pattern
            continue

        no_parag = [
            s for s in child.iter()
            if isinstance(s.tag, str) and _has_class(s, "no-parag")
        ]

        if child.tag == "div" and _has_class(child, "norm") and no_parag:
            key_match = PARAGRAPH_KEY.match(clean_text(no_parag[0]))
            paragraph = article.add(
                Node(
                    Kind.PARAGRAPH,
                    key=key_match.group("key") if key_match else None,
                    provenance=provenance,
                )
            )
            body = child.find_class("norm inline-element")
            source = body[0] if body else child
            _add_body(paragraph, source)
            current = paragraph
            continue

        if _has_class(child, "grid-container"):
            _add_points(current, child.getparent() if False else _wrap(child))
            continue

        if child.tag == "p" and _has_class(child, "norm"):
            text = clean_text(child)
            if text:
                current.add(Node(Kind.SUBPARAGRAPH, text=text, provenance=provenance))
            continue

        # Any other block that carries points (e.g. a bare div.list of points).
        if _direct_grid_containers(child):
            _add_points(current, child)

    return article


def _assemble_flat(doc, act: Act) -> None:
    """Assemble from the older EUR-Lex template.

    Consolidations before roughly 2024 carry no `eli-subdivision` wrappers: headings
    and content are flat siblings, and an article runs from its `title-article-norm`
    heading to the next heading or chapter division. Everything inside is unchanged,
    so only the delimitation differs.
    """
    body = doc.find("body") if doc.find("body") is not None else doc
    chapter: Node | None = None
    article: Node | None = None
    blocks: list = []

    def flush() -> None:
        if article is not None:
            _populate_article(article, blocks)

    for element in body:
        if not isinstance(element.tag, str):
            continue

        if _has_class(element, "title-division-1"):
            flush()
            article, blocks = None, []
            match = CHAPTER_NUMBER.match(clean_text(element))
            chapter = act.root.add(
                Node(Kind.CHAPTER, key=match.group("key") if match else None)
            )
            continue

        if _has_class(element, "title-division-2"):
            if chapter is not None and article is None:
                chapter.title = clean_text(element) or None
                continue

        if _has_class(element, "title-article-norm"):
            flush()
            blocks = []
            match = ARTICLE_NUMBER.match(clean_text(element))
            if match is None:
                article = None
                continue
            article = Node(Kind.ARTICLE, key=match.group("key").strip())
            (chapter or act.root).add(article)
            continue

        if article is not None:
            blocks.append(element)

    flush()


# ------------------------------------------------------- the act as adopted (sector 3)
#
# An act that has never been amended has no consolidation, so the text in force is the
# act as adopted. That document uses a parallel vocabulary to the consolidated one —
# `oj-ti-art` for "Article 4", `oj-sti-art` for its title, `oj-normal` for every block
# of text, a two-cell table for every point — and keeps the `eli-subdivision` wrappers.
# Verified across thirteen acts from 2005 to 2023. Acts adopted before roughly 2005 use
# one of two older templates and are not read here; that costs nothing, because an act
# old enough to carry them has been amended since and so has a consolidation to work
# from instead.

# "1.   In order to reach …" — the number opens the paragraph's first block of text
# rather than sitting in an element of its own, which is the only structural difference
# from the consolidated template that the vocabulary does not account for.
OJ_PARAGRAPH_NUMBER = re.compile(r"^(?P<key>\d+(?:-?[a-z]+)?)\.\s+(?P<rest>\S.*)$", re.S)


def _oj_point_cells(table) -> list:
    """The marker cell and body cell of a point, or [] if this is not one.

    Every point inside an article is one row of two cells — all 665 of them across
    Regulation 2021/1119, Regulation 2018/1999 and Directive 2010/75/EU — so a table
    shaped otherwise is a data table and not a point at all.
    """
    rows = table.xpath("./tbody/tr") or table.xpath("./tr")
    if len(rows) != 1:
        return []
    cells = [c for c in rows[0] if isinstance(c.tag, str) and c.tag == "td"]
    return cells if len(cells) == 2 else []


def _direct_point_tables(el) -> list:
    """Point tables under `el` that no other point already owns."""
    out = []
    for table in el.iter("table"):
        parent = table.getparent()
        nested = False
        while parent is not None and parent is not el:
            if parent.tag == "table":
                nested = True
                break
            parent = parent.getparent()
        if not nested and _oj_point_cells(table):
            out.append(table)
    return out


def _oj_point_parts(table) -> tuple[str | None, str]:
    """(marker, text) for a point, excluding any sub-points nested inside it."""
    cells = _oj_point_cells(table)
    if not cells:
        return None, ""
    marker = clean_text(cells[0])
    match = POINT_MARKER.match(marker)
    key = match.group("key") if match else (marker.strip("().") or None)

    body = cells[1]
    nested = _direct_point_tables(body)
    if not nested:
        return key, clean_text(body)
    for sub in nested:
        sub.getparent().remove(sub)
    text = clean_text(body)
    for sub in nested:
        body.append(sub)
    return key, text


def _add_oj_point(parent: Node, table, kind: Kind) -> None:
    key, text = _oj_point_parts(table)
    point = parent.add(Node(kind, key=key, text=text))
    cells = _oj_point_cells(table)
    if cells:
        for nested in _direct_point_tables(cells[1]):
            _add_oj_point(point, nested, Kind.SUBPOINT)


def _oj_paragraph_key(container) -> str | None:
    """The number the paragraph's first block of text opens with, if any."""
    for child in container:
        if (isinstance(child.tag, str) and child.tag == "p"
                and _has_class(child, "oj-normal")):
            match = OJ_PARAGRAPH_NUMBER.match(clean_text(child))
            return match.group("key") if match else None
    return None


def _add_oj_body(target: Node, container, strip_number: bool = False) -> None:
    """Add the subparagraphs and points inside `container` to `target`.

    In document order and in one pass, for the reason `_add_body` gives: an article
    that lists points and then refers back to them reads wrongly if the two are
    collected separately.
    """
    opening = strip_number
    for child in container:
        if not isinstance(child.tag, str):
            continue
        if child.tag == "p" and _has_class(child, "oj-normal"):
            text = clean_text(child)
            if opening:
                match = OJ_PARAGRAPH_NUMBER.match(text)
                if match:
                    # The number belongs to the paragraph, which carries it as its key
                    # the way the consolidated template's `span.no-parag` does. Leaving
                    # it in the text would print it twice.
                    text = match.group("rest").strip()
                opening = False
            if text:
                target.add(Node(Kind.SUBPARAGRAPH, text=text))
        elif child.tag == "table" and _oj_point_cells(child):
            _add_oj_point(target, child, Kind.POINT)


def _populate_article_oj(article: Node, blocks) -> None:
    current: Node = article  # where loose subparagraphs and points attach
    for child in blocks:
        if not isinstance(child.tag, str):
            continue

        # An unclassed div is one numbered paragraph, holding its own blocks of text.
        if child.tag == "div" and not (child.get("class") or "").strip():
            paragraph = article.add(
                Node(Kind.PARAGRAPH, key=_oj_paragraph_key(child))
            )
            _add_oj_body(paragraph, child, strip_number=True)
            current = paragraph
            continue

        if child.tag == "p" and _has_class(child, "oj-normal"):
            text = clean_text(child)
            if text:
                current.add(Node(Kind.SUBPARAGRAPH, text=text))
            continue

        if child.tag == "table" and _oj_point_cells(child):
            _add_oj_point(current, child, Kind.POINT)


def _build_article_oj(subdivision) -> Node | None:
    """Article from an `art_*` subdivision of the act as adopted.

    The heading has to be a *direct* child. An amending act quotes whole articles of
    the act it amends inside a point's cell, headings and all: Directive 2008/101/EC
    carries sixteen `oj-ti-art` headings for the four articles it actually has, and
    Regulation 2021/1119 seventeen for fourteen. Enumerating articles by their headings
    would invent them out of quoted text, so they are found by their subdivision — whose
    id is `art_*` — and read from the top down.
    """
    heads = [c for c in subdivision
             if isinstance(c.tag, str) and _has_class(c, "oj-ti-art")]
    if not heads:
        return None
    match = ARTICLE_NUMBER.match(clean_text(heads[0]))
    if match is None:
        return None

    article = Node(Kind.ARTICLE, key=match.group("key").strip(),
                   source_id=subdivision.get("id"))
    titles = [c for c in subdivision
              if isinstance(c.tag, str) and _has_class(c, "eli-title")]
    if titles:
        article.title = clean_text(titles[0]) or None

    blocks = [c for c in subdivision if isinstance(c.tag, str)
              and c not in heads and c not in titles]
    _populate_article_oj(article, blocks)
    return article


def _assemble_as_adopted(doc, act: Act) -> None:
    """Assemble an act as adopted, chapters and articles in document order."""
    ordered = doc.xpath(
        "//p[contains(@class,'oj-ti-section-1')]"
        " | //p[contains(@class,'oj-ti-section-2')]"
        " | //div[contains(@class,'eli-subdivision')]"
    )

    chapter: Node | None = None
    for element in ordered:
        classes = (element.get("class") or "").split()
        if "oj-ti-section-1" in classes:
            match = CHAPTER_NUMBER.match(clean_text(element))
            # Only a chapter heading opens a chapter; `oj-ti-section-1` also marks the
            # headings of annexes and of the parts a recital block is divided into.
            chapter = (
                act.root.add(Node(Kind.CHAPTER, key=match.group("key")))
                if match else None
            )
            continue
        if "oj-ti-section-2" in classes:
            if chapter is not None and not chapter.children:
                chapter.title = clean_text(element) or None
            continue
        if not (element.get("id") or "").startswith("art_"):
            continue
        article = _build_article_oj(element)
        if article is not None:
            (chapter or act.root).add(article)


def _wrap(el):
    """Adapter so a lone grid-container can be passed where a container is expected."""
    holder = LH.Element("div")
    parent, index = el.getparent(), None
    if parent is not None:
        index = parent.index(el)
    holder.append(el)
    if parent is not None and index is not None:
        parent.insert(index, holder)
    return holder


def _is_leaf_subdivision(element) -> bool:
    return _has_class(element, "eli-subdivision") and not any(
        isinstance(d.tag, str) and _has_class(d, "eli-subdivision")
        for d in element.iterdescendants()
    )


def _article_subdivisions(doc) -> list:
    """Leaf `eli-subdivision` elements — one per article."""
    return [
        sub
        for sub in doc.find_class("eli-subdivision")
        if not any(
            isinstance(d.tag, str) and _has_class(d, "eli-subdivision")
            for d in sub.iterdescendants()
        )
    ]


def assemble(full_text_html: str, celex: str, title: str,
             version_date: str | None = None) -> Act:
    """Build an `Act` from EUR-Lex consolidated HTML."""
    doc = LH.fromstring(full_text_html)
    act = Act(celex=celex, title=title, version_date=version_date)
    # The act's own title is amendable ("the title is replaced by the
    # following"), so the root carries it like any other heading.
    act.root.title = title

    # The act as adopted, rather than a consolidation of it. Probed before the others
    # because it keeps the `eli-subdivision` wrappers but none of their contents.
    if doc.find_class("oj-ti-art"):
        _assemble_as_adopted(doc, act)
        if not act.articles:
            raise AssemblyError(
                f"{celex}: no articles found. The source is the act as adopted "
                f"({len(doc.find_class('oj-ti-art'))} article headings) but none sits "
                f"in an art_* subdivision."
            )
        return act

    if not _article_subdivisions(doc):
        _assemble_flat(doc, act)
        if not act.articles:
            raise AssemblyError(f"{celex}: no articles found in either template")
        return act

    # Leaf-ness is recomputed per element rather than compared against a prebuilt
    # set: lxml creates element proxies on demand, so `id()` is not stable across
    # two traversals of the same tree.

    # Chapters and articles interleave in document order:
    #   p.title-division-1  "CHAPTER III"
    #   p.title-division-2  "STATIONARY INSTALLATIONS"   <- the amendable title
    #   div.eli-subdivision Article 3h ...
    ordered = doc.xpath(
        "//p[contains(@class,'title-division-1')]"
        " | //p[contains(@class,'title-division-2')]"
        " | //div[contains(@class,'eli-subdivision')]"
    )

    chapter: Node | None = None
    pending_key: str | None = None

    for element in ordered:
        classes = (element.get("class") or "").split()
        if "title-division-1" in classes:
            match = CHAPTER_NUMBER.match(clean_text(element))
            pending_key = match.group("key") if match else None
            chapter = act.root.add(Node(Kind.CHAPTER, key=pending_key))
            continue
        if "title-division-2" in classes:
            if chapter is not None:
                chapter.title = clean_text(element) or None
            continue
        if not _is_leaf_subdivision(element):
            continue
        article = _build_article(element)
        if article is not None:
            (chapter or act.root).add(article)

    if not act.articles:
        # An act with no articles is an assembly failure, not an empty act. Older
        # EUR-Lex consolidations use a different template with no `eli-subdivision`
        # wrappers; returning an empty tree makes every instruction look unresolvable
        # and misattributes a retrieval problem to the grammar.
        raise AssemblyError(
            f"{celex}: no articles found. The source has "
            f"{full_text_html.count('title-article-norm')} article headings but "
            f"{full_text_html.count('eli-subdivision')} eli-subdivision wrappers — "
            f"this is probably an older HTML template."
        )
    return act
