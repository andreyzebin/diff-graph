// Alpine component for /qa/sessions/{run_id} — per-session trace
// drill-in inside the QA SPA. Renders the same agent tree / paired
// steps that the old Jinja /runs/{id}/trace renderer shows, but
// consumes data via /api/runs/{id} (metadata strip) +
// /api/runs/{id}/json (prepared tree with paired_steps from
// orchestra.trace._prepare_agent).

document.addEventListener('alpine:init', () => {
  Alpine.data('sessionTraceView', () => ({
    runId: '',
    qaBase: '',
    fromQuery: '',
    meta: {},
    tree: null,        // prepared tree (paired_steps shape)
    flatAgents: [],    // flattened recursion for x-for

    // Side panel state
    tabs: [],          // [{id, title, content, rawJson, jsonMode, loading}]
    activeId: null,

    // Resizable split
    leftWidth: null,

    // Trace view state — drives the left-pane toggle between
    // hierarchical "Tree" (the original spawn-indented agent
    // outline) and flat "Table" (one row per event, sortable +
    // filterable like /qa/sessions). Persisted in URL params:
    //   ?view=table     toggle
    //   ?tlabel=foo     label substring filter
    //   ?tagent=name    agent-name dropdown
    //   ?tkind=kind     event-kind dropdown
    //   ?tsort=col:dir  sort column + direction
    traceViewMode: 'tree',
    traceEvents: [],
    traceLoading: false,
    tableFilters: { label: '', agent: '', kind: '' },
    tableSort: { col: 'ts', dir: 'asc' },

    async init() {
      this.runId = window.RUN_ID || '';
      this.qaBase = window.QA_BASE_PATH || '';
      this.fromQuery = new URLSearchParams(location.search).get('from') || '';
      this._restoreTableUrl();

      await this._refreshTraceData();
      // Live trace tail: while the run is still going, re-pull meta +
      // tree every 5s so new steps surface without a manual reload.
      // Stops itself the first tick after status flips out of
      // 'running' — see _refreshTraceData. Independent of the
      // diagram's own auto-refresh timer (`_diagramTimer`); the two
      // serve different panels and can be off/on independently.
      this._maybeStartTraceTimer();
    },

    async _refreshTraceData() {
      try {
        const [metaR, treeR] = await Promise.all([
          fetch(`${this.qaBase}/api/runs/${this.runId}`),
          fetch(`${this.qaBase}/api/runs/${this.runId}/json`),
        ]);
        if (metaR.ok) {
          const m = await metaR.json();
          this.meta = m.data || {};
        }
        if (treeR.ok) {
          this.tree = await treeR.json();
          this.flatAgents = this._flatten(this.tree, '', true);
        }
        // Table mode shares the same event stream — keep it in sync
        // while the run grows, so the table tail matches the tree.
        if (this.traceViewMode === 'table') {
          await this.loadTable();
        }
      } catch (e) {
        console.error('sessionTraceView _refreshTraceData failed', e);
      }
    },

    _maybeStartTraceTimer() {
      this._stopTraceTimer();
      if (this.meta.status && this.meta.status !== 'running') return;
      this._traceTimer = setInterval(async () => {
        await this._refreshTraceData();
        if (this.meta.status && this.meta.status !== 'running') {
          this._stopTraceTimer();
        }
      }, 5000);
    },

    _stopTraceTimer() {
      if (this._traceTimer) {
        clearInterval(this._traceTimer);
        this._traceTimer = null;
      }
    },

    // Recurse tree.children → flat array. Each agent keeps its own
    // paired_steps inline; rendering walks the flat list with parent
    // labels so spawn relationships are still visible without nested
    // <details> indentation hell.
    _flatten(node, parentName, isRoot) {
      if (!node) return [];
      const out = [{
        ...node,
        parent_name: parentName,
        is_root: !!isRoot,
      }];
      for (const c of (node.children || [])) {
        out.push(...this._flatten(c, node.agent_name, false));
      }
      return out;
    },

    _restoreTableUrl() {
      const qs = new URLSearchParams(location.search);
      if (qs.get('view') === 'table') this.traceViewMode = 'table';
      this.tableFilters.label = qs.get('tlabel') || '';
      this.tableFilters.agent = qs.get('tagent') || '';
      this.tableFilters.kind  = qs.get('tkind')  || '';
      const ts = qs.get('tsort');
      if (ts && ts.includes(':')) {
        const [col, dir] = ts.split(':');
        this.tableSort = { col, dir };
      }
    },

    _pushTableUrl() {
      const qs = new URLSearchParams(location.search);
      // Replace only the table-state params, leaving `from=...` etc. alone.
      qs.set('view', this.traceViewMode);
      if (this.traceViewMode === 'tree') {
        // No table-specific keys in tree mode; URL stays clean.
        for (const k of ['tlabel', 'tagent', 'tkind', 'tsort']) qs.delete(k);
      } else {
        if (this.tableFilters.label) qs.set('tlabel', this.tableFilters.label);
        else qs.delete('tlabel');
        if (this.tableFilters.agent) qs.set('tagent', this.tableFilters.agent);
        else qs.delete('tagent');
        if (this.tableFilters.kind)  qs.set('tkind',  this.tableFilters.kind);
        else qs.delete('tkind');
        const ts = `${this.tableSort.col}:${this.tableSort.dir}`;
        if (ts !== 'ts:asc') qs.set('tsort', ts);
        else qs.delete('tsort');
      }
      const url = location.pathname
        + (qs.toString() ? '?' + qs.toString() : '');
      history.replaceState({}, '', url);
    },

    async setViewMode(mode) {
      this.traceViewMode = mode;
      this._pushTableUrl();
      if (mode === 'table' && !this.traceEvents.length) {
        await this.loadTable();
      }
    },

    async loadTable() {
      this.traceLoading = true;
      try {
        const scopeUri = `session://${this.runId}`;
        // Reuse the diagram filter / budget so G6 click-filter and
        // ± budget propagate to the table for free.
        const actorParam = this.selectedActors.length
          ? `&actor=${encodeURIComponent(this.selectedActors.join(','))}`
          : '';
        const edgeParam = this.selectedEdges.length
          ? `&edge=${encodeURIComponent(
              this.selectedEdges.map(e => `${e.src}>${e.tgt}`).join(','))}`
          : '';
        const url = `${this.qaBase}/api/diagram`
          + `?scope=${encodeURIComponent(scopeUri)}`
          + `&format=events`
          + `&max_events=${this.diagramBudget}`
          + actorParam + edgeParam;
        const r = await fetch(url);
        if (r.ok) this.traceEvents = await r.json();
      } catch (e) { /* ignore — keep prior */ }
      finally { this.traceLoading = false; }
    },

    setSort(col) {
      if (this.tableSort.col === col) {
        this.tableSort.dir = this.tableSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        this.tableSort = { col, dir: 'asc' };
      }
      this._pushTableUrl();
    },

    get filteredTableEvents() {
      const f = this.tableFilters;
      let rows = this.traceEvents.slice();
      if (f.label) {
        const needle = f.label.toLowerCase();
        rows = rows.filter(e => (e.label || '').toLowerCase().includes(needle));
      }
      if (f.agent) {
        rows = rows.filter(e =>
          (e.actor_label || '').includes(f.agent) ||
          (e.target_label || '').includes(f.agent));
      }
      if (f.kind) rows = rows.filter(e => e.kind === f.kind);
      const { col, dir } = this.tableSort;
      const mul = dir === 'desc' ? -1 : 1;
      rows.sort((a, b) => {
        const va = a[col] ?? ''; const vb = b[col] ?? '';
        if (va < vb) return -1 * mul;
        if (va > vb) return  1 * mul;
        return 0;
      });
      return rows;
    },

    get tableAgentOptions() {
      const s = new Set();
      for (const e of this.traceEvents) {
        if (e.actor_label) s.add(e.actor_label);
        if (e.target_label) s.add(e.target_label);
      }
      return Array.from(s).sort();
    },

    get tableKindOptions() {
      const s = new Set(this.traceEvents.map(e => e.kind));
      return Array.from(s).sort();
    },

    onTableFilterChange() { this._pushTableUrl(); },

    // Click a row → open the step's request/response/history tabs.
    // Skip rows whose actor isn't an agent (system rows have no step).
    onRowClick(e) {
      if (!e.actor || !e.actor.startsWith('agent:') || e.step == null) return;
      const aid = e.actor.split(':')[2] || '';
      this.openStepDetails(aid, e.step);
    },

    formatTs(iso) {
      if (!iso) return '';
      // Show only the HH:MM:SS.mmm portion (time-of-day) — full ISO
      // is too wide for a table column, and the row's run context
      // makes the date redundant.
      return iso.length > 19 ? iso.substring(11, 23) : iso.substring(11);
    },

    get backHref() {
      if (!this.fromQuery) return '';
      return `${this.qaBase}/qa/traces?${this.fromQuery}`;
    },

    get activeTab() {
      return this.tabs.find(t => t.id === this.activeId) || null;
    },

    // Open a payload tab (call/result/messages) on the right panel.
    // url = API endpoint that returns the raw payload; msgIndex picks
    // out one message from a `messages` array if provided.
    // One click on a step → three side-panel tabs auto-loaded.
    // Semantics are framed around the TOOL CALL — that's the unit
    // the agent operates on per step:
    //   request  → the tool the agent invoked (the parsed
    //              `tool_calls` array from the LLM response).
    //              `/api/runs/{id}/step/{agent}/{n}/call`
    //   response → what the tool returned, post-execution.
    //              `/api/runs/{id}/step/{agent}/{n}/result`
    //   history  → the full messages context at step N+1 — that's
    //              the moment when the tool result has been folded
    //              back into the conversation. If the agent didn't
    //              reach N+1 (crashed, ran out of budget, this IS
    //              the last step), the tab shows `(end of
    //              conversation — agent did not reach this step)`.
    //              `/api/runs/{id}/step/{agent}/{n+1}/messages`
    // Existing manual tabs stay open; `openTab` dedupes by id.
    async openStepDetails(agentId, step) {
      const stem = `${(agentId || '').substring(0, 8)}/s${step}`;
      const base = `${this.qaBase}/api/runs/${this.runId}/step/${agentId}`;
      // History = step N's own LLM request — what the agent saw
      // when it decided this step. Previously this read N+1's
      // request (which includes step N's tool results), but that
      // made the LAST step's history always 404 (no N+1 exists for
      // done / final mode:single text step). N's own messages is
      // available for every step uniformly — same code path for
      // tool steps, text-only steps, judges, last-step done.
      await Promise.all([
        this.openTab(`req:${agentId}:${step}`,
                     `${stem} request`,
                     `${base}/${step}/call`),
        this.openTab(`resp:${agentId}:${step}`,
                     `${stem} response`,
                     `${base}/${step}/result`),
        this.openTab(`hist:${agentId}:${step}`,
                     `${stem} history`,
                     `${base}/${step}/messages`),
      ]);
      this.activeId = `req:${agentId}:${step}`;
    },

    async openTab(id, title, url, msgIndex) {
      // De-dupe — if tab is already open, just activate it.
      if (this.tabs.find(t => t.id === id)) {
        this.activeId = id;
        return;
      }
      this.tabs.push({
        id, title,
        content: 'Loading…',
        rawJson: null,
        jsonMode: false,
        loading: true,
      });
      this.activeId = id;

      let content;
      let rawJson = null;
      try {
        // Endpoints that have a server-side text renderer (see
        // tracing/server/messages_render.py + bench-log endpoint):
        // fetch both views once up front so the { } JSON toggle is
        // local (no refetch).
        //   /messages   → human-readable transcript (default)
        //   /call       → pretty-printed tool-call args / text content
        //   /bench-log  → combined stdout/stderr/system text (default)
        //                 with meta+streams JSON envelope for toggle
        // /result is already plain-text; everything else falls back
        // to client-side JSON pretty-printing.
        const supportsTextView = /\/(messages|call|bench-log)$/.test(url);
        if (supportsTextView) {
          const sep = url.includes('?') ? '&' : '?';
          const [tResp, jResp] = await Promise.all([
            fetch(`${url}${sep}as=text`),
            fetch(url),
          ]);
          if (!tResp.ok) {
            this._finishTab(id, this._notFoundHint(tResp.status, url), null);
            return;
          }
          content = await tResp.text();
          if (jResp.ok) {
            try { rawJson = JSON.stringify(await jResp.json(), null, 2); }
            catch (e) { /* leave rawJson null — toggle button stays disabled */ }
          }
          this._finishTab(id, content, rawJson);
          return;
        }

        const resp = await fetch(url);
        const raw = await resp.text();
        if (!resp.ok) {
          this._finishTab(id, this._notFoundHint(resp.status, url, raw), null);
          return;
        }
        content = raw;
        if (msgIndex !== undefined && msgIndex !== null) {
          try {
            const msgs = JSON.parse(raw);
            if (Array.isArray(msgs) && msgs[msgIndex]) {
              const msg = msgs[msgIndex];
              rawJson = JSON.stringify(msg, null, 2);
              content = typeof msg.content === 'string' && msg.content
                ? msg.content
                : rawJson;
            }
          } catch (e) { /* keep raw */ }
        } else {
          try {
            const parsed = JSON.parse(raw);
            rawJson = JSON.stringify(parsed, null, 2);
            content = rawJson;
          } catch (e) { /* not JSON */ }
        }
      } catch (e) {
        content = 'Error: ' + (e && e.message ? e.message : e);
      }

      this._finishTab(id, content, rawJson);
    },

    // Inline-friendly replacements for the FastAPI error blob on
    // expected 404s — used by the endpoint-text fetcher above and
    // the generic raw-fetch path below.
    _notFoundHint(status, url, raw) {
      if (status === 404) {
        if (url.includes('/messages')) return '(end of conversation — agent did not reach this step)';
        if (url.includes('/result'))   return '(no tool result for this step — control-flow only)';
        if (url.includes('/call'))     return '(no call payload for this step)';
      }
      return `HTTP ${status}${raw ? '\n\n' + raw : ''}`;
    },

    // Replace the tab slot in `this.tabs` instead of mutating the
    // local reference. Alpine's reactive proxy wraps the array but
    // the original tab object (built before `push`) is un-proxied —
    // mutating its fields directly didn't trigger re-render of
    // `x-text="activeTab.content"`. Reassigning the array index
    // forces the setter to fire so the panel updates as soon as
    // the fetch resolves.
    _finishTab(id, content, rawJson) {
      const i = this.tabs.findIndex(t => t.id === id);
      if (i < 0) return;
      this.tabs[i] = {
        ...this.tabs[i],
        content,
        rawJson: (content === rawJson) ? null : rawJson,
        loading: false,
      };
    },

    closeTab(id) {
      this.tabs = this.tabs.filter(t => t.id !== id);
      if (this.activeId === id) {
        this.activeId = this.tabs.length
          ? this.tabs[this.tabs.length - 1].id
          : null;
      }
    },

    toggleJson(tab) {
      if (!tab.rawJson) return;
      tab.jsonMode = !tab.jsonMode;
    },

    async copyActive() {
      const t = this.activeTab;
      if (!t) return;
      const content = t.jsonMode && t.rawJson ? t.rawJson : t.content;
      try {
        await navigator.clipboard.writeText(content);
        this._toast('✓ Copied');
      } catch (e) {
        this._toast('Error: ' + e.message);
      }
    },

    _toast(msg) {
      const el = document.createElement('div');
      el.className = 'toast show';
      el.textContent = msg;
      el.style.cssText = 'position:fixed;bottom:20px;right:20px;'
        + 'padding:8px 14px;background:#21262d;color:#c9d1d9;'
        + 'border-radius:4px;z-index:9999;border:1px solid #30363d;'
        + 'box-shadow:0 2px 8px rgba(0,0,0,0.3);';
      document.body.appendChild(el);
      setTimeout(() => el.remove(), 1500);
    },

    formatDuration(ms) {
      if (!ms || ms < 0) return '?';
      if (ms < 1000) return `${ms}ms`;
      if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
      const m = Math.floor(ms / 60_000);
      const s = Math.floor((ms % 60_000) / 1000);
      return `${m}m${s.toString().padStart(2, '0')}s`;
    },

    // ── Diagram (Mermaid / D2 / G6) ──────────────────────────────────
    //
    // The right-side panel for tool payloads is independent from this
    // block; both live on the page. Diagram lazy-loads its renderer
    // library the first time the user opens the block. /api/diagram
    // returns text for mermaid/d2 and JSON for g6.
    diagramScope: 'session',
    diagramFormat: 'mermaid',
    diagramSource: '',
    diagramG6: null,
    diagramLoading: false,
    diagramError: '',
    diagramOpen: false,          // <details> open state — drives auto-refresh
    diagramAutoRefresh: true,    // user-toggleable; default on
    diagramBudget: 60,           // max_events; default fits on one screen
    d2PlayHref: '',              // computed in _buildD2PlayHref after each load
    // Filter state — driven by G6 click handlers, applied to ALL
    // three formats via /api/diagram?actor=…&edge=…. Each entry is
    // a canonical actor URI (`agent:<run>:<aid>`, `system:diff`).
    selectedActors: [],
    selectedEdges: [],           // [{src, tgt}] using URI keys
    _diagramLoadedOnce: false,
    _diagramTimer: null,
    _g6Instance: null,
    _g6NodeUri: {},              // safe_id → canonical URI lookup

    // play.d2lang.com URL format is `?script=<urlsafe-base64(raw-deflate(source))>`,
    // NOT `?script=<urlencoded(source)>`. Sending raw-encoded source
    // made the page load and immediately reset to its default
    // `x -> y` script because the param wasn't recognised. Use the
    // browser's native CompressionStream (Chrome 80+, FF 113+,
    // Safari 16.4+) to deflate without pulling in pako.
    async _buildD2PlayHref(source) {
      if (!source) return '';
      if (!('CompressionStream' in window)) {
        // Older browser — no inline encoder, can't build a working
        // play URL. The UI will show the Copy fallback.
        return '';
      }
      try {
        const stream = new Blob([source]).stream()
          .pipeThrough(new CompressionStream('deflate-raw'));
        const reader = stream.getReader();
        const chunks = [];
        let total = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          total += value.length;
        }
        const buf = new Uint8Array(total);
        let off = 0;
        for (const c of chunks) { buf.set(c, off); off += c.length; }
        // Base64 → urlsafe (`+` → `-`, `/` → `_`). Padding stays.
        let bin = '';
        for (let i = 0; i < buf.length; i++) bin += String.fromCharCode(buf[i]);
        const b64 = btoa(bin).replace(/\+/g, '-').replace(/\//g, '_');
        const url = `https://play.d2lang.com/?script=${b64}`;
        // CloudFront still caps at ~8KB even on compressed input.
        return url.length <= 8000 ? url : '';
      } catch (e) {
        return '';
      }
    },

    bumpBudget(direction) {
      // Geometric steps so the value scales naturally — small
      // adjustments at the low end, bigger jumps at the high end.
      const cur = this.diagramBudget;
      const next = direction > 0
        ? Math.min(2000, Math.round(cur * 1.5))
        : Math.max(20, Math.round(cur / 1.5));
      if (next === cur) return;
      this.diagramBudget = next;
      this.loadDiagram();
      if (this.traceViewMode === 'table') this.loadTable();
    },

    onDiagramToggle(el) {
      this.diagramOpen = !!el.open;
      if (this.diagramOpen) {
        this.loadDiagram();
        this._maybeStartTimer();
      } else {
        this._stopTimer();
      }
    },

    _maybeStartTimer() {
      // Auto-refresh only while the underlying run is still going.
      // Once status flips to completed/failed/cancelled the diagram
      // is final; polling becomes pointless and just thrashes the
      // mermaid/G6 renderer.
      this._stopTimer();
      if (!this.diagramAutoRefresh) return;
      if (!this.diagramOpen) return;
      if (this.meta.status && this.meta.status !== 'running') return;
      this._diagramTimer = setInterval(() => {
        this._refreshMetaAndDiagram();
      }, 5000);
    },

    _stopTimer() {
      if (this._diagramTimer) {
        clearInterval(this._diagramTimer);
        this._diagramTimer = null;
      }
    },

    async _refreshMetaAndDiagram() {
      // Refresh meta first — if the run finished, we'll render one
      // last diagram and then stop the timer.
      try {
        const r = await fetch(`${this.qaBase}/api/runs/${this.runId}`);
        if (r.ok) {
          const m = await r.json();
          if (m.data) this.meta = m.data;
        }
      } catch (e) { /* keep prior meta */ }
      await this.loadDiagram();
      // The table view shares the same event stream; refresh in
      // step so a live run's table grows without a manual reload.
      if (this.traceViewMode === 'table') await this.loadTable();
      if (this.meta.status && this.meta.status !== 'running') {
        this._stopTimer();
      }
    },

    toggleAutoRefresh() {
      this.diagramAutoRefresh = !this.diagramAutoRefresh;
      if (this.diagramAutoRefresh) {
        this._maybeStartTimer();
      } else {
        this._stopTimer();
      }
    },

    async loadDiagram() {
      this._diagramLoadedOnce = true;
      this.diagramError = '';
      this.diagramLoading = true;

      // Build the scope URI from the current selection.
      const scopeUri = this.diagramScope === 'scenario_run'
        ? `scenario_run://${this.meta.scenario_run_id || this.runId}`
        : `session://${this.runId}`;
      // Carry the click-driven filter through the URL so the diagram
      // re-fetch on auto-refresh / scope change preserves the
      // current selection.
      const actorParam = this.selectedActors.length
        ? `&actor=${encodeURIComponent(this.selectedActors.join(','))}`
        : '';
      const edgeParam = this.selectedEdges.length
        ? `&edge=${encodeURIComponent(
            this.selectedEdges.map(e => `${e.src}>${e.tgt}`).join(','))}`
        : '';
      const url = `${this.qaBase}/api/diagram`
        + `?scope=${encodeURIComponent(scopeUri)}`
        + `&format=${this.diagramFormat}`
        + `&max_events=${this.diagramBudget}`
        + actorParam + edgeParam;
      try {
        const resp = await fetch(url);
        if (!resp.ok) {
          this.diagramError = `diagram fetch failed (${resp.status})`;
          this.diagramLoading = false;
          return;
        }
        if (this.diagramFormat === 'g6') {
          this.diagramG6 = await resp.json();
          this.diagramSource = '';
          this.d2PlayHref = '';
          await this._renderG6();
        } else {
          this.diagramSource = await resp.text();
          this.diagramG6 = null;
          if (this.diagramFormat === 'mermaid') {
            this.d2PlayHref = '';
            await this._renderMermaid();
          } else if (this.diagramFormat === 'd2') {
            // Build play.d2lang.com URL after source is in hand —
            // CompressionStream encoding is async.
            this.d2PlayHref = await this._buildD2PlayHref(this.diagramSource);
          }
        }
      } catch (e) {
        this.diagramError = String(e && e.message || e);
      } finally {
        this.diagramLoading = false;
      }
    },

    async _loadScript(src) {
      // De-dupe: once a script tag with this src exists, just wait
      // until window has whatever symbol shows up.
      if (document.querySelector(`script[data-src="${src}"]`)) {
        // Already (being) loaded — short-poll for ready.
        for (let i = 0; i < 200; i++) {
          await new Promise(r => setTimeout(r, 25));
          if (window.mermaid || window.G6) return;
        }
        return;
      }
      const s = document.createElement('script');
      s.src = src;
      s.dataset.src = src;
      const ready = new Promise((resolve, reject) => {
        s.onload = resolve;
        s.onerror = () => reject(new Error(`failed to load ${src}`));
      });
      document.head.appendChild(s);
      await ready;
    },

    async _renderMermaid() {
      await this._loadScript('https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js');
      if (!window.mermaid) {
        this.diagramError = 'mermaid did not load';
        return;
      }
      // Re-init in dark theme on each call so a re-render picks up
      // styling correctly (mermaid caches per-page settings).
      window.mermaid.initialize({ startOnLoad: false, theme: 'dark',
                                  securityLevel: 'loose' });
      const host = document.getElementById('diagram-mermaid-host');
      if (!host) return;
      // mermaid.render needs a unique id and the source string. The
      // returned svg goes into the host. We wipe the host first so a
      // re-render doesn't pile up SVGs.
      host.innerHTML = '';
      try {
        const id = 'mmd-' + Math.random().toString(36).slice(2);
        const { svg } = await window.mermaid.render(id, this.diagramSource);
        host.innerHTML = svg;
      } catch (e) {
        // mermaid throws on parser error — surface so we can fix the
        // generator without a silent failure.
        this.diagramError = 'mermaid parse error: ' + (e.message || e);
      }
    },

    async _renderG6() {
      await this._loadScript('https://cdn.jsdelivr.net/npm/@antv/g6@4/dist/g6.min.js');
      if (!window.G6) {
        this.diagramError = 'G6 did not load';
        return;
      }
      const host = document.getElementById('diagram-g6-host');
      if (!host) return;
      // Destroy previous instance before recreating — G6 leaks event
      // listeners otherwise on scope/format toggles.
      if (this._g6Instance) {
        try { this._g6Instance.destroy(); } catch (e) {}
        this._g6Instance = null;
      }
      host.innerHTML = '';
      const width = host.clientWidth || 800;
      const height = host.clientHeight || 480;
      const graph = new window.G6.Graph({
        container: host, width, height,
        // `renderer: 'svg'` lets us export the diagram as scalable
        // SVG by serialising the `<svg>` node G6 builds — same path
        // we use for Mermaid. Canvas render mode is faster on huge
        // graphs but agent-interaction graphs are tiny, the trade
        // doesn't matter and we gain export quality.
        renderer: 'svg',
        layout: { type: 'force', preventOverlap: true, nodeStrength: -100, edgeStrength: 0.4, linkDistance: 120 },
        defaultNode: {
          size: 32,
          style: { fill: '#1f6feb44', stroke: '#58a6ff', lineWidth: 1.5 },
          labelCfg: { style: { fill: '#c9d1d9', fontSize: 12, background: { fill: '#0d1117', padding: [2,4,2,4] } } },
        },
        defaultEdge: {
          style: { stroke: '#8b949e', endArrow: true, opacity: 0.7 },
          labelCfg: { autoRotate: true, style: { fill: '#8b949e', fontSize: 10 } },
        },
        nodeStateStyles: {
          hover:    { fill: '#1f6feb88', stroke: '#79c0ff', lineWidth: 2 },
          selected: { lineWidth: 4, stroke: '#f0883e' },
        },
        edgeStateStyles: {
          selected: { stroke: '#f0883e', lineWidth: 4, opacity: 1 },
        },
        modes: { default: ['drag-canvas', 'zoom-canvas', 'drag-node'] },
      });
      // Colours come from the server (role-keyed palette shared with
      // the mermaid/d2 renderers, see diagram._ROLE_PALETTE). Keeps
      // the same agent the same colour across all three formats.
      const nodes = (this.diagramG6.nodes || []).map(n => ({
        ...n,
        style: {
          fill: (n.fill || '#1f6feb') + '44',  // re-apply translucency
          stroke: n.stroke || '#58a6ff',
          lineWidth: 1.5,
        },
      }));
      // Edge labels show call count; thicker for hotter edges.
      const edges = (this.diagramG6.edges || []).map(e => ({
        ...e,
        style: { stroke: '#8b949e', endArrow: true,
                  opacity: 0.7,
                  lineWidth: Math.min(1 + Math.log2(e.weight || 1), 6) },
      }));
      graph.data({ nodes, edges });
      graph.render();

      // Map safe_id → canonical actor URI. The diagram backend
      // builds nodes keyed by `_safe_id(uri)`; we need the inverse
      // so click handlers can send the canonical URI back through
      // `/api/diagram?actor=…&edge=…`.
      this._g6NodeUri = {};
      for (const n of (this.diagramG6.nodes || [])) {
        // The server emits each node with id == safe_id of an actor
        // URI plus role/kind info. We carry the URI as a hidden
        // attribute on the node when building the JSON server-side
        // (see diagram._participants_of). Fall back to label-prefix
        // heuristics if not present.
        const uri = n.uri || this._uriFromNode(n);
        if (uri) this._g6NodeUri[n.id] = uri;
      }

      graph.on('node:mouseenter', e => graph.setItemState(e.item, 'hover', true));
      graph.on('node:mouseleave', e => graph.setItemState(e.item, 'hover', false));
      graph.on('node:click', e => this._onG6NodeClick(e.item));
      graph.on('edge:click', e => this._onG6EdgeClick(e.item));
      graph.on('canvas:click', () => this.clearFilter());
      this._g6Instance = graph;
      // Re-apply persistent visual selection after re-render
      // (auto-refresh / scope change rebuilds the graph).
      this._g6SyncSelection();
    },

    _uriFromNode(n) {
      // Server-side `to_g6` stores `kind` ∈ {agent, system, human}.
      // For systems, URI is `system:<label>`. For agents, the safe_id
      // already encodes `agent_<run>_<aid>` — invert to
      // `agent:<run>:<aid>` by splitting on the first three `_`.
      if (n.kind === 'system') return `system:${n.label}`;
      if (n.kind === 'agent' && typeof n.id === 'string'
          && n.id.startsWith('agent_')) {
        const parts = n.id.split('_');
        if (parts.length >= 3) return `agent:${parts[1]}:${parts[2]}`;
      }
      return null;
    },

    _onG6NodeClick(item) {
      const id = item.get('id');
      const uri = this._g6NodeUri[id];
      if (!uri) return;
      const idx = this.selectedActors.indexOf(uri);
      if (idx >= 0) this.selectedActors.splice(idx, 1);
      else this.selectedActors.push(uri);
      this._g6SyncSelection();
      this._reloadAfterFilter();
    },

    _onG6EdgeClick(item) {
      const model = item.get('model');
      const src = this._g6NodeUri[model.source];
      const tgt = this._g6NodeUri[model.target];
      if (!src || !tgt) return;
      const key = `${src}>${tgt}`;
      const idx = this.selectedEdges.findIndex(e => `${e.src}>${e.tgt}` === key);
      if (idx >= 0) this.selectedEdges.splice(idx, 1);
      else this.selectedEdges.push({ src, tgt });
      this._g6SyncSelection();
      this._reloadAfterFilter();
    },

    clearFilter() {
      if (!this.selectedActors.length && !this.selectedEdges.length) return;
      this.selectedActors = [];
      this.selectedEdges = [];
      this._g6SyncSelection();
      this._reloadAfterFilter();
    },

    _reloadAfterFilter() {
      this.loadDiagram();
      if (this.traceViewMode === 'table') this.loadTable();
    },

    _g6SyncSelection() {
      // Apply visual `selected` state to G6 items matching the
      // current selection sets. Cheap — graph has ≤20 nodes typical.
      if (!this._g6Instance) return;
      const actorSet = new Set(this.selectedActors);
      const edgeSet = new Set(
        this.selectedEdges.map(e => `${e.src}>${e.tgt}`));
      const g = this._g6Instance;
      g.getNodes().forEach(n => {
        const uri = this._g6NodeUri[n.get('id')];
        g.setItemState(n, 'selected', actorSet.has(uri));
      });
      g.getEdges().forEach(e => {
        const m = e.get('model');
        const src = this._g6NodeUri[m.source];
        const tgt = this._g6NodeUri[m.target];
        const key = `${src}>${tgt}`;
        const revKey = `${tgt}>${src}`;
        g.setItemState(e, 'selected',
                       edgeSet.has(key) || edgeSet.has(revKey));
      });
    },

    isActorSelected(uri) { return this.selectedActors.includes(uri); },
    get hasFilter() {
      return this.selectedActors.length > 0 || this.selectedEdges.length > 0;
    },

    // Three export actions, all format-aware:
    //   📋 Copy   → clipboard, source text (or JSON for G6).
    //   📥 Source → download the source file (.mmd / .d2 / .json).
    //   🖼 Image  → download a rendered image (Mermaid → .svg,
    //              G6 → .png). D2 has no inline render in this UI,
    //              so the image button is hidden for D2.

    _diagramFileStem() {
      return `session-${(this.runId || '').substring(0, 12)}`;
    },

    _sourceForCopy() {
      // G6 doesn't have a text source; serialise the node-edge
      // JSON instead so the user always has SOMETHING on the clipboard.
      if (this.diagramFormat === 'g6') {
        return JSON.stringify(this.diagramG6 || {}, null, 2);
      }
      return this.diagramSource || '';
    },

    async exportDiagramSource() {
      const fmt = this.diagramFormat;
      const stem = this._diagramFileStem();
      try {
        if (fmt === 'mermaid') {
          this._download(this.diagramSource || '', `${stem}.mmd`,
                         'text/plain');
          this._toast(`✓ exported ${stem}.mmd`);
        } else if (fmt === 'd2') {
          this._download(this.diagramSource || '', `${stem}.d2`,
                         'text/plain');
          this._toast(`✓ exported ${stem}.d2`);
        } else if (fmt === 'g6') {
          this._download(this._sourceForCopy(), `${stem}.json`,
                         'application/json');
          this._toast(`✓ exported ${stem}.json`);
        }
      } catch (e) {
        this._toast('export failed: ' + (e.message || e));
      }
    },

    async exportDiagramImage() {
      const fmt = this.diagramFormat;
      const stem = this._diagramFileStem();
      try {
        if (fmt === 'mermaid') {
          const svg = document.querySelector('#diagram-mermaid-host svg');
          if (!svg) { this._toast('nothing to export (no rendered svg)'); return; }
          // Standalone SVG needs the xmlns attrs that the inline
          // render omits.
          const cloned = svg.cloneNode(true);
          cloned.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
          cloned.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
          const xml = new XMLSerializer().serializeToString(cloned);
          this._download('<?xml version="1.0" encoding="UTF-8"?>\n' + xml,
                         `${stem}.svg`, 'image/svg+xml');
          this._toast(`✓ exported ${stem}.svg`);
        } else if (fmt === 'g6') {
          // We init G6 with `renderer: 'svg'` so the host already
          // has a real <svg> we can serialise — same path as Mermaid.
          const svg = document.querySelector('#diagram-g6-host svg');
          if (!svg) { this._toast('graph not ready'); return; }
          const cloned = svg.cloneNode(true);
          cloned.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
          cloned.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
          const xml = new XMLSerializer().serializeToString(cloned);
          this._download('<?xml version="1.0" encoding="UTF-8"?>\n' + xml,
                         `${stem}.svg`, 'image/svg+xml');
          this._toast(`✓ exported ${stem}.svg`);
        }
      } catch (e) {
        this._toast('image export failed: ' + (e.message || e));
      }
    },

    _download(content, filename, mime) {
      const blob = new Blob([content], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    },

    async copyDiagramSource() {
      try {
        await navigator.clipboard.writeText(this._sourceForCopy());
        this._toast(this.diagramFormat === 'g6'
                    ? '✓ JSON copied' : '✓ source copied');
      } catch (e) {
        this._toast('error: ' + (e.message || e));
      }
    },

    // ── Resizable split divider ──────────────────────────────────────
    startDrag(e) {
      const self = this;
      const leftEl = e.target.previousElementSibling;
      const startX = e.clientX;
      const startW = leftEl.getBoundingClientRect().width;
      const onMove = (ev) => {
        const dx = ev.clientX - startX;
        const newW = Math.max(280, Math.min(window.innerWidth - 320, startW + dx));
        leftEl.style.flex = 'none';
        leftEl.style.width = newW + 'px';
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    },
  }));
});
