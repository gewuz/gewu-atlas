# Obsidian Flavored Markdown Reference

> Source: `obsidianmd/obsidian-skills` (`obsidian-markdown`)

Create and edit valid Obsidian Flavored Markdown. Obsidian extends CommonMark and GitHub Flavored Markdown (GFM) with wikilinks, embeds, callouts, properties, comments, and other syntax. Standard Markdown (headings, bold, italic, lists, quotes, code blocks, tables) is assumed knowledge.

## When to Use

- Creating or modifying Obsidian notes (`.md` files)
- Implementing Obsidian wikilinks, internal links, and block references
- Adding Obsidian embeds (`![[...]]`) for notes, images, PDFs, or sections
- Formatting Obsidian callouts (`> [!note]`)
- Structuring note frontmatter and properties (YAML)
- Managing inline or nested tags (`#tag/subtag`)
- Writing math, highlights, footnotes, or Mermaid diagrams with Obsidian internal links

## Workflow: Creating an Obsidian Note

1. Add frontmatter with properties (`title`, `tags`, `aliases`) at the top of the file.
2. Write content using standard Markdown for structure, plus Obsidian-specific syntax.
3. Link related notes using wikilinks (`[[Note]]`) for internal vault connections, or standard Markdown links (`[text](url)`) for external URLs only.
4. Embed content from other notes, images, or PDFs using the `![[embed]]` syntax.
5. Add callouts for highlighted information using `> [!type]` syntax.
6. Verify the note structure and syntax.

## Internal Links (Wikilinks)

- `[[Note Name]]` - Link to note
- `[[Note Name|Display Text]]` - Custom display text
- `[[Note Name#Heading]]` - Link to heading
- `[[Note Name#^block-id]]` - Link to block
- `[[#Heading in same note]]` - Same-note heading link

Define a block ID by appending `^block-id` to any paragraph:

```markdown
This paragraph can be linked to. ^my-block-id
```

For lists and quotes, place the block ID on a separate line after the block:

```markdown
> A quote block
^quote-id
```

## Embeds

Prefix any wikilink with `!` to embed its content inline:

- `![[Note Name]]` - Embed full note
- `![[Note Name#Heading]]` - Embed section
- `![[image.png]]` - Embed image
- `![[image.png|300]]` - Embed image with specific width
- `![[document.pdf#page=3]]` - Embed PDF page

## Callouts

Basic callout syntax:

```markdown
> [!note]
> Basic callout.

> [!warning] Custom Title
> Callout with a custom title.

> [!faq]- Collapsed by default
> Foldable callout (- collapsed, + expanded).
```

Common callout types: `note`, `tip`, `warning`, `info`, `example`, `quote`, `bug`, `danger`, `success`, `failure`, `question`, `abstract`, `todo`.

## Properties (Frontmatter)

YAML frontmatter at the very top of the file:

```yaml
---
title: My Note
date: 2026-01-15
tags:
  - project
  - active
aliases:
  - Alternative Name
cssclasses:
  - custom-class
---
```

Default properties:
- `tags`: Searchable labels (can also be inline `#tag` or `#nested/tag`).
- `aliases`: Alternative note names for quick link suggestions.
- `cssclasses`: CSS classes for custom styling.

## Tags

- `#tag` - Inline tag
- `#nested/tag` - Nested tag with hierarchy
- Can contain letters, numbers (cannot be the first character), underscores, hyphens, and forward slashes.

## Obsidian-Specific Formatting

- Highlights: `==Highlighted text==`
- Comments (hidden in reading view): `%%hidden text%%` or multiline `%%\nhidden\n%%`
- Math (LaTeX): Inline `$e^{i\pi} + 1 = 0$` or block `$$\frac{a}{b} = c$$`
- Mermaid internal links: Add `class NodeName internal-link;` inside Mermaid blocks.
- Footnotes: `[^1]` with footnote definition `[^1]: Footnote content.` or inline `^[Inline footnote]`.

## Gotchas

- Wikilinks vs External links: Always use `[[Note Name]]` for vault notes so Obsidian can automatically track file renames; use `[text](url)` for external web URLs.
- Callouts: Ensure each line of the callout body starts with `>` to avoid breaking the block.
- Frontmatter: Must start on the very first line of the file with `---` and close with `---`.
