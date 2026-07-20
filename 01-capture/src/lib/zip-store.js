(() => {
  "use strict";

  const MAX_UINT16 = 0xffff;
  const MAX_UINT32 = 0xffffffff;
  const UTF8_FLAG = 0x0800;
  const STORE_METHOD = 0;
  const DOS_TIME = 0;
  const DOS_DATE = 0x0021;
  const encoder = new TextEncoder();

  class ZipStoreError extends Error {
    constructor(message, code) {
      super(message);
      this.name = "ZipStoreError";
      this.code = code;
    }
  }

  function makeCrcTable() {
    const table = new Uint32Array(256);
    for (let index = 0; index < table.length; index += 1) {
      let value = index;
      for (let bit = 0; bit < 8; bit += 1) {
        value = (value & 1) !== 0 ? (value >>> 1) ^ 0xedb88320 : value >>> 1;
      }
      table[index] = value >>> 0;
    }
    return table;
  }

  const crcTable = makeCrcTable();

  function toBytes(value) {
    if (typeof value === "string") {
      return encoder.encode(value);
    }
    if (value instanceof Uint8Array) {
      return value;
    }
    if (value instanceof ArrayBuffer) {
      return new Uint8Array(value);
    }
    if (ArrayBuffer.isView(value)) {
      return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    }
    throw new TypeError("ZIP entry data must be a string, ArrayBuffer, or typed array.");
  }

  function crc32(value) {
    const bytes = toBytes(value);
    let crc = 0xffffffff;
    for (let index = 0; index < bytes.length; index += 1) {
      crc = crcTable[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  function addWithinUint32(left, right, label) {
    const total = left + right;
    if (!Number.isSafeInteger(total) || total > MAX_UINT32) {
      throw new ZipStoreError(`${label} exceeds the 32-bit non-ZIP64 limit.`, "ZIP64_REQUIRED");
    }
    return total;
  }

  function validateName(name) {
    if (typeof name !== "string" || name.length === 0) {
      throw new ZipStoreError("Each ZIP entry needs a non-empty string name.", "INVALID_NAME");
    }
    if (name.includes("\0") || name.includes("\\") || name.startsWith("/") || name.endsWith("/")) {
      throw new ZipStoreError("ZIP entry names must be safe relative file paths.", "INVALID_NAME");
    }
    if (/^[A-Za-z]:/.test(name)) {
      throw new ZipStoreError("ZIP entry names must not use drive-qualified paths.", "INVALID_NAME");
    }
    for (let index = 0; index < name.length; index += 1) {
      const codeUnit = name.charCodeAt(index);
      if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
        const next = name.charCodeAt(index + 1);
        if (next < 0xdc00 || next > 0xdfff) {
          throw new ZipStoreError("ZIP entry names must contain well-formed Unicode.", "INVALID_NAME");
        }
        index += 1;
      } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
        throw new ZipStoreError("ZIP entry names must contain well-formed Unicode.", "INVALID_NAME");
      }
    }
    const segments = name.split("/");
    if (segments.some((segment) => segment.length === 0 || segment === "." || segment === "..")) {
      throw new ZipStoreError("ZIP entry names must not contain empty or traversal segments.", "INVALID_NAME");
    }
  }

  function localHeader(record) {
    const header = new Uint8Array(30);
    const view = new DataView(header.buffer);
    view.setUint32(0, 0x04034b50, true);
    view.setUint16(4, 20, true);
    view.setUint16(6, UTF8_FLAG, true);
    view.setUint16(8, STORE_METHOD, true);
    view.setUint16(10, DOS_TIME, true);
    view.setUint16(12, DOS_DATE, true);
    view.setUint32(14, record.crc, true);
    view.setUint32(18, record.data.length, true);
    view.setUint32(22, record.data.length, true);
    view.setUint16(26, record.nameBytes.length, true);
    view.setUint16(28, 0, true);
    return header;
  }

  function centralHeader(record) {
    const header = new Uint8Array(46);
    const view = new DataView(header.buffer);
    view.setUint32(0, 0x02014b50, true);
    view.setUint16(4, 20, true);
    view.setUint16(6, 20, true);
    view.setUint16(8, UTF8_FLAG, true);
    view.setUint16(10, STORE_METHOD, true);
    view.setUint16(12, DOS_TIME, true);
    view.setUint16(14, DOS_DATE, true);
    view.setUint32(16, record.crc, true);
    view.setUint32(20, record.data.length, true);
    view.setUint32(24, record.data.length, true);
    view.setUint16(28, record.nameBytes.length, true);
    view.setUint16(30, 0, true);
    view.setUint16(32, 0, true);
    view.setUint16(34, 0, true);
    view.setUint16(36, 0, true);
    view.setUint32(38, 0, true);
    view.setUint32(42, record.localOffset, true);
    return header;
  }

  function endRecord(entryCount, centralSize, centralOffset) {
    const record = new Uint8Array(22);
    const view = new DataView(record.buffer);
    view.setUint32(0, 0x06054b50, true);
    view.setUint16(4, 0, true);
    view.setUint16(6, 0, true);
    view.setUint16(8, entryCount, true);
    view.setUint16(10, entryCount, true);
    view.setUint32(12, centralSize, true);
    view.setUint32(16, centralOffset, true);
    view.setUint16(20, 0, true);
    return record;
  }

  function concatenate(chunks, totalSize) {
    let output;
    try {
      output = new Uint8Array(totalSize);
    } catch (_error) {
      throw new ZipStoreError("The ZIP archive is too large for this JavaScript runtime.", "RUNTIME_LIMIT");
    }
    let offset = 0;
    for (const chunk of chunks) {
      output.set(chunk, offset);
      offset += chunk.length;
    }
    return output;
  }

  function createZip(entries) {
    if (!Array.isArray(entries)) {
      throw new TypeError("ZIP entries must be provided as an array.");
    }
    if (entries.length > MAX_UINT16) {
      throw new ZipStoreError("The entry count exceeds the non-ZIP64 limit.", "ZIP64_REQUIRED");
    }

    const seenNames = new Set();
    const records = [];
    const localChunks = [];
    let localSize = 0;

    for (const entry of entries) {
      if (!entry || typeof entry !== "object") {
        throw new TypeError("Each ZIP entry must be an object with name and data fields.");
      }
      validateName(entry.name);
      if (seenNames.has(entry.name)) {
        throw new ZipStoreError(`Duplicate ZIP entry name: ${entry.name}`, "DUPLICATE_NAME");
      }
      seenNames.add(entry.name);

      const nameBytes = encoder.encode(entry.name);
      if (nameBytes.length > MAX_UINT16) {
        throw new ZipStoreError("A UTF-8 entry name exceeds the non-ZIP64 name limit.", "ZIP64_REQUIRED");
      }
      const data = toBytes(entry.data);
      if (data.byteLength > MAX_UINT32) {
        throw new ZipStoreError("An entry exceeds the 32-bit non-ZIP64 size limit.", "ZIP64_REQUIRED");
      }

      const record = {
        nameBytes,
        data,
        crc: crc32(data),
        localOffset: localSize
      };
      const header = localHeader(record);
      localChunks.push(header, nameBytes, data);
      localSize = addWithinUint32(localSize, header.length, "Local file area");
      localSize = addWithinUint32(localSize, nameBytes.length, "Local file area");
      localSize = addWithinUint32(localSize, data.length, "Local file area");
      records.push(record);
    }

    const centralChunks = [];
    let centralSize = 0;
    for (const record of records) {
      const header = centralHeader(record);
      centralChunks.push(header, record.nameBytes);
      centralSize = addWithinUint32(centralSize, header.length, "Central directory");
      centralSize = addWithinUint32(centralSize, record.nameBytes.length, "Central directory");
    }

    const centralOffset = localSize;
    let totalSize = addWithinUint32(localSize, centralSize, "ZIP archive");
    totalSize = addWithinUint32(totalSize, 22, "ZIP archive");
    const eocd = endRecord(records.length, centralSize, centralOffset);
    return concatenate([...localChunks, ...centralChunks, eocd], totalSize);
  }

  const api = Object.freeze({ createZip, crc32, ZipStoreError });
  globalThis.ScriptoriumZipStore = api;
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  }
})();
