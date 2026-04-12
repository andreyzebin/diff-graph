# DiffSearch

Virtual unified diff filesystem. Materializes a git diff as a virtual filesystem where changed files contain both old and new content with `+`/`-` markers, and unchanged files are copied from the working tree.

## Core concept: `ref` parameter

All tools accept a `ref` parameter that controls the view:

| ref | Mode | What you see |
|-----|------|-------------|
| `"base..source"` | Unified diff | Files with `+`/`-` markers, old/new line numbers |
| `"<sha1>..<sha2>"` | Commit range diff | Same, but between specific commits |
| `"source"` | Plain | Current files as-is, no markers |

**Default:** `"base..source"` when reviewing a PR (base/source refs available), `"source"` otherwise.

**Backward compatibility:** with `ref="source"`, all tools behave identically to plain filesystem access. No VFS, no markers, no old/new columns. Switching between modes is transparent to the caller.

**Lazy materialization:** VFS is created on the first tool call with a `..` ref. Subsequent calls with the same ref reuse the cached VFS. Different refs create separate VFS instances. All cleaned up on exit.

### Three coordinate systems

When `ref` is a range (contains `..`), each line in a virtual file has:

| Coord | Present on | Meaning |
|-------|-----------|---------|
| **L** | all lines | Position in the unified diff file (VFS). Use for `start_line`/`end_line` in read_file and outline. Includes both `+` and `-` lines, so always ≥ max(old, new). |
| **old** | `-` and context lines | Line number in the left (base/destination) commit. The version being merged INTO. |
| **new** | `+` and context lines | Line number in the right (source) commit. The branch being reviewed. Use for findings/Bitbucket comments. |

Line markers: `+` = added in source, `-` = deleted from base, ` ` = unchanged context.

For unchanged files and `ref="source"`: L == old == new (single coordinate system).

## Tools

### `read_file_vfs(vfs_dir, path, start_line?, end_line?, line_numbers?, changes_only?, context_before?, context_after?)`

Read a file from the virtual FS. `start_line`/`end_line` are L (virtual position, 1-indexed).

**Changed file — full view:**
```
# src/main/java/.../OrderService.java  L44-L56 (old=base, new=source)
old new
 50  50 |          if (order.getStatus() == OrderStatus.SHIPPED
 51  51 |                  || order.getStatus() == OrderStatus.DELIVERED) {
 52  52 |              throw new IllegalStateException("Cannot cancel");
 53  53 |          }
 54     |-         for (OrderItem item : order.getItems()) {
 55     |-             releaseInventory(item);
     54 |+         if (order.getItems() != null) {
     55 |+             for (OrderItem item : order.getItems()) {
     56 |+                 releaseInventory(item);
```

**Changed file — `changes_only=True` (replaces get_diff):**
```
# src/main/java/.../OrderService.java  changes only (old=base, new=source)
old new
 8  8 |      private final OrderRepository orderRepository;
 9    |-     private final InventoryClient inventoryClient;
    9 |+     private final InventoryService inventoryService;
10 10 |  
  --
24 24 |          Order order = orderRepository.findById(orderId)
25    |-                 .orElseThrow(RuntimeException::new);
   25 |+                 .orElseThrow(() -> new OrderNotFoundException(orderId));
```

Only lines near `+`/`-` markers are shown, with `--` separators between hunks. `context_before`/`context_after` control how many surrounding lines (default 3).

**New file (all `+`, no old column):**
```
# src/main/java/.../AuditLog.java  L1-L19 (new file)
new
 1 |+ package com.flowmart.orders.audit;
 2 |+
 3 |+ import java.time.Instant;
 4 |+
 5 |+ public class AuditLog {
```

**Deleted file (all `-`, no new column):**
```
# src/.../LegacyService.java  L1-L22 (deleted)
old
 1 |- package com.flowmart.orders.legacy;
 2 |-
 3 |- @Deprecated
 4 |- public class LegacyService {
```

**Unchanged file (single column, no markers):**
```
# src/main/java/.../Order.java  L1-L60
 1 | @Entity
 2 | @Table(name = "orders")
 3 | public class Order {
```

**Binary file:**
```
# gradle/wrapper/gradle-wrapper.jar
(binary file)
```

Column widths adapt dynamically to the max line number in the range. Empty columns (old or new) are omitted when all lines lack that coordinate.

### `search_vfs(vfs_dir, query, glob?, regex?, max_results?, before?, after?)`

Search across the virtual FS. Returns grep-like text grouped by file.

**Basic search:**
```
src/main/java/com/flowmart/orders/service/PricingService.java
  L60  new:60 |+  List<OrderItem> eligible = findEligibleItems(order, promotion);
  L65  new:65 |+  List<List<OrderItem>> groups = partitionGroups(eligible, ...);

src/main/java/com/flowmart/orders/service/OrderService.java
  L52 old:52 new:52 |   for (OrderItem item : order.getItems()) {
  --
  L23 old:23 new:23 |   public Order createOrder(Customer customer, List<OrderItem> items) {
```

**Search with context (`before=1, after=1`):**
```
src/main/java/com/flowmart/orders/service/OrderService.java
  L8 old:8 new:8 |      private final OrderRepository orderRepository;
  L9 old:9 |-     private final InventoryClient inventoryClient;
  L10  new:9 |+     private final InventoryService inventoryService;
  L11 old:10 new:10 |  
  --
  L12 old:11 |-     public OrderService(..., InventoryClient inventoryClient) {
  L13  new:11 |+     public OrderService(..., InventoryService inventoryService) {
  L14 old:12 new:12 |          this.orderRepository = orderRepository;
```

