const state = {
  service: 'ec2',
  config: null,
  rows: [],
};

const form = document.querySelector('#search-form');
const instanceTypes = document.querySelector('#instance-types');
const regions = document.querySelector('#regions');
const duration = document.querySelector('#duration');
const startDate = document.querySelector('#start-date');
const endDate = document.querySelector('#end-date');
const targetResource = document.querySelector('#target-resource');
const targetResourceField = document.querySelector('#target-resource-field');
const passwordField = document.querySelector('#password-field');
const appPassword = document.querySelector('#app-password');
const summary = document.querySelector('#search-summary');
const status = document.querySelector('#status');
const table = document.querySelector('#results-table');
const errorsPanel = document.querySelector('#errors-panel');
const errorsList = document.querySelector('#errors-list');
const exportJson = document.querySelector('#export-json');
const exportCsv = document.querySelector('#export-csv');

function selectedValues(select) {
  return Array.from(select.selectedOptions, (option) => option.value);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function option(value, label = value, selected = false) {
  return `<option value="${escapeHtml(value)}"${selected ? ' selected' : ''}>${escapeHtml(label)}</option>`;
}

function updateSummary() {
  const combinations = selectedValues(instanceTypes).length * selectedValues(regions).length;
  summary.textContent = combinations
    ? `${combinations} instance-type/Region combination${combinations === 1 ? '' : 's'}.`
    : 'Select at least one instance type and Region.';
}

function setService(service) {
  state.service = service;
  document.querySelectorAll('.service-button').forEach((button) => {
    button.classList.toggle('active', button.dataset.service === service);
  });
  targetResourceField.hidden = service !== 'sagemaker';
}

function setStatus(message, kind = 'neutral') {
  status.className = `status ${kind}`;
  status.textContent = message;
}

function displayDate(value) {
  if (!value) return '';
  return new Intl.DateTimeFormat('en-AU', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'UTC',
  }).format(new Date(value));
}

function displayMoney(value, currency) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? '');
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: currency || 'USD',
    maximumFractionDigits: 2,
  }).format(number);
}

const columns = [
  ['offeringId', 'Offering ID'],
  ['region', 'Region'],
  ['regionName', 'Region name'],
  ['instanceType', 'Instance type'],
  ['instanceCount', 'Count'],
  ['part', 'Part'],
  ['durationDays', 'Duration (days)'],
  ['startDate', 'Start date (UTC)'],
  ['endDate', 'End date (UTC)'],
  ['upfrontFee', 'Upfront fee'],
  ['numberOfParts', 'Segments'],
  ['availabilityZone', 'Availability Zone'],
  ['zoneId', 'Zone ID'],
];

function renderRows(rows) {
  state.rows = rows;
  table.querySelector('thead').innerHTML = `<tr>${columns.map(([, label]) => `<th>${label}</th>`).join('')}</tr>`;
  table.querySelector('tbody').innerHTML = rows.map((row) => (
    `<tr>${columns.map(([key]) => {
      let value = row[key];
      if (key === 'startDate' || key === 'endDate') value = displayDate(value);
      if (key === 'upfrontFee') value = displayMoney(value, row.currencyCode);
      return `<td>${escapeHtml(value)}</td>`;
    }).join('')}</tr>`
  )).join('');
  exportJson.disabled = rows.length === 0;
  exportCsv.disabled = rows.length === 0;
}

function renderErrors(errors) {
  errorsPanel.hidden = errors.length === 0;
  errorsList.innerHTML = errors.map((item) => (
    `<div class="error-row"><strong>${escapeHtml(item.region)} / ${escapeHtml(item.instanceType || '')}</strong>`
    + `<div>${escapeHtml(item.error)}</div><code>${escapeHtml(item.fullError || '')}</code></div>`
  )).join('');
}

function download(filename, type, content) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function csvCell(value) {
  return `"${String(value ?? '').replaceAll('"', '""')}"`;
}

async function loadConfig() {
  const response = await fetch('/api/config');
  if (!response.ok) throw new Error('Could not load application configuration');
  state.config = await response.json();

  instanceTypes.innerHTML = state.config.instanceTypes.map((value) => option(value, value, value === 'p5.48xlarge')).join('');
  regions.innerHTML = state.config.regions.map(({ value, label }) => option(value, `${label} (${value})`)).join('');
  Array.from(regions.options).forEach((item) => {
    item.selected = ['us-east-1', 'us-east-2', 'us-west-2', 'ap-southeast-2'].includes(item.value);
  });
  duration.innerHTML = state.config.durations.map((value) => option(value, `${value} day${value === 1 ? '' : 's'}`, value === 7)).join('');
  targetResource.innerHTML = state.config.targetResources.map(({ value, label }) => option(value, label)).join('');
  passwordField.hidden = !state.config.authRequired;
  startDate.value = todayIso();
  updateSummary();
}

document.querySelectorAll('.service-button').forEach((button) => {
  button.addEventListener('click', () => setService(button.dataset.service));
});
instanceTypes.addEventListener('change', updateSummary);
regions.addEventListener('change', updateSummary);

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const submitButton = form.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  renderRows([]);
  renderErrors([]);
  setStatus('Scanning AWS Regions. Broad searches can take a minute.', 'loading');

  const payload = {
    service: state.service,
    instanceTypes: selectedValues(instanceTypes),
    regions: selectedValues(regions),
    instanceCount: Number(document.querySelector('#instance-count').value),
    durationDays: Number(duration.value),
    startDate: startDate.value,
    targetResource: targetResource.value,
  };
  if (endDate.value) payload.endDate = endDate.value;

  try {
    const response = await fetch('/api/search', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        ...(appPassword.value ? { 'x-app-password': appPassword.value } : {}),
      },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      const details = result.details?.map((item) => `${item.field}: ${item.message}`).join('; ');
      throw new Error(details || result.error || `Search failed with HTTP ${response.status}`);
    }

    let rows = result.results || [];
    let message;
    if (rows.length > 0) {
      message = `Found ${rows.length} offering row${rows.length === 1 ? '' : 's'}.`;
      setStatus(message, 'success');
    } else if (result.fallback?.results?.length) {
      rows = result.fallback.results;
      message = `No exact match. Found ${rows.length} alternative row${rows.length === 1 ? '' : 's'} using ${result.fallback.instanceCount} instance(s) for ${result.fallback.durationDays} day(s).`;
      setStatus(message, 'warning');
    } else {
      setStatus('No offerings found for the selected parameters.', 'warning');
    }
    renderRows(rows);
    renderErrors([...(result.errors || []), ...(result.fallback?.errors || [])]);
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    submitButton.disabled = false;
  }
});

exportJson.addEventListener('click', () => {
  download('capacity-offerings.json', 'application/json', JSON.stringify(state.rows, null, 2));
});

exportCsv.addEventListener('click', () => {
  const header = columns.map(([, label]) => csvCell(label)).join(',');
  const body = state.rows.map((row) => columns.map(([key]) => csvCell(row[key])).join(',')).join('\n');
  download('capacity-offerings.csv', 'text/csv;charset=utf-8', `${header}\n${body}`);
});

loadConfig().catch((error) => setStatus(error.message, 'error'));
