// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
  });
});

function log(msg) {
  const el = document.getElementById('log');
  const ts = new Date().toLocaleTimeString();
  el.textContent = `[${ts}] ${msg}\n` + el.textContent;
}

function setStatus(elId, state, text) {
  const bar = document.getElementById(elId);
  bar.className = `status-bar ${state}`;
  const dot = bar.querySelector('.dot');
  dot.className = `dot ${state === 'ok' ? 'green' : state === 'error' ? 'red' : 'yellow'}`;
  bar.querySelector('span').textContent = text;
}

// Check bridge health
async function checkBridge() {
  chrome.runtime.sendMessage({ type: 'CHECK_BRIDGE' }, (resp) => {
    if (resp?.healthy) {
      setStatus('bridge-status', 'ok', 'Bridge connected (localhost:19780)');
    } else {
      setStatus('bridge-status', 'error', 'Bridge not reachable — start the Avigilon Bridge app');
    }
  });
}

// Load sync status
async function loadStatus() {
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

// Load config
async function loadConfig() {
  chrome.runtime.sendMessage({ type: 'GET_CONFIG' }, (config) => {
    if (!config) return;
    const ag = config.accessgrid || {};
    document.getElementById('ag-account').value = ag.account_id || '';
    document.getElementById('ag-secret').value = ag.api_secret || '';
    document.getElementById('ag-template').value = ag.template_id || '';
  });
}

// Save config
document.getElementById('btn-save').addEventListener('click', () => {
  const config = {
    accessgrid: {
      account_id: document.getElementById('ag-account').value.trim(),
      api_secret: document.getElementById('ag-secret').value,
      template_id: document.getElementById('ag-template').value.trim(),
    },
  };
  chrome.runtime.sendMessage({ type: 'SAVE_CONFIG', config }, () => {
    log('Configuration saved');
  });
});

// Test bridge
document.getElementById('btn-test-bridge').addEventListener('click', () => {
  checkBridge();
  log('Checking bridge connection...');
});

// Force sync
document.getElementById('btn-sync').addEventListener('click', () => {
  const btn = document.getElementById('btn-sync');
  btn.disabled = true;
  btn.textContent = 'Syncing...';
  log('Starting manual sync...');

  chrome.runtime.sendMessage({ type: 'FORCE_SYNC' }, (resp) => {
    btn.disabled = false;
    btn.textContent = 'Run Sync Now';
    if (resp?.result) {
      log(`Sync complete: ${resp.result.new} new, ${resp.result.deleted} deleted, ${resp.result.statusChanges} status changes`);
    } else {
      log('Sync returned no result (check bridge connection)');
    }
    loadStatus();
  });
});

// Initialize
checkBridge();
loadStatus();
loadConfig();
