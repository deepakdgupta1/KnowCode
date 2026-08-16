"""Tests for the reStructuredText dialect of the prose chunker.

RST diverges from markdown in three load-bearing ways, each exercised here:

* heading levels are assigned by **encounter order** of adornment styles, not by
  the adornment character (so ``=`` is not always level 1);
* code and tables are **indentation- and ruler-delimited** (``::``/``.. code``,
  grid borders, simple-table rulers), not fenced;
* there is **no YAML frontmatter** and the title comes from the first section.

Hermetic — no dependency on the sample corpus.
"""

from knowcode.indexing.prose_chunker import ProseChunker, RstDialect

D = RstDialect()


def _headings(text: str):
    return D.scan_headings(text.splitlines(), 1)


def _levels(text: str):
    return [(level, title) for _, level, title, *_ in _headings(text)]


# -- heading level assignment (the make-or-break difference) ----------------


def test_underline_headings_get_levels_by_encounter_order():
    text = "Title\n=====\n\nbody\n\nSection\n-------\n\nmid\n\nSub\n~~~\n\ndeep\n"
    assert _levels(text) == [(1, "Title"), (2, "Section"), (3, "Sub")]


def test_level_is_keyed_on_first_appearance_not_character():
    # '-' appears first => level 1; '=' appears later => level 2.
    text = "First\n-----\n\nbody\n\nSecond\n======\n\nmore\n"
    assert _levels(text) == [(1, "First"), (2, "Second")]


def test_overline_is_a_distinct_style_from_underline_only():
    text = "======\nPart\n======\n\nintro\n\nChapter\n=======\n\nbody\n"
    assert _levels(text) == [(1, "Part"), (2, "Chapter")]


def test_transition_rule_is_not_a_heading():
    text = "Intro paragraph.\n\n----\n\nMore text.\n"
    assert _headings(text) == []


def test_heading_inside_literal_block_is_ignored():
    text = "Real\n====\n\nExample::\n\n    Fake\n    ====\n\nDone.\n"
    assert [t for _, _, t, *_ in _headings(text)] == ["Real"]


# -- atomic block detection -------------------------------------------------


def test_code_block_directive_is_one_atomic_block():
    lines = [".. code-block:: c", "", "    int x = 1;", "    return x;"]
    blocks = D.blocks_in_span(lines, 1, len(lines))
    assert len(blocks) == 1
    assert blocks[0].kind == "code"
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 4)


def test_double_colon_literal_block_is_kept_atomic():
    lines = ["Example::", "", "    literal one", "    literal two"]
    code = [b for b in D.blocks_in_span(lines, 1, len(lines)) if b.kind == "code"]
    assert len(code) == 1
    assert (code[0].start_line, code[0].end_line) == (3, 4)


def test_grid_table_is_a_single_block():
    lines = [
        "+-----+-----+",
        "| a   | b   |",
        "+=====+=====+",
        "| 1   | 2   |",
        "+-----+-----+",
    ]
    blocks = D.blocks_in_span(lines, 1, len(lines))
    assert len(blocks) == 1 and blocks[0].kind == "table"
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 5)


def test_simple_table_is_a_single_block():
    lines = ["=====  =====", "  A      B", "=====  =====", "  1      2", "=====  ====="]
    blocks = D.blocks_in_span(lines, 1, len(lines))
    assert len(blocks) == 1 and blocks[0].kind == "table"
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 5)


def test_directive_with_body_is_kept_atomic():
    lines = [".. note::", "", "   Watch out for the locking order here.", "", "after"]
    blocks = D.blocks_in_span(lines, 1, len(lines))
    note = blocks[0]
    assert (note.start_line, note.end_line) == (1, 3)


# -- end-to-end through the chunker -----------------------------------------


def test_chunker_builds_parent_child_tree_for_rst():
    text = "Guide\n=====\n\nintro text\n\nSetup\n-----\n\ninstall steps here\n"
    chunks = ProseChunker(dialect=RstDialect()).chunk_text(text, "guide.rst")
    by_path = {c.heading_path: c for c in chunks}
    assert ("Guide",) in by_path
    assert ("Guide", "Setup") in by_path
    assert by_path[("Guide", "Setup")].parent_id == by_path[("Guide",)].section_id


def test_title_inferred_from_first_section_heading():
    text = "Kernel Docs\n===========\n\nwelcome\n"
    chunks = ProseChunker(dialect=RstDialect()).chunk_text(text, "path/index.rst")
    assert chunks[0].title == "Kernel Docs"
    assert chunks[0].context_header.startswith("Kernel Docs")


def test_line_ranges_reconstruct_rst_chunk_content():
    text = "Guide\n=====\n\nAlpha line one.\n\nAlpha line two.\n\nSetup\n-----\n\nBeta body.\n"
    chunks = ProseChunker(dialect=RstDialect()).chunk_text(text, "g.rst")
    src = text.splitlines()
    assert chunks
    for c in chunks:
        assert "\n".join(src[c.start_line - 1 : c.end_line]).strip("\n") == c.content


def test_chunk_file_autodetects_rst(tmp_path):
    p = tmp_path / "doc.rst"
    p.write_text("Topic\n=====\n\nthe body of the topic\n", encoding="utf-8")
    chunks = ProseChunker().chunk_file(p)  # no explicit dialect -> detect by suffix
    assert chunks
    assert chunks[0].heading_path == ("Topic",)
