"""Synthetic regressions for code-example preservation and DOCX title choice."""
import ast
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from notes.atlas_markdown_blocks import code_line_mask
from notes.atlas_yamlmini import extract_wikilinks
from notes import atlas_note_engine as notes
from pipeline import atlas_vfs_engine as vfs


class ContentBoundaryTests(unittest.TestCase):
    def test_indented_and_tab_code_are_not_links_or_rewritten(self):
        code = '    [[Old]]\r\n\r\n\t[example](Old.md)\r\n'
        text = code + '\r\nActual [[Old]]\r\n'
        self.assertEqual(extract_wikilinks(code), [])
        result = vfs._rewrite_prose(text, lambda s: s.replace('Old', 'New'))
        self.assertEqual(result, code + '\r\nActual [[New]]\r\n')

    def test_indent_cannot_interrupt_paragraph(self):
        text = 'Actual paragraph\n    [[Old]]\n'
        self.assertEqual(code_line_mask(text), [False, False])
        self.assertEqual(extract_wikilinks(text), ['Old'])
        self.assertIn('[[New]]', vfs._rewrite_prose(text, lambda s: s.replace('Old', 'New')))

    def test_list_container_distinguishes_prose_and_code(self):
        text = '- Item\n\n    Actual [[Old]]\n\n      [[Code]]\n'
        self.assertEqual(extract_wikilinks(text), ['Old'])
        self.assertEqual(vfs._rewrite_prose(text, lambda s: s.replace('Old', 'New').replace('Code', 'BAD')),
                         text.replace('Old', 'New'))

    def test_fence_in_list_and_quote_indented_code(self):
        text = '- ```md\n  [[Code]]\n  ```\n\n>     [[QuotedCode]]\n>\n> Actual [[Old]]\n'
        self.assertEqual(extract_wikilinks(text), ['Old'])
        result = vfs._rewrite_prose(text, lambda s: s.replace('Old', 'New').replace('Code', 'BAD'))
        self.assertEqual(result, text.replace('Old', 'New'))

    def test_fenced_quote_and_list_markers_are_literal_code(self):
        examples = ['```\n> literal\n[[Target]]\n```\n',
                    '> ```\n> > literal\n> [[Target]]\n> ```\n',
                    '- ```\n  > literal\n  [[Target]]\n  ```\n']
        for text in examples:
            with self.subTest(text=text):
                self.assertTrue(all(code_line_mask(text)))
                self.assertEqual(extract_wikilinks(text), [])
                self.assertEqual(vfs._rewrite_prose(text, lambda s: s.replace('Target', 'BAD')), text)

    def test_long_fence_not_closed_by_short_fence(self):
        text = '````md\n```\n[[Code]]\n````\n[[Old]]\n'
        self.assertEqual(extract_wikilinks(text), ['Old'])
        self.assertEqual(vfs._rewrite_prose(text, lambda s: s.replace('Old', 'New').replace('Code', 'BAD')), text.replace('Old', 'New'))

    def test_frozen_code_examples_do_not_block_real_move(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'Old.md').write_text('body')
            holder = root/'Frozen.md'
            holder.write_text('    [[Old]]\n\n\t[example](Old.md)\n')
            original = holder.read_bytes()
            with mock.patch.object(vfs, '_is_rel_frozen', side_effect=lambda _root, rel: 'frozen' if rel == 'Frozen.md' else None):
                notes._preflight_note_moves(root, {'Old.md': 'New.md'})
            self.assertEqual(holder.read_bytes(), original)
            self.assertTrue((root/'Old.md').exists())
            self.assertFalse((root/'New.md').exists())

    def test_frontmatter_reference_is_still_rewritten(self):
        text = '---\nsources:\n    - "[[Old]]"\n---\n\n    [[Code]]\n'
        result = vfs._rewrite_prose(text, lambda s: s.replace('Old', 'New').replace('Code', 'BAD'))
        self.assertEqual(result, text.replace('Old', 'New'))

    def test_docx_title_selection_without_optional_runtime_import(self):
        # Load the actual pure selection method without importing python-docx.
        # The real converter is separately exercised through fmt in a fresh pilot.
        tree = ast.parse((SCRIPTS/'converters/docx_converter.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DocxToMarkdownConverter')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'resolve_document_title')
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<actual-docx-title-method>', 'exec'), namespace)
        choose = namespace['resolve_document_title']
        def paragraph(style, text):
            return SimpleNamespace(style=SimpleNamespace(name=style), text=text)
        def selection(explicit, paragraphs):
            obj = SimpleNamespace(doc=SimpleNamespace(core_properties=SimpleNamespace(title=explicit), paragraphs=paragraphs), doc_slug='source-file', get_heading_level=lambda p: 1 if p.style.name == 'Heading 1' else 0)
            return choose(obj)
        cases = [('', [paragraph('Title', '正文标题')], '正文标题'),
                 ('Word Document', [paragraph('Heading 1', '岚舟记录仪操作手册')], '岚舟记录仪操作手册'),
                 ('真实元数据标题', [paragraph('Title', '正文标题')], '真实元数据标题'),
                 (' ', [], 'source-file'),
                 ('Word Document', [paragraph('Normal', '普通正文')], 'source-file')]
        for explicit, paragraphs, expected in cases:
            with self.subTest(explicit=explicit):
                self.assertEqual(selection(explicit, paragraphs), expected)


if __name__ == '__main__':
    unittest.main()
