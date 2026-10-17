import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import rateLimit from 'express-rate-limit';
import helmet from 'helmet';
import { ZodError } from 'zod';
import { requireAppPassword } from './auth.js';
import {
  INSTANCE_TYPES,
  REGION_LABELS,
  TARGET_RESOURCES,
  VALID_DURATIONS,
} from './config.js';
import { getEnabledRegions } from './regions.js';
import { searchOfferings } from './search.js';
import { validateSearchRequest } from './validation.js';

const currentDirectory = dirname(fileURLToPath(import.meta.url));
const publicDirectory = join(currentDirectory, '..', 'public');

export function createApp({
  search = searchOfferings,
  listRegions = getEnabledRegions,
} = {}) {
  const app = express();
  app.disable('x-powered-by');
  app.use(helmet({
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        scriptSrc: ["'self'"],
        styleSrc: ["'self'"],
        imgSrc: ["'self'", 'data:'],
        connectSrc: ["'self'"],
      },
    },
  }));
  app.use(express.json({ limit: '64kb' }));

  app.get('/api/health', (_req, res) => {
    res.json({
      status: 'ok',
      region: process.env.AWS_REGION || 'local',
      authRequired: process.env.REQUIRE_APP_AUTH === 'true',
    });
  });

  app.get('/api/config', async (_req, res, next) => {
    try {
      const enabledRegions = await listRegions();
      res.json({
        instanceTypes: INSTANCE_TYPES,
        regions: enabledRegions.map((region) => ({ value: region, label: REGION_LABELS[region] || region })),
        durations: VALID_DURATIONS,
        targetResources: Object.entries(TARGET_RESOURCES).map(([value, label]) => ({ value, label })),
        authRequired: process.env.REQUIRE_APP_AUTH === 'true',
      });
    } catch (error) {
      next(error);
    }
  });

  const searchLimiter = rateLimit({
    windowMs: 60_000,
    limit: 10,
    standardHeaders: 'draft-8',
    legacyHeaders: false,
    message: { error: 'Too many searches. Wait a minute and try again.' },
  });

  app.post('/api/search', searchLimiter, requireAppPassword, async (req, res, next) => {
    try {
      const enabledRegions = await listRegions();
      const input = validateSearchRequest(req.body, enabledRegions);
      const startedAt = Date.now();
      const result = await search(input, { fallbackRegions: enabledRegions });
      console.log(JSON.stringify({
        level: 'INFO',
        message: 'Capacity search completed',
        service: input.service,
        combinations: input.instanceTypes.length * input.regions.length,
        resultCount: result.results.length,
        errorCount: result.errors.length,
        durationMs: Date.now() - startedAt,
      }));
      res.json(result);
    } catch (error) {
      if (error instanceof ZodError) {
        return res.status(400).json({
          error: 'Invalid search request',
          details: error.issues.map((issue) => ({
            field: issue.path.join('.'),
            message: issue.message,
          })),
        });
      }
      return next(error);
    }
  });

  app.use(express.static(publicDirectory, {
    etag: true,
    maxAge: process.env.NODE_ENV === 'production' ? '1h' : 0,
  }));

  app.use((error, req, res, _next) => {
    console.error(JSON.stringify({
      level: 'ERROR',
      message: 'Unhandled request error',
      method: req.method,
      path: req.path,
      errorName: error.name,
      errorMessage: error.message,
    }));
    res.status(500).json({ error: 'Internal server error' });
  });

  return app;
}
