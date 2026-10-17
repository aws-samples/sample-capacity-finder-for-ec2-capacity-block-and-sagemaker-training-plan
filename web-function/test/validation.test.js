import test from 'node:test';
import assert from 'node:assert/strict';
import { validateSearchRequest } from '../src/validation.js';

const validRequest = {
  service: 'ec2',
  instanceTypes: ['p5.48xlarge'],
  regions: ['us-east-1'],
  instanceCount: 1,
  durationDays: 7,
  startDate: '2026-10-17',
  targetResource: 'training-job',
};

test('accepts a valid search request', () => {
  assert.deepEqual(validateSearchRequest(validRequest), validRequest);
});

test('rejects unsupported durations', () => {
  assert.throws(
    () => validateSearchRequest({ ...validRequest, durationDays: 20 }),
    /Duration must be/,
  );
});

test('rejects an end date before the start date', () => {
  assert.throws(
    () => validateSearchRequest({ ...validRequest, endDate: '2026-10-16' }),
    /End date must not be before/,
  );
});

test('rejects a region that is not enabled for the account', () => {
  assert.throws(
    () => validateSearchRequest(
      { ...validRequest, regions: ['ap-south-2'] },
      ['us-east-1'],
    ),
    /Region is not enabled/,
  );
});
