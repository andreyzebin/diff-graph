// Auto-expand agents with findings
document.querySelectorAll('.output-section').forEach(el => {
  let details = el.closest('details');
  if (details) details.open = true;
});

// Tab system for right panel
const tabs = {};
let activeTab = null;

function openTabById(title, dataId) {
  // Read content from hidden script tag
  const dataEl = document.getElementById(dataId);
  if (!dataEl) return;
  const content = dataEl.textContent;
  openTab(title, content, dataId);
}

function openTab(title, content, id) {
  const bar = document.getElementById('tab-bar');

  // Remove hint
  const hint = bar.querySelector('.tab-hint');
  if (hint) hint.remove();

  // If tab already exists, just activate it
  if (tabs[id]) {
    activateTab(id);
    return;
  }

  // Create tab button
  const btn = document.createElement('div');
  btn.className = 'tab-btn';
  btn.dataset.id = id;
  const titleSpan = document.createElement('span');
  titleSpan.className = 'tab-title';
  titleSpan.textContent = title.substring(0, 30);
  const closeSpan = document.createElement('span');
  closeSpan.className = 'tab-close';
  closeSpan.textContent = '×';
  closeSpan.onclick = (e) => { e.stopPropagation(); closeTab(id); };
  btn.appendChild(titleSpan);
  btn.appendChild(closeSpan);
  btn.onclick = () => activateTab(id);
  bar.appendChild(btn);

  // Store tab data
  tabs[id] = { title, content, btn };

  // Activate
  activateTab(id);
}

function activateTab(id) {
  const area = document.getElementById('tab-content');
  const tab = tabs[id];
  if (!tab) return;

  // Deactivate all
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

  // Activate this one
  tab.btn.classList.add('active');
  activeTab = id;

  // Try to format as JSON, otherwise plain text
  let display = tab.content;
  try {
    const parsed = JSON.parse(display);
    display = JSON.stringify(parsed, null, 2);
  } catch(e) {}

  area.innerHTML = '';
  const pre = document.createElement('pre');
  pre.textContent = display;
  area.appendChild(pre);
}

function closeTab(id) {
  const tab = tabs[id];
  if (!tab) return;
  tab.btn.remove();
  delete tabs[id];

  if (activeTab === id) {
    const remaining = Object.keys(tabs);
    if (remaining.length > 0) {
      activateTab(remaining[remaining.length - 1]);
    } else {
      document.getElementById('tab-content').innerHTML = '';
      const bar = document.getElementById('tab-bar');
      bar.innerHTML = '<span class="tab-hint">Click [⧉] to open details</span>';
    }
  }
}

function downloadJSON(filename, dataId) {
  const dataEl = document.getElementById(dataId);
  if (!dataEl) return;
  let content = dataEl.textContent;
  try { content = JSON.stringify(JSON.parse(content), null, 2); } catch(e) {}
  navigator.clipboard.writeText(content).then(() => {
    showToast('✓ Copied to clipboard');
  });
}

function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 1500);
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
