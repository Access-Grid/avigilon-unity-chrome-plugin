/**
 * Avigilon Unity Chrome Plugin — Service Worker
 *
 * Stateless sync engine that compares live Avigilon data (via bridge)
 * against live AccessGrid data to determine what actions are needed.
 * No local sync state DB — safe to run from multiple machines.
 *
 * Triggers:
 *   - chrome.alarms (every 1 minute)
 *   - chrome.webNavigation.onCompleted (any page load, debounced)
 *   - Manual trigger from popup
 *
 * Lock: prevents concurrent sync cycles.
 */

import AccessGrid from './accessgrid-sdk.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BRIDGE_URL = 'http://localhost:19780';
const ALARM_NAME = 'avigilon-sync';
const ALARM_PERIOD_MINUTES = 1;
const DEBOUNCE_MS = 5000;
const MAX_LOG_LINES = 500;

const AVIGILON_TO_AG_STATUS = {
  '1': 'active',
  '2': 'suspended',
  '3': 'suspended',
  '4': 'suspended',
};

const AG_TO_AVIGILON_STATUS = {
  active: '1',
  suspended: '2',
  created: '1',
};

// ---------------------------------------------------------------------------
// State (in-memory, resets on service worker restart — that's fine)
// ---------------------------------------------------------------------------

let syncRunning = false;
let lastSyncTime = null;
let lastSyncResult = null;
let lastSyncError = null;
let debounceTimer = null;

// ---------------------------------------------------------------------------
// Log buffer — stored in memory, readable by popup via GET_LOGS message
// ---------------------------------------------------------------------------

const logBuffer = [];

function log(level, ...args) {
  const ts = new Date().toISOString().replace('T', ' ').replace('Z', '');
  const msg = args.map(a => typeof a === 'string' ? a : JSON.stringify(a)).join(' ');
  const line = `${ts} [${level}] ${msg}`;
  logBuffer.push(line);
  while (logBuffer.length > MAX_LOG_LINES) logBuffer.shift();

  if (level === 'ERROR') console.error(`[sync]`, ...args);
  else if (level === 'WARN') console.warn(`[sync]`, ...args);
  else console.log(`[sync]`, ...args);
}

// ---------------------------------------------------------------------------
// Config helpers
// ---------------------------------------------------------------------------

async function getConfig() {
  const result = await chrome.storage.local.get('config');
  return result.config || {};
}

async function saveConfig(config) {
  await chrome.storage.local.set({ config });
}

// ---------------------------------------------------------------------------
// Pass cache — persists across service worker restarts in chrome.storage.local
//
// Tracks which AG cards we know about and their linked Avigilon tokens.
// Deletions only happen for entries that were previously cached, preventing
// mass-deletion if Avigilon returns empty due to an error.
//
// Schema: { "passCache": { "employeeId:tokenId": { agCardId, fullName, lastSeen } } }
// ---------------------------------------------------------------------------

async function getPassCache() {
  const result = await chrome.storage.local.get('passCache');
  return result.passCache || {};
}

async function savePassCache(cache) {
  await chrome.storage.local.set({ passCache: cache });
}

async function getAGClient() {
  const config = await getConfig();
  const ag = config.accessgrid || {};
  if (!ag.account_id || !ag.api_secret) return null;
  const client = new AccessGrid(ag.account_id, ag.api_secret);

  // Wire HTTP logging into both API sub-clients
  const httpLogger = (dir, statusOrMethod, path, body, elapsed) => {
    if (dir === 'req') {
      log('HTTP', `→ AG ${statusOrMethod} ${path}${body ? ` body=${body}` : ''}`);
    } else {
      log('HTTP', `← AG ${statusOrMethod} ${path} (${elapsed}ms) ${body}`);
    }
  };
  client.accessCards.onRequest = httpLogger;
  client.console.onRequest = httpLogger;

  return client;
}

