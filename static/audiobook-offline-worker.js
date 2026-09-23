/* Same-origin dedicated worker: bounded-memory hashing and durable OPFS writes. */
'use strict';
importScripts('/static/sha256-stream.js?v=1');

const BLOCK = 4 * 1024 * 1024;
let access = null, hash = null, written = 0, checkpoint = 0, total = 0, flushedAt = 0;

function close() {
  if (access) { try { access.close(); } finally { access = null; } }
}

function durable() {
  access.flush();
  checkpoint = written;
  flushedAt = Date.now();
}

async function execute(message) {
  const busy = () => self.postMessage({request_id: message.request_id, busy: true});
  if (message.command === 'verify') {
    const digest = self.DavidPiSha256.create(), file = message.file;
    for (let offset = 0; offset < file.size; offset += BLOCK) {
      digest.update(await file.slice(offset, Math.min(file.size, offset + BLOCK)).arrayBuffer());
      busy();
    }
    return digest.hex();
  }
  if (message.command === 'open') {
    if (message.root !== 'david-pi-audiobooks-v1' || !/^[0-9a-f]{32}\.audio(?:\.part)?$/.test(message.name)) throw new Error('Invalid offline storage destination.');
    if (!Number.isSafeInteger(message.offset) || message.offset < 0 || !Number.isSafeInteger(message.total) || message.total < message.offset || message.total > 4 * 1024 ** 3) throw new Error('Invalid offline checkpoint.');
    close();
    const directory = await (await navigator.storage.getDirectory()).getDirectoryHandle(message.root, {create: true});
    const handle = await directory.getFileHandle(message.name, {create: true});
    if (!handle.createSyncAccessHandle) throw new Error('This browser cannot checkpoint downloads safely. Existing saved books still play.');
    access = await handle.createSyncAccessHandle();
    if (access.getSize() < message.offset) throw new Error('The durable checkpoint is missing. Retry the download.');
    // Bytes newer than the manifest's flushed checkpoint are never trusted.
    access.truncate(message.offset);
    hash = self.DavidPiSha256.create();
    written = checkpoint = message.offset;total = message.total;flushedAt = Date.now();
    const buffer = new Uint8Array(Math.min(BLOCK, written));
    for (let offset = 0; offset < written;) {
      const view = buffer.subarray(0, Math.min(buffer.length, written - offset));
      const count = access.read(view, {at: offset});
      if (count <= 0) throw new Error('The saved checkpoint could not be read.');
      hash.update(view.subarray(0, count));offset += count;busy();
    }
    durable();
    return {written, checkpoint};
  }
  if (!access) throw new Error('Offline writer is not open.');
  if (message.command === 'write') {
    const bytes = message.bytes;
    if (!(bytes instanceof Uint8Array) || bytes.byteLength > BLOCK || written + bytes.byteLength > total) throw new Error('Invalid offline download chunk.');
    // Split at checkpoint boundaries so even irregular network chunks leave
    // no more than 4 MiB outside an acknowledged durable checkpoint.
    for (let offset = 0; offset < bytes.byteLength;) {
      const count = Math.min(bytes.byteLength - offset, BLOCK - (written - checkpoint));
      const view = bytes.subarray(offset, offset + count);
      let consumed = 0;
      while (consumed < view.byteLength) {
        const result = access.write(view.subarray(consumed), {at: written + consumed});
        if (result <= 0) throw new Error('Offline storage made no progress.');
        consumed += result;
      }
      hash.update(view);written += count;offset += count;
      if (written - checkpoint >= BLOCK || Date.now() - flushedAt >= 2000) durable();
    }
    return {written, checkpoint};
  }
  if (message.command === 'finish') {
    if (written !== total) throw new Error('The saved copy was incomplete.');
    durable();
    const actual = hash.hex();
    close();
    if (!/^[0-9a-f]{64}$/.test(message.sha256) || actual !== message.sha256) throw new Error('The saved audiobook failed its integrity check.');
    return {written, checkpoint, verified: true};
  }
  throw new Error('Unknown offline storage operation.');
}

self.onmessage = async event => {
  const message = event.data;
  try { self.postMessage({request_id: message.request_id, result: await execute(message)}); }
  catch (error) { close();self.postMessage({request_id: message.request_id, error: error.message || 'Offline storage failed.'}); }
};
