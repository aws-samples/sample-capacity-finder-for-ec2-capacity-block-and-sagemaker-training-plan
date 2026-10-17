import { createApp } from './src/app.js';

const app = createApp();
const [host, rawPort] = (process.env.AWS_LAMBDA_HTTP_ENDPOINT || '0.0.0.0:3000').split(':');
const port = Number(rawPort);

const server = app.listen(port, host, () => {
  console.log(JSON.stringify({
    level: 'INFO',
    message: 'Capacity Finder listening',
    host,
    port,
  }));
});

function shutdown(signal) {
  console.log(JSON.stringify({ level: 'INFO', message: 'Shutting down', signal }));
  server.close(() => process.exit(0));
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
