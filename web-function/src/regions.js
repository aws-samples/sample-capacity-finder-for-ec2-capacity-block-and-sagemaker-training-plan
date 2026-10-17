import {
  DescribeRegionsCommand,
  EC2Client,
} from '@aws-sdk/client-ec2';
import { AWS_REGIONS } from './config.js';

const CACHE_TTL_MS = 15 * 60 * 1000;
let cachedRegions;
let cachedAt = 0;
let discoveryPromise;

export async function discoverEnabledRegions(client) {
  const response = await client.send(new DescribeRegionsCommand({ AllRegions: true }));
  const enabled = new Set(
    (response.Regions || [])
      .filter((region) => region.OptInStatus !== 'not-opted-in')
      .map((region) => region.RegionName),
  );
  return AWS_REGIONS.filter((region) => enabled.has(region));
}

export async function getEnabledRegions() {
  if (cachedRegions && Date.now() - cachedAt < CACHE_TTL_MS) return cachedRegions;
  if (discoveryPromise) return discoveryPromise;

  const client = new EC2Client({ region: process.env.AWS_REGION || 'us-east-1' });
  discoveryPromise = discoverEnabledRegions(client)
    .then((regions) => {
      cachedRegions = regions;
      cachedAt = Date.now();
      return regions;
    })
    .catch((error) => {
      console.warn(JSON.stringify({
        level: 'WARN',
        message: 'Could not discover enabled AWS Regions; using the full supported catalogue',
        errorName: error.name,
      }));
      return AWS_REGIONS;
    })
    .finally(() => {
      discoveryPromise = undefined;
    });
  return discoveryPromise;
}

export function resetRegionCacheForTests() {
  cachedRegions = undefined;
  cachedAt = 0;
  discoveryPromise = undefined;
}
