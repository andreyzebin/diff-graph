# DiffSearch

Virtual unified diff filesystem. Materializes a git diff as a virtual filesystem where changed files contain both old and new content with `+`/`-` markers, and unchanged files are written as-is from the source commit.

## What it does

Given two git refs (base and source), DiffSearch:

1. Runs `git diff -U99999` to get full-context unified diffs for changed files
2. Builds `VirtualFile` objects with three coordinate systems per line:
   - **L** — virtual line number (position in the unified view, includes both `+` and `-` lines)
   - **old** — line number in the left (base) commit. `None` for added lines.
   - **new** — line number in the right (source) commit. `None` for deleted lines.
3. Materializes to a temp directory where changed files are plain text (searchable by grep) with metadata sidecars in `.diffmeta/`

## Line types

| Marker | L | old | new | Meaning |
|--------|---|-----|-----|---------|
| `+`    | yes | — | yes | Added in source |
| `-`    | yes | yes | — | Deleted from base |
| ` `    | yes | yes | yes | Unchanged context |

## Tools

### `read_file_vfs(vfs_dir, path, start_line?, end_line?, line_numbers?)`

Read a file from the virtual FS. `start_line`/`end_line` are L (virtual position).

For changed files, output shows `old`/`new` columns and `+`/`-` markers:
```
# OrderService.java  lines L20-L38  (old=left commit, new=right commit)
   old  new
    18   18 |     public void cancelOrder(Long orderId) {
    19   19 |         Order order = orderRepository.findById(orderId)
    20      |-           .orElseThrow(RuntimeException::new);
         20 |+           .orElseThrow(() -> new OrderNotFoundException(orderId));
         21 |+       if (order.getItems() != null) {
    21   22 |             for (OrderItem item : order.getItems()) {
```

For unchanged files, plain output with line numbers (L == old == new).

### `search_vfs(vfs_dir, query, glob?, regex?, max_results?)`

Grep across the virtual FS. Finds both added (`+`) and deleted (`-`) content. Returns `SearchHit` objects with L, old, new, marker, snippet.

### `list_files_vfs(vfs_dir, glob_pattern?)`

List files in the virtual FS, excluding `.diffmeta/`.

### `read_outline_vfs(vfs_dir, path, repo_path?)`

Structural outline via tree-sitter. For changed files, enriches with L ranges and old/new mapping.

## Usage

```python
from diffsearch import build_virtual_file, materialize_vfs, get_changed_files
from diffsearch import read_file_vfs, search_vfs, list_files_vfs

# Build virtual file in memory
vf = build_virtual_file(base_sha, source_sha, "src/Order.java", repo_path)
for line in vf.lines:
    print(f"L{line.L} old:{line.old} new:{line.new} {line.marker} {line.content}")

# Materialize to disk for grep-based search
vfs_dir = materialize_vfs(repo_path, base_sha, source_sha)

# Use tools
print(read_file_vfs(vfs_dir, "src/Order.java", start_line=20, end_line=38))

hits = search_vfs(vfs_dir, "InventoryClient")
for h in hits:
    print(f"{h.file} L{h.L} old:{h.old} new:{h.new} {h.marker} {h.snippet}")

files = list_files_vfs(vfs_dir, "**/*.java")
```

## Tests

```bash
source .venv/bin/activate
pip install pyyaml  # needed for fixture manifests

# Run all diffsearch tests
pytest diffsearch/tests/ -v

# Run a specific test class
pytest diffsearch/tests/test_virtual_fs.py::TestSearchVFS -v

# Run tests for one fixture only
pytest diffsearch/tests/test_virtual_fs.py -k "rename_field" -v
```

Tests build real git repos from fixture directories in `tests/fixtures/`. Each fixture has a `manifest.yaml` describing commits and `base/`/`source/` directories with file snapshots.

### Adding a test fixture

1. Create `diffsearch/tests/fixtures/<name>/manifest.yaml`:
   ```yaml
   name: my_scenario
   description: What this tests
   commits:
     - ref: base
       message: "initial state"
     - ref: source
       message: "the change being tested"
   ```
2. Add files under `base/` and `source/` subdirectories
3. Add fixture name to the `params` list in `conftest.py::any_repo`
4. Write tests using the fixture

### Test coverage (68 tests)

- **VirtualFile invariants** — L monotonic, marker partition, old/new correctness, bidirectional mappings (runs across all fixtures)
- **Content checks** — deleted/added code detection per fixture scenario
- **Materialization** — files on disk, metadata, unchanged files
- **read_file** — old/new columns, markers, L-based slicing, line_numbers toggle
- **search** — finds added/deleted/unchanged code, enrichment with old/new, max_results, .diffmeta exclusion
- **list_files** — listing, glob filtering, .diffmeta exclusion
- **outline** — tree-sitter output, file not found
