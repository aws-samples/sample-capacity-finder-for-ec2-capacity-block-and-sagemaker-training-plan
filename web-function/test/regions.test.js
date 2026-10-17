import test from 'node:test';
import assert from 'node:assert/strict';
import { discoverEnabledRegions } from '../src/regions.js';

test('discovers enabled regions and preserves application ordering', async () => {
  const client = {
    send: async () => ({
      Regions: [
        { RegionName: 'ap-south-2', OptInStatus: 'not-opted-in' },
        { RegionName: 'us-east-2', OptInStatus: 'opt-in-not-required' },
        { RegionName: 'eu-north-1', OptInStatus: 'opt-in-not-required' },
        { RegionName: 'ap-southeast-4', OptInStatus: 'opted-in' },
        { RegionName: 'moon-east-1', OptInStatus: 'opt-in-not-required' },
      ],
    }),
  };

  assert.deepEqual(await discoverEnabledRegions(client), [
    'us-east-2',
    'eu-north-1',
    'ap-southeast-4',
  ]);
});
