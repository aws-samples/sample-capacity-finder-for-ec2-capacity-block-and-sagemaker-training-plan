import crypto from 'node:crypto';
import { GetSecretValueCommand, SecretsManagerClient } from '@aws-sdk/client-secrets-manager';

let cachedPassword;
let cachedAt = 0;
const CACHE_TTL_MS = 5 * 60 * 1000;

async function getConfiguredPassword() {
  const secretId = process.env.APP_AUTH_SECRET_ID;
  if (!secretId) return null;
  if (cachedPassword && Date.now() - cachedAt < CACHE_TTL_MS) return cachedPassword;

  const client = new SecretsManagerClient({ region: process.env.AWS_REGION });
  const result = await client.send(new GetSecretValueCommand({ SecretId: secretId }));
  const raw = result.SecretString;
  if (!raw) throw new Error('The application auth secret has no SecretString');

  let password = raw;
  try {
    const parsed = JSON.parse(raw);
    password = parsed.password;
  } catch {
    // A plain-string secret is also supported.
  }

  if (typeof password !== 'string' || password.length < 12) {
    throw new Error('The application password must contain at least 12 characters');
  }

  cachedPassword = password;
  cachedAt = Date.now();
  return password;
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length
    && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

export async function requireAppPassword(req, res, next) {
  if (process.env.REQUIRE_APP_AUTH !== 'true') return next();

  try {
    const configured = await getConfiguredPassword();
    if (!configured) {
      return res.status(503).json({ error: 'Application authentication is not configured' });
    }
    const supplied = req.get('x-app-password') || '';
    if (!safeEqual(configured, supplied)) {
      return res.status(401).json({ error: 'Invalid password' });
    }
    return next();
  } catch (error) {
    console.error(JSON.stringify({
      level: 'ERROR',
      message: 'Authentication check failed',
      errorName: error.name,
    }));
    return res.status(503).json({ error: 'Authentication is temporarily unavailable' });
  }
}

export function resetAuthCacheForTests() {
  cachedPassword = undefined;
  cachedAt = 0;
}