Results grouped by file. `--` separates non-adjacent match groups. Each line shows L (for navigation) + old/new (for reference) + marker + content. Case-insensitive by default.

Key feature: finds both **deleted** code (`-` lines from base) and **added** code (`+` lines in source) in one search.

### `list_files_vfs(vfs_dir, glob_pattern?)`

List files in the virtual FS. Excludes `.diffmeta/` internal directory. Includes both changed and unchanged files. Binary files are listed (read_file returns `(binary file)` for them).

```python
files = list_files_vfs(vfs_dir, "**/*.java")
# ['src/main/java/.../Order.java',
#  'src/main/java/.../OrderService.java',
#  'src/main/java/.../PricingService.java']
```

### `read_outline_vfs(vfs_dir, path, repo_path?)`

Structural outline via tree-sitter. For changed files, enriches with L ranges and old/new mapping.

**Changed file outline:**
```
# src/.../OrderService.java  (45 lines)
[class] OrderService  L6-45 (old:6-32 → new:6-39) *
  [field] orderRepository  L8-8 (old:8-8 → new:8-8)
  [field] inventoryClient  L9-9 (old:9-9 → ) *
  [field] inventoryService  L10-10 ( → new:9-9) *
  [method] createOrder  L19-24 (old:16-21 → new:16-21)
  [method] cancelOrder  L26-39 (old:23-31 → new:23-33) *
  [method] getOrder  L41-44 ( → new:35-38) *
```

- `L` range = position in virtual file (use for `read_file` start/end)
- `old → new` = mapping between commit versions
- `*` = symbol changed in this diff
- Empty old = added, empty new = deleted

**Unchanged file outline:** plain line numbers, no old/new mapping.

## Integration with DiffGraph agent

DiffSearch is wired into DiffGraph's agent tools via `orchestra_tools.py`. When the orchestrator has `base_ref` and `source_ref` (PR mode), tools default to `ref="base..source"`. The agent can override per-call:

```python
# Agent sees changes in a file (default ref="base..source")
read_file("OrderService.java", changes_only=True, before=3, after=3)

# Agent reads plain file without markers
read_file("Order.java", ref="source")

# Agent reviews a specific commit range
read_file("PricingService.java", changes_only=True, ref="a1b2c3d..e4f5g6h")

# Search finds both deleted and added code
search("getItems")
```

## Usage (standalone)

```python
from diffsearch import build_virtual_file, materialize_vfs, get_changed_files
from diffsearch import read_file_vfs, search_vfs, list_files_vfs

# Build virtual file in memory
vf = build_virtual_file(base_sha, source_sha, "src/Order.java", repo_path)
for line in vf.lines:
    print(f"L{line.L} old:{line.old} new:{line.new} {line.marker} {line.content}")

# Materialize to disk for grep-based search
vfs_dir = materialize_vfs(repo_path, base_sha, source_sha)

# Read with changes only
print(read_file_vfs(vfs_dir, "src/Order.java", changes_only=True))

# Search across all files
print(search_vfs(vfs_dir, "getItems", before=2, after=2))

# List files
files = list_files_vfs(vfs_dir, "**/*.java")

# Clean up
import shutil
shutil.rmtree(vfs_dir)
```

## How VFS is built

1. `git diff -U99999 <base> <source> -- <path>` produces a full-context unified diff
2. Each line is parsed into a `VirtualLine(content, marker, L, old, new)`
3. Bidirectional mappings built: `L_to_new`, `new_to_L`, `L_to_old`, `old_to_L`
4. Changed files: content (without markers) written to temp dir, metadata as `.diffmeta/<path>.json`
5. Unchanged files: copied from working tree (works with blobless clones)
6. Binary files: marker file written, visible in list, read returns `(binary file)`, excluded from search

## Tests

```bash
source .venv/bin/activate
pip install pyyaml  # needed for fixture manifests

# Run all diffsearch tests
pytest diffsearch/tests/ -v

# Run with log output to see tool responses
pytest diffsearch/tests/ -v --log-cli-level=INFO

# Run a specific test file
pytest diffsearch/tests/test_search.py -v

# Run tests for one fixture only
pytest diffsearch/tests/test_build_virtual_file.py -k "rename_field" -v
```

Tests build real git repos from fixture directories in `tests/fixtures/`. Each fixture has a `manifest.yaml` describing commits and `base/`/`source/` directories with file snapshots.

### Test fixtures

| Fixture | Scenario |
|---------|----------|
| `rename_field` | Rename field + add null check + new method |
| `split_method` | Split one method into two + add new method |
| `new_file` | Add entirely new file |
| `deleted_file` | Remove a file |
| `renamed_file` | Rename file + modify content |

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

### Test coverage (135 tests total: 30 existing + 105 diffsearch)

- **VirtualFile invariants** — L monotonic, marker partition, old/new correctness, bidirectional mappings (parameterized across all 5 fixtures)
- **Content checks** — deleted/added code detection per fixture scenario
- **Materialization** — files on disk, metadata, binary handling, unchanged files
- **read_file** — old/new columns, markers, L-based slicing, line_numbers toggle, changes_only, new/deleted/unchanged file formats
- **search** — finds added/deleted/unchanged code, grouping by file, context before/after, separators, case-insensitive, .diffmeta exclusion
- **list_files** — listing, glob filtering, .diffmeta exclusion, binary files visible
- **outline** — tree-sitter output with L/old/new enrichment, file not found
