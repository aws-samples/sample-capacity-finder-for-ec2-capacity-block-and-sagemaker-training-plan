import { z } from 'zod';
import {
  AWS_REGIONS,
  INSTANCE_TYPES,
  MAX_COMBINATIONS,
  TARGET_RESOURCES,
  VALID_DURATIONS,
} from './config.js';

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Use YYYY-MM-DD');

const searchSchema = z.object({
  service: z.enum(['ec2', 'sagemaker']),
  instanceTypes: z.array(z.enum(INSTANCE_TYPES)).min(1),
  regions: z.array(z.enum(AWS_REGIONS)).min(1),
  instanceCount: z.number().int().min(1).max(64),
  durationDays: z.number().int().refine(
    (value) => VALID_DURATIONS.includes(value),
    'Duration must be 1-14 days, 21 days, 28 days, or a multiple of 7 up to 182 days',
  ),
  startDate: isoDate,
  endDate: isoDate.optional(),
  targetResource: z.enum(Object.keys(TARGET_RESOURCES)).default('training-job'),
});

export function validateSearchRequest(input, enabledRegions = AWS_REGIONS) {
  const parsed = searchSchema.parse(input);
  if (parsed.endDate && parsed.startDate > parsed.endDate) {
    throw new z.ZodError([{
      code: 'custom',
      path: ['endDate'],
      message: 'End date must not be before the start date',
    }]);
  }

  const combinations = parsed.instanceTypes.length * parsed.regions.length;
  if (combinations > MAX_COMBINATIONS) {
    throw new z.ZodError([{
      code: 'custom',
      path: ['regions'],
      message: `Search is limited to ${MAX_COMBINATIONS} instance-type/region combinations`,
    }]);
  }

  const disabledRegions = parsed.regions.filter((region) => !enabledRegions.includes(region));
  if (disabledRegions.length > 0) {
    throw new z.ZodError([{
      code: 'custom',
      path: ['regions'],
      message: `Region is not enabled for this AWS account: ${disabledRegions.join(', ')}`,
    }]);
  }
  return parsed;
}
