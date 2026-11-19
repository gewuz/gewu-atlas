# Obsidian Dataview Reference

> Source: [Obsidian Dataview Official Documentation](https://blacksmithgu.github.io/obsidian-dataview/)

Create and edit live queries over indexed vault metadata using Obsidian Dataview (`dataview` DQL codeblocks, inline DQL, `dataviewjs`, and inline DataviewJS). Dataview is a read-only display and calculation engine (except when interactively checking a `TASK` item in a `TASK` view).

## When to Use

- Writing `dataview` DQL queries (`TABLE`, `LIST`, `TASK`, `CALENDAR`) inside Markdown notes
- Embedding single computed values inline with `` `= expression` `` or `` `$= js_expression` ``
- Writing `dataviewjs` scripts using the `dv` API for custom tables, lists, aggregations, or views
- Annotating notes with Dataview inline fields (`[key:: value]`, `(key:: value)`) alongside YAML frontmatter
- Querying implicit file metadata (`file.tags`, `file.inlinks`, `file.outlinks`, `file.tasks`, `file.ctime`, `file.mtime`)

## Metadata Indexing

Dataview only queries **indexed data**: YAML frontmatter, inline fields, tags, links, and list/task items. Arbitrary body paragraphs are not indexed.

- **YAML Frontmatter**: Standard `---` block at top of note (`status: active`, `rating: 8`).
- **Inline Fields**:
  - `[key:: value]` — renders both key and value inline
  - `(key:: value)` — hides the key and renders only the value
  - `Key:: value` — full-line inline field
- **Implicit Page Fields (`file.*`)**:
  - Identity & paths: `file.name`, `file.folder`, `file.path`, `file.ext`, `file.link`, `file.aliases`, `file.size`, `file.starred`
  - Dates: `file.ctime` / `file.cday`, `file.mtime` / `file.mday`, `file.day` (from `yyyy-mm-dd` / `yyyymmdd` filename or `Date` field)
  - Graph & tags: `file.tags` (expanded hierarchy `#a/b` -> `#a`, `#a/b`), `file.etags` (explicit tags only), `file.inlinks`, `file.outlinks`
  - Content & raw metadata: `file.tasks`, `file.lists`, `file.frontmatter`

## DQL Query Structure & Types

```dataview
<QUERY-TYPE> [WITHOUT ID] <fields>
FROM <source>
WHERE <condition>
FLATTEN <array_or_expr> AS <alias>
GROUP BY <field_or_expr> [AS <name>]
SORT <field> [ASC|DESC]
LIMIT <N>
```

Only `<QUERY-TYPE>` is mandatory (plus a date expression for `CALENDAR`). Always add `FROM` on large vaults to avoid slow full-vault scans.

### Four Query Types

1. **`TABLE [WITHOUT ID] col1 AS "Header", col2`**: Tabular view of page-level fields/expressions. Use `TABLE WITHOUT ID` to hide or rename the default `File`/`Group` first column (e.g. `TABLE WITHOUT ID file.link AS "Note", status`).
2. **`LIST [WITHOUT ID] [expr]`**: Bullet list of matching page links (or group keys) plus at most one optional expression (concatenate multiple values with `+`). Use `LIST rows.file.link GROUP BY type` for grouped lists.
3. **`TASK`**: Interactive task list operating at the `file.tasks` level (not page level). Supports task fields like `completed`, `text`, `tags`, `section`, `due`. Checking a task updates the source note.
4. **`CALENDAR <date_expr>`**: Monthly calendar plotting dots on `<date_expr>`. `SORT` and `GROUP BY` have no effect. Guard with `WHERE typeof(due) = "date"` so invalid values do not break rendering.

## Data Commands

Commands execute **in the exact order written**. `FROM` may appear at most once (immediately after the Query Type); all other commands can repeat in any order.

- **`FROM <source>`**:
  - Tag (including subtags): `FROM #project`
  - Folder (including subfolders): `FROM "30-Projects/AI"`
  - Single file: `FROM "path/to/note"`
  - Links: `FROM [[Target]]` (pages linking TO `Target`), `FROM outgoing([[Target]])` (pages linked FROM `Target`)
  - Combine with `AND`, `OR`, parentheses, and negation `-`: `FROM (#status/open AND "30-Projects") AND -"90-Archive"`
- **`WHERE <clause>`**: Filters rows (equality uses single `=`, e.g. `WHERE status = "active" AND !completed`).
- **`SORT <field> [ASC|DESC], ...`**: Multi-key sort (`ASC` / `ASCENDING` / `DESC` / `DESCENDING`).
- **`GROUP BY <field_or_expr> [AS <name>]`**: Produces one row per unique key with `key` and `rows` array. Supports field swizzling (e.g. `rows.file.link`, `sum(rows.hours)`, `length(rows)`).
- **`FLATTEN <expr> [AS <alias>]`**: Unpacks an array into one row per element (e.g. `FLATTEN file.lists AS L WHERE contains(L.tags, "#idea")`).
- **`LIMIT <N>`**: Caps rows at the current pipeline stage (put `SORT` before `LIMIT` to get top-N).

## Inline DQL & DataviewJS

### Inline DQL

Renders a single expression value inline (no Query Types or Data Commands). Access current page via `this.` or another page via `[[Note]].`:

- `` `= this.file.mtime` ``
- `` `= [[Project Hub]].deadline - date(today)` ``
- `` `= choice(this.status = "done", "Complete", "In Progress")` ``

### DataviewJS (`dataviewjs` & Inline `` `$= ...` ``)

Use a `dataviewjs` codeblock (or inline `` `$= dv.current().file.mtime` ``) when DQL is insufficient:

```dataviewjs
const pages = dv.pages('#books and -#books/finished')
  .where(p => p.rating >= 7)
  .sort(p => p.rating, 'desc');

dv.table(
  ["Book", "Genre", "Rating"],
  pages.map(p => [p.file.link, p.genre, p.rating])
);
```

Core `dv` API methods:
- **Query**: `dv.current()`, `dv.pages(source)`, `dv.pagePaths(source)`, `dv.page(path)`
- **Render**: `dv.table(headers, rows)`, `dv.list(elements)`, `dv.taskList(tasks, groupByFile)`, `dv.header(level, text)`, `dv.paragraph(text)`, `dv.span(text)`, `dv.el(tag, text, opts)`, `dv.execute(dql)`, `await dv.view(path, input)`
- **Markdown strings**: `dv.markdownTable(headers, rows)`, `dv.markdownList(values)`, `dv.markdownTaskList(tasks)`
- **Utility & Eval**: `dv.array(v)`, `dv.fileLink(path, embed?, display?)`, `dv.date(text)`, `dv.duration(text)`, `await dv.io.load(path)`, `await dv.io.csv(path)`, `await dv.query(dql)`

## Common DQL Functions

Most functions vectorize automatically over lists.

- **Constructors**: `date(any)`, `date(text, format)`, `dur(any)`, `list(...)`, `object(k, v, ...)`, `number(str)`, `string(any)`, `link(path, [display])`, `embed(link)`, `elink(url, [display])`, `typeof(any)`
- **Numeric**: `round(n, [digits])`, `trunc(n)`, `floor(n)`, `ceil(n)`, `min(...)`, `max(...)`, `sum(arr)`, `product(arr)`, `average(arr)`, `minby(arr, fn)`, `maxby(arr, fn)`
- **Collections**: `contains(c, v)`, `icontains(c, v)`, `econtains(c, v)`, `containsword(c, v)`, `length(c)`, `nonnull(arr)`, `firstvalue(arr)`, `filter(arr, fn)`, `map(arr, fn)`, `flat(arr, [depth])`, `unique(arr)`, `sort(arr)`, `reverse(arr)`, `join(arr, [delim])`, `slice(arr, start, [end])`, `all(arr, [fn])`, `any(arr, [fn])`, `none(arr, [fn])`
- **Strings**: `lower(s)`, `upper(s)`, `replace(s, pat, rep)`, `regexreplace(s, pat, rep)`, `regextest(pat, s)`, `regexmatch(pat, s)`, `split(s, delim, [limit])`, `startswith(s, pre)`, `endswith(s, suf)`, `substring(s, start, [end])`, `truncate(s, len, [suffix])`
- **Utility**: `default(field, fallback)`, `choice(cond, a, b)`, `striptime(date)`, `dateformat(date, fmt)`, `durationformat(dur, fmt)`, `display(v)`, `meta(link)` (`.path`, `.subpath`, `.display`, `.type`, `.embed`)

## Gotchas

- **Equality operator**: DQL uses `=` (not `==` as in Obsidian Bases or JavaScript).
- **Folder quotes in JS**: `dv.pages('"folder"')` requires double quotes inside the string; `dv.pages("folder")` will not match a folder.
- **Date comparisons**: `dateformat()` returns a string, not a date. Compare `date` values directly (or via `striptime(file.ctime)`) before formatting.
- **Custom views path**: `dv.view(path, input)` resolves from vault root, must be `await`ed, and cannot load from dot-prefixed directories (like `.views`).
- **Command order matters**: `LIMIT 5` followed by `SORT` limits first and sorts only those 5 rows; always put `SORT` before `LIMIT` for top-N queries.
- **Task headings**: Filter tasks under a specific heading with `WHERE meta(section).subpath = "Next Actions"`.