// ---------------------------------------------------------------------------
// Bridge communication (Avigilon via localhost HTTP)
// ---------------------------------------------------------------------------

async function bridgeFetch(path, options = {}) {
  const url = `${BRIDGE_URL}${path}`;
  const method = options.method || 'GET';
  const bodyStr = options.body || '';

  log('HTTP', `→ ${method} ${path}${bodyStr ? ` body=${bodyStr.substring(0, 200)}` : ''}`);

  const start = Date.now();
  const resp = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });

  const elapsed = Date.now() - start;
  const respText = await resp.text();
  const truncated = respText.length > 300 ? respText.substring(0, 300) + '...' : respText;

  log('HTTP', `← ${resp.status} ${path} (${elapsed}ms) ${truncated}`);

  if (!resp.ok) {
    let parsed;
    try { parsed = JSON.parse(respText); } catch { parsed = {}; }
    const errMsg = parsed.error || parsed.detail || `Bridge HTTP ${resp.status}`;
    const errType = parsed.type ? ` (${parsed.type})` : '';
    throw new Error(`${errMsg}${errType}`);
  }

  try {
    return JSON.parse(respText);
  } catch {
    throw new Error(`Invalid JSON from bridge: ${respText.substring(0, 100)}`);
  }
}

async function isBridgeHealthy() {
  try {
    const data = await bridgeFetch('/api/health');
    return data.status === 'ok';
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Stateless Sync Engine
// ---------------------------------------------------------------------------

async function buildSnapshot(agClient, templateId) {
  log('INFO', 'Building snapshot: fetching identities from Avigilon via bridge...');
  const identitiesResp = await bridgeFetch('/api/avigilon/identities');
  const identities = identitiesResp.identities || [];

  const avigilonIdentities = new Map();
  const avigilonTokens = new Map();
  let totalTokens = 0;
  let activeIdentities = 0;
  let skippedInactive = 0;

  for (const ident of identities) {
    if (!ident.id) continue;
    avigilonIdentities.set(ident.id, ident);
  }

  log('INFO', `Avigilon: ${avigilonIdentities.size} identities loaded`);

  for (const [iid, ident] of avigilonIdentities) {
    if (ident.status !== '1') {
      skippedInactive++;
      continue;
    }
    activeIdentities++;
    try {
      const tokResp = await bridgeFetch(`/api/avigilon/identities/${iid}/tokens`);
      const tokens = tokResp.tokens || [];
      avigilonTokens.set(iid, tokens);
      totalTokens += tokens.length;
      if (tokens.length > 0) {
        const agTokens = tokens.filter(t => (t.embossed_number || '').toLowerCase() === 'accessgrid');
        if (agTokens.length > 0) {
          log('DEBUG', `  ${ident.full_name || iid}: ${tokens.length} token(s), ${agTokens.length} AccessGrid`);
        }
      }
    } catch (e) {
      log('WARN', `Failed to fetch tokens for ${ident.full_name || iid} (${iid}): ${e.message}`);
      avigilonTokens.set(iid, []);
    }
  }

  log('INFO', `Avigilon: ${activeIdentities} active identities, ${skippedInactive} inactive, ${totalTokens} total tokens`);
  log('INFO', `Fetching AG cards for template ${templateId}...`);

  let agCards = [];
  try {
    agCards = await agClient.accessCards.list({ templateId });
    log('INFO', `AccessGrid: ${agCards.length} card(s) found`);
  } catch (e) {
    log('ERROR', `Failed to list AG cards: ${e.message}`);
  }

  // Index AG cards by employeeId and by (employeeId, tokenId) via metadata
  const agCardsByEmployee = new Map();
  const agCardsByToken = new Map();  // key: "employeeId:tokenId"
  const agCardMap = new Map();
  for (const card of agCards) {
    agCardMap.set(card.id, card);
    if (card.employeeId) {
      if (!agCardsByEmployee.has(card.employeeId)) {
        agCardsByEmployee.set(card.employeeId, []);
      }
      agCardsByEmployee.get(card.employeeId).push(card);

      // Per-token index via metadata.avigilon_token_id
      const tokenId = (card.metadata || {}).avigilon_token_id;
      if (tokenId) {
        const key = `${card.employeeId}:${tokenId}`;
        agCardsByToken.set(key, card);
      }
    }
  }

  log('INFO', `Snapshot complete: ${avigilonIdentities.size} identities, ${totalTokens} tokens, ${agCards.length} AG cards (${agCardsByToken.size} token-matched)`);
  return { avigilonIdentities, avigilonTokens, agCardsByEmployee, agCardsByToken, agCardMap };
}

async function phase1NewIdentities(agClient, templateId, snapshot, passCache) {
  let provisioned = 0;
  let skipped = 0;
  const { avigilonIdentities, avigilonTokens, agCardsByToken } = snapshot;

  log('INFO', 'Phase 1: Checking for new identities to provision...');

  for (const [iid, tokens] of avigilonTokens) {
    for (const token of tokens) {
      if (!token.id) continue;
      if (token.status !== '1') { skipped++; continue; }
      if ((token.embossed_number || '').toLowerCase() !== 'accessgrid') { skipped++; continue; }

      // Per-token check: does an AG card already exist for this specific token?
      const tokenKey = `${iid}:${token.id}`;
      if (agCardsByToken.has(tokenKey)) {
        // Update cache lastSeen for existing cards
        const card = agCardsByToken.get(tokenKey);
        passCache[tokenKey] = {
          agCardId: card.id,
          fullName: card.fullName || '',
          lastSeen: new Date().toISOString(),
        };
        log('DEBUG', `  ${iid}/${token.id}: AG card already exists, skipping`);
        continue;
      }

      let identity = avigilonIdentities.get(iid);
      try {
        const detail = await bridgeFetch(`/api/avigilon/identities/${iid}`);
        if (detail && detail.id) identity = detail;
      } catch (e) {
        log('WARN', `  Failed to fetch detail for ${iid}: ${e.message}`);
      }

      const fullName = identity.full_name || `${identity.first_name || ''} ${identity.last_name || ''}`.trim();
      const email = identity.email || '';
      const phone = identity.phone || '';

      if (!fullName) { log('WARN', `  ${iid}: no name, skipping`); continue; }
      if (!email && !phone) { log('WARN', `  ${iid} (${fullName}): no email or phone, skipping`); continue; }

      const cardNumber = token.internal_number || token.embossed_number || '';
      const now = new Date().toISOString();
      const oneYearLater = new Date(Date.now() + 365 * 24 * 60 * 60 * 1000).toISOString();

      try {
        const params = {
          cardTemplateId: templateId,
          employeeId: iid,
          fullName,
          email: email || undefined,
          phoneNumber: phone || undefined,
          title: identity.title || undefined,
          startDate: token.activate_date || now,
          expirationDate: token.deactivate_date || oneYearLater,
          metadata: { avigilon_token_id: token.id },
        };
        if (cardNumber) params.cardNumber = cardNumber;

        log('INFO', `  Provisioning: ${fullName} (${iid}), token=${token.id}, card#=${cardNumber}, email=${email}`);
        const result = await agClient.accessCards.provision(params);
        provisioned++;

        // Cache the new card
        passCache[tokenKey] = {
          agCardId: result.id,
          fullName,
          lastSeen: now,
        };

        log('INFO', `  Provisioned AG card ${result.id} for ${fullName} (token ${token.id})`);
      } catch (e) {
        log('ERROR', `  Failed to provision for ${fullName} (${iid}/${token.id}): ${e.message}`);
      }
    }
  }

  log('INFO', `Phase 1 done: ${provisioned} provisioned, ${skipped} skipped`);
  return provisioned;
}

async function phase2StatusChanges(agClient, snapshot) {
  let updated = 0;
  const { avigilonTokens, agCardsByToken, agCardsByEmployee } = snapshot;

  log('INFO', 'Phase 2: Checking for status changes...');

  for (const [iid, tokens] of avigilonTokens) {
    for (const token of tokens) {
      if (!token.id) continue;

      // Find the AG card tied to this specific token
      const tokenKey = `${iid}:${token.id}`;
      const card = agCardsByToken.get(tokenKey);
      if (!card) continue;
      if ((card.state || '').toLowerCase() === 'deleted') continue;

      // If embossed number is no longer AccessGrid, terminate this card
      if ((token.embossed_number || '').toLowerCase() !== 'accessgrid') {
        try {
          log('INFO', `  Terminating card ${card.id} — token ${token.id} embossed no longer AccessGrid`);
          await agClient.accessCards.delete({ cardId: card.id });
          updated++;
        } catch (e) {
          log('ERROR', `  Failed to delete AG card ${card.id}: ${e.message}`);
        }
        continue;
      }

      const desiredAGState = AVIGILON_TO_AG_STATUS[token.status] || 'suspended';
      const currentAGState = (card.state || 'active').toLowerCase();
      if (currentAGState === desiredAGState) continue;

      try {
        if (desiredAGState === 'suspended' && currentAGState === 'active') {
          log('INFO', `  Suspending card ${card.id} (token ${token.id} status=${token.status})`);
          await agClient.accessCards.suspend({ cardId: card.id });
          updated++;
        } else if (desiredAGState === 'active' && currentAGState === 'suspended') {
          log('INFO', `  Resuming card ${card.id} (token ${token.id} status=${token.status})`);
          await agClient.accessCards.resume({ cardId: card.id });
          updated++;
        }
      } catch (e) {
        log('ERROR', `  Failed to update AG card ${card.id}: ${e.message}`);
      }
    }
  }

  log('INFO', `Phase 2 done: ${updated} status change(s)`);
  return updated;
}

async function phase3Deletions(agClient, snapshot, passCache) {
  let deleted = 0;
  const { avigilonIdentities, avigilonTokens } = snapshot;

  log('INFO', 'Phase 3: Checking for deletions...');

  // Safety check: if Avigilon returned 0 identities, something is wrong.
  // Skip deletions to prevent mass-deletion from a bad response.
  if (avigilonIdentities.size === 0) {
    log('WARN', 'Phase 3: Avigilon returned 0 identities — skipping deletions (likely an error)');
    return 0;
  }

  // Walk the cache — only delete cards we previously knew about
  for (const [tokenKey, cached] of Object.entries(passCache)) {
    const [employeeId, tokenId] = tokenKey.split(':');
    if (!employeeId || !tokenId) continue;

    const agCardId = cached.agCardId;
    if (!agCardId) continue;

    let shouldDelete = false;
    let reason = '';

    if (!avigilonIdentities.has(employeeId)) {
      // Identity gone from Avigilon
      shouldDelete = true;
      reason = `identity ${employeeId} gone from Avigilon`;
    } else {
      // Identity exists — check the specific token
      const tokens = avigilonTokens.get(employeeId) || [];
      const token = tokens.find(t => t.id === tokenId);

      if (!token) {
        // Token removed from identity
        shouldDelete = true;
        reason = `token ${tokenId} gone from identity ${employeeId}`;
      } else if ((token.embossed_number || '').toLowerCase() !== 'accessgrid') {
        // Token no longer marked AccessGrid
        shouldDelete = true;
        reason = `token ${tokenId} no longer AccessGrid (embossed=${token.embossed_number})`;
      }
    }

    if (shouldDelete) {
      try {
        log('INFO', `  Deleting card ${agCardId} — ${reason}`);
        await agClient.accessCards.delete({ cardId: agCardId });
        deleted++;
        // Remove from cache
        delete passCache[tokenKey];
      } catch (e) {
        // If card is already gone (404), clean up cache silently
        if (e.message && (e.message.includes('not found') || e.message.includes('404'))) {
          log('DEBUG', `  Card ${agCardId} already deleted, removing from cache`);
          delete passCache[tokenKey];
        } else {
          log('ERROR', `  Failed to delete AG card ${agCardId}: ${e.message}`);
        }
      }
    }
  }

  log('INFO', `Phase 3 done: ${deleted} deletion(s)`);
  return deleted;
}

async function phase4AGToAvigilon(snapshot) {
  let updated = 0;
  const { avigilonTokens, agCardsByEmployee } = snapshot;

  log('INFO', 'Phase 4: Checking for AG → Avigilon status sync...');

  for (const [iid, cards] of agCardsByEmployee) {
    const tokens = avigilonTokens.get(iid) || [];

    for (const card of cards) {
      const agState = (card.state || 'active').toLowerCase();
      const desiredAvigilon = AG_TO_AVIGILON_STATUS[agState];
      if (!desiredAvigilon) continue;

      // Find the linked token via metadata, or fall back to first matching
      const linkedTokenId = (card.metadata || {}).avigilon_token_id;
      let token;
      if (linkedTokenId) {
        token = tokens.find(t => t.id === linkedTokenId && t.status !== desiredAvigilon);
      } else {
        token = tokens.find(t =>
          (t.embossed_number || '').toLowerCase() === 'accessgrid' && t.status !== desiredAvigilon
        );
      }
      if (!token) continue;

      try {
        log('INFO', `  Updating Avigilon token ${token.id} to status ${desiredAvigilon} (AG card ${card.id} is ${agState})`);
        await bridgeFetch(`/api/avigilon/identities/${iid}/tokens/${token.id}/status`, {
          method: 'PUT',
          body: JSON.stringify({ status: desiredAvigilon, current_token_data: token }),
        });
        updated++;
      } catch (e) {
        log('ERROR', `  Failed to update Avigilon token ${token.id}: ${e.message}`);
      }
    }
  }

  log('INFO', `Phase 4 done: ${updated} Avigilon update(s)`);
  return updated;
}

async function phase5Retries() {
  log('INFO', 'Phase 5: Retries (implicit in stateless mode)');
  return 0;
}

async function phase6FieldChanges(agClient, snapshot) {
  let changed = 0;
  const { avigilonIdentities, agCardsByEmployee } = snapshot;

  log('INFO', 'Phase 6: Checking for field changes...');

  for (const [iid, cards] of agCardsByEmployee) {
    const identity = avigilonIdentities.get(iid);
    if (!identity) continue;

    let detail = identity;
    try {
      const resp = await bridgeFetch(`/api/avigilon/identities/${iid}`);
      if (resp && resp.id) detail = resp;
    } catch {
      continue;
    }

    const fullName = detail.full_name || '';
    const title = detail.title || '';

    for (const card of cards) {
      if ((card.state || '').toLowerCase() === 'deleted') continue;

      const updates = {};
      if (fullName && fullName !== (card.fullName || '')) updates.fullName = fullName;
      if (title !== (card.title || '')) updates.title = title;

      if (Object.keys(updates).length === 0) continue;

      try {
        log('INFO', `  Updating fields for card ${card.id}: ${JSON.stringify(updates)}`);
        await agClient.accessCards.update({ cardId: card.id, ...updates });
        changed++;
      } catch (e) {
        log('ERROR', `  Failed to update AG card ${card.id} fields: ${e.message}`);
      }
    }
  }

  log('INFO', `Phase 6 done: ${changed} field update(s)`);
  return changed;
}

async function runSyncCycle() {
  if (syncRunning) {
    log('WARN', 'Sync already running, skipping');
    return null;
  }

  syncRunning = true;
  const startTime = Date.now();

  try {
    log('INFO', '=== Sync cycle starting ===');

    const healthy = await isBridgeHealthy();
    if (!healthy) {
      log('WARN', 'Bridge not reachable at localhost:19780 — skipping sync');
      lastSyncError = 'Bridge not reachable';
      return null;
    }
    log('INFO', 'Bridge health check: OK');

    const agClient = await getAGClient();
    if (!agClient) {
      log('WARN', 'AccessGrid not configured (missing account_id or api_secret) — skipping sync');
      lastSyncError = 'AccessGrid not configured';
      return null;
    }

    const config = await getConfig();
    const templateId = config.accessgrid?.template_id;
    if (!templateId) {
      log('WARN', 'No template ID configured — skipping sync');
      lastSyncError = 'No template ID';
      return null;
    }
    log('INFO', `Config: template_id=${templateId}`);

    const snapshot = await buildSnapshot(agClient, templateId);

    // Load pass cache from persistent storage
    const passCache = await getPassCache();
    log('INFO', `Pass cache: ${Object.keys(passCache).length} entries loaded`);

    const results = {
      new: await phase1NewIdentities(agClient, templateId, snapshot, passCache),
      statusChanges: await phase2StatusChanges(agClient, snapshot),
      deleted: await phase3Deletions(agClient, snapshot, passCache),
      agToAvigilon: await phase4AGToAvigilon(snapshot),
      retried: await phase5Retries(),
      fieldChanges: await phase6FieldChanges(agClient, snapshot),
      duration: Date.now() - startTime,
      identityCount: snapshot.avigilonIdentities.size,
      agCardCount: snapshot.agCardMap.size,
      cacheSize: Object.keys(passCache).length,
    };

    // Persist updated cache
    await savePassCache(passCache);

    lastSyncTime = new Date().toISOString();
    lastSyncResult = results;
    lastSyncError = null;

    log('INFO', `=== Sync cycle complete in ${results.duration}ms ===`);
    log('INFO', `Results: +${results.new} new, ${results.statusChanges} status, -${results.deleted} deleted, ${results.agToAvigilon} ag→avigilon, ${results.fieldChanges} fields, cache=${results.cacheSize}`);
    return results;

  } catch (e) {
    log('ERROR', `Sync cycle failed: ${e.message}`);
    lastSyncError = e.message;
    return null;
  } finally {
    syncRunning = false;
  }
}

// ---------------------------------------------------------------------------
// Triggers
// ---------------------------------------------------------------------------

chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    log('DEBUG', 'Alarm trigger fired');
    runSyncCycle();
  }
});

