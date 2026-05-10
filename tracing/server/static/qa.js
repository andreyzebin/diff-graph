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

// ── Time formatting ─────────────────────────────────────────────────
// Server stores all timestamps in UTC ISO (e.g. 2026-05-09T16:59:30+00:00);
// these helpers render them in the BROWSER's locale and timezone so
// the user always sees their wall-clock time. fmtLocal returns
// "YYYY-MM-DD HH:MM:SS" in local tz; fmtLocalTime returns "HH:MM:SS"
// only — used for in-day cells like lease_expires_at.
window.fmtLocal = function (iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  // YYYY-MM-DD HH:MM:SS in local tz, no seconds-fraction, no offset.
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};
window.fmtLocalTime = function (iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

document.addEventListener('alpine:init', () => {

  Alpine.data('dash', () => ({
    
        totalRuns: '…',
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
    
        dims: { agent_name: [], model: [], scenario_id: [],
                generation: [], project: [], status: [],
                scenario_tags: [] },
        // Inline initialiser — calling this.defaultFilters() in the
        // object literal would fail (this is undefined at that point).
        filters: {
          agent: '', model: '', scenario: '',
          generation: '', mutation: '', project: '', status: '',
          plan: '', task: '', session: '', scenario_run: '',
          window: '24h',  // time-window default: last 24h (Jaeger convention)
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
            agent: '', model: '', scenario: '',
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
            agent: 'agent', model: 'model',
            scenario: 'scenario', generation: 'generation',
            mutation: 'mutation', project: 'project', status: 'status',
            plan: 'plan', task: 'task', session: 'session',
            scenario_run: 'scenario_run',
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
          const scalarOut = { agent: f.agent, model: f.model,
            scenario: f.scenario, generation: f.generation, mutation: f.mutation,
            project: f.project, status: f.status, plan: f.plan, task: f.task,
            session: f.session, scenario_run: f.scenario_run,
            file: f.file, jira: f.jira,
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
          const scalarOut = { agent: f.agent, model: f.model,
            scenario: f.scenario, generation: f.generation, mutation: f.mutation,
            project: f.project, status: f.status, plan: f.plan, task: f.task,
            session: f.session, scenario_run: f.scenario_run,
            file: f.file, jira: f.jira,
          };
          for (const [k, v] of Object.entries(scalarOut)) {
            if (v) qs.append(k, v);
          }
          if (f.duration_gt_ms) qs.append('duration_gt_ms', String(f.duration_gt_ms));
          if (f.scenario_tag_arr.length) qs.append('scenario_tag', f.scenario_tag_arr.join(','));
          // Time window → ISO `since`. "all time" = no since (caller
          // opts in to a full scan; server's default-window fallback
          // does NOT trigger because we explicitly send empty since).
          if (f.window) {
            const m = /^(\d+)([hd])$/.exec(f.window);
            if (m) {
              const n = parseInt(m[1], 10);
              const ms = (m[2] === 'h' ? n*3600 : n*86400) * 1000;
              const since = new Date(Date.now() - ms).toISOString();
              qs.append('since', since);
            }
          }
          qs.append('limit', String(f.limit));
          qs.append('offset', String(f.offset));
          qs.append('sort', f.sort);
          qs.append('order', f.order);
          // /qa/runs displays sub-agent rows by default — one row per
          // (session × agent_id). Production webhook sessions and
          // bench-driven CLI sessions both expand into their
          // dispatcher → reviewer → investigator-N children.
          const url = `${window.QA_BASE_PATH || ''}/api/search/sub_runs?` + qs.toString();
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
          const bk = pr.by_kind || {};
          const a = bk.agent  || {};
          const j = bk.judge  || {};
          // If we have judge phantom rows, show "agents X/Y · judges A/B"
          // — gives visibility for how the in-process judge tracks the
          // agent. Otherwise fall back to single counter (legacy plans).
          if (j && (j.total || 0) > 0) {
            const aDone = (a.finished || 0) + (a.error || 0) + (a.cancelled || 0);
            const jDone = (j.finished || 0) + (j.error || 0) + (j.cancelled || 0);
            return ` agents ${aDone}/${a.total || 0} · judges ${jDone}/${j.total || 0}`;
          }
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
        scoreText(p) {
          const s = p.live_score;
          if (!s || !s.n) return '';
          return ` · score ${s.mean.toFixed(2)} (n=${s.n})`;
        },
        scoreTooltip(p) {
          const s = p.live_score;
          if (!s || !s.n) return '';
          const tail = (s.last || []).map(x =>
            `${x.scenario || '?'}: ${x.score.toFixed(2)}`).join('\n');
          return `running mean across ${s.n} judge runs\n— recent —\n${tail}`;
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

  Alpine.data('scoringView', () => ({
    lineages: [],                        // multi-select; empty = "show all"
    scenario: '',                        // single-scenario filter
    rows: [],                            // per_run_scores rows
    availableScenarios: [],
    availableLineages: [],
    status: '',
    async init() {
      const base = window.QA_BASE_PATH || '';
      const dims = (await (await fetch(`${base}/api/search/dimensions`)).json()).data || {};
      this.availableScenarios = (dims.scenario_id || []).filter(Boolean).sort();
      this.availableLineages = (dims.lineage || []).filter(Boolean).sort();
      // URL pre-pick: ?lineage=master&lineage=feature/X (multi); ?scenario=…
      const sp = new URLSearchParams(window.location.search);
      const initLineages = sp.getAll('lineage').flatMap(s => s.split(',')).filter(Boolean);
      this.lineages = initLineages.filter(l => this.availableLineages.includes(l));
      // Default: show every lineage we have data for. User can subset later.
      if (this.lineages.length === 0) this.lineages = [...this.availableLineages];
      const initScen = sp.get('scenario');
      if (initScen) this.scenario = initScen;
      // Re-render on tab refocus — fixes vega "0-width line" when the
      // tab was hidden during initial render. window.dispatchEvent
      // makes vega views resize() against the now-visible container.
      window.addEventListener('focus', () => window.dispatchEvent(new Event('resize')));
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) setTimeout(() => this.renderCharts(), 50);
      });
      await this.reload();
    },
    toggleLineage(l) {
      if (this.lineages.includes(l)) {
        this.lineages = this.lineages.filter(x => x !== l);
      } else {
        this.lineages = [...this.lineages, l];
      }
      this.reload();
    },
    pushUrl() {
      const qs = new URLSearchParams();
      // Only emit lineages if user has subset; "all" is implicit.
      if (this.lineages.length > 0 &&
          this.lineages.length < this.availableLineages.length) {
        for (const l of this.lineages) qs.append('lineage', l);
      }
      if (this.scenario) qs.set('scenario', this.scenario);
      const url = window.location.pathname + (qs.toString() ? '?' + qs.toString() : '');
      window.history.replaceState({}, '', url);
    },
    async reload() {
      this.pushUrl();
      if (this.lineages.length === 0) { this.rows = []; return; }
      this.status = 'loading…';
      const base = window.QA_BASE_PATH || '';
      const all = [];
      // One fetch per selected lineage (parallel for nicer latency).
      const fetches = this.lineages.map(async (lin) => {
        const qs = new URLSearchParams({lineage: lin, limit: '5000'});
        if (this.scenario) qs.set('scenario', this.scenario);
        const r = await (await fetch(`${base}/api/search/per_run_scores?${qs}`)).json();
        return (r.data || []);
      });
      const arrs = await Promise.all(fetches);
      for (const a of arrs) for (const row of a) all.push(row);
      this.rows = all;
      this.status = all.length
        ? `${all.length} judge runs across ${this.lineages.length} lineage(s)`
        : 'no data';
      // Wait for Alpine to reactively flip the x-show divs + browser
      // to repaint, THEN measure container widths and embed. Double-
      // RAF is the cheapest reliable way to land after a layout cycle.
      requestAnimationFrame(() =>
        requestAnimationFrame(() => this.renderCharts()));
    },
    renderCharts() {
      if (!window.vegaEmbed || this.rows.length === 0) return;
      const data = this.rows.map(r => ({...r, mutation_short: (r.mutation || '').slice(0, 8)}));
      const dark = {
        config: {
          background: 'transparent',
          view: {stroke: 'transparent'},
          axis: {labelColor: '#c9d1d9', titleColor: '#8b949e',
                  domainColor: '#30363d', gridColor: '#21262d', tickColor: '#30363d'},
          legend: {labelColor: '#c9d1d9', titleColor: '#8b949e'},
          title:  {color: '#c9d1d9'},
          range: {category: ['#56d364', '#58a6ff', '#d2a8ff', '#c69026',
                              '#f47067', '#ff9b73', '#fbcb6f']},
        },
      };
      // Width strategy: vega-lite `width: 'container'` is correct in
      // theory; the trick is making sure
      //  (1) the container is laid out (not display:none) at embed time
      //  (2) the container has a sensible upper bound so vega doesn't
      //      grow it unbounded.
      // Caller does (1) via double-RAF before calling renderCharts.
      // (2) is enforced via CSS on .chart-frame (width:100%; max-width:
      // 100%; box-sizing:border-box) — vega measures the parent's
      // content-box and stays inside it.
      const embed = (sel, spec) => {
        spec.width = 'container';
        return vegaEmbed(sel, spec, {actions: false}).catch(() => {});
      };

      // (1) Trend per lineage — one line per (lineage × scenario);
      // mutations appear automatically as POINTS along their lineage.
      // x = ts of judge run, y = mean per (mutation × lineage × scenario).
      // Default view, always rendered.
      const trendSpec = {
        ...dark,
        width: 'container', height: 360,
        data: {values: data},
        mark: {type: 'line', point: {filled: true, size: 80},
                interpolate: 'monotone'},
        transform: [
          {
            aggregate: [
              {op: 'mean',  field: 'score', as: 'mean_score'},
              {op: 'count', field: 'score', as: 'n'},
              {op: 'min',   field: 'ts',    as: 'first_ts'},
            ],
            groupby: ['mutation_short', 'scenario', 'mutation'],
          },
        ],
        encoding: {
          x: {field: 'first_ts', type: 'temporal',
              title: 'commit chronology'},
          y: {field: 'mean_score', type: 'quantitative',
              title: 'mean overall_score', scale: {domain: [0, 1]}},
          color: {field: 'scenario', type: 'nominal', title: 'scenario',
                  scale: {scheme: 'category10'}},
          // Use stroke-dash to differentiate lineages WHEN multi-select.
          // (this requires injecting `lineage` per row — already in data
          // via per_run_scores join; pull it through.)
          strokeDash: this.lineages.length > 1
            ? {field: 'scenario', type: 'nominal'}  // placeholder — see note
            : undefined,
          tooltip: [
            {field: 'mutation_short', type: 'nominal', title: 'mutation (commit)'},
            {field: 'scenario', type: 'nominal'},
            {field: 'mean_score', type: 'quantitative', format: '.3f'},
            {field: 'n', type: 'quantitative', title: 'samples'},
            {field: 'first_ts', type: 'temporal'},
          ],
        },
      };
      embed('#chart-trend', trendSpec);

      // (3) Per-attempt timeline — when ONE scenario is picked.
      // Every dot = one judge verdict in chronological order. Same
      // lineage tints with one colour; mutations are visible as
      // chronologically-clustered point groups.
      if (this.scenario) {
        const tSpec = {
          ...dark,
          width: 'container', height: 280,
          data: {values: data},
          mark: {type: 'point', filled: true, size: 80},
          encoding: {
            x: {field: 'ts', type: 'temporal', title: 'judge finished_at'},
            y: {field: 'score', type: 'quantitative', title: 'overall_score',
                scale: {domain: [0, 1]}},
            color: {field: 'mutation_short', type: 'nominal',
                    title: 'mutation (commit)'},
            tooltip: [
              {field: 'mutation_short', type: 'nominal'},
              {field: 'score', type: 'quantitative'},
              {field: 'fp_count', type: 'quantitative'},
              {field: 'warnings_count', type: 'quantitative'},
              {field: 'ts', type: 'temporal'},
            ],
          },
        };
        embed('#chart-timeline', tSpec);
      }
    },
  }))

  Alpine.data('mutationsView', () => ({
    mutations: [],
    selected: [],
    comparison: null,
    scoringCompare: null,
    scoring: {},                                  // mutation hash → scoring blob
    onDemandConfigs: [],                          // schedules with mode=on_demand
    bulkFireConfigId: null,                       // currently-picked schedule for bulk fire
    fireStatus: '',
    allScenarios: [],                             // /api/qa/scenarios cache (for anonymous fire)
    anonModal: {                                  // anonymous-fire modal state
      open: false,
      picked: [],                                  // list of scenario ids checked
      tierFilter: '',
      agentFilter: '',
      q: '',
      provider: 'deepseek',
      firing: false,
    },
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
      if (this.onDemandConfigs.length && this.bulkFireConfigId === null) {
        this.bulkFireConfigId = this.onDemandConfigs[0].id;
      }
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
    async bulkFireSchedule() {
      // Fire the picked schedule on every selected mutation.
      const configId = parseInt(this.bulkFireConfigId, 10);
      if (!configId || !this.selected.length) return;
      const base = window.QA_BASE_PATH || '';
      const cfg = this.onDemandConfigs.find(c => c.id === configId);
      const results = [];
      for (const mut of this.selected) {
        const r = await fetch(`${base}/api/qa/auto-plan/configs/${configId}/fire-on`, {
          method: 'POST', headers: {'content-type': 'application/json'},
          body: JSON.stringify({lineage: 'master', sha: mut}),
        });
        const j = await r.json();
        if (j.error) {
          results.push(`${mut.slice(0,7)}: ${j.error.message}`);
        } else {
          results.push(`${mut.slice(0,7)} → plan #${j.data.plan_id}`);
        }
      }
      this.fireStatus = `"${cfg?.name || configId}" × ${this.selected.length}: ${results.join(' · ')}`;
    },
    async openAnonModal() {
      if (!this.allScenarios.length) {
        const base = window.QA_BASE_PATH || '';
        const r = await (await fetch(`${base}/api/qa/scenarios`)).json();
        this.allScenarios = r.data || [];
      }
      this.anonModal.open = true;
    },
    toggleScenario(sid) {
      if (this.anonModal.picked.includes(sid)) {
        this.anonModal.picked = this.anonModal.picked.filter(x => x !== sid);
      } else {
        this.anonModal.picked = [...this.anonModal.picked, sid];
      }
    },
    filteredScenarios() {
      const m = this.anonModal;
      const q = (m.q || '').trim().toLowerCase();
      return this.allScenarios.filter(s =>
        (!m.tierFilter || s.tier === m.tierFilter) &&
        (!m.agentFilter || s.agent === m.agentFilter) &&
        (!q || (s.id + s.rel_path).toLowerCase().includes(q))
      );
    },
    async bulkFireAnonymous() {
      const m = this.anonModal;
      if (!m.picked.length || !this.selected.length) return;
      m.firing = true;
      try {
        const base = window.QA_BASE_PATH || '';
        const results = [];
        for (const mut of this.selected) {
          const r = await fetch(`${base}/api/qa/fire-anonymous`, {
            method: 'POST', headers: {'content-type': 'application/json'},
            body: JSON.stringify({
              scenarios: m.picked, sha: mut,
              lineage: 'master', provider: m.provider || 'deepseek',
            }),
          });
          const j = await r.json();
          if (j.error) {
            results.push(`${mut.slice(0,7)}: ${j.error.message}`);
          } else {
            results.push(`${mut.slice(0,7)} → plan #${j.data.id}`);
          }
        }
        this.fireStatus = `anon × ${this.selected.length} mutation(s) × ${m.picked.length} scenario(s): ${results.join(' · ')}`;
        m.open = false;
      } finally {
        m.firing = false;
      }
    },
    toggleSelect(mut) {
      if (this.selected.includes(mut)) {
        this.selected = this.selected.filter(m => m !== mut);
      } else {
        // Allow many — compare button still requires exactly 2.
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
        historyMeta: {},    // config_id → "N plans · M lineages"
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
          const lineages = new Set(this.history[configId].map(h => h.lineage));
          this.historyMeta[configId] = `${this.history[configId].length} plans · ${lineages.size} lineage(s)`;
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

  Alpine.data('scenariosView', () => ({
    all: [],
    picked: [],
    tierFilter: '',
    agentFilter: '',
    q: '',
    fireStatus: '',
    fireModal: {
      open: false,
      sha: '',
      lineage: 'master',
      provider: 'deepseek',
      name: '',
      firing: false,
    },
    async load() {
      const base = window.QA_BASE_PATH || '';
      const r = await (await fetch(`${base}/api/qa/scenarios`)).json();
      this.all = r.data || [];
    },
    filtered() {
      const q = (this.q || '').trim().toLowerCase();
      return this.all.filter(s =>
        (!this.tierFilter || s.tier === this.tierFilter) &&
        (!this.agentFilter || s.agent === this.agentFilter) &&
        (!q || (s.id + ' ' + s.rel_path).toLowerCase().includes(q))
      );
    },
    toggle(sid) {
      if (this.picked.includes(sid)) {
        this.picked = this.picked.filter(x => x !== sid);
      } else {
        this.picked = [...this.picked, sid];
      }
    },
    openFireModal() {
      this.fireModal.open = true;
      // Try to default-fill SHA from query string ?sha=...
      const sp = new URLSearchParams(window.location.search);
      const sha = sp.get('sha');
      if (sha) this.fireModal.sha = sha;
    },
    async fireNow() {
      const m = this.fireModal;
      if (!this.picked.length || !m.sha) return;
      m.firing = true;
      try {
        const base = window.QA_BASE_PATH || '';
        const r = await fetch(`${base}/api/qa/fire-anonymous`, {
          method: 'POST', headers: {'content-type': 'application/json'},
          body: JSON.stringify({
            scenarios: this.picked, sha: m.sha,
            lineage: m.lineage || 'master',
            provider: m.provider || 'deepseek',
            name: m.name || '',
          }),
        });
        const j = await r.json();
        if (j.error) {
          this.fireStatus = `error: ${j.error.message}`;
        } else {
          this.fireStatus = `plan #${j.data.id} created · ${this.picked.length} scenario(s) on ${m.sha.slice(0,7)}`;
          m.open = false;
        }
      } finally {
        m.firing = false;
      }
    },
  }))

  Alpine.data('workersView', () => ({
    workers: [],
    leasedTasks: [],
    pools: [],
    poolForm: { name: '', provider: 'deepseek', target_workers: 1,
                max_idle_seconds: 120, task_timeout_seconds: 900 },
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
