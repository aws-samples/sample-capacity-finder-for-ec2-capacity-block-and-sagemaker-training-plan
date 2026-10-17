import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createApp } from '../src/app.js';

async function withServer(callback) {
  const app = createApp({
    search: async () => ({ results: [{ region: 'us-east-1' }], errors: [] }),
    listRegions: async () => ['us-east-1'],
  });
  const server = app.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    await callback(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test('health route reports local status', async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/health`);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).status, 'ok');
  });
});

test('search route validates and returns results', async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/search`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        service: 'ec2',
        instanceTypes: ['p5.48xlarge'],
        regions: ['us-east-1'],
        instanceCount: 1,
        durationDays: 7,
        startDate: '2026-10-17',
        targetResource: 'training-job',
      }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual((await response.json()).results, [{ region: 'us-east-1' }]);
  });
});

test('config route returns only enabled regions', async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/config`);
    assert.equal(response.status, 200);
    assert.deepEqual((await response.json()).regions, [
      { value: 'us-east-1', label: 'N. Virginia' },
    ]);
  });
});

test('search route rejects a region that is not enabled', async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/search`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        service: 'ec2',
        instanceTypes: ['p5.48xlarge'],
        regions: ['ap-south-2'],
        instanceCount: 1,
        durationDays: 7,
        startDate: '2026-10-18',
        targetResource: 'training-job',
      }),
    });
    assert.equal(response.status, 400);
    assert.match((await response.json()).details[0].message, /not enabled/);
  });
});