chrome.webNavigation.onCompleted.addListener((details) => {
  if (details.frameId !== 0) return;
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    log('DEBUG', `Page load trigger: ${details.url?.substring(0, 80)}`);
    runSyncCycle();
  }, DEBOUNCE_MS);
});

// ---------------------------------------------------------------------------
// Message handler (for popup communication)
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'GET_STATUS') {
    sendResponse({
      syncRunning,
      lastSyncTime,
      lastSyncResult,
      lastSyncError,
    });
    return false;
  }

  if (message.type === 'FORCE_SYNC') {
    log('INFO', 'Manual sync triggered from popup');
    runSyncCycle().then(result => {
      sendResponse({ result });
    });
    return true;
  }

  if (message.type === 'GET_CONFIG') {
    getConfig().then(config => sendResponse(config));
    return true;
  }

  if (message.type === 'SAVE_CONFIG') {
    saveConfig(message.config).then(() => {
      log('INFO', 'Config saved from popup');
      sendResponse({ ok: true });
    });
    return true;
  }

  if (message.type === 'CHECK_BRIDGE') {
    isBridgeHealthy().then(ok => sendResponse({ healthy: ok }));
    return true;
  }

  if (message.type === 'GET_LOGS') {
    sendResponse({ logs: logBuffer.slice() });
    return false;
  }
});

chrome.runtime.onInstalled.addListener(() => {
  log('INFO', 'Avigilon Unity Sync extension installed');
  runSyncCycle();
});

chrome.runtime.onStartup.addListener(() => {
  log('INFO', 'Extension startup');
  runSyncCycle();
});
