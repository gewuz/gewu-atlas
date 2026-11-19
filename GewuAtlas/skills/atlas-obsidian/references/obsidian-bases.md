# Obsidian Bases Reference

> Source: `obsidianmd/obsidian-skills` (`obsidian-bases`)

Create and edit valid Obsidian Bases (`.base` files). A base file uses the `.base` extension and contains YAML definitions for views, filters, formulas, and summaries to create database-like views of notes in Obsidian.

## When to Use

- Creating or modifying Obsidian `.base` files
- Configuring database-like views (`table`, `cards`, `list`, `map`) of vault notes
- Setting up global or view-specific filters (`and`, `or`, `not`)
- Defining calculated formula properties and summaries
- Embedding bases inside Markdown notes with `![[MyBase.base]]`

## Core Workflow

1. Create a file with the `.base` extension containing valid YAML.
2. Define scope using `filters` to select notes (by tag, folder, property, or date).
3. Add `formulas` (optional) to define computed properties.
4. Configure `views` (`table`, `cards`, `list`, or `map`) specifying `order` of properties to display.
5. Validate YAML quoting rules and ensure formula properties referenced in views are defined.

## Schema Overview

```yaml
filters:
  and:
    - 'status == "active"'
    - not:
      - 'file.hasTag("archived")'

formulas:
  days_until_due: 'if(due, (date(due) - today()).days, "")'

properties:
  status:
    displayName: Status
  formula.days_until_due:
    displayName: "Days Until Due"

views:
  - type: table
    name: "Active Tasks"
    filters:
      and:
        - 'status != "done"'
    order:
      - file.name
      - status
      - formula.days_until_due
    groupBy:
      property: status
      direction: ASC
    summaries:
      formula.days_until_due: Average
```

## Filters and Operators

Filter structure supports single filters, logical lists (`and:`, `or:`, `not:`), and nested filters.

Comparison and logical operators:
- `==` equals, `!=` not equal
- `>`, `<`, `>=`, `<=`
- `&&` logical and, `||` logical or, `!` logical not

Helper methods for files:
- `file.hasTag("tag")`
- `file.inFolder("Folder")`
- `file.hasLink("Note")`

## Properties Reference

- Frontmatter properties: referenced directly like `status` or `author`.
- Built-in file metadata: `file.name`, `file.basename`, `file.path`, `file.folder`, `file.ext`, `file.size`, `file.ctime`, `file.mtime`, `file.tags`, `file.links`, `file.backlinks`.
- Formula properties: `formula.formula_name`.

## Formula Rules and Functions

- Simple arithmetic: `"price * quantity"`
- Conditionals: `'if(condition, trueResult, falseResult)'`
- Dates: `date("2026-01-01")`, `today()`, `now()`
- Duration math: Subtracting two dates returns a Duration object. Access `.days`, `.hours`, etc. before applying math functions (e.g. `'(now() - file.ctime).days.round(0)'`). Never run `.round()` directly on a Duration object.
- Null checks: Use `if(property, ...)` guards to handle empty properties gracefully.

## View Types and Summaries

Supported view types: `table`, `cards`, `list`, `map`.

Default summary functions:
- Number: `Average`, `Min`, `Max`, `Sum`, `Range`, `Median`, `Stddev`
- Date: `Earliest`, `Latest`, `Range`
- Boolean: `Checked`, `Unchecked`
- Any: `Empty`, `Filled`, `Unique`

## Gotchas

- Quoting: Enclose formulas with double quotes in single quotes: `'if(done, "Yes", "No")'`. Strings containing special YAML characters (`:`, `{`, `}`, `[`, `]`, `&`, `*`, `#`, `?`, `|`, `-`, `<`, `>`, `=`, `!`, `%`, `@`) must be quoted.
- Undefined formulas: Ensure any property referenced as `formula.foo` is explicitly declared in the top-level `formulas` section.
- Embedding: Embed inside Markdown using `![[BaseName.base]]` or `![[BaseName.base#View Name]]`.
