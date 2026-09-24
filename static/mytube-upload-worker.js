'use strict';
importScripts('/static/sha256-stream.js?v=1');
self.onmessage = async ({data}) => {
  try {
    const file = data.file, hash = self.DavidPiSha256.create(), chunkSize = 4 * 1024 * 1024;
    for (let offset = 0; offset < file.size; offset += chunkSize) {
      hash.update(await file.slice(offset, offset + chunkSize).arrayBuffer());
      self.postMessage({progress: Math.min(file.size, offset + chunkSize) / file.size});
    }
    self.postMessage({sha256: hash.hex()});
  } catch (_error) { self.postMessage({error: 'The selected video could not be read. Choose it again.'}); }
};
