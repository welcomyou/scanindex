# Graphify evaluation for ScanIndex / ocrtool

Date: 2026-05-11

## What was run

Graphify was installed as a Python package in an isolated venv:

```powershell
python -m venv temp\graphify-venv
temp\graphify-venv\Scripts\python.exe -m pip install graphifyy
```

No clone of `github.com/safishamsi/graphify` was needed.

The normal headless CLI is:

```powershell
temp\graphify-venv\Scripts\graphify.exe extract . --out graphify
```

On this machine there is no LLM API key, so full semantic extraction for docs/images was not run. I used Graphify's local AST/build/cluster/report modules directly to produce code graphs.

## How Graphify works

1. `detect`: scans files and applies `.graphifyignore`.
2. `extract`: parses code into nodes and edges. For Python, this gives files, classes, functions, methods, imports, calls, and containment relationships.
3. Semantic extraction: optional LLM pass for Markdown/docs/images. This needs `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `MOONSHOT_API_KEY`. Skipped here.
4. `build`: turns extracted nodes/edges into a NetworkX graph.
5. `cluster`: groups related nodes into communities.
6. `report/export/query`: writes `GRAPH_REPORT.md`, `graph.json`, HTML views, and supports `query`, `path`, `explain`.

## Outputs

Recommended output for this repo:

```text
graphify/graphify-out-nodedup/
```

This variant disables Graphify deduplication because this codebase has many scripts with common names like `main()`, `parse_args()`, and `_log()`. Default dedup merged some unrelated symbols and made false hubs.

Default Graphify-style output:

```text
graphify/graphify-out/
```

Both include:

```text
GRAPH_REPORT.md
graph.json
graph.html
GRAPH_TREE.html
callflow.html
.graphify_analysis.json
manifest.json
```

## Final graph stats

`graphify-out-nodedup`:

- 274 code files detected
- 56 document files detected but not semantically extracted
- 4,953 nodes
- 10,824 edges
- 251 communities
- Benchmark estimate: about 36.9x fewer tokens per graph query than reading the whole corpus

Top hubs in the no-dedup graph:

1. `KieViewer`
2. `RepositoryScreen`
3. `ArchiveStep2Kie`
4. `MainWindow`
5. `ArchiveStep3Sign`
6. `KieArchiveViewer`
7. `PdfViewerWidget`
8. `DossierInfoDialog`
9. `ArchiveStore`
10. `HybridIndex`

## Useful commands

Explain a node:

```powershell
temp\graphify-venv\Scripts\graphify.exe explain "SearchEngine" --graph graphify\graphify-out-nodedup\graph.json
```

Find path between two concepts:

```powershell
temp\graphify-venv\Scripts\graphify.exe path "RepositoryScreen" "SearchEngine" --graph graphify\graphify-out-nodedup\graph.json
```

Query by keywords:

```powershell
temp\graphify-venv\Scripts\graphify.exe query "archive KIE export Excel metadata" --graph graphify\graphify-out-nodedup\graph.json --budget 2000
```

Open HTML outputs:

```powershell
start graphify\graphify-out-nodedup\graph.html
start graphify\graphify-out-nodedup\GRAPH_TREE.html
start graphify\graphify-out-nodedup\callflow.html
```

## Practical assessment

Useful for this project:

- Quickly finding architectural hubs before editing UI, OCR, KIE, table, and repository/search flows.
- Answering "what connects A to B?" with `path`.
- Getting a map before a refactor, especially around `ArchiveStep2Kie`, `RepositoryScreen`, `ArchiveStore`, `HybridIndex`, and `KieViewer`.
- Producing lightweight codebase documentation for future agent sessions.

Limitations:

- Treat `INFERRED` edges as hints, not proof.
- Default dedup is noisy for this repo; prefer `graphify-out-nodedup`.
- Docs/Markdown were only counted, not semantically extracted, because no LLM API key was present.
- The graph is a navigation aid. For final implementation decisions, still inspect the referenced source files.
