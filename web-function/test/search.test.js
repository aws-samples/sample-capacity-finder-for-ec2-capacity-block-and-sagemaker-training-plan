import test from 'node:test';
import assert from 'node:assert/strict';
import { parseAwsError } from '../src/search.js';

test('explains regional credential failures without exposing raw AWS errors', () => {
  assert.match(
    parseAwsError({
      name: 'UnrecognizedClientException',
      message: 'The security token included in the request is invalid',
    }),
    /Region opt-in and regional STS compatibility/,
  );
});

test('explains unsupported regional Capacity Block APIs', () => {
  assert.equal(
    parseAwsError({
      name: 'InvalidAction',
      message: 'The action DescribeCapacityBlockOfferings is not valid for this web service.',
    }),
    'Capacity search is not supported in this region',
  );
});
