import {
  DescribeAvailabilityZonesCommand,
  DescribeCapacityBlockOfferingsCommand,
  EC2Client,
} from '@aws-sdk/client-ec2';
import {
  SageMakerClient,
  SearchTrainingPlanOfferingsCommand,
} from '@aws-sdk/client-sagemaker';
import {
  AWS_REGIONS,
  MAX_CONCURRENT_AWS_CALLS,
  REGION_LABELS,
} from './config.js';

const ec2Clients = new Map();
const sageMakerClients = new Map();
const zoneIdPromises = new Map();
const AWS_CALL_TIMEOUT_MS = 45_000;

function clientFor(cache, Client, region) {
  if (!cache.has(region)) cache.set(region, new Client({ region }));
  return cache.get(region);
}

function toDate(value) {
  return value ? new Date(`${value}T00:00:00Z`) : undefined;
}

function iso(value) {
  return value instanceof Date ? value.toISOString() : value || null;
}

async function sendWithTimeout(client, command) {
  return client.send(command, { abortSignal: AbortSignal.timeout(AWS_CALL_TIMEOUT_MS) });
}

async function mapLimit(items, limit, mapper) {
  const results = new Array(items.length);
  let nextIndex = 0;

  async function worker() {
    while (nextIndex < items.length) {
      const currentIndex = nextIndex;
      nextIndex += 1;
      results[currentIndex] = await mapper(items[currentIndex]);
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results.flat();
}

export function parseAwsError(error) {
  const fullError = `${error?.name || ''}: ${error?.message || String(error)}`;
  if (
    fullError.includes('AuthFailure')
    || fullError.includes('UnrecognizedClientException')
    || fullError.includes('security token included in the request is invalid')
  ) {
    return 'Region could not be queried with the current AWS credentials; verify Region opt-in and regional STS compatibility';
  }
  if (fullError.includes('InvalidAction')) return 'Capacity search is not supported in this region';
  if (fullError.includes('UnknownOperationException')) return 'Not supported in this region';
  if (fullError.includes('ResourceLimitExceeded') && fullError.includes('instance quota is not sufficient')) {
    return 'Reserved-capacity instance quota is not sufficient';
  }
  if (fullError.includes('Invalid instance count')) return 'Invalid instance count for this instance type';
  if (fullError.includes('Invalid instance type')) return 'Instance type is not supported in this region';
  if (fullError.includes('InvalidParameterValue')) {
    if (fullError.includes('start date is not valid')) return 'Start date is not valid';
    if (fullError.includes('duration is not valid')) return 'Invalid duration';
    return 'Instance type is not supported in this region';
  }
  if (error?.name === 'TimeoutError' || error?.name === 'AbortError') return 'AWS API request timed out';
  return error?.message || 'AWS API request failed';
}

async function getZoneId(availabilityZone) {
  if (!availabilityZone) return 'N/A';
  if (!zoneIdPromises.has(availabilityZone)) {
    const region = availabilityZone.match(/^[a-z]{2}-[a-z]+-\d+/)?.[0];
    const promise = (async () => {
      if (!region) return 'N/A';
      const client = clientFor(ec2Clients, EC2Client, region);
      const response = await sendWithTimeout(
        client,
        new DescribeAvailabilityZonesCommand({
          ZoneNames: [availabilityZone],
          AllAvailabilityZones: true,
        }),
      );
      const zone = response.AvailabilityZones?.[0];
      if (!zone?.ZoneId) return 'N/A';
      return zone.ZoneType === 'local-zone' ? `${zone.ZoneId} (Local Zone)` : zone.ZoneId;
    })().catch(() => 'N/A');
    zoneIdPromises.set(availabilityZone, promise);
  }
  return zoneIdPromises.get(availabilityZone);
}

async function scanEc2({ region, instanceType, instanceCount, durationDays, startDate, endDate }) {
  const client = clientFor(ec2Clients, EC2Client, region);
  try {
    const response = await sendWithTimeout(
      client,
      new DescribeCapacityBlockOfferingsCommand({
        InstanceType: instanceType,
        InstanceCount: instanceCount,
        CapacityDurationHours: durationDays * 24,
        StartDateRange: toDate(startDate),
        EndDateRange: toDate(endDate),
        AllAvailabilityZones: true,
        MaxResults: 100,
      }),
    );

    const rows = [];
    for (const offering of response.CapacityBlockOfferings || []) {
      const parts = offering.ReservedCapacityOfferings?.length
        ? offering.ReservedCapacityOfferings
        : [offering];
      const totalDays = (offering.CapacityBlockDurationHours || durationDays * 24) / 24;
      const zoneId = await getZoneId(offering.AvailabilityZone);

      parts.forEach((part, index) => {
        const partDays = (part.CapacityBlockDurationHours || offering.CapacityBlockDurationHours || 0) / 24;
        rows.push({
          offeringId: offering.CapacityBlockOfferingId || '',
          region,
          regionName: REGION_LABELS[region] || '',
          instanceType,
          instanceCount: offering.InstanceCount || instanceCount,
          part: `${index + 1} of ${parts.length}`,
          durationDays: parts.length > 1 ? `${partDays.toFixed(2)} of ${totalDays.toFixed(2)}` : totalDays.toFixed(2),
          startDate: iso(part.StartDate || offering.StartDate),
          endDate: iso(part.EndDate || offering.EndDate),
          upfrontFee: offering.UpfrontFee || '0',
          currencyCode: offering.CurrencyCode || 'USD',
          numberOfParts: parts.length,
          availabilityZone: offering.AvailabilityZone || 'N/A',
          zoneId,
        });
      });
    }
    return rows;
  } catch (error) {
    return [{
      error: parseAwsError(error),
      fullError: `${error?.name || 'Error'}: ${error?.message || String(error)}`,
      region,
      instanceType,
    }];
  }
}

async function scanSageMaker({
  region,
  instanceType,
  instanceCount,
  durationDays,
  startDate,
  endDate,
  targetResource,
}) {
  const client = clientFor(sageMakerClients, SageMakerClient, region);
  try {
    const response = await sendWithTimeout(
      client,
      new SearchTrainingPlanOfferingsCommand({
        TargetResources: [targetResource],
        InstanceType: `ml.${instanceType}`,
        InstanceCount: instanceCount,
        DurationHours: durationDays * 24,
        StartTimeAfter: toDate(startDate),
        EndTimeBefore: toDate(endDate),
      }),
    );

    const rows = [];
    for (const offering of response.TrainingPlanOfferings || []) {
      const parts = offering.ReservedCapacityOfferings || [];
      const totalDays = (offering.DurationHours || 0) / 24;
      for (let index = 0; index < parts.length; index += 1) {
        const part = parts[index];
        const partHours = (part.DurationHours || 0) + (part.DurationMinutes || 0) / 60;
        const zoneId = await getZoneId(part.AvailabilityZone);
        rows.push({
          offeringId: offering.TrainingPlanOfferingId || '',
          region,
          regionName: REGION_LABELS[region] || '',
          instanceType: part.InstanceType || `ml.${instanceType}`,
          instanceCount: part.InstanceCount || instanceCount,
          part: `${index + 1} of ${parts.length}`,
          durationDays: parts.length > 1
            ? `${(partHours / 24).toFixed(2)} of ${totalDays.toFixed(2)}`
            : totalDays.toFixed(2),
          startDate: iso(part.StartTime),
          endDate: iso(part.EndTime),
          upfrontFee: offering.UpfrontFee || '0',
          currencyCode: offering.CurrencyCode || 'USD',
          numberOfParts: parts.length,
          availabilityZone: part.AvailabilityZone || 'N/A',
          zoneId,
        });
      }
    }
    return rows;
  } catch (error) {
    if (error?.name === 'InvalidAction') return [];
    return [{
      error: parseAwsError(error),
      fullError: `${error?.name || 'Error'}: ${error?.message || String(error)}`,
      region,
      instanceType,
    }];
  }
}

async function runSearch(input) {
  const jobs = input.regions.flatMap((region) => (
    input.instanceTypes.map((instanceType) => ({ ...input, region, instanceType }))
  ));
  const scanner = input.service === 'ec2' ? scanEc2 : scanSageMaker;
  const rows = await mapLimit(jobs, MAX_CONCURRENT_AWS_CALLS, scanner);
  return {
    results: rows.filter((row) => !row.error).sort((left, right) => (
      String(left.startDate).localeCompare(String(right.startDate))
    )),
    errors: rows.filter((row) => row.error),
  };
}

export async function searchOfferings(input, { fallbackRegions = AWS_REGIONS } = {}) {
  const primary = await runSearch(input);
  if (input.service !== 'ec2' || primary.results.length > 0) return primary;

  const fallbackInput = {
    ...input,
    regions: fallbackRegions,
    instanceCount: Math.max(1, Math.floor(input.instanceCount / 2)),
    durationDays: Math.max(1, Math.floor(input.durationDays / 2)),
  };
  const fallback = await runSearch(fallbackInput);
  return {
    ...primary,
    fallback: {
      attempted: true,
      instanceCount: fallbackInput.instanceCount,
      durationDays: fallbackInput.durationDays,
      results: fallback.results,
      errors: fallback.errors,
    },
  };
}
