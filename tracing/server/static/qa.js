// Quality-API UI components — Alpine.data() registrations.
// Loaded once per session from <head>; survives HTMX body swaps
// because the registry persists across DOM updates.
// Each x-data="NAME" (without parens) looks up its factory
// in this registry, so newly-swapped DOM finds its component
// even though the inline script that *would* define it is gone.

// Replaces the inline <script>function NAME(){return{…}}</script>
// blocks that used to live inside each qa_*.html body.

// HTMX glue: when hx-boost swaps the <body>, Alpine's original
// MutationObserver was attached to the OLD body which gets removed.
// The new body's x-data attributes are never auto-initialised.
// Re-init the swapped subtree manually after each swap.
document.addEventListener('htmx:afterSettle', (e) => {
  if (window.Alpine && e.detail && e.detail.elt) {
    try { window.Alpine.initTree(e.detail.elt); } catch (err) { /* ignore */ }
  }
});

document.addEventListener('alpine:init', () => {

  Alpine.data('dash', () => ({
    
        totalRuns: '…', agentRuns: '…', judgeRuns: '…',
        mutations: '…', scenarios: '…',
        byProvider: [], byScenario: [],
        async load() {
          const get = async (path) => (await (await fetch(`${window.QA_BASE_PATH || ''}${path}`)).json()).data;
    
          // counts via /api/search/runs?limit=1 — we just need meta.total
          const fetchCount = async (extra = '') => {
            const r = await (await fetch(`${window.QA_BASE_PATH || ''}/api/search/runs?limit=1${extra}`)).json();
            return (r.meta && r.meta.total) || 0;
          };
          this.totalRuns = await fetchCount();
          this.agentRuns = await fetchCount('&kind=agent');
          this.judgeRuns = await fetchCount('&kind=judge');
    
          this.byProvider = await get('/api/search/aggregates/by_provider');
          this.byScenario = await get('/api/search/aggregates/by_scenario');
    
          // unique mutations / scenarios — count distinct keys via tables loaded
          this.scenarios = this.byScenario.length;
          const muts = new Set();
          // crude — sample first page of runs to estimate; replace with /api/aggregates/by_mutation later
          const runs = await get('/api/search/runs?limit=200');
          runs.forEach(r => { if (r.mutation) muts.add(r.mutation); });
          this.mutations = muts.size;
        },
      
  }))

  Alpine.data('runsView', () => ({
    
        dims: { kind: [], agent_name: [], model: [], scenario_id: [],
                generation: [], project: [], status: [],
                scenario_tags: [] },
        // Inline initialiser — calling this.defaultFilters() in the
        // object literal would fail (this is undefined at that point).
        filters: {
          kind: '', agent: '', model: '', scenario: '',
          generation: '', mutation: '', project: '', status: '',
          scenario_tag_arr: [],
          file: '', jira: '', duration_gt_ms: null,
          limit: 50, offset: 0, sort: 'started_at', order: 'desc',
        },
        rows: [],
        total: 0,
        hasMore: false,
        meta: '',
        async init() {
          // Fetch dropdown options + read URL params, then do first search.
          await this.refreshDimensions();
          this.applyUrlParams();
          await this.search(/* skipUrlPush */ true);
          // Keep dropdowns fresh — new mutations / scenarios / providers
          // appear as the bench keeps producing runs.
          setInterval(() => this.refreshDimensions(), 30_000);
        },
        async refreshDimensions() {
          try {
            const r = await fetch(`${window.QA_BASE_PATH || ''}/api/search/dimensions`);
            const j = await r.json();
            if (j && j.data) this.dims = j.data;
          } catch (e) { /* keep last known dims */ }
        },
        defaultFilters() {
          return {
            kind: '', agent: '', model: '', scenario: '',
            generation: '', mutation: '', project: '', status: '',
            scenario_tag_arr: [],
            file: '', jira: '', duration_gt_ms: null,
            limit: 50, offset: 0, sort: 'started_at', order: 'desc',
          };
        },
        applyUrlParams() {
          const sp = new URLSearchParams(window.location.search);
          // Scalar filters: copy the URL param into the corresponding filter key.
          const scalarMap = {
            kind: 'kind', agent: 'agent', model: 'model',
            scenario: 'scenario', generation: 'generation',
            mutation: 'mutation', project: 'project', status: 'status',
            file: 'file', jira: 'jira',
            duration_gt_ms: 'duration_gt_ms',
          };
          for (const [param, key] of Object.entries(scalarMap)) {
            const v = sp.get(param);
            if (v !== null && v !== '') {
              this.filters[key] = (key === 'duration_gt_ms') ? parseInt(v, 10) : v;
            }
          }
          // Multi-value: API uses comma-CSV `scenario_tag=a,b`; UI keeps array.
          // ALSO accept `?scenario_tag=a&scenario_tag=b` repeats for nice URL semantics.
          const collect = (param) => {
            const all = sp.getAll(param);
            const out = [];
            for (const piece of all) {
              piece.split(',').forEach(s => { const t = s.trim(); if (t) out.push(t); });
            }
            return out;
          };
          this.filters.scenario_tag_arr = collect('scenario_tag');
          // limit/offset come from URL too if present.
          const lim = sp.get('limit'); if (lim) this.filters.limit = parseInt(lim, 10);
          const off = sp.get('offset'); if (off) this.filters.offset = parseInt(off, 10);
        },
        pushUrl() {
          // Reflect filters into URL for shareable links / browser back.
          const qs = new URLSearchParams();
          const f = this.filters;
          const scalarOut = { kind: f.kind, agent: f.agent, model: f.model,
            scenario: f.scenario, generation: f.generation, mutation: f.mutation,
            project: f.project, status: f.status, file: f.file, jira: f.jira,
          };
          for (const [k, v] of Object.entries(scalarOut)) {
            if (v) qs.set(k, v);
          }
          if (f.duration_gt_ms) qs.set('duration_gt_ms', String(f.duration_gt_ms));
          if (f.scenario_tag_arr.length) qs.set('scenario_tag', f.scenario_tag_arr.join(','));
          if (f.limit !== 50)  qs.set('limit', String(f.limit));
          if (f.offset !== 0)  qs.set('offset', String(f.offset));
          const url = window.location.pathname + (qs.toString() ? '?' + qs.toString() : '');
          window.history.replaceState({}, '', url);
        },
        addToList(key, value) {
          if (!value) return;
          if (!this.filters[key].includes(value)) {
            this.filters[key] = [...this.filters[key], value];
            this.search();
          }
        },
        async search(skipUrlPush) {
          const qs = new URLSearchParams();
          const f = this.filters;
          // Scalars (use URL param names that match the API).
          const scalarOut = { kind: f.kind, agent: f.agent, model: f.model,
            scenario: f.scenario, generation: f.generation, mutation: f.mutation,
            project: f.project, status: f.status, file: f.file, jira: f.jira,
          };
          for (const [k, v] of Object.entries(scalarOut)) {
            if (v) qs.append(k, v);
          }
          if (f.duration_gt_ms) qs.append('duration_gt_ms', String(f.duration_gt_ms));
          if (f.scenario_tag_arr.length) qs.append('scenario_tag', f.scenario_tag_arr.join(','));
          qs.append('limit', String(f.limit));
          qs.append('offset', String(f.offset));
          qs.append('sort', f.sort);
          qs.append('order', f.order);
          const url = `${window.QA_BASE_PATH || ''}/api/search/runs?` + qs.toString();
          const t0 = performance.now();
          const r = await fetch(url);
          const j = await r.json();
          this.rows = j.data || [];
          this.total = (j.meta && j.meta.total) || 0;
          this.hasMore = (j.meta && j.meta.has_more) || false;
          this.meta = `${this.total} match · ${(performance.now() - t0).toFixed(0)}ms`;
          if (!skipUrlPush) this.pushUrl();
        },
        reset() {
          this.filters = this.defaultFilters();
          this.search();
        },
      
  }))

  Alpine.data('plansView', () => ({

        plans: [],
        page: 1,
        pageSize: 50,
        total: 0,
        async load() {
          const offset = (this.page - 1) * this.pageSize;
          const r = await fetch(`${window.QA_BASE_PATH || ''}/api/qa/plans` +
                                `?limit=${this.pageSize}&offset=${offset}`);
          const j = await r.json();
          this.plans = j.data || [];
          this.total = (j.meta && j.meta.total) || this.plans.length;
        },
        get pageCount() { return Math.max(1, Math.ceil(this.total / this.pageSize)); },
        get rangeText() {
          if (!this.total) return '0';
          const lo = (this.page - 1) * this.pageSize + 1;
          const hi = Math.min(this.total, lo + this.plans.length - 1);
          return `${lo}–${hi} of ${this.total}`;
        },
        async goPage(delta) {
          const next = Math.min(this.pageCount, Math.max(1, this.page + delta));
          if (next === this.page) return;
          this.page = next;
          await this.load();
        },
        bars(p) {
          const pr = p.progress || {};
          const order = ['finished', 'running', 'queued', 'leased', 'error', 'cancelled'];
          const segs = [];
          for (const k of order) {
            const n = pr[k] || 0;
            if (n > 0) segs.push({k: k === 'leased' ? 'running' : k, n});
          }
          return segs.length ? segs : [{k: 'queued', n: 1}];
        },
        bartip(p) {
          const pr = p.progress || {};
          return Object.entries(pr).filter(([k]) => k !== 'total').map(([k,n]) => `${k}: ${n}`).join(' · ');
        },
        progressText(p) {
          const pr = p.progress || {};
          const done = (pr.finished || 0) + (pr.error || 0) + (pr.cancelled || 0);
          const total = pr.total || 0;
          return ` ${done}/${total}`;
        },
        etaText(p) {
          const e = p.eta;
          if (!e || !e.remaining_tasks) return '';
          const s = e.eta_seconds;
          if (!s) return '';
          if (s < 60) return `~${s}s`;
          if (s < 3600) return `~${Math.round(s/60)}m`;
          return `~${(s/3600).toFixed(1)}h`;
        },
        etaTooltip(p) {
          const e = p.eta;
          if (!e) return '';
          const parts = e.per_provider.map(pp =>
            `${pp.provider}: ${pp.remaining_tasks} tasks · ${pp.workers} worker(s) · ~${Math.round(pp.eta_seconds/60)}m`);
          return `eta_at: ${e.eta_at}\n` + parts.join('\n') +
                 `\nbased on ${e.based_on.history_runs} historical runs`;
        },
        canCancel(p) { return p.state !== 'done' && p.state !== 'cancelled'; },
        async cancelPlan(p) {
          const pr = p.progress || {};
          const remaining = (pr.queued || 0) + (pr.leased || 0) + (pr.running || 0);
          if (!confirm(`Cancel plan #${p.id} "${p.name || ''}"?\n` +
                       `${remaining} task(s) will be cancelled (already-running tasks finish on their own).`)) return;
          const r = await fetch(`${window.QA_BASE_PATH || ''}/api/qa/plans/${p.id}/cancel`,
                                {method: 'POST'});
          await r.json();
          await this.load();
        },

  }))

  // Format engineering-assessment axis (0..1) as a coloured bar with
  // numeric label. Used by /qa/mutations.
  function _axisBarHTML(scoring, axis) {
    if (!scoring || !scoring.axes) return '<span class="axis-na">—</span>';
    const v = scoring.axes[axis];
    if (v === null || v === undefined) return '<span class="axis-na">—</span>';
    const pct = Math.round(v * 100);
    const cls = v >= 0.85 ? 'axis-good' : (v >= 0.6 ? 'axis-mid' : 'axis-bad');
    return `<span class="axis-bar"><div class="${cls}" style="width:${pct}%"></div></span>` +
           `<span style="font-variant-numeric:tabular-nums;">${pct}%</span>`;
  }
  function _passRate(scoring) {
    if (!scoring || !scoring.overall || scoring.overall.pass_rate === undefined) return '—';
    const r = scoring.overall.pass_rate;
    return r === null ? '—' : `${Math.round(r * 100)}%`;
  }

  Alpine.data('mutationsView', () => ({
    mutations: [],
    selected: [],
    comparison: null,
    scoringCompare: null,
    scoring: {},                                  // mutation hash → scoring blob
    onDemandConfigs: [],                          // schedules with mode=on_demand for "fire" dropdown
    fireStatus: '',
    page: 1,
    pageSize: 50,
    total: 0,
    get pageCount() { return Math.max(1, Math.ceil(this.total / this.pageSize)); },
    get rangeText() {
      if (!this.total) return '0';
      const lo = (this.page - 1) * this.pageSize + 1;
      const hi = Math.min(this.total, lo + this.mutations.length - 1);
      return `${lo}–${hi} of ${this.total}`;
    },
    async goPage(delta) {
      const next = Math.min(this.pageCount, Math.max(1, this.page + delta));
      if (next === this.page) return;
      this.page = next;
      await this.load();
    },
    async load() {
      const offset = (this.page - 1) * this.pageSize;
      const base = window.QA_BASE_PATH || '';
      const r = await (await fetch(`${base}/api/search/aggregates/by_mutation` +
                                   `?limit=${this.pageSize}&offset=${offset}`)).json();
      this.mutations = r.data || [];
      this.total = (r.meta && r.meta.total) || this.mutations.length;
      const allConfigsR = await (await fetch(`${base}/api/qa/auto-plan/configs`)).json();
      const allConfigs = allConfigsR.data || [];
      this.onDemandConfigs = allConfigs.filter(c => c.mode === 'on_demand' && c.enabled);
      // Load scoring for each mutation in parallel.
      const tasks = this.mutations.map(async (m) => {
        try {
          const r = await fetch(`${base}/api/search/scoring/${m.mutation}`);
          const j = await r.json();
          this.scoring[m.mutation] = j.data;
        } catch (e) { /* skip */ }
      });
      await Promise.all(tasks);
    },
    async fireSchedule(mutation, event) {
      // Picks the schedule selected in the row's <select> dropdown.
      const select = event.currentTarget.parentElement.querySelector('select');
      const configId = parseInt(select.value, 10);
      if (!configId) return;
      const base = window.QA_BASE_PATH || '';
      const r = await fetch(`${base}/api/qa/auto-plan/configs/${configId}/fire-on`, {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({branch: 'master', sha: mutation}),
      });
      const j = await r.json();
      if (j.error) {
        this.fireStatus = `${mutation.slice(0,7)}: ${j.error.message}`;
      } else {
        const cfg = this.onDemandConfigs.find(c => c.id === configId);
        this.fireStatus = `${mutation.slice(0,7)} → "${cfg.name}": plan #${j.data.plan_id} (${j.data.task_count} tasks)`;
      }
    },
    toggleSelect(mut) {
      if (this.selected.includes(mut)) {
        this.selected = this.selected.filter(m => m !== mut);
      } else {
        if (this.selected.length >= 2) this.selected.shift();
        this.selected.push(mut);
      }
    },
    async loadCompare() {
      if (this.selected.length !== 2) return;
      const [a, b] = this.selected;
      const base = window.QA_BASE_PATH || '';
      const [cmp, score] = await Promise.all([
        fetch(`${base}/api/search/compare?a=${a}&b=${b}`).then(r => r.json()),
        fetch(`${base}/api/search/scoring-compare?a=${a}&b=${b}`).then(r => r.json()),
      ]);
      this.comparison = cmp.data;
      this.scoringCompare = score.data;
    },
    axisBar(scoring, axis)   { return _axisBarHTML(scoring, axis); },
    passRate(scoring)        { return _passRate(scoring); },
    axisDelta(axis) {
      const a = this.scoringCompare && this.scoringCompare.a.axes[axis];
      const b = this.scoringCompare && this.scoringCompare.b.axes[axis];
      if (a === null || b === null || a === undefined || b === undefined) return '—';
      const d = b - a;
      if (Math.abs(d) < 0.005) return '0';
      return (d > 0 ? '+' : '') + (d * 100).toFixed(1) + 'pp';
    },
    axisDeltaClass(axis) {
      const a = this.scoringCompare && this.scoringCompare.a.axes[axis];
      const b = this.scoringCompare && this.scoringCompare.b.axes[axis];
      if (a === null || b === null || a === undefined || b === undefined) return 'delta-zero';
      const d = b - a;
      if (Math.abs(d) < 0.005) return 'delta-zero';
      return d > 0 ? 'delta-pos' : 'delta-neg';
    },
    deltaClass(a, b) {
      if (!a || !b) return 'delta-zero';
      const d = (b || 0) - (a || 0);
      if (Math.abs(d) < 50) return 'delta-zero';
      return d > 0 ? 'delta-neg' : 'delta-pos';
    },
    deltaText(a, b) {
      if (!a && !b) return '';
      if (!a || !b) return '?';
      const d = Math.round((b || 0) - (a || 0));
      return d > 0 ? `+${d}` : String(d);
    },
  }))

  Alpine.data('autoPlan', () => ({
    
        configs: [],
        lastCreated: [],
        discoverStatus: '',
        createStatus: '',
        editStatus: '',
        editingId: null,
        editForm: {},
        history: {},        // config_id → array of planned commits
        historyOpen: {},    // config_id → bool
        historyMeta: {},    // config_id → "N plans · M branches"
        form: {
          name: '', repo_path: '/home/andrey/repos/diff-graph',
          branch_pattern: 'master,feature/*',
          bench_repo_path: '/home/andrey/repos/code-review-benchmarks',
          providers: 'deepseek',
          scenarios: '',
          scenario_tags: 'tier:unit',
          min_gap_seconds: 0,
          pacing: 'aggressive',
          pacing_window_seconds: 0,
          attempts_min: 1,
        },
        async load() {
          const r = await fetch(`${window.QA_BASE_PATH || ''}/api/qa/auto-plan/configs`);
          this.configs = (await r.json()).data || [];
        },
        async toggleHistory(configId) {
          this.historyOpen[configId] = !this.historyOpen[configId];
          if (this.historyOpen[configId] && !this.history[configId]) {
            await this.fetchHistory(configId);
          }
        },
        async fetchHistory(configId) {
          const r = await fetch(`${window.QA_BASE_PATH || ''}/api/qa/auto-plan/configs/${configId}/history?limit=50`);
          const j = await r.json();
          this.history[configId] = j.data || [];
          const branches = new Set(this.history[configId].map(h => h.branch));
          this.historyMeta[configId] = `${this.history[configId].length} plans · ${branches.size} branch(es)`;
        },
        bars(progress) {
          const pr = progress || {};
          const order = ['finished', 'running', 'leased', 'queued', 'error', 'cancelled'];
          const segs = [];
          for (const k of order) {
            const n = pr[k] || 0;
            if (n > 0) segs.push({k: k === 'leased' ? 'running' : k, n});
          }
          return segs.length ? segs : [{k: 'queued', n: 1}];
        },
        bartip(p) {
          const pr = p || {};
          return Object.entries(pr).filter(([k]) => k !== 'total').map(([k,n]) => `${k}: ${n}`).join(' · ');
        },
        progressText(p) {
          const pr = p || {};
          const done = (pr.finished || 0) + (pr.error || 0) + (pr.cancelled || 0);
          const total = pr.total || 0;
          return ` ${done}/${total}`;
        },
        async discoverAll() {
          this.discoverStatus = 'discovering...';
          const r = await fetch(`${window.QA_BASE_PATH || ''}/api/qa/auto-plan/discover`, {method: 'POST'});
          const j = await r.json();
          this.lastCreated = j.data || [];
          this.discoverStatus = `created ${(j.meta && j.meta.created_count) || 0} plan(s)`;
          await this.load();
        },
        startEdit(c) {
          this.editingId = c.id;
          this.editForm = {
            name: c.name,
            branch_pattern: c.branch_pattern,
            bench_repo_path: c.bench_repo_path,
            providers: c.providers.join(','),
            scenarios: (c.scenarios || []).join(','),
            scenario_tags: (c.scenario_tags || []).join(','),
            min_gap_seconds: c.min_gap_seconds,
            pacing: c.pacing,
            pacing_window_seconds: c.pacing_window_seconds,
            attempts_min: c.attempts_min,
          };
          this.editStatus = '';
        },
        async saveEdit(id) {
          this.editStatus = 'saving...';
          const csv = (s) => s.split(',').map(x => x.trim()).filter(Boolean);
          const payload = {
            name: this.editForm.name,
            branch_pattern: this.editForm.branch_pattern,
            bench_repo_path: this.editForm.bench_repo_path,
            providers: csv(this.editForm.providers),
            scenarios: csv(this.editForm.scenarios),
            scenario_tags: csv(this.editForm.scenario_tags),
            min_gap_seconds: this.editForm.min_gap_seconds,
            pacing: this.editForm.pacing,
            pacing_window_seconds: this.editForm.pacing_window_seconds,
            attempts_min: this.editForm.attempts_min,
          };
          const r = await fetch(`${window.QA_BASE_PATH || ''}/api/qa/auto-plan/configs/${id}`, {
            method: 'PUT', headers: {'content-type': 'application/json'},
            body: JSON.stringify(payload),
          });
          if (r.ok) {
            this.editStatus = 'saved';
            this.editingId = null;
            await this.load();
          } else {
            const err = await r.json();
            this.editStatus = `error: ${(err.error && err.error.message) || r.status}`;
          }
        },
        async toggle(c) {
          await fetch(`${window.QA_BASE_PATH || ''}/api/qa/auto-plan/configs/${c.id}?enabled=${!c.enabled}`,
                      {method: 'PATCH'});
          await this.load();
        },
        async del(id) {
          if (!confirm(`delete config #${id}?`)) return;
          await fetch(`${window.QA_BASE_PATH || ''}/api/qa/auto-plan/configs/${id}`, {method: 'DELETE'});
          await this.load();
        },
        async create() {
          this.createStatus = 'creating...';
          const csv = (s) => s.split(',').map(x => x.trim()).filter(Boolean);
          const payload = {
            name: this.form.name,
            repo_path: this.form.repo_path,
            branch_pattern: this.form.branch_pattern,
            bench_repo_path: this.form.bench_repo_path,
            providers: csv(this.form.providers),
            scenarios: csv(this.form.scenarios),
            scenario_tags: csv(this.form.scenario_tags),
            min_gap_seconds: this.form.min_gap_seconds,
            pacing: this.form.pacing,
            pacing_window_seconds: this.form.pacing_window_seconds,
            attempts_min: this.form.attempts_min,
            enabled: true,
          };
          const r = await fetch(`${window.QA_BASE_PATH || ''}/api/qa/auto-plan/configs`, {
            method: 'POST', headers: {'content-type': 'application/json'},
            body: JSON.stringify(payload),
          });
          if (r.ok) {
            this.createStatus = 'created';
            await this.load();
          } else {
            const err = await r.json();
            this.createStatus = `error: ${(err.error && err.error.message) || r.status}`;
          }
        },

  }))

  Alpine.data('workersView', () => ({
    workers: [],
    leasedTasks: [],
    pools: [],
    poolForm: { name: '', provider: 'deepseek', target_workers: 1, max_idle_seconds: 120 },
    poolStatus: '',
    cleanupStatus: '',
    get busyCount()    { return this.workers.filter(w => w.health === 'running' && w.current_task).length },
    get idleCount()    { return this.workers.filter(w => w.health === 'running' && !w.current_task).length },
    get stoppedCount() { return this.workers.filter(w => w.health === 'stopped').length },
    get deadCount()    { return this.workers.filter(w => w.health === 'dead').length },
    async load() {
      const base = window.QA_BASE_PATH || '';
      const r = await fetch(`${base}/api/qa/workers`);
      this.workers = (await r.json()).data || [];
      this.leasedTasks = this.workers
        .map(w => w.current_task)
        .filter(t => t);
    },
    async loadPools() {
      const base = window.QA_BASE_PATH || '';
      const r = await fetch(`${base}/api/qa/worker-pools`);
      this.pools = (await r.json()).data || [];
    },
    async createPool() {
      this.poolStatus = 'creating…';
      const base = window.QA_BASE_PATH || '';
      const r = await fetch(`${base}/api/qa/worker-pools`, {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify(this.poolForm),
      });
      if (r.ok) {
        this.poolStatus = 'created';
        await this.loadPools();
      } else {
        const e = await r.json();
        this.poolStatus = `error: ${(e.error && e.error.message) || r.status}`;
      }
    },
    async togglePool(p) {
      const base = window.QA_BASE_PATH || '';
      await fetch(`${base}/api/qa/worker-pools/${p.id}`, {
        method: 'PUT', headers: {'content-type': 'application/json'},
        body: JSON.stringify({ enabled: !p.enabled }),
      });
      await this.loadPools();
    },
    async deletePool(id) {
      if (!confirm(`delete pool #${id}?`)) return;
      const base = window.QA_BASE_PATH || '';
      await fetch(`${base}/api/qa/worker-pools/${id}`, {method: 'DELETE'});
      await this.loadPools();
    },
    async cleanupDead() {
      this.cleanupStatus = 'cleaning…';
      const base = window.QA_BASE_PATH || '';
      const r = await fetch(`${base}/api/qa/workers/cleanup-dead`, {method: 'POST'});
      const j = await r.json();
      this.cleanupStatus = `removed ${(j.data && j.data.deleted) || 0}`;
      await this.load();
    },
    dotClass(w) {
      if (w.health === 'dead')    return 'dot-dead';
      if (w.health === 'stopped') return 'dot-stopped';
      return w.current_task ? 'dot-busy' : 'dot-idle';
    },
    stateLabel(w) {
      if (w.health === 'dead')    return 'dead';
      if (w.health === 'stopped') return 'stopped';
      return w.current_task ? 'busy' : 'idle';
    },
    heartbeatLabel(w) {
      if (w.heartbeat_age_s === null) return '?';
      const s = w.heartbeat_age_s;
      if (s < 5) return 'just now';
      if (s < 60) return `${s}s ago`;
      if (s < 3600) return `${Math.floor(s/60)}m ago`;
      return `${Math.floor(s/3600)}h ago`;
    },
  }))
})
