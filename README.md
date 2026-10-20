# mcp_vosviewer

MCP server that drives a local **VOSviewer** installation (Windows) from any MCP client.
It runs batch analyses, reads maps, captures screenshots, and can open/inspect/close the
live GUI window.

## Requirements

- Windows (uses Win32 APIs for process/window control)
- VOSviewer 1.6.x, standalone `VOSviewer.exe`
- **Java 8** (VOSviewer 1.6.x does not run on Java 9+; the server refuses to start a run
  with a newer runtime instead of failing with an opaque Java stack trace)
- Python 3.10+
- `mcp` SDK, `Pillow`

```bash
pip install -r requirements.txt
```

## Configure

| Variable | Meaning | Default |
|---|---|---|
| `VOSVIEWER_EXE` | Absolute path to `VOSviewer.exe` | `C:\Users\TOOR\Downloads\actividad\hoy\VOSviewer.exe` |
| `VOSVIEWER_OUT_DIR` | Default folder for captures and dialogs | `C:\Users\TOOR\Downloads\actividad\hoy\vosviewer_out` |
| `VOSVIEWER_JAVA_HOME` | Java 8 runtime used to launch VOSviewer | `JAVA_HOME` if it points to Java 8 |
| `JAVA_HOME` | Fallback for `VOSVIEWER_JAVA_HOME` | - |

Set them to your own paths. `VOSVIEWER_JAVA_HOME` wins over `JAVA_HOME`.

## Register with OpenCode

```bash
opencode mcp add vosviewer -- <python> <repo>/vosviewer_mcp.py
```

Transport `stdio`.

## Tools

| Tool | Purpose | Main inputs | Returns |
|---|---|---|---|
| `vos_analyze` | Batch run: writes map/network/json and captures | `source`, `source_path`, `outputs`, `network_path`, `layout`, `visualization`, `corpus`, `timeout_s` | `ok`, `exit_code`, `outputs`, `stats`, `warnings`, `screenshot_recovery` |
| `vos_read` | Analyze a map/network/json file, no process launched | `path`, `top_n`, `metric`, `cluster_id`, `search` | `items`, `links`, `clusters`, `bbox`, `available_metrics` |
| `vos_capture` | Screenshot the live window (`scope="window"`) or re-render a map (`scope="map"`) | `scope`, `out_path`, `target`, `source_path`, `network_path` | `path`, `bytes`, `width`, `height`, `method` |
| `vos_open` | Launch the interactive GUI and leave it running | `source`, `source_path`, `network_path`, `layout`, `visualization` | `pid`, `gui_pid`, `hwnd`, `title`, `rect`, `windows` |
| `vos_ui` | Click, drag, type or scroll inside the GUI by window coordinates | `action`, `x`, `y`, `clicks`, `delta`, `text`, `combo`, `to_x`, `to_y` | `screen_xy`, `hwnd`, `error_dialog` |
| `vos_close` | Close every running VOSviewer, whole process tree | - | `closed`, `pids`, `terminated`, `remaining` |
| `vos_status` | Live processes, windows and last outputs | - | `running`, `pids`, `windows`, `hwnd`, `uptime_s`, `last_outputs` |
| `vos_dialog` | List, capture or answer modal dialogs (yes / no / cancel) | `action`, `hwnd` | `dialogs`, `screenshot` |
| `vos_from_scopus` | Scopus CSV -> map + network + 3 visualisations | `csv_path`, `analysis`, `out_dir`, `params` | `folder`, `stats`, `screenshots`, `outputs`, `runs` |
| `vos_capabilities` | Machine-readable catalog of flags, formats, modes, dialog policy | `section` | `parameters`, `rules`, `modes`, `file_formats`, `measured_facts` |

Resource: `vosviewer://status` (same payload as `vos_status`).

