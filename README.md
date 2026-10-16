# mcp_vosviever

MCP server that drives a local **VOSviewer** installation (Windows) from any MCP client.
It runs batch analyses, reads maps, captures screenshots, and can open/inspect/close the
live GUI window.

## Requirements

- Windows (uses Win32 APIs for process/window control)
- VOSviewer 1.6.x, standalone `VOSviewer.exe`
- Python 3.10+
- `mcp` SDK, `Pillow`

```bash
pip install mcp pillow
```

## Configure

The server reads two environment variables:

| Variable | Meaning | Default |
|---|---|---|
| `VOSVIEWER_EXE` | Absolute path to `VOSviewer.exe` | `C:\Users\TOOR\Downloads\actividad\hoy\VOSviewer.exe` |
| `VOSVIEWER_OUT_DIR` | Default output folder | `C:\Users\TOOR\Downloads\actividad\hoy\vosviewer_out` |

Set them to your own paths.

## Register with OpenCode

```bash
opencode mcp add vosviewer --global
```

Command: `<python> <repo>/vosviewer_mcp.py`, transport `stdio`.

## Tools

| Tool | Purpose |
|---|---|
| `vos_analyze` | Batch run: map/network input, layout, clustering, saves, screenshot |
| `vos_from_scopus` | CSV export → analysis → 3 visualisations (`coauthorship`, `authors_sources`, `title_terms`) |
| `vos_read` | Parse an existing `map` / `network` / `json` file into items, links, clusters |
| `vos_capture` | Screenshot the main window or a specific window |
| `vos_open` | Launch the interactive GUI (no `save_*` flags) |
| `vos_ui` | Click/type inside the GUI by window coordinates |
| `vos_close` | Close the whole process tree (launcher + `javaw.exe`) |
| `vos_status` | Report live processes, windows and last outputs |
| `vos_dialog` | List, capture or answer modal dialogs (yes / no / cancel) |
| `vos_capabilities` | Machine-readable catalog: flags, formats, modes, dialog policy |

## Quickstart

Batch run on your own files:

```
vos_analyze(input={"map": "...", "network": "..."}, save={"save_map": "...", "save_screenshot_png": "..."})
```

From a Scopus CSV export:

```
vos_from_scopus(csv_path="resultados.csv", analysis="coauthorship", out_dir="out")
```

Each analysis writes `mapa.txt`, `red.txt`, `mapa.json`, a `manifest.json` and three
PNGs (network, overlay, density).

## Behaviour notes

- **Batch vs interactive.** VOSviewer exits by itself when argv contains at least one
  `save_*` flag; without one it stays open. The server never blends the two paths.
- **Flags must be exact.** `run_layout`, `run_clustering`, `skip_clustering`,
  `largest_component` and the `*_visualization` selectors take **no value**; passing
  `1` triggers a modal `Program argument '1' is invalid` and hangs.
- **Dialogs.** `vos_analyze` / `vos_from_scopus` answer routine dialogs by policy
  (overwrite → yes, error → stop), capture them to `$VOSVIEWER_OUT_DIR/dialogs`, and
  ignore transient progress windows (`Layout`, `Clustering`).
- **`VOSviewer.exe` is a launcher.** The Swing GUI runs in a child `javaw.exe`; teardown
  walks the whole process tree.

## Known limits

- Windows only.
- `authors_sources` runs with `run_layout` disabled: VOSviewer's layout progress dialog
  on bipartite networks does not return. Pass `run_layout: true` to override.
- Keyword co-occurrence, bibliographic coupling and co-citation need a Scopus export
  with Abstract / Author-Index Keywords / References. The 19-column basic export is not
  enough.
