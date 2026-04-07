/**
 * Avigilon Unity Chrome Plugin — Service Worker
 *
 * Stateless sync engine that compares live Plasec data (via bridge)
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

const PLASEC_TO_AG_STATUS = {
  '1': 'active',
  '2': 'suspended',
  '3': 'suspended',
  '4': 'suspended',
};

const AG_TO_PLASEC_STATUS = {
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
// Config helpers
// ---------------------------------------------------------------------------

async function getConfig() {
  const result = await chrome.storage.local.get('config');
  return result.config || {};
}

async function saveConfig(config) {
  await chrome.storage.local.set({ config });
}

async function getAGClient() {
  const config = await getConfig();
  const ag = config.accessgrid || {};
  if (!ag.account_id || !ag.api_secret) return null;
  return new AccessGrid(ag.account_id, ag.api_secret);
}

// ---------------------------------------------------------------------------
// Bridge communication (Plasec via localhost HTTP)
// ---------------------------------------------------------------------------

async function bridgeFetch(path, options = {}) {
  const url = `${BRIDGE_URL}${path}`;
  const resp = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (!resp.ok && resp.status !== 502) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.error || `Bridge HTTP ${resp.status}`);
  }
  return resp.json();
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

/**
 * Build a snapshot of the current state from both systems.
 *
 * Returns:
 *   plasecIdentities: Map<identityId, identity>
 *   plasecTokens: Map<identityId, token[]>
 *   agCards: Map<employeeId, card[]>  (employee_id = plasec identity CN)
 *   agCardMap: Map<cardId, card>
 */
async function buildSnapshot(agClient, templateId) {
  // Fetch from Plasec via bridge
  const identitiesResp = await bridgeFetch('/api/plasec/identities');
  const identities = identitiesResp.identities || [];

  const plasecIdentities = new Map();
  const plasecTokens = new Map();

  for (const ident of identities) {
    if (!ident.id) continue;
    plasecIdentities.set(ident.id, ident);
  }

  // Fetch tokens for each active identity
  for (const [iid, ident] of plasecIdentities) {
    if (ident.status !== '1') continue;
    try {
      const tokResp = await bridgeFetch(`/api/plasec/identities/${iid}/tokens`);
      plasecTokens.set(iid, tokResp.tokens || []);
    } catch (e) {
      console.warn(`Failed to fetch tokens for ${iid}:`, e);
      plasecTokens.set(iid, []);
    }
  }

  // Fetch from AccessGrid
  let agCards = [];
  try {
    agCards = await agClient.accessCards.list({ templateId });
  } catch (e) {
    console.warn('Failed to list AG cards:', e);
  }

  // Index AG cards by employeeId (= Plasec identity CN)
  const agCardsByEmployee = new Map();
  const agCardMap = new Map();
  for (const card of agCards) {
    agCardMap.set(card.id, card);
    if (card.employeeId) {
      if (!agCardsByEmployee.has(card.employeeId)) {
        agCardsByEmployee.set(card.employeeId, []);
      }
      agCardsByEmployee.get(card.employeeId).push(card);
    }
  }

  return { plasecIdentities, plasecTokens, agCardsByEmployee, agCardMap };
}

/**
 * Phase 1: Provision new AG cards for Plasec tokens marked "AccessGrid"
 * that don't yet have a corresponding AG card.
 */
async function phase1NewIdentities(agClient, templateId, snapshot) {
  let provisioned = 0;
  const { plasecIdentities, plasecTokens, agCardsByEmployee } = snapshot;

  for (const [iid, tokens] of plasecTokens) {
    for (const token of tokens) {
      if (!token.id) continue;
      if (token.status !== '1') continue;
      if ((token.embossed_number || '').toLowerCase() !== 'accessgrid') continue;

      // Check if AG already has a card for this employee
      const existingCards = agCardsByEmployee.get(iid) || [];
      if (existingCards.length > 0) continue;

      // Need full identity detail for email/phone
      let identity = plasecIdentities.get(iid);
      try {
        const detail = await bridgeFetch(`/api/plasec/identities/${iid}`);
        if (detail && detail.id) identity = detail;
      } catch (e) {
        console.warn(`Failed to fetch detail for ${iid}:`, e);
      }

      const fullName = identity.full_name || `${identity.first_name || ''} ${identity.last_name || ''}`.trim();
      const email = identity.email || '';
      const phone = identity.phone || '';

      if (!fullName) continue;
      if (!email && !phone) continue;

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
        };

        if (cardNumber) {
          params.cardNumber = cardNumber;
        }

        await agClient.accessCards.provision(params);
        provisioned++;
        console.log(`Provisioned AG card for ${fullName} (${iid})`);
      } catch (e) {
        console.error(`Failed to provision for ${iid}:`, e);
      }
    }
  }

  return provisioned;
}

