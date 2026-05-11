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

    // Resizable split divider
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
