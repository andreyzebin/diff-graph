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

    async init() {
      this.runId = window.RUN_ID || '';
      this.qaBase = window.QA_BASE_PATH || '';
      this.fromQuery = new URLSearchParams(location.search).get('from') || '';

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
      } catch (e) {
        console.error('sessionTraceView init failed', e);
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
        const resp = await fetch(url);
        const raw = await resp.text();
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
            // Tool-call array: pretty-print each argument JSON.
            if (Array.isArray(parsed) && parsed.length && parsed[0].arguments !== undefined) {
              const parts = parsed.map(tc => {
                try { return JSON.stringify(JSON.parse(tc.arguments), null, 2); }
                catch (e) { return tc.arguments; }
              });
              content = parts.join('\n\n---\n\n');
            } else {
              content = rawJson;
            }
          } catch (e) { /* not JSON */ }
        }
      } catch (e) {
        content = 'Error: ' + (e && e.message ? e.message : e);
      }

      // Replace the tab slot in `this.tabs` instead of mutating the
      // local reference. Alpine's reactive proxy wraps the array but
      // the `tab` object we built before `push` is the un-proxied
      // original — mutating its fields directly didn't trigger the
      // re-render of `x-text="activeTab.content"`. Replacing the
      // entry forces the array setter to fire so the panel updates
      // as soon as the fetch resolves (no more "Loading…" stuck
      // until you click another tab and back).
      const i = this.tabs.findIndex(t => t.id === id);
      if (i >= 0) {
        this.tabs[i] = {
          ...this.tabs[i],
          content,
          rawJson: (content === rawJson) ? null : rawJson,
          loading: false,
        };
      }
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
    _diagramLoadedOnce: false,
    _diagramTimer: null,
    _g6Instance: null,

    // play.d2lang.com is fronted by CloudFront which rejects URLs
    // > ~8KB with 414. Encoded source can balloon — return empty
    // when over the safe threshold so the UI shows a "too large
    // for URL" hint instead of a link that won't open.
    get d2PlayHref() {
      if (!this.diagramSource) return '';
      const encoded = encodeURIComponent(this.diagramSource);
      const url = `https://play.d2lang.com/?script=${encoded}`;
      // Total URL bytes (UTF-8). CloudFront caps requests at
      // ~8192 bytes; leave a small margin for header/cookie
      // overhead. If the diagram source exceeds, the user gets a
      // hint to use Copy and paste into play.d2lang.com manually,
      // or shrinks via the `−` budget button.
      return url.length <= 8000 ? url : '';
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
      const url = `${this.qaBase}/api/diagram`
        + `?scope=${encodeURIComponent(scopeUri)}`
        + `&format=${this.diagramFormat}`
        + `&max_events=${this.diagramBudget}`;
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
          await this._renderG6();
        } else {
          this.diagramSource = await resp.text();
          this.diagramG6 = null;
          if (this.diagramFormat === 'mermaid') {
            await this._renderMermaid();
          }
          // D2: source already in `diagramSource`, the <pre> binds to it.
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
          hover: { fill: '#1f6feb88', stroke: '#79c0ff', lineWidth: 2 },
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
      graph.on('node:mouseenter', e => graph.setItemState(e.item, 'hover', true));
      graph.on('node:mouseleave', e => graph.setItemState(e.item, 'hover', false));
      this._g6Instance = graph;
    },

    async copyDiagramSource() {
      try {
        await navigator.clipboard.writeText(this.diagramSource);
        this._toast('✓ source copied');
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
