const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { join, resolve } = require('node:path');

async function verify() {
  assert.equal(process.platform, 'linux');
  assert.equal(process.arch, 'x64');
  assert.equal(process.versions.modules, '147');
  const root = resolve(process.argv[2]);
  const metadata = require(join(root, 'package.json'));
  assert.equal(metadata.name, '@confluentinc/kafka-javascript');
  assert.equal(metadata.version, '1.9.1');
  const load = createRequire(join(root, 'package.json'));
  const kafka = load(root);
  assert.equal(kafka.librdkafkaVersion, '2.14.2');
  for (const feature of ['ssl', 'sasl', 'gzip', 'lz4', 'zstd', 'sasl_gssapi', 'sasl_scram', 'sasl_oauthbearer', 'http', 'oidc']) {
    assert(kafka.features.includes(feature), `Falta ${feature}`);
  }
  assert.equal(typeof kafka.KafkaJS.Kafka, 'function');
  // Cada cliente usa un broker Kafka de prueba de librdkafka en loopback.
  const mock = { 'test.mock.num.brokers': 1, 'log_level': 0 };
  const clients = [new kafka.Producer(mock), new kafka.KafkaConsumer({ ...mock, 'group.id': 'n8n-offline-probe' })];
  for (const client of clients) {
    assert.equal(client.isConnected(), false);
    await new Promise((done, reject) => {
      const deadline = setTimeout(() => reject(new Error('Tiempo de conexión agotado')), 15000);
      client.connect({ timeout: 5000 }, (error, metadata) => {
        clearTimeout(deadline);
        if (error) reject(error);
        else done(metadata);
      });
    }).then((metadata) => assert.equal(metadata.brokers.length, 1));
    assert.equal(client.isConnected(), true);
    await new Promise((done, reject) => {
      const deadline = setTimeout(() => reject(new Error('Tiempo de cierre agotado')), 15000);
      client.disconnect((error) => {
        clearTimeout(deadline);
        if (error) reject(error);
        else done();
      });
    });
    assert.equal(client.isConnected(), false);
  }
  console.log(JSON.stringify({ success: true, version: metadata.version, librdkafka: kafka.librdkafkaVersion, features: kafka.features, abi: process.versions.modules, loopback_mock_clients: clients.length }));
}

verify().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
