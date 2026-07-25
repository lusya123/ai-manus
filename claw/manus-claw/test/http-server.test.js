import assert from 'node:assert/strict';
import { once } from 'node:events';
import test from 'node:test';

import { ManusClawHttpServer } from '../src/http-server.js';

test('health is unavailable until the Gateway bridge is ready', async (t) => {
  const gatewayState = { ready: false };
  const server = new ManusClawHttpServer({
    port: 18788,
    host: '127.0.0.1',
    logger: {},
    gatewayBridge: {
      isGatewayReady: () => gatewayState.ready,
    },
  });
  // Let the kernel allocate a collision-free port for this unit test.
  server.port = 0;
  server.start();
  await once(server.server, 'listening');

  const nodeServer = server.server;
  t.after(async () => {
    if (nodeServer.listening) {
      await new Promise((resolve) => nodeServer.close(resolve));
    }
  });

  const { port } = nodeServer.address();
  const healthUrl = `http://127.0.0.1:${port}/health`;

  const starting = await fetch(healthUrl);
  assert.equal(starting.status, 503);
  assert.deepEqual(await starting.json(), {
    status: 'starting',
    gateway_ready: false,
  });

  gatewayState.ready = true;
  const ready = await fetch(healthUrl);
  assert.equal(ready.status, 200);
  assert.deepEqual(await ready.json(), {
    status: 'ok',
    gateway_ready: true,
  });
});