`vos_analyze` sources: `map`, `network`, `corpus`, `gml`, `json`, `pajek_network`,
`pajek_partition`, `pajek_vector`, `thesaurus`, `scores`. `outputs` maps `save_*` flags
to paths: `save_map`, `save_network`, `save_json`, `save_gml`, `save_pajek_*` and the ten
`save_screenshot_<format>` flags.

## Quickstart

Batch run on your own files:

```
vos_analyze(
  source="map",
  source_path="C:/data/mapa.txt",
  network_path="C:/data/red.txt",
  layout={"run_layout": True, "run_clustering": True, "min_cluster_size": 1},
  visualization={"network_visualization": True},
  outputs={"save_map": "C:/out/mapa_out.txt", "save_network": "C:/out/red_out.txt"}
)
```

From a Scopus CSV export:

```
vos_from_scopus(csv_path="resultados.csv", analysis="coauthorship", out_dir="out")
```

Each analysis writes `mapa.txt`, `red.txt`, `mapa.json`, a `manifest.json` and the three
captures (`01_red.png`, `02_overlay.png`, `03_densidad.png`).

## Behaviour notes

- **Batch vs interactive.** VOSviewer exits by itself when argv contains at least one
  `save_*` flag; without one it stays open. The server never blends the two paths.
- **A capture waits for the paint.** `vos_capture(scope="window")` rejects a flat
  image: if the window has not painted yet it waits and retries, and reports
  `painted` and `attempts` so the caller knows what it got.
- **Captures do not travel in the batch argv.** `-save_screenshot_png` aborts the batch
  run on this machine and leaves an empty file. The server therefore runs the batch
  without the screenshot flags and takes the picture from the live window afterwards
  (open, wait for the render to stabilise, capture). The result is reported in
  `screenshot_recovery`.
- **Output folders are prepared.** The `javaw.exe` of VOSviewer runs with a low
  integrity level and cannot write into a medium-integrity folder: Windows answers
  "Access denied" and Java shows a modal dialog the user has to close by hand. The
  server lowers the integrity label of the output folder with `icacls` before every run.
- **Inputs are validated before the run.** A map whose items all share the same `x`/`y`
  coordinates, a network that is not tab-delimited and a missing file are all reported
  as an ordinary error, instead of becoming a modal dialog.
- **One instance at a time.** A batch run refuses to start while another VOSviewer is
  running: with the application open the run waits for a dialog that never appears.
- **Flags must be exact.** `run_layout`, `run_clustering`, `skip_clustering`,
  `largest_component` and the `*_visualization` selectors take **no value**; passing
  `1` triggers a modal `Program argument '1' is invalid` and hangs.
- **Dialogs.** `vos_analyze` / `vos_from_scopus` answer routine dialogs by policy
  (overwrite -> yes, error -> stop), capture them to `$VOSVIEWER_OUT_DIR/dialogs`, and
  ignore transient progress windows (`Layout`, `Clustering`, `Saving screenshot`).
- **`VOSviewer.exe` is a launcher.** The Swing GUI runs in a child `javaw.exe`; teardown
  walks the whole process tree.

## Known limits

- Windows only.
- `authors_sources` runs with `run_layout` disabled: VOSviewer's layout progress dialog
  on bipartite networks does not return. Pass `run_layout: true` to override.
- Keyword co-occurrence, bibliographic coupling and co-citation need an export with
  Abstract / Author-Index Keywords / References. A basic 19-column export is not enough.
- The integrity label of the output folder is changed with `icacls`. On a machine where
  that is not allowed the run still starts, and the report says the label could not be
  applied.

## Tests

```bash
python -m pytest tests -q
```

The suite covers the pure logic (argument building, map/network validation, output
collection) and three integration tests that launch the real VOSviewer: one over the
MCP stdio protocol, one that writes into a fresh output folder, and one that checks the
screenshot recovery. The integration tests need `VOSviewer.exe` and a Java 8 runtime;
they are skipped when either is missing.

## License

MIT
