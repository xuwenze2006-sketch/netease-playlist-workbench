'use strict';

// This helper performs no account command, network request or file write.
// The URL is stdin data, never a command-line argument or diagnostic message.
const path = require('node:path');
const input = [];
let byteCount = 0;

function fail() {
  process.stderr.write('Local authorization QR generation failed.\n');
  process.exit(1);
}

function validUrl(value) {
  if (!value || value.length > 4096 || !/^[\x21-\x7e]+$/.test(value) || value.includes('\\')) return false;
  // URL() canonicalizes empty userinfo away; reject the raw authority first.
  if (!/^https:\/\//i.test(value) || value.slice(value.indexOf('://') + 3).split(/[/?#]/)[0].includes('@')) return false;
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase();
    const officialHost = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*music\.163\.com$/;
    return parsed.protocol === 'https:' && host.length <= 253 && (officialHost.test(host) || host === '163cn.tv')
      && !parsed.username && !parsed.password && (parsed.port === '' || parsed.port === '443');
  } catch (_) {
    return false;
  }
}

process.stdin.on('error', fail);
process.stdout.on('error', fail);
process.stdin.on('data', chunk => {
  byteCount += chunk.length;
  if (byteCount > 4096) fail();
  input.push(chunk);
});
process.stdin.on('end', () => {
  try {
    const url = Buffer.concat(input).toString('utf8');
    if (!validUrl(url)) fail();
    const vendor = path.join(__dirname, '..', '.tools', 'ncm-cli', 'node_modules',
      'qrcode-terminal', 'vendor', 'QRCode');
    const QRCode = require(vendor);
    const QRErrorCorrectLevel = require(path.join(vendor, 'QRErrorCorrectLevel'));
    const qr = new QRCode(-1, QRErrorCorrectLevel.L);
    qr.addData(url);
    qr.make();
    const matrix = qr.modules;
    const size = matrix.length;
    if (size < 21 || size > 177 || (size - 21) % 4 !== 0
      || matrix.some(row => !Array.isArray(row) || row.length !== size || row.some(cell => typeof cell !== 'boolean'))) fail();
    process.stdout.write(JSON.stringify(matrix));
  } catch (_) {
    fail();
  }
});
