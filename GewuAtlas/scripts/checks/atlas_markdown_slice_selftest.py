"""Synthetic Markdown parsing regressions; no vault or runtime access."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converters.chunk_window import extract_sections_and_tables, split_chunks_by_section
from notes.atlas_note_engine import _split_markdown_sections
from pipeline.atlas_slicer_engine import _parse_heading_ast
from pipeline.atlas_slice_planner import plan_adaptive


class MarkdownSliceTests(unittest.TestCase):
    def assert_parsers(self, source, headings):
        elements = extract_sections_and_tables(source)
        self.assertEqual([e["title"] for e in elements if e["type"] == "heading"], headings)
        self.assertEqual("".join(e["content"] for e in elements), source)
        plan = plan_adaptive(source, "Root")
        self.assertEqual([h["title"] for h in plan["headings"]], headings)
        ast = _parse_heading_ast(source, "Root")
        def titles(node):
            return [title for child in node.children for title in [child.title, *titles(child)]]
        self.assertEqual(titles(ast), headings)
        sections = _split_markdown_sections(source, "Root", 3)
        self.assertEqual([title for title, _ in sections if title != "Root-intro" and not title.endswith("导言")], headings or ["Root"])
        return elements, ast, sections

    def test_long_fence_does_not_close_on_shorter_marker(self):
        source = "# A\n\n````python\n```\n# false\n| fake | row |\n````\n\n## B\n"
        elements, _, _ = self.assert_parsers(source, ["A", "B"])
        self.assertFalse(any(e["type"] == "table" for e in elements))
        self.assertEqual(sum(e["type"] == "code" for e in elements), 1)

    def test_tilde_fence_and_closing_info_are_not_headings(self):
        source = "# A\n~~~lang\n# false1\n~~~not-close\n# false2\n~~~~\n## B\n"
        self.assert_parsers(source, ["A", "B"])

    def test_indented_and_tab_code_keep_exact_text(self):
        source = "    # code1\n    | fake | row |\n\n\t# code2\n\n# Real\n"
        elements, ast, sections = self.assert_parsers(source, ["Real"])
        self.assertFalse(any(e["type"] == "table" for e in elements))
        self.assertTrue(ast.full_markdown().startswith("    # code1"))
        self.assertTrue(sections[0][1].startswith("    # code1"))

    def test_indented_fence_literal_does_not_hide_next_heading(self):
        self.assert_parsers("    ```\n    # literal\n\n# Real\n", ["Real"])

    def test_unclosed_fence_protects_until_eof(self):
        self.assert_parsers("# A\n```\n# false\n", ["A"])

    def test_three_space_heading_and_real_table_still_work(self):
        elements, _, _ = self.assert_parsers("   # Real\n\n| a | b |\n| - | - |\n| 1 | 2 |\n", ["Real"])
        self.assertTrue(any(e["type"] == "table" for e in elements))

    def test_window_chunks_preserve_bytes_and_atomic_code(self):
        code = "````\n" + "# fake\n" * 60 + "```\n# still-fake\n````\n"
        source = "# A\n" + "a" * 900 + "\n" + code + "# B\nshort\n"
        chunks = split_chunks_by_section(source, min_cut=50, max_cut=200)
        self.assertEqual("".join(c["content"] for c in chunks), source)
        self.assertTrue(any(code in c["content"] for c in chunks))

    def test_indented_paragraph_continuation_is_not_heading_or_table(self):
        source = "ordinary paragraph\n    # continuation\n    | not | table |\n\n# Real\n"
        elements, _, _ = self.assert_parsers(source, ["Real"])
        self.assertFalse(any(e["type"] in ("table", "code") for e in elements))

    def test_no_heading_indented_code_preserves_leading_spaces(self):
        source = "    print(1)\n    print(2)\n"
        _, ast, sections = self.assert_parsers(source, [])
        self.assertEqual(ast.full_markdown(), source.rstrip("\n"))
        self.assertEqual(sections[0][1], source.rstrip("\n"))


    def test_sibling_sections_pack_toward_target(self):
        source = "\n\n".join("## Section" + str(i) + "\n" + str(i)*600 for i in range(5))
        plan = plan_adaptive(source, "Root")
        self.assertLess(len(plan["cards"]), 5)
        self.assertTrue(any(c["is_bundle"] for c in plan["cards"]))
        for i in range(5):
            self.assertEqual(sum(c["content"].count(str(i)*600) for c in plan["cards"]), 1)
        self.assertTrue(all(not c["budget_exceeded"] for c in plan["cards"]))

    def test_headingless_and_long_leaf_split_without_losing_paragraphs(self):
        paragraphs = [str(i)*600 for i in range(8)]
        for heading in ["", "# Long leaf\n\n"]:
            plan = plan_adaptive(heading + "\n\n".join(paragraphs), "Root", 300, 4000, 2000)
            self.assertGreater(len(plan["cards"]), 1)
            for paragraph in paragraphs:
                self.assertEqual(sum(c["content"].count(paragraph) for c in plan["cards"]), 1)
            self.assertTrue(all(len(c["content"]) <= 4000 for c in plan["cards"]))

    def test_preamble_overview_and_appendix_calibration(self):
        source = "PREAMBLE "*20 + "\n\n# Main\n\n" + "OVERVIEW "*20 + "\n\n## Child\n\n" + "CHILD "*200 + "\n\n## 附录 A\n\nAPPENDIX"
        plan = plan_adaptive(source, "Root", 100, 500, 300)
        self.assertTrue(any(c.get("is_preamble") for c in plan["cards"]))
        self.assertTrue(any(c.get("is_lead_overview") for c in plan["cards"]))
        appendix = next(h for h in plan["headings"] if h["title"] == "附录 A")
        self.assertEqual(appendix["level"], 1)
        self.assertIsNone(appendix["parent"])
        joined = "\n".join(c["content"] for c in plan["cards"])
        for marker in ["PREAMBLE", "OVERVIEW", "CHILD", "APPENDIX"]:
            self.assertEqual(joined.count(marker), source.count(marker))

    def test_oversize_code_and_table_stay_whole_and_report_budget(self):
        code = "````python\n" + "# hidden heading\n"*100 + "```\n# still code\n````"
        table = "| a | b |\n| --- | --- |\n" + "| VALUE | VALUE |\n"*100
        for unit in [code, table, "x"*5000]:
            plan = plan_adaptive(unit, "Root", 100, 500, 300)
            matches = [c for c in plan["cards"] if unit.rstrip("\n") in c["content"]]
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0]["budget_exceeded"])
        self.assertEqual(plan_adaptive(code, "Root")["headings"], [])

    def test_paragraph_pack_flushes_before_budget_overflow(self):
        source = "# Leaf\n\n" + "a"*1900 + "\n\n" + "b"*3000
        cards = plan_adaptive(source, "Root", 300, 4000, 2000)["cards"]
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(len(c["content"]) <= 4000 for c in cards))

    def test_code_preamble_keeps_exact_indentation(self):
        prefix = "    print(1)\n    print(2)"
        cards = plan_adaptive(prefix + "\n\n# Main\n\nBody", "Root")["cards"]
        self.assertEqual(sum(c["content"].count(prefix) for c in cards), 1)
        self.assertTrue(cards[0]["has_code"])

    def test_empty_source_and_indented_code_keep_semantics(self):
        self.assertEqual(plan_adaptive("\n\n", "Root")["cards"], [])
        source = "    print(1)\n    print(2)\n"
        self.assertEqual(plan_adaptive(source, "Root")["cards"][0]["content"], source.rstrip("\n"))


if __name__ == "__main__":
    unittest.main()