/**
 * Phase 2: Push Plasec token status changes to AG.
 * Compare Plasec token status against AG card state.
 */
async function phase2StatusChanges(agClient, snapshot) {
  let updated = 0;
  const { plasecTokens, agCardsByEmployee } = snapshot;

  for (const [iid, tokens] of plasecTokens) {
    const agCards = agCardsByEmployee.get(iid) || [];
    if (agCards.length === 0) continue;

    for (const token of tokens) {
      if ((token.embossed_number || '').toLowerCase() !== 'accessgrid') {
        // Embossed number changed away from AccessGrid — terminate
        for (const card of agCards) {
          if ((card.state || '').toLowerCase() !== 'deleted') {
            try {
              await agClient.accessCards.delete({ cardId: card.id });
              updated++;
              console.log(`Terminated AG card ${card.id} — embossed_number no longer AccessGrid`);
            } catch (e) {
              console.error(`Failed to delete AG card ${card.id}:`, e);
            }
          }
        }
        continue;
      }

      const desiredAGState = PLASEC_TO_AG_STATUS[token.status] || 'suspended';

      for (const card of agCards) {
        const currentAGState = (card.state || 'active').toLowerCase();
        if (currentAGState === desiredAGState) continue;
        if (currentAGState === 'deleted') continue;

        try {
          if (desiredAGState === 'suspended' && currentAGState === 'active') {
            await agClient.accessCards.suspend({ cardId: card.id });
            updated++;
          } else if (desiredAGState === 'active' && currentAGState === 'suspended') {
            await agClient.accessCards.resume({ cardId: card.id });
            updated++;
          }
        } catch (e) {
          console.error(`Failed to update AG card ${card.id} status:`, e);
        }
      }
    }
  }

  return updated;
}

/**
 * Phase 3: Terminate AG cards for identities/tokens deleted from Plasec.
 */
async function phase3Deletions(agClient, snapshot) {
  let deleted = 0;
  const { plasecIdentities, plasecTokens, agCardsByEmployee } = snapshot;

  for (const [employeeId, cards] of agCardsByEmployee) {
    // Identity gone from Plasec
    if (!plasecIdentities.has(employeeId)) {
      for (const card of cards) {
        if ((card.state || '').toLowerCase() === 'deleted') continue;
        try {
          await agClient.accessCards.delete({ cardId: card.id });
          deleted++;
          console.log(`Deleted AG card ${card.id} — identity ${employeeId} gone from Plasec`);
        } catch (e) {
          console.error(`Failed to delete AG card ${card.id}:`, e);
        }
      }
      continue;
    }

    // Check if any AccessGrid tokens remain for this identity
    const tokens = plasecTokens.get(employeeId) || [];
    const hasAccessGridToken = tokens.some(
      t => (t.embossed_number || '').toLowerCase() === 'accessgrid'
    );

    if (!hasAccessGridToken) {
      for (const card of cards) {
        if ((card.state || '').toLowerCase() === 'deleted') continue;
        try {
          await agClient.accessCards.delete({ cardId: card.id });
          deleted++;
          console.log(`Deleted AG card ${card.id} — no AccessGrid tokens for ${employeeId}`);
        } catch (e) {
          console.error(`Failed to delete AG card ${card.id}:`, e);
        }
      }
    }
  }

  return deleted;
}

/**
 * Phase 4: Pull AG card state changes back to Plasec.
 */
async function phase4AGToPLasec(snapshot) {
  let updated = 0;
  const { plasecTokens, agCardsByEmployee } = snapshot;

  for (const [iid, cards] of agCardsByEmployee) {
    const tokens = plasecTokens.get(iid) || [];

    for (const card of cards) {
      const agState = (card.state || 'active').toLowerCase();
      const desiredPlasec = AG_TO_PLASEC_STATUS[agState];
      if (!desiredPlasec) continue;

      // Find matching token
      const token = tokens.find(t =>
        (t.embossed_number || '').toLowerCase() === 'accessgrid' && t.status !== desiredPlasec
      );
      if (!token) continue;

      try {
        await bridgeFetch(`/api/plasec/identities/${iid}/tokens/${token.id}/status`, {
          method: 'PUT',
          body: JSON.stringify({ status: desiredPlasec, current_token_data: token }),
        });
        updated++;
        console.log(`Updated Plasec token ${token.id} to status ${desiredPlasec}`);
      } catch (e) {
        console.error(`Failed to update Plasec token ${token.id}:`, e);
      }
    }
  }

  return updated;
}

