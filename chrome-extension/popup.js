// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
    // Auto-refresh logs when switching to logs tab
    if (tab.dataset.tab === 'logs') loadLogs();
  });
});

function setStatus(elId, state, text) {
  const bar = document.getElementById(elId);
  bar.className = `status-bar ${state}`;
  const dot = bar.querySelector('.dot');
  dot.className = `dot ${state === 'ok' ? 'green' : state === 'error' ? 'red' : 'yellow'}`;
  bar.querySelector('span').textContent = text;
}

function setFeedback(elId, state, text) {
  const el = document.getElementById(elId);
  el.className = `feedback ${state}`;
  el.textContent = text;
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

function loadLogs() {
  chrome.runtime.sendMessage({ type: 'GET_LOGS' }, (resp) => {
    if (!resp?.logs) return;
    const el = document.getElementById('log');
    el.innerHTML = '';
    for (const line of resp.logs) {
      const span = document.createElement('span');
      span.className = 'log-line';
      if (line.includes('[ERROR]')) span.classList.add('error');
      else if (line.includes('[WARN]')) span.classList.add('warn');
      else if (line.includes('[DEBUG]')) span.classList.add('debug');
      span.textContent = line;
      el.appendChild(span);
      el.appendChild(document.createTextNode('\n'));
    }
    el.scrollTop = el.scrollHeight;
  });
}

document.getElementById('btn-copy-log').addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'GET_LOGS' }, (resp) => {
    if (!resp?.logs) return;
    const text = resp.logs.join('\n');
    navigator.clipboard.writeText(text).then(() => {
      const btn = document.getElementById('btn-copy-log');
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
    });
  });
});

document.getElementById('btn-refresh-log').addEventListener('click', loadLogs);

// ---------------------------------------------------------------------------
// Bridge health
// ---------------------------------------------------------------------------

function checkBridge(showFeedback) {
  if (showFeedback) setFeedback('bridge-feedback', 'loading', 'Connecting to bridge...');

  chrome.runtime.sendMessage({ type: 'CHECK_BRIDGE' }, (resp) => {
    if (resp?.healthy) {
      setStatus('bridge-status', 'ok', 'Bridge connected (localhost:19780)');
      if (showFeedback) setFeedback('bridge-feedback', 'ok', 'Bridge is reachable');
    } else {
      setStatus('bridge-status', 'error', 'Bridge not reachable — start the Avigilon Bridge app');
      if (showFeedback) setFeedback('bridge-feedback', 'error', 'Bridge not reachable — is the Avigilon Bridge app running?');
    }
  });
}

// ---------------------------------------------------------------------------
// Sync status
// ---------------------------------------------------------------------------

function loadStatus() {
  chrome.runtime.sendMessage({ type: 'GET_STATUS' }, (resp) => {
    if (!resp) return;

    if (resp.syncRunning) {
      setStatus('sync-status', 'warn', 'Sync in progress...');
    } else if (resp.lastSyncError) {
      setStatus('sync-status', 'error', `Last error: ${resp.lastSyncError}`);
    } else if (resp.lastSyncTime) {
      const t = new Date(resp.lastSyncTime).toLocaleTimeString();
      setStatus('sync-status', 'ok', `Last sync: ${t}`);
    }

    if (resp.lastSyncResult) {
      const r = resp.lastSyncResult;
      document.getElementById('metrics').style.display = 'grid';
      document.getElementById('m-identities').textContent = r.identityCount || 0;
      document.getElementById('m-cards').textContent = r.agCardCount || 0;
      document.getElementById('m-new').textContent = r.new || 0;
      document.getElementById('m-deleted').textContent = r.deleted || 0;
    }
  });
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

function loadConfig() {
  chrome.runtime.sendMessage({ type: 'GET_CONFIG' }, (config) => {
    if (!config) return;
    const ag = config.accessgrid || {};
    document.getElementById('ag-account').value = ag.account_id || '';
    document.getElementById('ag-secret').value = ag.api_secret || '';
    document.getElementById('ag-template').value = ag.template_id || '';
  });
}

document.getElementById('btn-save').addEventListener('click', () => {
  const config = {
    accessgrid: {
      account_id: document.getElementById('ag-account').value.trim(),
      api_secret: document.getElementById('ag-secret').value,
      template_id: document.getElementById('ag-template').value.trim(),
    },
  };
  chrome.runtime.sendMessage({ type: 'SAVE_CONFIG', config }, () => {
    setFeedback('bridge-feedback', 'ok', 'Configuration saved');
  });
});

// Test bridge — reactive with feedback
document.getElementById('btn-test-bridge').addEventListener('click', () => {
  const btn = document.getElementById('btn-test-bridge');
  btn.disabled = true;
  btn.textContent = 'Testing...';
  checkBridge(true);
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = 'Test Bridge';
  }, 2000);
});

// Force sync
document.getElementById('btn-sync').addEventListener('click', () => {
  const btn = document.getElementById('btn-sync');
  btn.disabled = true;
  btn.textContent = 'Syncing...';

  chrome.runtime.sendMessage({ type: 'FORCE_SYNC' }, (resp) => {
    btn.disabled = false;
    btn.textContent = 'Run Sync Now';
    loadStatus();
  });
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

checkBridge(false);
loadStatus();
loadConfig();
