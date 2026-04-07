/**
 * AccessGrid JS SDK — bundled for Chrome extension service worker.
 *
 * Adapted from https://github.com/Access-Grid/accessgrid-js
 * Uses fetch() and Web Crypto (crypto.subtle) — fully browser-compatible.
 */

class AccessGridError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AccessGridError';
  }
}

class AuthenticationError extends AccessGridError {
  constructor(message = 'Invalid credentials') {
    super(message);
    this.name = 'AuthenticationError';
  }
}

class AccessCard {
  constructor(data = {}) {
    this.id = data.id;
    this.installUrl = data.install_url;
    this.state = data.state;
    this.fullName = data.full_name;
    this.expirationDate = data.expiration_date;
    this.cardTemplateId = data.card_template_id;
    this.cardNumber = data.card_number;
    this.siteCode = data.site_code;
    this.title = data.title;
    this.employeeId = data.employee_id;
    this.email = data.email;
    this.phoneNumber = data.phone_number;
    this.organizationName = data.organization_name;
    this.createdAt = data.created_at;
    this.devices = data.devices || [];
    this.metadata = data.metadata || {};
  }
}

class Template {
  constructor(data = {}) {
    this.id = data.id;
    this.name = data.name;
    this.platform = data.platform;
    this.protocol = data.protocol;
    this.useCase = data.use_case;
  }
}

class BaseApi {
  constructor(accountId, secretKey, baseUrl = 'https://api.accessgrid.com') {
    this.accountId = accountId;
    this.secretKey = secretKey;
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.version = '1.3.0';
  }

  async request(path, options = {}) {
    const url = `${this.baseUrl}${path}`;
    const method = options.method || 'GET';

    let resourceId = null;
    if (method === 'GET' || (method === 'POST' && (!options.body || Object.keys(options.body).length === 0))) {
      const parts = path.split('/').filter(p => p);
      if (parts.length >= 2) {
        if (['suspend', 'resume', 'unlink', 'delete'].includes(parts[parts.length - 1])) {
          resourceId = parts[parts.length - 2];
        } else {
          resourceId = parts[parts.length - 1];
        }
      }
    }

    let payload, sigPayload;
    if ((method === 'POST' && !options.body) || method === 'GET') {
      if (resourceId) {
        sigPayload = JSON.stringify({ id: resourceId });
      } else {
        payload = '{}';
        sigPayload = payload;
      }
    } else {
      payload = options.body ? JSON.stringify(options.body) : '';
      sigPayload = payload;
    }

    const signature = await this._generateSignature(sigPayload);

    const headers = {
      'Content-Type': 'application/json',
      'X-ACCT-ID': this.accountId,
      'X-PAYLOAD-SIG': signature,
      'User-Agent': `accessgrid.js @ v${this.version}`,
      ...(options.headers || {}),
    };

    let finalUrl = url;
    if (method === 'GET' || (method === 'POST' && !options.body)) {
      if (resourceId) {
        const sep = finalUrl.includes('?') ? '&' : '?';
        finalUrl = `${finalUrl}${sep}sig_payload=${encodeURIComponent(JSON.stringify({ id: resourceId }))}`;
      }
    }

    const response = await fetch(finalUrl, {
      method,
      headers,
      body: method !== 'GET' ? payload : undefined,
    });

    const data = await response.json();

    if (!response.ok) {
      if (response.status === 401) throw new AuthenticationError();
      if (response.status === 402) throw new AccessGridError('Insufficient account balance');
      throw new AccessGridError(data.message || 'Request failed');
    }

    return data;
  }

  async _generateSignature(payload) {
    const encodedPayload = btoa(payload);
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey(
      'raw',
      encoder.encode(this.secretKey),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign'],
    );
    const signature = await crypto.subtle.sign('HMAC', key, encoder.encode(encodedPayload));
    return Array.from(new Uint8Array(signature))
      .map(b => b.toString(16).padStart(2, '0'))
      .join('');
  }
}

class AccessCardsApi extends BaseApi {
  async provision(params) {
    const body = {
      card_template_id: params.cardTemplateId,
      full_name: params.fullName,
      start_date: params.startDate,
      expiration_date: params.expirationDate,
    };

    const mapping = {
      employeeId: 'employee_id', email: 'email', phoneNumber: 'phone_number',
      title: 'title', siteCode: 'site_code', cardNumber: 'card_number',
      department: 'department', organizationName: 'organization_name',
      metadata: 'metadata',
    };

    for (const [js, api] of Object.entries(mapping)) {
      if (params[js] !== undefined && params[js] !== null) {
        body[api] = params[js];
      }
    }

    const response = await this.request('/v1/key-cards', { method: 'POST', body });
    return new AccessCard(response);
  }

  async get(params) {
    const response = await this.request(`/v1/key-cards/${params.cardId}`);
    return new AccessCard(response);
  }

  async update(params) {
    const body = {};
    const mapping = {
      fullName: 'full_name', email: 'email', phoneNumber: 'phone_number',
      title: 'title', expirationDate: 'expiration_date',
      organizationName: 'organization_name', metadata: 'metadata',
    };
    for (const [js, api] of Object.entries(mapping)) {
      if (params[js] !== undefined && params[js] !== null) {
        body[api] = params[js];
      }
    }
    const response = await this.request(`/v1/key-cards/${params.cardId}`, { method: 'PATCH', body });
    return new AccessCard(response);
  }

  async list(params = {}) {
    const qp = new URLSearchParams();
    if (params.templateId) qp.append('template_id', params.templateId);
    if (params.state) qp.append('state', params.state);
    const response = await this.request(`/v1/key-cards?${qp.toString()}`);
    return (response.keys || []).map(item => new AccessCard(item));
  }

  async suspend(params) {
    const response = await this.request(`/v1/key-cards/${params.cardId}/suspend`, { method: 'POST' });
    return new AccessCard(response);
  }

  async resume(params) {
    const response = await this.request(`/v1/key-cards/${params.cardId}/resume`, { method: 'POST' });
    return new AccessCard(response);
  }

  async delete(params) {
    const response = await this.request(`/v1/key-cards/${params.cardId}/delete`, { method: 'POST' });
    return new AccessCard(response);
  }
}

class ConsoleApi extends BaseApi {
  async readTemplate(params) {
    const response = await this.request(`/v1/console/card-templates/${params.cardTemplateId}`);
    return new Template(response);
  }
}

class AccessGrid {
  constructor(accountId, secretKey, options = {}) {
    if (!accountId) throw new Error('Account ID is required');
    if (!secretKey) throw new Error('Secret Key is required');
    const baseUrl = options.baseUrl || 'https://api.accessgrid.com';
    this.accessCards = new AccessCardsApi(accountId, secretKey, baseUrl);
    this.console = new ConsoleApi(accountId, secretKey, baseUrl);
  }
}

export { AccessGrid, AccessGridError, AuthenticationError, AccessCard, Template };
export default AccessGrid;
