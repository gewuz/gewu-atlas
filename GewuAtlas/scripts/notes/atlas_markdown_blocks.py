"""Byte-preserving block-code classification shared by Atlas Markdown readers.

Only classify block code here. Inline backticks are handled by the caller.
Indentation is measured after blockquote/list containers; indented lines cannot
interrupt an open paragraph. Fences retain their opening character and length.
"""
from __future__ import annotations

import re
from typing import List


def code_line_mask(text: str) -> List[bool]:
    mask: List[bool] = []
    fence = None
    fence_base = fence_quote = 0
    indented = False
    paragraph = False
    list_indents: List[int] = []
    quote_depth = 0
    after_blank = True
    for original in text.splitlines(keepends=True):
        line = original.expandtabs(4).rstrip("\r\n")
        if fence is not None:
            # Inside a fence, new '>' or list markers are literal code. Only
            # remove the containers that existed at the opening delimiter.
            content = line
            in_container = True
            for _ in range(fence_quote):
                container = re.match(r"^ {0,3}> ?", content)
                if not container:
                    in_container = False
                    break
                content = content[container.end():]
            if in_container and (not content.strip() or len(content) - len(content.lstrip(" ")) >= fence_base):
                relative = content[fence_base:]
                char, length = fence
                mask.append(True)
                if re.match(r"^ {0,3}" + re.escape(char) + "{" + str(length) + r",}[ \t]*$", relative):
                    fence = None
                    paragraph = False
                after_blank = not content.strip()
                continue
            fence = None
            paragraph = False
        depth = 0
        while True:
            quote = re.match(r"^ {0,3}> ?", line)
            if not quote:
                break
            depth += 1
            line = line[quote.end():]
        if depth != quote_depth:
            fence = None
            indented = paragraph = False
            list_indents = []
            after_blank = True
            quote_depth = depth

        blank = not line.strip()
        indent = len(line) - len(line.lstrip(" "))
        # A list's content indentation is part of its container, not code.
        if not blank:
            while list_indents and indent < list_indents[-1]:
                if not after_blank and paragraph and indent < list_indents[0]:
                    break  # Lazy paragraph continuation in the current list.
                list_indents.pop()
        base = list_indents[-1] if list_indents and indent >= list_indents[-1] else 0
        relative = line[base:]

        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", relative)
        if marker and (marker.group(1)[0] != "`" or "`" not in marker.group(2)):
            fence = (marker.group(1)[0], len(marker.group(1)))
            fence_base, fence_quote = base, depth
            indented = paragraph = False
            mask.append(True)
            after_blank = False
            continue

        if indented and not blank and len(relative) - len(relative.lstrip(" ")) < 4:
            indented = False
        list_match = re.match(r"^( {0,3})(?:[-+*]|[0-9]{1,9}[.)])([ ]+|$)(.*)$", relative)
        if list_match and not indented:
            padding = len(list_match.group(2))
            content_start = list_match.start(3)
            if padding > 4:
                content_start -= padding - 1
            new_base = base + content_start
            list_indents.append(new_base)
            relative = line[new_base:]
            # A new item starts a block; its body may itself be indented code.
            paragraph = False
            indented = False
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", relative)
            if marker and (marker.group(1)[0] != "`" or "`" not in marker.group(2)):
                fence = (marker.group(1)[0], len(marker.group(1)))
                fence_base, fence_quote = new_base, depth
                mask.append(True)
                after_blank = False
                continue

        if blank:
            mask.append(indented)
            paragraph = False
            after_blank = True
            continue
        relative_indent = len(relative) - len(relative.lstrip(" "))
        if relative_indent >= 4 and (indented or not paragraph):
            mask.append(True)
            indented = True
            after_blank = False
            continue
        indented = False
        mask.append(False)
        # These block forms end/intercept paragraphs; a following indented
        # block does not need an extra blank line after them.
        structural = bool(re.match(r"^ {0,3}(?:#{1,6}(?:\s|$)|(?:-{3,}|_{3,}|\*{3,})\s*$|(?:=+|-+)\s*$)", relative))
        paragraph = not structural
        after_blank = False
    return mask