/**
 * Phase 5: Retry — in stateless mode, phase 1 naturally retries on next cycle.
 * This is a no-op but kept for parity with the reference implementation.
 */
async function phase5Retries() {
  return 0;
}

/**
 * Phase 6: Push field changes (name, email, phone, title) to AG.
 */
async function phase6FieldChanges(agClient, snapshot) {
  let changed = 0;
  const { plasecIdentities, agCardsByEmployee } = snapshot;

  for (const [iid, cards] of agCardsByEmployee) {
    const identity = plasecIdentities.get(iid);
    if (!identity) continue;

    // Fetch full detail for email/phone/title
    let detail = identity;
    try {
      const resp = await bridgeFetch(`/api/plasec/identities/${iid}`);
      if (resp && resp.id) detail = resp;
    } catch {
      continue;
    }

    const fullName = detail.full_name || '';
    const email = detail.email || '';
    const phone = detail.phone || '';
    const title = detail.title || '';

    for (const card of cards) {
      if ((card.state || '').toLowerCase() === 'deleted') continue;

      const updates = {};
      if (fullName && fullName !== (card.fullName || '')) updates.fullName = fullName;
      if (title !== (card.title || '')) updates.title = title;

      if (Object.keys(updates).length === 0) continue;

      try {
        await agClient.accessCards.update({ cardId: card.id, ...updates });
        changed++;
      } catch (e) {
        console.error(`Failed to update AG card ${card.id} fields:`, e);
      }
    }
  }

  return changed;
}

/**
 * Run a full sync cycle — all 6 phases.
 */
async function runSyncCycle() {
  if (syncRunning) {
    console.log('Sync already running, skipping');
    return null;
  }

  syncRunning = true;
  const startTime = Date.now();

  try {
    // Check bridge health
    const healthy = await isBridgeHealthy();
    if (!healthy) {
      console.log('Bridge not reachable — skipping sync');
      lastSyncError = 'Bridge not reachable';
      return null;
    }

    // Get AG client
    const agClient = await getAGClient();
    if (!agClient) {
      console.log('AccessGrid not configured — skipping sync');
      lastSyncError = 'AccessGrid not configured';
      return null;
    }

    const config = await getConfig();
    const templateId = config.accessgrid?.template_id;
    if (!templateId) {
      console.log('No template ID configured — skipping sync');
      lastSyncError = 'No template ID';
      return null;
    }

    console.log('Starting sync cycle...');
    const snapshot = await buildSnapshot(agClient, templateId);

    const results = {
      new: await phase1NewIdentities(agClient, templateId, snapshot),
      statusChanges: await phase2StatusChanges(agClient, snapshot),
      deleted: await phase3Deletions(agClient, snapshot),
      agToPlasec: await phase4AGToPLasec(snapshot),
      retried: await phase5Retries(),
      fieldChanges: await phase6FieldChanges(agClient, snapshot),
      duration: Date.now() - startTime,
      identityCount: snapshot.plasecIdentities.size,
      agCardCount: snapshot.agCardMap.size,
    };

    lastSyncTime = new Date().toISOString();
    lastSyncResult = results;
    lastSyncError = null;

    console.log('Sync cycle complete:', results);
    return results;

  } catch (e) {
    console.error('Sync cycle failed:', e);
    lastSyncError = e.message;
    return null;
  } finally {
    syncRunning = false;
  }
}

// ---------------------------------------------------------------------------
// Triggers
// ---------------------------------------------------------------------------

// Alarm-based periodic sync
chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    runSyncCycle();
  }
});

// Page-load trigger (debounced)
chrome.webNavigation.onCompleted.addListener((details) => {
  if (details.frameId !== 0) return; // Only top-level frames
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
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
    runSyncCycle().then(result => {
      sendResponse({ result });
    });
    return true; // async response
  }

  if (message.type === 'GET_CONFIG') {
    getConfig().then(config => sendResponse(config));
    return true;
  }

  if (message.type === 'SAVE_CONFIG') {
    saveConfig(message.config).then(() => sendResponse({ ok: true }));
    return true;
  }

  if (message.type === 'CHECK_BRIDGE') {
    isBridgeHealthy().then(ok => sendResponse({ healthy: ok }));
    return true;
  }
});

// Run initial sync on install/startup
chrome.runtime.onInstalled.addListener(() => {
  console.log('Avigilon Unity Sync extension installed');
  runSyncCycle();
});

chrome.runtime.onStartup.addListener(() => {
  runSyncCycle();
});
