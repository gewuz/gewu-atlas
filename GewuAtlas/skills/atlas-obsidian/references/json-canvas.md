# JSON Canvas Reference

> Source: `obsidianmd/obsidian-skills` (`json-canvas`)

Create and edit valid JSON Canvas files (`.canvas`) compliant with the JSON Canvas Spec 1.0. A canvas file is an open format represented as JSON containing nodes and edges for diagrams, mind maps, and visual notes.

## When to Use

- Creating or editing Obsidian `.canvas` files
- Generating visual flowcharts, mind maps, concept maps, or project boards
- Constructing nodes (`text`, `file`, `link`, `group`) with coordinates and dimensions
- Connecting nodes with directed or undirected edges and labels
- Validating canvas JSON integrity and structure

## File Structure

A `.canvas` file must be valid JSON containing two top-level arrays:

```json
{
  "nodes": [],
  "edges": []
}
```

## Node Types and Attributes

All nodes require:
- `id`: Unique 16-character lowercase hex string (e.g. `"6f0ad84f44ce9c17"`)
- `type`: `"text"`, `"file"`, `"link"`, or `"group"`
- `x`, `y`: Top-left coordinates in pixels (integers, can be negative)
- `width`, `height`: Node dimensions in pixels (integers)
- `color`: (Optional) Preset `"1"` to `"6"` or hex string (e.g. `"#FF0000"`)

Node-specific fields:
- Text Node (`type: "text"`): `text` (string containing Markdown). Use `\n` for line breaks.
- File Node (`type: "file"`): `file` (path to vault file), optional `subpath` (heading `#Heading` or block `#^blockid`).
- Link Node (`type: "link"`): `url` (external URL string).
- Group Node (`type: "group"`): optional `label`, `background`, `backgroundStyle` (`"cover"`, `"ratio"`, `"repeat"`).

## Edges

Edges connect two nodes:
- `id`: Unique 16-character lowercase hex string
- `fromNode`: Source node `id`
- `toNode`: Target node `id`
- `fromSide` / `toSide`: (Optional) `"top"`, `"right"`, `"bottom"`, or `"left"`
- `fromEnd` / `toEnd`: (Optional) `"none"` or `"arrow"` (default `toEnd` is `"arrow"`, default `fromEnd` is `"none"`)
- `label`: (Optional) Text label along the connection line
- `color`: (Optional) Preset `"1"` to `"6"` or hex string

## Color Presets

- `"1"`: Red
- `"2"`: Orange
- `"3"`: Yellow
- `"4"`: Green
- `"5"`: Cyan
- `"6"`: Purple

## Layout and Sizing Guidelines

- Grid and spacing: Keep 50 to 100px space between adjacent nodes. Leave 20 to 50px inner padding for groups.
- Suggested dimensions:
  - Small text: 200-300px width, 80-150px height
  - Medium text: 300-450px width, 150-300px height
  - Large text: 400-600px width, 300-500px height
  - File preview: 300-500px width, 200-400px height
  - Link preview: 250-400px width, 100-200px height

## Validation Checklist

- All IDs (both nodes and edges) are unique 16-character hex strings.
- Every `fromNode` and `toNode` in `edges` strictly matches an existing node `id`.
- String escapes: Markdown inside text nodes must escape quotation marks properly and use standard `\n` instead of literal backslash-n strings.
- Valid JSON syntax without trailing commas.
